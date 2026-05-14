"""抓 YouTube TED 频道前 N 条视频元数据，输出 JSON。

只抓元数据，不下视频。`yt-dlp --flat-playlist --dump-json` 一次返回所有条目，
每条单独一行 JSON。我们只挑必要字段，省内存。

用法：
  python -m scripts.fetch_ted_channel --limit 100
  python -m scripts.fetch_ted_channel --channel @TED-Ed --limit 50 --out fetch.json

输出每条 JSON 结构（精简）：
  {
    "video_id": "eIho2S0ZahI",
    "title": "How to speak so that...",
    "author": "TED",
    "source": "TED",
    "duration_seconds": 598,
    "thumbnail_url": "https://i.ytimg.com/vi/eIho2S0ZahI/maxresdefault.jpg",
    "description": "..."
  }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


async def fetch_channel(channel: str, limit: int) -> list[dict]:
    url = f"https://www.youtube.com/{channel}/videos"
    cmd = [
        "yt-dlp",
        url,
        "--flat-playlist",
        "--dump-json",
        "--playlist-end", str(limit),
        # --flat-playlist 模式下没有 duration / channel 字段；用 --extractor-args 拉拓展
        # 实测 flat-playlist 已包含 title/id/duration/uploader，basic 字段够用
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise SystemExit(f"yt-dlp exit {proc.returncode}\n{stderr.decode()[:2000]}")

    out: list[dict] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue

        # flat-playlist 字段名：id / title / duration / uploader / channel
        video_id = raw.get("id")
        if not video_id:
            continue
        title = raw.get("title") or "(untitled)"
        # 时长偶尔是 None（直播 / 已下架），跳过
        duration = raw.get("duration")
        if not duration:
            continue
        uploader = raw.get("uploader") or raw.get("channel") or "TED"

        out.append({
            "video_id": video_id,
            "title": title,
            "author": uploader,
            "source": "TED" if "TED" in uploader and "Ed" not in uploader else uploader,
            "duration_seconds": int(duration),
            "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            "description": (raw.get("description") or "")[:500],
        })

    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="抓 YouTube 频道前 N 条视频元数据")
    parser.add_argument("--channel", default="@TED", help="频道 handle，如 @TED / @TED-Ed")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "cdn-staging" / "ted-fetch.json"),
    )
    args = parser.parse_args()

    print(f"抓 {args.channel} 前 {args.limit} 条…", file=sys.stderr)
    items = asyncio.run(fetch_channel(args.channel, args.limit))
    print(f"实际拿到 {len(items)} 条（过滤掉无 duration 的）", file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
