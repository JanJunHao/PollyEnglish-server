"""字幕生成流水线：三级 fallback。

Tier 1：yt-dlp 拉 YouTube auto-caption（字级时间戳，免费）
Tier 2：forced alignment（aeneas / WhisperX 跑句级 srt → 字级，~免费但要本地 GPU/CPU）
Tier 3：Whisper API 转录（$0.006/分钟，最贵但全自动）

当前实现：tier 1 + tier 3。tier 2 标 TODO（需要单独的 worker 镜像装 aeneas）。

Worker 形态：MVP 用 FastAPI BackgroundTasks 内联跑。
生产前换 Celery / Dramatiq + Redis broker，多机扩展。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Content, SubtitleJob
from app.storage import get_storage, guess_content_type

log = logging.getLogger("polly.subtitle_pipeline")


def _now_update(db: Session, job: SubtitleJob, **fields) -> None:
    for k, v in fields.items():
        setattr(job, k, v)
    db.commit()


async def run_job(job_id: str) -> None:
    """主入口：FastAPI BackgroundTasks 调这个。每个 job 一个 DB session。"""
    with SessionLocal() as db:
        job = db.get(SubtitleJob, job_id)
        if job is None:
            log.error("job %s not found", job_id)
            return
        _now_update(db, job, status="running")

        try:
            tier_used, subtitle_source, segments = await _try_tiers(job.youtube_id)
            # 写 subtitles.json 到 storage（路径 subtitles/{job_id}.json）
            # source 字段写进 JSON 里，下游 ingest 可以选择「auto 字幕不入库」
            storage = get_storage()
            tmp_dir = Path(tempfile.mkdtemp(prefix="polly_sub_"))
            try:
                doc = {
                    "video_id": job_id,
                    "subtitle_source": subtitle_source,  # 'manual' / 'auto' / 'whisper'
                    "segments": segments,
                }
                local = tmp_dir / f"{job_id}.json"
                local.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
                remote_key = f"subtitles/{job_id}.json"
                url = storage.put(local, remote_key, content_type=guess_content_type(local))
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

            _now_update(
                db, job,
                status="done",
                result_subtitle_url=url,
                segments_count=len(segments),
            )
            log.info("job %s done via %s/%s, segments=%d",
                     job_id, tier_used, subtitle_source, len(segments))
        except Exception as exc:
            log.exception("job %s failed", job_id)
            _now_update(db, job, status="failed", error_message=str(exc)[:2000])


async def _try_tiers(youtube_id: str | None) -> tuple[str, str, list[dict]]:
    """三级回退：tier1 yt-dlp vtt → tier3 Whisper API。
    返回 (tier_used, subtitle_source, segments)。
      tier_used      : 'vtt' / 'whisper'
      subtitle_source: 'manual' / 'auto' / 'whisper'（whisper 转录算 auto 同档质量）
    """
    if not youtube_id:
        raise RuntimeError("youtube_id is required (没接其他源时)")

    # Tier 1: 试 yt-dlp 拉字幕（manual 优先 → auto 兜底）
    try:
        segs, source = await _tier1_youtube_vtt(youtube_id)
        if segs:
            return "vtt", source, segs
    except FileNotFoundError as e:
        log.warning("tier1 skipped: %s", e)  # yt-dlp 不在 PATH
    except Exception as e:
        log.warning("tier1 failed for %s: %s", youtube_id, e)

    # Tier 3: Whisper API（质量同 auto，专有名词错字概率类似）
    segs = await _tier3_whisper(youtube_id)
    return "whisper", "whisper", segs


# ---------------- Tier 1: yt-dlp 拉字幕（manual 优先，auto 兜底）----------------

# manual 字幕是作者人工上传的，专有名词 / 标点 / 大小写都规整，适合做精读教材。
# auto-caption 错字多（特别是专有名词），不适合做精读 — 留作兜底，且 ingest 层会用
# subtitle_source 标记，未来可以做 "只允许 manual 入库" 的硬性 gate。
ALLOW_AUTO_SUBTITLE = True  # 关掉这个，auto 字幕的视频直接 fail，不入库


async def _tier1_youtube_vtt(youtube_id: str) -> tuple[list[dict], str]:
    """用 yt-dlp 拉英文字幕（manual 优先），转 Polly schema。
    返回 (segments, source) — source 是 'manual' 或 'auto'。
    yt-dlp 不在 PATH 直接 raise FileNotFoundError 让上层 fallback。
    """
    if not shutil.which("yt-dlp"):
        raise FileNotFoundError("yt-dlp not in PATH")

    # 第一遍：manual 字幕
    segs = await _run_ytdlp_subtitle(youtube_id, auto=False)
    if segs:
        log.info("subtitle %s: manual", youtube_id)
        return segs, "manual"

    if not ALLOW_AUTO_SUBTITLE:
        raise RuntimeError("无 manual 字幕，且 ALLOW_AUTO_SUBTITLE=False，拒绝入库")

    # 第二遍：auto-caption 兜底
    segs = await _run_ytdlp_subtitle(youtube_id, auto=True)
    if segs:
        log.info("subtitle %s: auto (manual missing)", youtube_id)
        return segs, "auto"

    raise RuntimeError("yt-dlp 跑完没产出 .en.vtt（manual + auto 都没有）")


async def _run_ytdlp_subtitle(youtube_id: str, auto: bool) -> list[dict]:
    """单次 yt-dlp 调用：要么拉 manual，要么拉 auto。空结果返回 []，不抛错。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="polly_ytdlp_"))
    try:
        flag = "--write-auto-sub" if auto else "--write-sub"
        cmd = [
            "yt-dlp",
            f"https://www.youtube.com/watch?v={youtube_id}",
            "--skip-download",
            flag,
            "--sub-langs", "en.*",
            "--sub-format", "vtt",
            "-o", str(tmp_dir / "%(id)s.%(ext)s"),
            # YouTube 反爬：缺 JS 挑战求解器时视频流不可用会让进程非 0 退出，
            # 但字幕走独立 endpoint 已经写盘了。加这个保住 exit 0，字幕才能被采集。
            "--ignore-no-formats-error",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.debug("yt-dlp %s subtitle (auto=%s) exit %d: %s",
                      youtube_id, auto, proc.returncode, stderr.decode()[:300])
            return []

        # yt-dlp 按区域返回文件名：en → en.vtt / en-US → en-US.vtt / en-GB → en-GB.vtt
        # 用宽匹配以兼容各 channel 的字幕语言变体（VOA 用 en-US）
        vtts = list(tmp_dir.glob("*.en*.vtt"))
        if not vtts:
            return []
        return _parse_youtube_vtt(vtts[0])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


_TIME_RE = re.compile(r"(\d+):(\d+):(\d+)\.(\d+)\s*-->\s*(\d+):(\d+):(\d+)\.(\d+)")


def _parse_youtube_vtt(path: Path) -> list[dict]:
    """简化版：每个 cue 输出一个 segment（句级）。字级解析留给 Polly/scripts/vtt_to_subtitles.py。
    本服务端 MVP 用句级精度（YT 自动字幕本身句级就够看）。
    """
    text = path.read_text(encoding="utf-8")
    segments: list[dict] = []
    seg_id = 0
    for block in text.split("\n\n"):
        lines = [ln for ln in block.strip().split("\n") if ln]
        if not lines:
            continue
        m = next((_TIME_RE.search(ln) for ln in lines if "-->" in ln), None)
        if not m:
            continue
        sh, sm, ss, sms, eh, em, es, ems = (int(x) for x in m.groups())
        start = sh * 3600 + sm * 60 + ss + sms / 1000
        end = eh * 3600 + em * 60 + es + ems / 1000
        body = " ".join(ln for ln in lines if "-->" not in ln)
        body = re.sub(r"<[^>]+>", "", body).strip()
        if not body:
            continue
        segments.append({"id": seg_id, "start": start, "end": end, "text": body, "words": []})
        seg_id += 1
    return segments


# ---------------- Tier 3: Whisper API ----------------

async def _tier3_whisper(youtube_id: str) -> list[dict]:
    """yt-dlp 拉音频 → OpenAI Whisper API 转录（含字级时间戳）。"""
    if not shutil.which("yt-dlp"):
        raise FileNotFoundError("yt-dlp not in PATH (tier3 也需要)")
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured (Whisper tier 不可用)")

    from openai import AsyncOpenAI

    tmp_dir = Path(tempfile.mkdtemp(prefix="polly_whisper_"))
    try:
        # 拉 m4a 音频（小、快、Whisper 友好）
        cmd = [
            "yt-dlp",
            f"https://www.youtube.com/watch?v={youtube_id}",
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "-o", str(tmp_dir / "%(id)s.%(ext)s"),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"yt-dlp audio exit {proc.returncode}: {stderr.decode()[:500]}")

        audios = list(tmp_dir.iterdir())
        if not audios:
            raise RuntimeError("音频文件没下到")
        audio_path = audios[0]

        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )
        with audio_path.open("rb") as fh:
            tr = await client.audio.transcriptions.create(
                file=fh,
                model="whisper-1",
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
        segments: list[dict] = []
        for i, s in enumerate(tr.segments or []):
            segments.append({
                "id": i,
                "start": float(s.start),
                "end": float(s.end),
                "text": s.text.strip(),
                "words": [],
            })
        return segments
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------- 调度入口 ----------------

def enqueue_job(db: Session, youtube_id: str) -> SubtitleJob:
    """提交任务，返回 SubtitleJob 记录。worker 跑在 BackgroundTasks 里。"""
    job = SubtitleJob(
        id=str(uuid.uuid4()),
        source_url=f"https://www.youtube.com/watch?v={youtube_id}",
        youtube_id=youtube_id,
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
