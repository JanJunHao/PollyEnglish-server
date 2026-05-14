"""抓 Internet Archive PD 视频元数据。

archive.org 公开搜索 API：
  GET https://archive.org/advancedsearch.php
      ?q=collection:<collection>+mediatype:movies+language:eng
      &fl=identifier,title,description,downloads,subject,year
      &output=json
      &rows=<n>

视频文件清单：
  GET https://archive.org/metadata/<identifier>
  返回 files[] 含每个 file 的 name + format + length（秒）

下载 URL：
  https://archive.org/download/<identifier>/<filename>

PD 收藏推荐：
- prelinger（短片 / 教学片，含早期英语片段，1900-1960）
- classic_tv（公有领域早期电视）
- feature_films（公有领域电影）

用法：
  python -m scripts.fetch_internet_archive --collection prelinger --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"


async def _search(client: httpx.AsyncClient, collection: str, limit: int) -> list[dict]:
    params = {
        "q": f"collection:{collection} AND mediatype:movies AND language:eng",
        "fl[]": ["identifier", "title", "description", "subject", "year"],
        "output": "json",
        "rows": str(limit * 2),  # 多拉一倍，避免后面拿不到 mp4 被过滤剩太少
        "sort[]": "downloads desc",
    }
    r = await client.get(SEARCH_URL, params=params, timeout=20.0)
    r.raise_for_status()
    return r.json().get("response", {}).get("docs", []) or []


async def _files(client: httpx.AsyncClient, identifier: str) -> list[dict]:
    r = await client.get(METADATA_URL.format(identifier=identifier), timeout=20.0)
    r.raise_for_status()
    return r.json().get("files", []) or []


def _pick_mp4(files: list[dict]) -> tuple[str | None, int]:
    """返回 (file_name, duration_seconds)。优先 mp4 / 512Kb / Low Bitrate 等小文件。"""
    mp4s = [f for f in files if (f.get("name") or "").lower().endswith((".mp4", ".m4v"))]
    # 选小尺寸（移动端友好）：按文件 size 升序
    mp4s.sort(key=lambda f: int(f.get("size") or 0))
    if not mp4s:
        return None, 0
    chosen = mp4s[0]
    # IA 的 length 是 "MM:SS" 或 "HH:MM:SS" 字符串
    raw_len = chosen.get("length") or "0"
    seconds = _parse_length(raw_len)
    return chosen.get("name"), seconds


def _parse_length(s: str) -> int:
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] * 3600 + nums[1] * 60 + nums[2]


async def fetch(collection: str, limit: int) -> list[dict]:
    async with httpx.AsyncClient() as client:
        docs = await _search(client, collection, limit)

        out: list[dict] = []
        for doc in docs:
            if len(out) >= limit:
                break
            identifier = doc.get("identifier")
            if not identifier:
                continue
            try:
                files = await _files(client, identifier)
            except Exception as exc:  # noqa: BLE001
                print(f"  skip {identifier}: metadata failed: {exc}", file=sys.stderr)
                continue

            mp4_name, duration = _pick_mp4(files)
            if not mp4_name:
                continue

            from urllib.parse import quote
            video_url = f"https://archive.org/download/{quote(identifier, safe='-_.')}/{quote(mp4_name, safe='-_.')}"
            # IA 的 thumb 约定：https://archive.org/services/img/<identifier>
            thumb = f"https://archive.org/services/img/{quote(identifier, safe='-_.')}"

            # 取标题里的合理字符当 slug
            slug = "ia-" + identifier.replace("_", "-").replace(" ", "-").lower()[:60]

            title = doc.get("title", "(untitled)")
            if isinstance(title, list):
                title = title[0] if title else "(untitled)"

            out.append({
                "video_id": slug,
                "title": title,
                "author": "Internet Archive",
                "source": "Internet Archive",
                "duration_seconds": duration,
                "thumbnail_url": thumb,
                "description": (doc.get("description", "") or "")[:500] if isinstance(doc.get("description"), str) else "",
                "video_url": video_url,
                "categories_hint": ["movie"],
                "play_mode": "native",
            })

        return out


def main() -> None:
    parser = argparse.ArgumentParser(description="抓 Internet Archive 公有领域视频")
    parser.add_argument("--collection", default="prelinger", help="IA 收藏 ID（prelinger / feature_films / ...）")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "cdn-staging" / "ia-fetch.json"),
    )
    args = parser.parse_args()

    print(f"IA 抓 {args.collection} × {args.limit}…", file=sys.stderr)
    items = asyncio.run(fetch(args.collection, args.limit))
    print(f"实际拿到 {len(items)} 条带可播 mp4 的视频", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
