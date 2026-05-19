"""字幕翻译流水线。

输入：一份 Polly SubtitleDocument JSON 的 URL（segments[].text 是英文）
输出：同 schema，但 segments[].translation 被填上 target_lang 译文

批量策略：每 30 句一批喂 gpt-4o（context 短、错误隔离好、并行 3 路）。
结果写回 storage 的 translations/{job_id}.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import SubtitleTranslationJob
from app.storage import get_storage, guess_content_type

log = logging.getLogger("polly.translation_pipeline")

_BATCH_SIZE = 30
_MAX_PARALLEL = 3


def _now_update(db: Session, job: SubtitleTranslationJob, **fields) -> None:
    for k, v in fields.items():
        setattr(job, k, v)
    db.commit()


async def run_job(job_id: str) -> None:
    """主入口：FastAPI BackgroundTasks 调这个。"""
    with SessionLocal() as db:
        job = db.get(SubtitleTranslationJob, job_id)
        if job is None:
            log.error("translation job %s not found", job_id)
            return
        _now_update(db, job, status="running")

        try:
            doc = await _download_subtitle(job.source_subtitle_url)
            segments = doc.get("segments") or []
            if not segments:
                raise RuntimeError("source subtitle has no segments")

            translated = await translate_segments(segments, job.target_lang)

            # 写回输出 JSON
            storage = get_storage()
            tmp_dir = Path(tempfile.mkdtemp(prefix="polly_tr_"))
            try:
                out_doc = {
                    "video_id": doc.get("video_id", job_id),
                    "language": doc.get("language", "en"),
                    "target_language": job.target_lang,
                    "segments": translated,
                }
                local = tmp_dir / f"{job_id}.json"
                local.write_text(json.dumps(out_doc, ensure_ascii=False, indent=2), encoding="utf-8")
                remote_key = f"translations/{job_id}.json"
                url = storage.put(local, remote_key, content_type=guess_content_type(local))
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

            _now_update(
                db, job,
                status="done",
                result_subtitle_url=url,
                segments_count=len(translated),
            )
            log.info("translation job %s done, segments=%d", job_id, len(translated))
        except Exception as exc:
            log.exception("translation job %s failed", job_id)
            _now_update(db, job, status="failed", error_message=str(exc)[:2000])


async def _download_subtitle(url: str) -> dict:
    """拉远端字幕 JSON。同 server 部署的 /static/ 也走 http。"""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


async def translate_segments(
    segments: list[dict], target_lang: str, model: str = "gpt-4o-mini"
) -> list[dict]:
    """分批翻译。保留原 segment 全部字段，只填 translation。
    被翻译 job 流程和 subtitle_pipeline 字幕生成两处复用。
    默认 gpt-4o-mini——字幕翻译够用，比 gpt-4o 便宜约 15 倍。"""
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured (翻译不可用)")

    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )

    batches: list[list[dict]] = [
        segments[i : i + _BATCH_SIZE] for i in range(0, len(segments), _BATCH_SIZE)
    ]
    sem = asyncio.Semaphore(_MAX_PARALLEL)

    async def _translate_batch(batch: list[dict]) -> list[dict]:
        async with sem:
            return await _gpt_translate(client, batch, target_lang, model)

    results = await asyncio.gather(*[_translate_batch(b) for b in batches])
    flat: list[dict] = []
    for chunk in results:
        flat.extend(chunk)
    return flat


_LANG_NAMES = {
    "zh-CN": "Simplified Chinese (中文)",
    "zh-TW": "Traditional Chinese (繁體中文)",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
}


async def _gpt_translate(
    client, batch: list[dict], target_lang: str, model: str = "gpt-4o-mini"
) -> list[dict]:
    """单批翻译。返回填好 translation 的 segments 列表。

    用 JSON mode + 序列化 id→text 的小字典请模型逐条翻，错位风险最小。
    """
    lang_name = _LANG_NAMES.get(target_lang, target_lang)
    payload = {str(seg["id"]): seg["text"] for seg in batch}

    system = (
        "You are a translator for Polly, an English learning app for Chinese speakers. "
        f"Translate the values into natural, fluent {lang_name}. "
        "Keep idiom and tone. Don't add explanations. "
        "Return STRICT JSON: same keys, translated values."
    )
    user = json.dumps(payload, ensure_ascii=False)

    # 模型偶发返回截断 / 不合法 JSON。重试一次；仍失败就让这一批译文留空，
    # 不抛错——否则 asyncio.gather 会让整份字幕的翻译全部失败。
    translations: dict[str, str] = {}
    for attempt in range(2):
        resp = await client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            temperature=0.2,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = resp.choices[0].message.content or "{}"
        try:
            translations = json.loads(raw)
            break
        except json.JSONDecodeError as e:
            log.warning("批次翻译 JSON 解析失败（第 %d 次）: %s", attempt + 1, e)

    out: list[dict] = []
    for seg in batch:
        copy = dict(seg)
        copy["translation"] = translations.get(str(seg["id"]), "")
        out.append(copy)
    return out


# ---------------- 调度入口 ----------------

def enqueue_job(db: Session, source_subtitle_url: str, target_lang: str = "zh-CN") -> SubtitleTranslationJob:
    job = SubtitleTranslationJob(
        id=str(uuid.uuid4()),
        source_subtitle_url=source_subtitle_url,
        target_lang=target_lang,
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
