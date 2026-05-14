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
import sys
from dataclasses import dataclass
from pathlib import Path

from app.api.ai import classify_one
from app.db import SessionLocal
from app.db_bootstrap import ensure_schema_dev
from app.models import Content, SubtitleJob
from app.services.subtitle_pipeline import enqueue_job, run_job
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


def _upsert_from_fetch(item: dict, *, categories: list[str], confidence: float,
                       subtitle_url: str | None, status: str) -> str:
    """upsert contents 行。返回 'inserted' / 'updated'。

    兼容两种 fetch 输出：
    - YouTube embed（fetch_ted_channel.py）：item['play_mode'] 不显式给 → 默认 youtube_embed
    - native CDN（fetch_nasa.py / fetch_internet_archive.py）：item['play_mode'] == 'native' + item['video_url']
    """
    video_id = item["video_id"]
    play_mode = item.get("play_mode", "youtube_embed")
    is_youtube = play_mode == "youtube_embed"

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
        "thumbnail_url": item.get("thumbnail_url") or "",
        "subtitle_url": subtitle_url,
        "vocabulary_url": None,
        "explanation_url": None,
        "categories": final_cats,
        "category_color_hex": 0xFFE066 if is_youtube else 0x4ECDC4,
        "is_recommended": False,
        "classify_confidence": confidence,
        "status": status,
    }

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

        # 2) subtitle（可关 + native 视频暂跳过 yt-dlp 流程；将来 NASA / IA 字幕另搞）
        sub_url: str | None = None
        sub_err: str | None = None
        if do_subtitles and not is_native:
            async with subtitle_sem:
                sub_url, sub_err = await _subtitle_with_fallback(item["video_id"])

        # 3) status 策略：
        #    - classify 成功 + confidence >= 阈值 → published
        #    - classify 成功 + confidence < 阈值 → review_pending（AI 不确定，人工复审）
        #    - classify 失败 + 来源已知（TED / NASA / Internet Archive）→ 默认 published + categories_hint
        #    - classify 失败 + 来源不明 → review_pending
        source = item.get("source", "")
        known_trusted = (
            source in {"TED", "TED-Ed", "NASA", "Internet Archive"}
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

        # 4) upsert（同步）
        action = _upsert_from_fetch(
            item,
            categories=categories,
            confidence=confidence,
            subtitle_url=sub_url,
            status=status,
        )

        return {
            "video_id": item["video_id"],
            "title": item["title"][:60],
            "action": action,
            "status": status,
            "confidence": round(confidence, 2),
            "categories": categories,
            "subtitle": "ok" if sub_url else f"miss({sub_err[:30] if sub_err else 'skipped'})",
            "classify_err": cls_err,
        }

    results = await asyncio.gather(*(process_one(i) for i in items))
    for r in results:
        print(json.dumps(r, ensure_ascii=False))

    # 汇总
    total = len(results)
    published = sum(1 for r in results if r["status"] == "published")
    sub_ok = sum(1 for r in results if r["subtitle"] == "ok")
    print(f"\n--- 汇总 ---", file=sys.stderr)
    print(f"总计 {total}  published {published}  review_pending {total - published}  "
          f"subtitle_ok {sub_ok}/{total}", file=sys.stderr)


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
