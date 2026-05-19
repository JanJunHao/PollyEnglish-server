"""抓 Internet Archive 公有领域视频元数据（+ 自带字幕轨，若有）。

archive.org 公开搜索 API：
  GET https://archive.org/advancedsearch.php
      ?q=collection:<collection>+mediatype:movies+language:eng
      &fl=identifier,title,description,subject,year
      &output=json&rows=<n>

视频文件清单：
  GET https://archive.org/metadata/<identifier>
  返回 files[] 含每个 file 的 name / format / length（秒）

下载 URL：
  https://archive.org/download/<identifier>/<filename>

合规边界（规划文档 Q3 白名单）：
  ✅ 只允许公有领域收藏：prelinger（教学/短片）、feature_films、classic_tv 等。
  ❌ 绝不碰 IA 的 "TV News Archive"（BBCNEWS / RT / CNN 等录播）——那是带版权的
     广播录像，且 BBC/CNN 在 Polly 明令禁区。这类收藏恰恰是 IA 上"带字幕轨"最多的，
     所以**不能**用"有字幕"当筛选条件去全站搜，只能在 PD 收藏白名单内抓。

字幕现状（重要）：
  PD 老片（prelinger 等）基本没有 .srt/.vtt 字幕轨。本连接器会尽力挑出自带字幕轨的
  条目并填 caption_url（ingest 端可直接转 Polly 字幕）；没有字幕轨的条目仍会产出，
  但 video_url 指向 mp4——ingest 端目前不会给 native 源自动跑 Whisper，
  这类条目入库后会落 review_pending。是否给 IA 视频接 ASR 转写见文末 TODO。

每条产出（与 fetch_nasa.py 同 schema 兼容）：
  {
    "video_id": "ia-<identifier>",
    "title": "...",
    "author": "Internet Archive",
    "source": "Internet Archive",
    "duration_seconds": 0,
    "thumbnail_url": "https://archive.org/services/img/<id>",
    "description": "...",
    "video_url": "https://archive.org/download/<id>/<file>.mp4",
    "caption_url": "https://archive.org/download/<id>/<file>.srt",  # 仅当自带字幕轨
    "categories_hint": ["movie"],
    "play_mode": "native",
    "attribution": "Internet Archive — <collection> (public domain)"
  }

用法：
  python -m scripts.fetch_internet_archive --collection prelinger --limit 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"

# 公有领域收藏白名单 —— 只从这些收藏里抓。
# 任何不在表内的收藏（尤其 "tv"、"television_news"）一律拒绝。
_PD_COLLECTION_WHITELIST = {
    "prelinger",        # Prelinger Archives：教学片 / 广告 / 短片，1900-1980，PD
    "feature_films",    # 公有领域长片
    "classic_tv",       # 公有领域早期电视
    "academic_films",   # 教学影片
    "more_animation",   # 公有领域动画
}
# 明令禁止的收藏关键词（命中即拒绝）——IA 的广播录播区，含 BBC/CNN/RT 等版权内容。
_FORBIDDEN_COLLECTION_HINTS = ("tvnews", "tv-news", "television_news", "bbc", "cnn")

_UA = "Mozilla/5.0 polly-fetcher (educational content sourcing)"


def _check_collection(collection: str) -> None:
    """合规闸门：收藏不在 PD 白名单 / 命中禁区关键词 → 直接报错退出。"""
    low = collection.lower()
    if any(h in low for h in _FORBIDDEN_COLLECTION_HINTS):
        sys.exit(f"拒绝：收藏 '{collection}' 命中版权禁区（BBC/CNN/TV News 等）")
    if collection not in _PD_COLLECTION_WHITELIST:
        sys.exit(
            f"拒绝：收藏 '{collection}' 不在公有领域白名单。"
            f"允许：{sorted(_PD_COLLECTION_WHITELIST)}"
        )


async def _search(client: httpx.AsyncClient, collection: str, limit: int) -> list[dict]:
    params = {
        "q": f"collection:{collection} AND mediatype:movies AND language:eng",
        "fl[]": ["identifier", "title", "description", "subject", "year"],
        "output": "json",
        "rows": str(limit * 3),  # 多拉，避免后面拿不到 mp4 被过滤剩太少
        "sort[]": "downloads desc",
    }
    r = await client.get(SEARCH_URL, params=params, timeout=25.0)
    r.raise_for_status()
    return r.json().get("response", {}).get("docs", []) or []


async def _files(client: httpx.AsyncClient, identifier: str) -> list[dict]:
    """取条目文件清单。IA metadata API 偶发返空，重试两次。"""
    for attempt in range(3):
        try:
            r = await client.get(
                METADATA_URL.format(identifier=identifier), timeout=25.0
            )
            r.raise_for_status()
            files = r.json().get("files", []) or []
            if files:
                return files
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                raise
        await asyncio.sleep(1.5 * (attempt + 1))
    return []


def _parse_length(s: str | None) -> int:
    """IA 的 length 可能是 "MM:SS" / "HH:MM:SS" / 纯秒数字符串。"""
    if not s:
        return 0
    s = str(s)
    if ":" in s:
        parts = s.split(":")
        try:
            nums = [int(float(p)) for p in parts]
        except ValueError:
            return 0
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        return nums[0] if nums else 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _pick_mp4(files: list[dict]) -> tuple[str | None, int]:
    """返回 (file_name, duration_seconds)。优先小尺寸 mp4（移动端友好）。

    时长：优先取选中文件的 length；该文件没有就从同条目任意文件的 length 兜底。
    """
    mp4s = [f for f in files
            if (f.get("name") or "").lower().endswith((".mp4", ".m4v"))]
    mp4s.sort(key=lambda f: int(f.get("size") or 0))
    if not mp4s:
        return None, 0
    chosen = mp4s[0]
    seconds = _parse_length(chosen.get("length"))
    if seconds <= 0:  # 选中文件没标 length，从别的文件兜底
        for f in files:
            seconds = _parse_length(f.get("length"))
            if seconds > 0:
                break
    return chosen.get("name"), seconds


def _pick_caption(files: list[dict]) -> str | None:
    """挑自带字幕轨文件名：优先 .vtt，回退 .srt。多数 PD 老片没有，返回 None。"""
    names = [f.get("name") or "" for f in files]
    for ext in (".vtt", ".srt"):
        for n in names:
            if n.lower().endswith(ext):
                return n
    return None


def _dl_url(identifier: str, filename: str) -> str:
    return (f"https://archive.org/download/"
            f"{quote(identifier, safe='-_.')}/{quote(filename, safe='-_.')}")


async def fetch(collection: str, limit: int) -> list[dict]:
    out: list[dict] = []
    async with httpx.AsyncClient(headers={"User-Agent": _UA}) as client:
        docs = await _search(client, collection, limit)
        print(f"  搜到 {len(docs)} 条候选", file=sys.stderr)

        for doc in docs:
            if len(out) >= limit:
                break
            identifier = doc.get("identifier")
            if not identifier:
                continue
            try:
                files = await _files(client, identifier)
            except Exception as exc:  # noqa: BLE001
                print(f"  skip {identifier}: metadata 失败 {exc}", file=sys.stderr)
                continue
            if not files:
                print(f"  skip {identifier}: metadata 返空", file=sys.stderr)
                continue

            mp4_name, duration = _pick_mp4(files)
            if not mp4_name:
                print(f"  skip {identifier}: 无可播 mp4", file=sys.stderr)
                continue

            title = doc.get("title", "(untitled)")
            if isinstance(title, list):
                title = title[0] if title else "(untitled)"
            desc = doc.get("description", "")
            if isinstance(desc, list):
                desc = desc[0] if desc else ""

            caption_name = _pick_caption(files)
            item = {
                "video_id": "ia-" + identifier.replace("_", "-").replace(" ", "-").lower()[:60],
                "title": title,
                "author": "Internet Archive",
                "source": "Internet Archive",
                "duration_seconds": duration,
                "thumbnail_url": f"https://archive.org/services/img/{quote(identifier, safe='-_.')}",
                "description": (desc or "")[:500] if isinstance(desc, str) else "",
                "video_url": _dl_url(identifier, mp4_name),
                "categories_hint": ["movie"],
                "play_mode": "native",
                "attribution": f"Internet Archive — {collection} (public domain)",
            }
            if caption_name:
                item["caption_url"] = _dl_url(identifier, caption_name)
            out.append(item)
            tag = "（带字幕轨）" if caption_name else "（无字幕轨）"
            print(f"  ✓ {item['video_id']} {duration}s {tag}", file=sys.stderr)

    return out[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description="抓 Internet Archive 公有领域视频")
    parser.add_argument("--collection", default="prelinger",
                        help=f"IA 公有领域收藏（白名单：{sorted(_PD_COLLECTION_WHITELIST)}）")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "cdn-staging" / "ia-fetch.json"),
    )
    args = parser.parse_args()

    _check_collection(args.collection)  # 合规闸门
    print(f"IA 抓 {args.collection} × {args.limit}…", file=sys.stderr)
    items = asyncio.run(fetch(args.collection, args.limit))
    n_cap = sum(1 for i in items if i.get("caption_url"))
    print(f"实际拿到 {len(items)} 条（其中 {n_cap} 条带自带字幕轨）", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
