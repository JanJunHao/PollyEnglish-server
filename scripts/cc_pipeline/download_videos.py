"""批量下 manifest.json 里 youtube CC 视频到 cdn-staging/videos/，并把对应 contents 行切到 native。

设计要点：
- 顺序下载（并发会让单代理打架，整体不会更快）
- 幂等：mp4 已存在且 > 1MB → 跳过下载；DB 行已是 native + video_url → 跳过 UPDATE
- 每条独立 try/catch；单条失败不阻塞其他
- 用 app.storage.get_storage().url() 生成 URL，将来切 R2 直接复用

用法：
    python -m scripts.cc_pipeline.download_videos \\
        --manifest cdn-staging/cc-content/voa/manifest.json
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from app.db import SessionLocal
from app.models import Content
from app.storage import get_storage


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def download_video(youtube_id: str, dest_dir: Path, quality: int = 480, retries: int = 3) -> Path | None:
    """yt-dlp 拉单个视频到 dest_dir/{youtube_id}.mp4。被 yt_cc_scraper 也复用。"""
    dst = dest_dir / f"{youtube_id}.mp4"
    # 幂等：已下载且大小合理（> 1MB）→ 跳过 yt-dlp
    if dst.exists() and dst.stat().st_size > 1_000_000:
        return dst

    fmt = f"best[height<={quality}][ext=mp4]/best[height<={quality}]/best"
    cmd = [
        "yt-dlp",
        f"https://www.youtube.com/watch?v={youtube_id}",
        # YouTube 反爬：解 JS 挑战才能拿到视频流签名 URL
        "--remote-components", "ejs:github",
        "-f", fmt,
        "--no-playlist",
        "--no-progress",
        "--merge-output-format", "mp4",
        "-o", str(dest_dir / f"{youtube_id}.%(ext)s"),
    ]

    for attempt in range(1, retries + 1):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 1_000_000:
            return dst
        tail = (proc.stderr or "")[-300:]
        log.warning("%s attempt %d/%d failed (rc=%d): %s",
                    youtube_id, attempt, retries, proc.returncode, tail)
        if attempt < retries:
            time.sleep(5)
    return None


def _set_native(youtube_id: str, video_url: str) -> bool:
    with SessionLocal() as db:
        row = db.get(Content, youtube_id)
        if not row:
            return False
        row.play_mode = "native"
        row.video_url = video_url
        db.commit()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--manifest", required=True, help="yt_cc_scraper 产的 manifest.json")
    parser.add_argument("--quality", type=int, default=480, help="最高视频高度（px），默认 480")
    parser.add_argument("--limit", type=int, default=0, help="只下前 N 条，默认全量（0）")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        log.error("manifest not found: %s", manifest_path)
        return 1

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    videos = data.get("videos", [])
    if args.limit:
        videos = videos[: args.limit]
    log.info("manifest 共 %d 条要处理", len(videos))

    # cdn-staging/videos/ 是 LocalStorage 的根，URL 由 storage.url() 拼好
    repo_root = Path(__file__).resolve().parent.parent.parent
    dest_dir = repo_root / "cdn-staging" / "videos"
    dest_dir.mkdir(parents=True, exist_ok=True)

    storage = get_storage()

    ok = skipped = 0
    failed: list[str] = []

    for i, v in enumerate(videos, 1):
        yid = v.get("youtube_id")
        if not yid:
            continue
        prefix = f"[{i}/{len(videos)}] {yid}"

        # DB 幂等检查
        with SessionLocal() as db:
            row = db.get(Content, yid)
        if row and row.play_mode == "native" and row.video_url:
            log.info("%s: 已是 native，跳过", prefix)
            skipped += 1
            continue
        if not row:
            log.warning("%s: DB 找不到，跳过（manifest 比 DB 多？）", prefix)
            failed.append(yid)
            continue

        log.info("%s: 下载中…", prefix)
        t0 = time.time()
        path = download_video(yid, dest_dir, quality=args.quality)
        if not path:
            log.error("%s: 下载失败（3 次重试后放弃）", prefix)
            failed.append(yid)
            continue

        size_mb = path.stat().st_size / 1024 / 1024
        elapsed = time.time() - t0
        video_url = storage.url(f"videos/{yid}.mp4")

        if not _set_native(yid, video_url):
            log.error("%s: DB UPDATE 失败", prefix)
            failed.append(yid)
            continue

        log.info("%s: ✓ %.1f MB · %.0fs · %s", prefix, size_mb, elapsed, video_url)
        ok += 1

    log.info("\n--- 汇总 ---")
    log.info("成功 %d   跳过 %d   失败 %d", ok, skipped, len(failed))
    if failed:
        log.info("失败 ids: %s", failed)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
