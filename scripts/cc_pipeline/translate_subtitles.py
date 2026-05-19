"""给字幕 JSON 就地补中文 translation 字段。

背景：subtitle_pipeline.py 生成的字幕只有 text，没有 translation。批量入库的
VOA 内容因此在 App 字幕列表里只有英文、没有中文对照。本脚本把 contents 表里
引用到的字幕文件逐个补翻译，写回原文件（不改 subtitle_url）。

幂等：segment 已有非空 translation 的文件整体跳过。
模型：gpt-4o-mini（字幕翻译够用，比 gpt-4o 便宜约 15 倍）。

用法：
    python -m scripts.cc_pipeline.translate_subtitles
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from app.config import get_settings
from app.db import SessionLocal
from app.models import Content

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

_BATCH_SIZE = 30
_MAX_PARALLEL = 3
_MODEL = "gpt-4o-mini"


async def _translate_batch(client, batch: list[dict]) -> dict[str, str]:
    """单批：id→英文 喂模型，要 id→中文。JSON mode 防错位。"""
    payload = {str(seg["id"]): seg["text"] for seg in batch}
    system = (
        "You are a translator for Polly, an English learning app for Chinese speakers. "
        "Translate the values into natural, fluent Simplified Chinese (中文). "
        "Keep idiom and tone. Don't add explanations. "
        "Return STRICT JSON: same keys, translated values."
    )
    # 模型偶发返回截断 / 不合法 JSON。重试一次；仍失败这批留空，
    # 不抛错——否则 gather 会让整份字幕文件翻译失败。
    for attempt in range(2):
        resp = await client.chat.completions.create(
            model=_MODEL,
            response_format={"type": "json_object"},
            temperature=0.2,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        try:
            return json.loads(resp.choices[0].message.content or "{}")
        except json.JSONDecodeError as e:
            log.warning("批次翻译 JSON 解析失败（第 %d 次）: %s", attempt + 1, e)
    return {}


async def _translate_file(client, path: Path) -> int:
    """翻译单个字幕文件，写回。返回翻译的 segment 数；已翻译则返回 0。"""
    doc = json.loads(path.read_text(encoding="utf-8"))
    segs = doc.get("segments") or []
    if not segs:
        return 0
    # 幂等：已有非空 translation 就跳过
    if all((s.get("translation") or "").strip() for s in segs):
        return 0

    batches = [segs[i : i + _BATCH_SIZE] for i in range(0, len(segs), _BATCH_SIZE)]
    sem = asyncio.Semaphore(_MAX_PARALLEL)

    async def _one(batch):
        async with sem:
            return await _translate_batch(client, batch)

    results = await asyncio.gather(*[_one(b) for b in batches])
    merged: dict[str, str] = {}
    for r in results:
        merged.update(r)

    n = 0
    for seg in segs:
        tr = merged.get(str(seg["id"]), "")
        if tr:
            seg["translation"] = tr
            n += 1
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return n


async def main() -> int:
    settings = get_settings()
    if not settings.openai_api_key:
        log.error("OPENAI_API_KEY 没配")
        return 1

    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )

    # 只翻 contents 表实际引用到的字幕，避免给 cdn-staging 里的陈旧文件烧 token
    repo_root = Path(__file__).resolve().parent.parent.parent
    with SessionLocal() as db:
        rows = db.query(Content).all()
        sub_files = []
        for r in rows:
            if not r.subtitle_url:
                continue
            fname = r.subtitle_url.rsplit("/", 1)[-1]
            p = repo_root / "cdn-staging" / "subtitles" / fname
            if p.exists():
                sub_files.append((r.id, p))

    log.info("contents 引用到 %d 个字幕文件", len(sub_files))

    ok = skipped = failed = 0
    for i, (vid, path) in enumerate(sub_files, 1):
        try:
            n = await _translate_file(client, path)
            if n == 0:
                log.info("[%d/%d] %s: 已有翻译，跳过", i, len(sub_files), vid)
                skipped += 1
            else:
                log.info("[%d/%d] %s: ✓ 翻译 %d 句", i, len(sub_files), vid, n)
                ok += 1
        except Exception as exc:
            log.error("[%d/%d] %s: 失败 %s", i, len(sub_files), vid, exc)
            failed += 1

    log.info("\n--- 汇总 ---  翻译 %d  跳过 %d  失败 %d", ok, skipped, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
