"""入库 polly contents。两种模式：

1) Demo 模式（向后兼容）：
   python -m scripts.ingest --slug julian-treasure
   python -m scripts.ingest --all

   读 iOS bundle Resources 的 mp4/字幕/缩略图，拷到 cdn-staging（或上传 R2）。

2) Fetch 模式（100 条 TED 批量入库）：
   python -m scripts.ingest --from-fetch cdn-staging/ted-fetch.json [--limit 100]

   读 fetch_ted_channel.py 产的 JSON，逐条：
     a. upsert 到 contents（youtube_embed 模式，thumbnail 走 YouTube CDN）
     b. gpt-4o-mini 自动分类（classify_one），confidence >= 0.7 → published
     c. 入 subtitle_jobs + 同步跑 tier-1 yt-dlp auto-caption
     d. 失败降级：失败步骤记 None，contents 行照常存在
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.api.ai import classify_one
from app.db import SessionLocal
from app.db_bootstrap import ensure_schema_dev
from app.models import Content, SubtitleJob
from app.services.quality_scorer import (
    QualityResult,
    load_subtitle_doc,
    score_from_subtitle_doc,
)
from app.services.subtitle_pipeline import distribute_words, enqueue_job, run_job
from app.storage import Storage, get_storage, guess_content_type


# 把 DemoVideo.swift 的元信息再镜像一份到 Python 端。
# Phase B 后期接 yt-dlp / WhisperX 时，这些就改从 YouTube API + 自动打标产出。
DEMO_META: dict[str, dict] = {
    "julian-treasure": {
        "title": "How to speak so that people want to listen",
        "author": "Julian Treasure",
        "source": "TED",
        "duration_seconds": 9 * 60 + 58,
        "cefr_level": "B2",
        "youtube_id": "eIho2S0ZahI",
        "categories": ["ted", "highlights"],
        "category_color_hex": 0xFFE066,
        "is_recommended": True,
    },
    "ted-ed-dream": {
        "title": "Why do we dream?",
        "author": "TED-Ed",
        "source": "TED-Ed",
        "duration_seconds": 4 * 60 + 58,
        "cefr_level": "B1",
        "youtube_id": "2W85Dwxx218",
        "categories": ["discovery", "ted"],
        "category_color_hex": 0xB8C4FF,
        "is_recommended": False,
    },
    "tim-urban": {
        "title": "Inside the mind of a master procrastinator",
        "author": "Tim Urban",
        "source": "TED",
        "duration_seconds": 14 * 60 + 4,
        "cefr_level": "C1",
        "youtube_id": "arj7oStGLkU",
        "categories": ["ted", "highlights"],
        "category_color_hex": 0xFFAC75,
        "is_recommended": False,
    },
}


@dataclass
class IngestPaths:
    server_root: Path
    polly_root: Path

    @property
    def videos(self) -> Path:
        return self.polly_root / "Polly" / "Resources" / "Videos"

    @property
    def subtitles(self) -> Path:
        return self.polly_root / "Polly" / "Resources" / "Subtitles"

    @property
    def thumbnails(self) -> Path:
        return self.polly_root / "Polly" / "Resources" / "Thumbnails"


def _put_if_exists(storage: Storage, src: Path, remote_key: str) -> str | None:
    if not src.exists():
        return None
    return storage.put(src, remote_key, content_type=guess_content_type(src))


# fetcher 给的 thumbnail_url 常指向这些外部图床；ingest 时下载转存到自己 storage，
# 避免 App 端依赖第三方 CDN（国内访问 i.ytimg.com 不稳，封面会大面积加载失败）。
_EXTERNAL_THUMB_HOSTS = {"i.ytimg.com", "img.youtube.com", "i9.ytimg.com"}


def _download_bytes(url: str) -> bytes | None:
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if resp.status_code == 200 and resp.content:
        return resp.content
    return None


def mirror_thumbnail(storage: Storage, thumbnail_url: str, video_id: str) -> str:
    """把外部图床的缩略图下载并转存到自己 storage，返回自托管 URL。

    幂等：空值 / bundle:// 占位 / 已自托管（host 不在外部图床名单）原样返回。
    下载失败时降级返回原 URL，不阻塞 ingest。
    YouTube 的 maxresdefault 对部分视频不存在（404），自动回退 hqdefault。
    """
    if not thumbnail_url or thumbnail_url.startswith("bundle://"):
        return thumbnail_url
    if urlparse(thumbnail_url).netloc.lower() not in _EXTERNAL_THUMB_HOSTS:
        return thumbnail_url

    data = _download_bytes(thumbnail_url)
    if data is None and "maxresdefault" in thumbnail_url:
        data = _download_bytes(thumbnail_url.replace("maxresdefault", "hqdefault"))
    if data is None:
        print(f"[mirror_thumbnail] download failed, keeping remote URL: {thumbnail_url}",
              file=sys.stderr)
        return thumbnail_url

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        return storage.put(tmp_path, f"thumbnails/{video_id}.jpg", content_type="image/jpeg")
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def ingest_one(slug: str, paths: IngestPaths, storage: Storage) -> dict:
    meta = DEMO_META.get(slug)
    if not meta:
        raise SystemExit(f"unknown slug: {slug}")

    # 1) 视频
    video_url = _put_if_exists(storage, paths.videos / f"{slug}.mp4", f"videos/{slug}.mp4")

    # 2) 字幕（demo- 前缀是 iOS bundle 命名约定，CDN 上拉平）
    subtitle_url = _put_if_exists(
        storage, paths.subtitles / f"demo-{slug}.json", f"subtitles/{slug}.json"
    )

    # 3) 缩略图（优先 maxres）
    thumb_url: str | None = None
    for cand in (f"{slug}-maxresdefault.jpg", f"{slug}-hqdefault.jpg"):
        thumb_url = _put_if_exists(storage, paths.thumbnails / cand, f"thumbnails/{cand}")
        if thumb_url:
            break

    # 4) 写库
    play_mode = "youtube_embed" if meta["source"].startswith("TED") else "native"
    payload = {
        "id": slug,
        "title": meta["title"],
        "author": meta["author"],
        "source": meta["source"],
        "duration_seconds": meta["duration_seconds"],
        "cefr_level": meta["cefr_level"],
        "play_mode": play_mode,
        # TED 走 YouTube embed，video_url 即便存在也不暴露给客户端（合规）
        "video_url": None if play_mode == "youtube_embed" else video_url,
        "youtube_id": meta.get("youtube_id"),
        "thumbnail_url": thumb_url or f"bundle://{slug}-maxresdefault",
        "subtitle_url": subtitle_url,
        "vocabulary_url": None,  # 暂未生成
        "explanation_url": None,  # 暂未生成
        "categories": meta["categories"],
        "category_color_hex": meta["category_color_hex"],
        "is_recommended": meta["is_recommended"],
        "classify_confidence": 0.98,
        "status": "published",
    }

    with SessionLocal() as db:
        existing = db.get(Content, slug)
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
            action = "updated"
        else:
            db.add(Content(**payload))
            action = "inserted"
        db.commit()

    return {
        "slug": slug,
        "action": action,
        "video": "ok" if video_url else "missing",
        "subtitle": "ok" if subtitle_url else "missing",
        "thumbnail": "ok" if thumb_url else "missing",
        "play_mode": play_mode,
    }


# ============================================================
# Fetch 模式：从 fetch_ted_channel.py 输出批量入库
# ============================================================

# 100 条以内 OpenAI / yt-dlp 并发上限。再高会触发 rate limit 或 YouTube 速控
_CLASSIFY_CONCURRENCY = 5
_SUBTITLE_CONCURRENCY = 3
_CLASSIFY_CONFIDENCE_THRESHOLD = 0.7


async def _classify_with_fallback(item: dict) -> tuple[list[str], float, str | None]:
    """gpt-4o-mini 自动分类。失败返 ([], 0.0, error_str)。
    上游会把 confidence < 阈值 的标 review_pending。
    """
    try:
        result = await classify_one(
            title=item["title"],
            author=item.get("author"),
            source=item.get("source"),
            subtitle_excerpt=item.get("description", ""),  # 暂用 description 当 excerpt
        )
        return result.categories, result.confidence, None
    except Exception as exc:  # noqa: BLE001
        return [], 0.0, str(exc)[:200]


async def _subtitle_with_fallback(video_id: str) -> tuple[str | None, str | None]:
    """跑 subtitle job，返回 (subtitle_url, error)。"""
    with SessionLocal() as db:
        job = enqueue_job(db, youtube_id=video_id)
        job_id = job.id

    try:
        await run_job(job_id)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:200]

    with SessionLocal() as db:
        j = db.get(SubtitleJob, job_id)
        if j is None:
            return None, "job vanished"
        if j.status == "done":
            return j.result_subtitle_url, None
        return None, j.error_message or f"job ended in status {j.status}"


# ---- native 源自带字幕轨（NASA media library 的 .vtt/.srt）→ Polly 字幕 JSON ----

_CAPTION_TS_RE = re.compile(
    r"(\d+):(\d+):(\d+)[.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[.,](\d+)"
)


def _parse_caption(text: str) -> list[dict]:
    """解析 VTT 或 SRT 文本 → Polly 字幕 segments。

    两格式统一处理：按空行切 cue，定位含 '-->' 的时间行，正文取其后续行
    （VTT 的 cue 标识行 / SRT 的序号行都在时间行之前，自然被丢掉）。
    没有字级时间戳，按词数在句内均匀估算——点词查义不需要精确字时间。
    """
    segments: list[dict] = []
    seg_id = 0
    for block in text.replace("\r\n", "\n").split("\n\n"):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        ts_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ts_idx is None:
            continue
        m = _CAPTION_TS_RE.search(lines[ts_idx])
        if not m:
            continue
        sh, sm, ss, sms, eh, em, es, ems = (int(x) for x in m.groups())
        start = sh * 3600 + sm * 60 + ss + sms / 1000
        end = eh * 3600 + em * 60 + es + ems / 1000
        body = " ".join(lines[ts_idx + 1:])
        body = re.sub(r"<[^>]+>", "", body).strip()
        if not body:
            continue
        words = distribute_words(body.split(), start, end)
        segments.append({"id": seg_id, "start": start, "end": end,
                          "text": body, "words": words})
        seg_id += 1
    return segments


# ---- 自带逐字稿（无时间戳）→ Polly 字幕 JSON ----
# VOA Learning English 官网连接器（fetch_voa_learning_english.py）产出的条目带
# transcript 字段（逐字稿段落字符串列表，无时间轴）。这里按句切分、按词数在
# 整段时长内均匀估时间——精读交互（点词、长按句子）不需要精确字时间戳。

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")


def _transcript_to_segments(transcript: list[str], duration_seconds: int) -> list[dict]:
    """逐字稿段落列表 → Polly 字幕 segments（按句切，按词数均匀分配时间轴）。"""
    # 先把段落拆成句子（精读以句为单位更自然）
    sentences: list[str] = []
    for para in transcript:
        para = (para or "").strip()
        if not para:
            continue
        parts = [s.strip() for s in _SENTENCE_SPLIT_RE.split(para) if s.strip()]
        sentences.extend(parts or [para])
    if not sentences:
        return []

    # 按各句词数占比分配时间窗口
    word_counts = [max(len(s.split()), 1) for s in sentences]
    total_words = sum(word_counts)
    dur = max(float(duration_seconds or 0), 1.0)

    segments: list[dict] = []
    cursor = 0.0
    for seg_id, (text, wc) in enumerate(zip(sentences, word_counts)):
        span = dur * (wc / total_words)
        start, end = cursor, cursor + span
        cursor = end
        tokens = text.split()
        seg_dur = max(end - start, 0.01)
        words = [
            {
                "w": tok,
                "s": round(start + (i / len(tokens)) * seg_dur, 3),
                "e": round(start + ((i + 1) / len(tokens)) * seg_dur, 3),
            }
            for i, tok in enumerate(tokens)
        ]
        segments.append({"id": seg_id, "start": round(start, 3),
                          "end": round(end, 3), "text": text, "words": words})
    return segments


async def _build_subtitle_from_transcript(item: dict) -> tuple[str | None, str | None]:
    """带 transcript 字段的源（VOA 官网）：逐字稿 → 解析 → 翻译 → 写 subtitle JSON。

    返回 (subtitle_url, error)。VOA 逐字稿是官方编写的，subtitle_source 记 manual。
    翻译 best-effort——失败只留英文，不阻塞入库。
    """
    video_id = item["video_id"]
    transcript = item.get("transcript") or []
    if not transcript:
        return None, "no transcript"
    try:
        segments = _transcript_to_segments(transcript, item.get("duration_seconds") or 0)
        if not segments:
            return None, "transcript parsed to 0 segments"

        try:
            from app.services.translation_pipeline import translate_segments
            segments = await translate_segments(segments, "zh-CN")
        except Exception as e:  # noqa: BLE001
            print(f"[{video_id}] 字幕翻译失败，仅英文: {e}", file=sys.stderr)

        doc = {"video_id": video_id, "subtitle_source": "manual", "segments": segments}
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8"
            ) as tmp:
                json.dump(doc, tmp, ensure_ascii=False, indent=2)
                tmp_path = Path(tmp.name)
            url = get_storage().put(
                tmp_path, f"subtitles/{video_id}.json",
                content_type=guess_content_type(tmp_path),
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        return url, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:200]


async def _build_native_asr_subtitle(item: dict) -> tuple[str | None, str | None, dict | None]:
    """native 源无字幕轨时的 Whisper 兜底：转录 video_url 媒体 → 写 subtitle JSON。

    用于 Internet Archive 等无 transcript、无 caption_url 的公有领域老片——
    原本这类条目会因没字幕沉底。这里下载 native 媒体走 Whisper 转录兜底。

    返回 (subtitle_url, error, asr_meta)。Whisper 转录的 subtitle_source 记 whisper，
    asr_meta 含每段置信度指标，供 Q1 打分器 L1 复用。
    翻译 best-effort——失败只留英文，不阻塞入库。
    """
    video_id = item["video_id"]
    media_url = item.get("video_url")
    if not media_url:
        return None, "no video_url for ASR fallback", None
    try:
        from app.services.subtitle_pipeline import transcribe_native_media
        segments, asr_meta = await transcribe_native_media(media_url)
        if not segments:
            return None, "Whisper 转录产出 0 segments", None

        try:
            from app.services.translation_pipeline import translate_segments
            segments = await translate_segments(segments, "zh-CN")
        except Exception as e:  # noqa: BLE001
            print(f"[{video_id}] 字幕翻译失败，仅英文: {e}", file=sys.stderr)

        # Whisper 转录质量同 auto 档，subtitle_source 记 whisper
        doc = {"video_id": video_id, "subtitle_source": "whisper",
               "segments": segments, "asr_meta": asr_meta}
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8"
            ) as tmp:
                json.dump(doc, tmp, ensure_ascii=False, indent=2)
                tmp_path = Path(tmp.name)
            url = get_storage().put(
                tmp_path, f"subtitles/{video_id}.json",
                content_type=guess_content_type(tmp_path),
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        return url, None, asr_meta
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:200], None


async def _build_native_subtitle(item: dict) -> tuple[str | None, str | None]:
    """native 源（NASA）：下载自带字幕轨 → 解析 → 翻译 → 写 subtitles/{id}.json。

    返回 (subtitle_url, error)。NASA 字幕是人工编写的，subtitle_source 记 manual。
    翻译 best-effort——失败只留英文，不阻塞入库。
    """
    video_id = item["video_id"]
    caption_url = item.get("caption_url")
    if not caption_url:
        return None, "no caption_url"
    try:
        data = _download_bytes(caption_url)
        if not data:
            return None, "caption download failed"
        segments = _parse_caption(data.decode("utf-8", errors="replace"))
        if not segments:
            return None, "caption parsed to 0 segments"

        try:
            from app.services.translation_pipeline import translate_segments
            segments = await translate_segments(segments, "zh-CN")
        except Exception as e:  # noqa: BLE001
            print(f"[{video_id}] 字幕翻译失败，仅英文: {e}", file=sys.stderr)

        doc = {"video_id": video_id, "subtitle_source": "manual", "segments": segments}
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8"
            ) as tmp:
                json.dump(doc, tmp, ensure_ascii=False, indent=2)
                tmp_path = Path(tmp.name)
            url = get_storage().put(
                tmp_path, f"subtitles/{video_id}.json",
                content_type=guess_content_type(tmp_path),
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        return url, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:200]


def _upsert_from_fetch(item: dict, *, categories: list[str], confidence: float,
                       subtitle_url: str | None, status: str,
                       quality: QualityResult | None = None) -> str:
    """upsert contents 行。返回 'inserted' / 'updated'。

    兼容两种 fetch 输出：
    - YouTube embed（fetch_ted_channel.py）：item['play_mode'] 不显式给 → 默认 youtube_embed
    - native CDN（fetch_nasa.py / fetch_internet_archive.py）：item['play_mode'] == 'native' + item['video_url']
    """
    video_id = item["video_id"]
    play_mode = item.get("play_mode", "youtube_embed")
    is_youtube = play_mode == "youtube_embed"

    # 缩略图转存到自己 storage，App 端不再依赖第三方图床
    thumbnail_url = mirror_thumbnail(
        get_storage(), item.get("thumbnail_url") or "", video_id
    )

    # 分类兜底：fetcher 显式给 categories_hint 优先；否则 AI 分类结果；都没 → ['ted'] / ['discovery']
    hint = item.get("categories_hint", [])
    final_cats = categories or hint or (["ted"] if is_youtube else ["discovery"])
    # 加上 'youtube' 标签让首页能归到 YouTube 分类
    if is_youtube and "youtube" not in final_cats:
        final_cats = list(final_cats) + ["youtube"]

    payload = {
        "id": video_id,
        "title": item["title"],
        "author": item.get("author", "TED"),
        "source": item.get("source", "TED"),
        "duration_seconds": item.get("duration_seconds", 0),
        # cefr_level 来源优先级：fetcher 显式给（CC pipeline 的 cefr_grader） > 默认 B1
        # 老 fetcher（fetch_ted_channel / fetch_nasa）不给，自动用 B1 兜底。
        "cefr_level": item.get("cefr_level") or "B1",
        "play_mode": play_mode,
        "video_url": None if is_youtube else item.get("video_url"),
        "youtube_id": video_id if is_youtube else None,
        "thumbnail_url": thumbnail_url,
        "subtitle_url": subtitle_url,
        "vocabulary_url": None,
        "explanation_url": None,
        "categories": final_cats,
        "category_color_hex": 0xFFE066 if is_youtube else 0x4ECDC4,
        "is_recommended": False,
        "classify_confidence": confidence,
        "status": status,
    }
    # Q1 质量打分器产出：L1 音频可教性 + 综合质量分写入对应列
    if quality is not None:
        payload["audio_teachability"] = quality.audio_teachability
        payload["quality_score"] = quality.quality_score

    with SessionLocal() as db:
        existing = db.get(Content, video_id)
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
            action = "updated"
        else:
            db.add(Content(**payload))
            action = "inserted"
        db.commit()
    return action


async def ingest_from_fetch(items: list[dict], *, do_subtitles: bool) -> None:
    """主流程：classify 并发 + subtitle 并发，最后串行 upsert。"""
    classify_sem = asyncio.Semaphore(_CLASSIFY_CONCURRENCY)
    subtitle_sem = asyncio.Semaphore(_SUBTITLE_CONCURRENCY)

    async def process_one(item: dict) -> dict:
        play_mode = item.get("play_mode", "youtube_embed")
        is_native = play_mode == "native"

        # 1) classify
        async with classify_sem:
            categories, confidence, cls_err = await _classify_with_fallback(item)

        # 2) subtitle
        #    - fetch 给了 transcript（VOA 官网 / MIT OCW）→ 逐字稿转 Polly schema
        #      （与 play_mode 无关：OCW 视频走 youtube_embed 但自带逐字稿）
        #    - native 且 fetch 给了 caption_url（NASA）→ 字幕轨转 schema
        #    - native 且无 caption_url（IA 无字幕老片）→ Whisper 兜底转录 video_url 媒体
        #    - 非 native 且无 transcript（YouTube）→ 走 yt-dlp tier1/tier3 流水线
        sub_url: str | None = None
        sub_err: str | None = None
        if do_subtitles:
            if item.get("transcript"):
                async with subtitle_sem:
                    sub_url, sub_err = await _build_subtitle_from_transcript(item)
            elif is_native and item.get("caption_url"):
                async with subtitle_sem:
                    sub_url, sub_err = await _build_native_subtitle(item)
            elif is_native:
                # native 源无 transcript、无 caption_url（如 IA 无字幕公有领域老片）：
                # 走 Whisper 兜底转录 native 媒体，避免无字幕沉底。
                async with subtitle_sem:
                    sub_url, sub_err, _ = await _build_native_asr_subtitle(item)
            else:
                async with subtitle_sem:
                    sub_url, sub_err = await _subtitle_with_fallback(item["video_id"])

        # 3) status 策略：
        #    - classify 成功 + confidence >= 阈值 → published
        #    - classify 成功 + confidence < 阈值 → review_pending（AI 不确定，人工复审）
        #    - classify 失败 + 来源已知（TED / NASA / Internet Archive）→ 默认 published + categories_hint
        #    - classify 失败 + 来源不明 → review_pending
        source = item.get("source", "")
        known_trusted = (
            source in {"TED", "TED-Ed", "NASA", "Internet Archive",
                       "VOA Learning English", "MIT OpenCourseWare"}
            or "TED" in source
            or "NASA" in source.upper()
        )
        if cls_err is None:
            status = "published" if confidence >= _CLASSIFY_CONFIDENCE_THRESHOLD else "review_pending"
        elif known_trusted:
            status = "published"
            # 用 fetcher 给的 categories_hint，否则按源猜
            categories = item.get("categories_hint", []) or (
                ["discovery"] if is_native else ["ted"]
            )
        else:
            status = "review_pending"

        # 字幕硬性卡：Polly 的核心是精读，没字幕没产品价值。
        # subtitle_pipeline 失败（tier1 yt-dlp + tier3 Whisper 都失败）就降级到 review_pending，
        # 既不进首页也不丢数据；后续重新跑 pipeline 成功后可手动 / 脚本恢复 published。
        if not sub_url:
            status = "review_pending"

        # 3.5) Q1 质量打分器（L0/L1/L2）
        #    在 status 已由分类逻辑初定后再跑——打分器只会"降级"不会"升级"：
        #    分类说 published、但质量档 review/reject → 取更保守的 status；
        #    分类已 review_pending 的不会被打分器抬回 published。
        #    无字幕时跳过（已是 review_pending，且 L2 无 segments 评不出）。
        quality: QualityResult | None = None
        if sub_url:
            try:
                sub_doc = load_subtitle_doc(sub_url)
                if sub_doc:
                    quality = score_from_subtitle_doc(
                        sub_doc,
                        duration_seconds=item.get("duration_seconds"),
                        source=item.get("source"),
                        kind=item.get("kind", "video"),
                    )
            except Exception as exc:  # noqa: BLE001 — 打分失败不阻塞入库
                print(f"[{item['video_id']}] 质量打分失败: {exc}", file=sys.stderr)

        if quality is not None:
            q_status = quality.status  # published / review_pending
            # 取更保守者：published(0) < review_pending(1)，质量档只下不上
            rank = {"published": 0, "review_pending": 1}
            if rank.get(q_status, 1) > rank.get(status, 1):
                status = q_status

        # 4) upsert（同步）
        action = _upsert_from_fetch(
            item,
            categories=categories,
            confidence=confidence,
            subtitle_url=sub_url,
            status=status,
            quality=quality,
        )

        return {
            "video_id": item["video_id"],
            "title": item["title"][:60],
            "action": action,
            "status": status,
            "confidence": round(confidence, 2),
            "categories": categories,
            "subtitle": "ok" if sub_url else f"miss({sub_err[:30] if sub_err else 'skipped'})",
            "quality": (
                f"{quality.tier}/{quality.quality_score}"
                f"(L1={quality.audio_teachability},L2={quality.teaching_value})"
                if quality else "n/a"
            ),
            "classify_err": cls_err,
        }

    results = await asyncio.gather(*(process_one(i) for i in items))
    for r in results:
        print(json.dumps(r, ensure_ascii=False))

    # 汇总
    total = len(results)
    published = sum(1 for r in results if r["status"] == "published")
    sub_ok = sum(1 for r in results if r["subtitle"] == "ok")
    scored = sum(1 for r in results if r.get("quality", "n/a") != "n/a")
    print(f"\n--- 汇总 ---", file=sys.stderr)
    print(f"总计 {total}  published {published}  review_pending {total - published}  "
          f"subtitle_ok {sub_ok}/{total}  quality_scored {scored}/{total}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="ingest Polly contents")
    parser.add_argument("--slug", help="单个 demo 视频 slug（demo 模式）")
    parser.add_argument("--all", action="store_true", help="入库所有 demo 视频（demo 模式）")
    parser.add_argument("--from-fetch", help="fetch_ted_channel.py 产的 JSON 路径（fetch 模式）")
    parser.add_argument("--limit", type=int, help="fetch 模式：只处理前 N 条")
    parser.add_argument("--no-subtitles", action="store_true",
                        help="fetch 模式：跳过字幕生成（只入元数据 + 分类）")
    parser.add_argument(
        "--polly-root",
        default=str(Path(__file__).resolve().parent.parent.parent / "Polly"),
        help="Polly iOS 工程根目录（demo 模式用）",
    )
    args = parser.parse_args()

    chosen = [bool(args.slug), bool(args.all), bool(args.from_fetch)]
    if sum(chosen) != 1:
        parser.error("--slug / --all / --from-fetch 三选一")

    ensure_schema_dev()

    # ---- Fetch 模式 ----
    if args.from_fetch:
        path = Path(args.from_fetch)
        if not path.exists():
            sys.exit(f"fetch file not found: {path}")
        items = json.loads(path.read_text(encoding="utf-8"))
        if args.limit:
            items = items[:args.limit]
        print(f"准备入库 {len(items)} 条（fetch 模式）", file=sys.stderr)
        asyncio.run(ingest_from_fetch(items, do_subtitles=not args.no_subtitles))
        return

    # ---- Demo 模式 ----
    server_root = Path(__file__).resolve().parent.parent
    paths = IngestPaths(server_root=server_root, polly_root=Path(args.polly_root))
    if not paths.polly_root.exists():
        sys.exit(f"polly-root 不存在: {paths.polly_root}")
    storage = get_storage()

    targets = [args.slug] if args.slug else list(DEMO_META.keys())
    for slug in targets:
        result = ingest_one(slug, paths, storage)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
