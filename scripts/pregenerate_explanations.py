"""批量预生成 AI 句子讲解并入库。

读 Polly/Polly/Resources/Subtitles/demo-<slug>.json，逐句调 OpenAI，写入 explanations 表。
跑过一次后，iOS 端 POST /v1/ai/explain 会从 DB 命中（cached=True），不再烧 token。

用法：
  python -m scripts.pregenerate_explanations --slug julian-treasure
  python -m scripts.pregenerate_explanations --all
  python -m scripts.pregenerate_explanations --slug X --force   # 覆写已有缓存
  python -m scripts.pregenerate_explanations --slug X --limit 5 # 只跑前 5 句（调试）

要求：
- .env 里 OPENAI_API_KEY 有效
- contents 表已 ingest 过对应 slug
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from openai import APIError, AsyncOpenAI

from app.api.ai import EXPLAIN_SYSTEM
from app.config import get_settings
from app.db import SessionLocal
from app.db_bootstrap import ensure_schema_dev
from app.models import Content, Explanation


def _subtitle_path(polly_root: Path, slug: str) -> Path:
    return polly_root / "Polly" / "Resources" / "Subtitles" / f"demo-{slug}.json"


def _load_segments(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segs = data.get("segments") or data.get("Segments") or []
    if not segs:
        raise SystemExit(f"no segments in {path}")
    return segs


def _build_user_prompt(sentence: str, video: Content | None, context_before: str | None,
                       context_after: str | None) -> str:
    parts = [f"原句：{sentence}"]
    if video is not None:
        parts.append(f"视频：{video.title}（{video.author}，{video.source}）")
        parts.append(f"用户水平：{video.cefr_level}")
    if context_before:
        parts.append(f"上文：{context_before}")
    if context_after:
        parts.append(f"下文：{context_after}")
    return "\n".join(parts)


async def _explain_one(client: AsyncOpenAI, model: str, prompt: str) -> dict:
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EXPLAIN_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    data["_model"] = resp.model
    return data


async def process_video(slug: str, polly_root: Path, *, force: bool, limit: int | None,
                         concurrency: int) -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        sys.exit("OPENAI_API_KEY not configured")

    sub_path = _subtitle_path(polly_root, slug)
    if not sub_path.exists():
        sys.exit(f"subtitle not found: {sub_path}")

    segs = _load_segments(sub_path)
    if limit:
        segs = segs[:limit]

    with SessionLocal() as db:
        video = db.get(Content, slug)
        if video is None:
            print(f"WARN: contents 表里没有 {slug}，prompt 里会缺标题/作者/CEFR")

        existing_ids: set[int] = set()
        if not force:
            rows = db.query(Explanation.segment_id).filter(Explanation.video_id == slug).all()
            existing_ids = {r[0] for r in rows}

    todo = [s for s in segs if s["id"] not in existing_ids]
    print(f"[{slug}] segments total={len(segs)} cached={len(existing_ids)} to_generate={len(todo)}")

    if not todo:
        return

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    sem = asyncio.Semaphore(concurrency)

    async def worker(seg: dict, idx: int) -> tuple[dict, dict] | None:
        async with sem:
            ctx_before = segs[idx - 1]["text"] if idx > 0 else None
            ctx_after = segs[idx + 1]["text"] if idx + 1 < len(segs) else None
            prompt = _build_user_prompt(seg["text"], video, ctx_before, ctx_after)
            try:
                result = await _explain_one(client, settings.openai_explain_model, prompt)
                return seg, result
            except APIError as exc:
                print(f"[{slug}] seg {seg['id']} FAILED: {exc}")
                return None
            except json.JSONDecodeError as exc:
                print(f"[{slug}] seg {seg['id']} bad JSON: {exc}")
                return None

    tasks = [worker(s, segs.index(s)) for s in todo]
    results = await asyncio.gather(*tasks)

    # 入库（同步会话，单次 commit 批量写）
    with SessionLocal() as db:
        wrote = 0
        for pair in results:
            if pair is None:
                continue
            seg, data = pair
            row = Explanation(
                video_id=slug,
                segment_id=seg["id"],
                sentence=seg["text"],
                natural_translation=data.get("natural_translation", ""),
                core_explanation=data.get("core_explanation", ""),
                key_vocab=data.get("key_vocab", []) or [],
                grammar_point=data.get("grammar_point"),
                cultural_note=data.get("cultural_note"),
                pronunciation_tip=data.get("pronunciation_tip"),
                similar_expressions=data.get("similar_expressions"),
                model=data.get("_model", settings.openai_explain_model),
            )
            db.merge(row)
            wrote += 1
        db.commit()
        print(f"[{slug}] wrote {wrote} explanations")


def main() -> None:
    parser = argparse.ArgumentParser(description="批量预生成 AI 句子讲解到 explanations 表")
    parser.add_argument("--slug", help="单个视频 slug")
    parser.add_argument("--all", action="store_true", help="处理所有 contents 表里的视频")
    parser.add_argument("--force", action="store_true", help="已存在的也重新生成")
    parser.add_argument("--limit", type=int, help="每个视频只跑前 N 句（调试用）")
    parser.add_argument("--concurrency", type=int, default=5, help="并发 OpenAI 调用数")
    parser.add_argument(
        "--polly-root",
        default=str(Path(__file__).resolve().parent.parent.parent / "Polly"),
    )
    args = parser.parse_args()

    if not args.slug and not args.all:
        parser.error("--slug 或 --all 二选一")

    polly_root = Path(args.polly_root)
    if not polly_root.exists():
        sys.exit(f"polly-root 不存在: {polly_root}")

    ensure_schema_dev()

    if args.all:
        with SessionLocal() as db:
            slugs = [c.id for c in db.query(Content).filter(Content.status == "published").all()]
    else:
        slugs = [args.slug]

    for slug in slugs:
        try:
            asyncio.run(process_video(
                slug, polly_root,
                force=args.force, limit=args.limit, concurrency=args.concurrency,
            ))
        except SystemExit as e:
            print(f"[{slug}] skipped: {e}")


if __name__ == "__main__":
    main()
