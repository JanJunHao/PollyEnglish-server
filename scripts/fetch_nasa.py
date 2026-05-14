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
  "categories_hint": ["discovery"], # ingest 用
  "play_mode": "native"
}

用法：
  python -m scripts.fetch_nasa --query earth --limit 7 --out cdn-staging/nasa-fetch.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


async def _search(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    """搜出 limit 条 video 类型条目。"""
    r = await client.get(
        SEARCH_URL,
        params={"q": query, "media_type": "video", "page_size": str(limit)},
        timeout=20.0,
    )
    r.raise_for_status()
    items = r.json().get("collection", {}).get("items", []) or []
    return items[:limit]


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


async def fetch(query: str, limit: int) -> list[dict]:
    async with httpx.AsyncClient() as client:
        items = await _search(client, query, limit)

        out: list[dict] = []
        for it in items:
            data_list = it.get("data") or []
            if not data_list:
                continue
            meta = data_list[0]
            nasa_id = meta.get("nasa_id")
            if not nasa_id:
                continue

            try:
                files = await _asset_files(client, nasa_id)
            except Exception as exc:  # noqa: BLE001
                print(f"  skip {nasa_id}: asset query failed: {exc}", file=sys.stderr)
                continue

            video_url = _pick_video_url(files)
            if not video_url:
                continue

            video_url = _safe_url(video_url)
            thumb = _safe_url(_pick_thumbnail(files) or "")

            # 视频 ID 要可作 SQL 主键 + URL slug；NASA ID 含空格，转 slug
            slug = f"nasa-{nasa_id.replace(' ', '-').replace('_', '-').lower()[:60]}"

            out.append({
                "video_id": slug,
                "title": (meta.get("title") or "").strip() or "(untitled)",
                "author": meta.get("center") or "NASA",
                "source": "NASA",
                "duration_seconds": 0,  # API 不直接给；ingest 时不卡这一项
                "thumbnail_url": thumb or "",
                "description": (meta.get("description") or "")[:500],
                "video_url": video_url,
                "categories_hint": ["discovery"],
                "play_mode": "native",
            })

        return out


def main() -> None:
    parser = argparse.ArgumentParser(description="抓 NASA Images and Video Library 视频")
    parser.add_argument("--query", default="earth", help="搜索关键词（默认 earth）")
    parser.add_argument("--limit", type=int, default=7)
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "cdn-staging" / "nasa-fetch.json"),
    )
    args = parser.parse_args()

    print(f"NASA 搜 '{args.query}' × {args.limit}…", file=sys.stderr)
    items = asyncio.run(fetch(args.query, args.limit))
    print(f"实际拿到 {len(items)} 条带可播 mp4 的视频", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
