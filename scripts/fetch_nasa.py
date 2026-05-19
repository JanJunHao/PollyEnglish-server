"""抓 NASA Images and Video Library 视频元数据 → 通用 fetch JSON。

API 文档：https://api.nasa.gov/  + https://images.nasa.gov/docs/images.nasa.gov_api_docs.pdf
- 搜索：GET https://images-api.nasa.gov/search?q=<keyword>&media_type=video
- 取文件清单：GET https://images-api.nasa.gov/asset/<nasa_id>
  返回 collection.items[].href 是各分辨率 mp4 / vtt / srt URL

每条产出（跟 fetch_ted_channel.py 同 schema 兼容 + 加 video_url）：
{
  "video_id": "nasa-<nasa_id>",
  "title": "...",
  "author": "NASA",
  "source": "NASA",
  "duration_seconds": 0,            # NASA API 不直接给时长；先填 0，后期 ffprobe 补
  "thumbnail_url": "https://...",
  "description": "...",
  "video_url": "https://...mp4",    # 直接 CDN URL；native 模式 AVPlayer 播
  "caption_url": "https://...vtt",  # NASA 自带字幕轨；ingest 端转 Polly 字幕 JSON
  "categories_hint": ["discovery"], # ingest 用
  "play_mode": "native"
}

只产出带字幕轨的条目 —— 无字幕的纯 B-roll 直接丢（Polly 精读用不上）。

用法：
  python -m scripts.fetch_nasa --query earth --limit 7 --out cdn-staging/nasa-fetch.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx


def _safe_url(url: str) -> str:
    """NASA 返的 URL 含空格 / 中文，AVPlayer / WKWebView 必须 URL-encode 路径段。"""
    if not url:
        return url
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    parts = urlparse(url)
    # 只 encode path（保留 / 和已有的 %）。query 不动。
    safe_path = quote(parts.path, safe="/%")
    return f"{parts.scheme}://{parts.netloc}{safe_path}" + (f"?{parts.query}" if parts.query else "")

SEARCH_URL = "https://images-api.nasa.gov/search"
ASSET_URL_TEMPLATE = "https://images-api.nasa.gov/asset/{nasa_id}"


async def _search(client: httpx.AsyncClient, query: str, pool: int) -> list[dict]:
    """搜出 pool 条 video 类型候选条目（候选要超量，后面按字幕词数筛）。"""
    r = await client.get(
        SEARCH_URL,
        params={"q": query, "media_type": "video", "page_size": str(min(pool, 100))},
        timeout=20.0,
    )
    r.raise_for_status()
    items = r.json().get("collection", {}).get("items", []) or []
    return items[:pool]


# 字幕词数下限：NASA 媒体库里不少 "视频" 是纯配乐 B-roll，字幕文件只有
# "♪ music ♪" 之类几个词。Polly 是精读应用，这种没价值。300 词约等于
# 2~3 分钟有效解说，足够保守地把 B-roll 和真正的解说短片分开。
_MIN_CAPTION_WORDS = 300

# 时长上限：超过 30 分钟的长视频先不收（学习者难一次跟完，也吃带宽）。
_MAX_DURATION_SECONDS = 30 * 60

_CAP_END_RE = re.compile(r"-->\s*(\d+):(\d+):(\d+)[.,](\d+)")


async def _caption_stats(client: httpx.AsyncClient, url: str) -> tuple[int, float]:
    """下载字幕轨，返回 (词数, 时长秒)。

    词数：去掉时间行 / 序号行 / 标签后粗略数。
    时长：取最后一条 cue 的结束时间戳（NASA API 不直接给时长，字幕兜底）。
    失败返回 (0, 0.0)。
    """
    try:
        r = await client.get(url, timeout=20.0)
        r.raise_for_status()
        text = r.text
    except Exception:  # noqa: BLE001
        return 0, 0.0
    words = 0
    duration = 0.0
    for ln in text.replace("\r\n", "\n").split("\n"):
        s = ln.strip()
        if "-->" in s:
            m = _CAP_END_RE.search(s)
            if m:
                h, mn, sec, ms = (int(x) for x in m.groups())
                duration = max(duration, h * 3600 + mn * 60 + sec + ms / 1000)
            continue
        if not s or s.isdigit() or s.upper() == "WEBVTT":
            continue
        words += len(re.sub(r"<[^>]+>", "", s).split())
    return words, duration


async def _asset_files(client: httpx.AsyncClient, nasa_id: str) -> list[str]:
    r = await client.get(ASSET_URL_TEMPLATE.format(nasa_id=nasa_id), timeout=20.0)
    r.raise_for_status()
    items = r.json().get("collection", {}).get("items", []) or []
    return [it.get("href") for it in items if it.get("href")]


def _pick_video_url(files: list[str]) -> str | None:
    """选合适的 mp4：优先 mobile / small 分辨率（省流量、移动端友好）；找不到再选 mp4。"""
    mp4s = [f for f in files if f.lower().endswith(".mp4")]
    for marker in ("mobile", "small", "preview", "medium"):
        for f in mp4s:
            if marker in f.lower():
                return f
    return mp4s[0] if mp4s else None


def _pick_caption(files: list[str]) -> str | None:
    """选字幕轨：优先 .vtt，回退 .srt。

    没字幕的 NASA 条目多半是无解说的纯太空 B-roll —— Polly 是精读应用，
    这种没产品价值。返回 None 让上层直接丢弃，相当于一道质量闸。
    """
    for ext in (".vtt", ".srt"):
        for f in files:
            if f.lower().endswith(ext):
                return f
    return None


def _pick_thumbnail(files: list[str]) -> str | None:
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        for f in files:
            if f.lower().endswith(ext) and "thumb" in f.lower():
                return f
    # 没 thumb 就拿首张图
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        for f in files:
            if f.lower().endswith(ext):
                return f
    return None


async def fetch(queries: list[str], limit: int) -> list[dict]:
    """遍历多个搜索词，超量取候选，逐条按「有可播 mp4 + 有字幕轨 + 字幕够长」筛，
    跨搜索词按 slug 去重，凑满 limit 条即停。"""
    out: list[dict] = []
    seen: set[str] = set()
    async with httpx.AsyncClient() as client:
        for query in queries:
            if len(out) >= limit:
                break
            # 每词超量取候选（不少视频会因无字幕 / 字幕太短被淘汰）
            candidates = await _search(client, query, pool=60)
            print(f"  '{query}': {len(candidates)} 候选", file=sys.stderr)

            for it in candidates:
                if len(out) >= limit:
                    break
                data_list = it.get("data") or []
                if not data_list:
                    continue
                meta = data_list[0]
                nasa_id = meta.get("nasa_id")
                if not nasa_id:
                    continue
                slug = f"nasa-{nasa_id.replace(' ', '-').replace('_', '-').lower()[:60]}"
                if slug in seen:
                    continue

                try:
                    files = await _asset_files(client, nasa_id)
                except Exception as exc:  # noqa: BLE001
                    print(f"  skip {nasa_id}: asset query failed: {exc}", file=sys.stderr)
                    continue

                video_url = _pick_video_url(files)
                if not video_url:
                    continue

                # 字幕硬性卡：无字幕轨 = 无解说 B-roll，精读用不上
                caption_url = _pick_caption(files)
                if not caption_url:
                    continue

                # 字幕词数 / 时长卡：滤掉纯配乐片，以及超 30 分钟的长视频
                wc, duration = await _caption_stats(client, _safe_url(caption_url))
                if wc < _MIN_CAPTION_WORDS:
                    print(f"  skip {nasa_id}: 字幕仅 {wc} 词（<{_MIN_CAPTION_WORDS}）",
                          file=sys.stderr)
                    continue
                if duration > _MAX_DURATION_SECONDS:
                    print(f"  skip {nasa_id}: 时长 {duration/60:.0f} 分钟（>30）",
                          file=sys.stderr)
                    continue

                seen.add(slug)
                out.append({
                    "video_id": slug,
                    "title": (meta.get("title") or "").strip() or "(untitled)",
                    "author": meta.get("center") or "NASA",
                    "source": "NASA",
                    "duration_seconds": int(duration),  # 取自字幕末时间戳
                    "thumbnail_url": _safe_url(_pick_thumbnail(files) or ""),
                    "description": (meta.get("description") or "")[:500],
                    "video_url": _safe_url(video_url),
                    "caption_url": _safe_url(caption_url),
                    "categories_hint": ["discovery"],
                    "play_mode": "native",
                })
                print(f"  ✓ {slug} （字幕 {wc} 词）", file=sys.stderr)

    return out[:limit]


# 默认搜索词：偏向有旁白解说的科普短片（ScienceCasts 系列尤佳），
# 避开纯发射直播 / 无解说轨道画面。
_DEFAULT_QUERIES = [
    "ScienceCasts", "how does", "explained", "mission",
    "Mars", "James Webb", "astronaut", "discovery",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="抓 NASA Images and Video Library 视频")
    parser.add_argument("--query", default=",".join(_DEFAULT_QUERIES),
                        help="搜索关键词，逗号分隔多个（默认一组科普向关键词）")
    parser.add_argument("--limit", type=int, default=25,
                        help="最终产出条数上限")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "cdn-staging" / "nasa-fetch.json"),
    )
    args = parser.parse_args()

    queries = [q.strip() for q in args.query.split(",") if q.strip()]
    print(f"NASA 搜 {queries}，目标 {args.limit} 条…", file=sys.stderr)
    items = asyncio.run(fetch(queries, args.limit))
    print(f"实际拿到 {len(items)} 条（有 mp4 + 有字幕 + 字幕够长）", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
