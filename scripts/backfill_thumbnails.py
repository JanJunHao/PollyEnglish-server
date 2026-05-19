"""一次性回填：把 contents 表里指向外部图床的 thumbnail_url 转存到自己 storage。

fetch ingest 现已内置转存（scripts/ingest.py 的 mirror_thumbnail），本脚本只用来
处理「转存逻辑上线前」已入库的历史数据。改 thumbnail_url 会触发 updated_at，
App 端下次 since 增量拉取即可拿到新封面。

用法：
    python -m scripts.backfill_thumbnails --dry-run   # 只列出会被处理的行
    python -m scripts.backfill_thumbnails             # 实跑
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Content
from app.storage import get_storage
from scripts.ingest import _EXTERNAL_THUMB_HOSTS, mirror_thumbnail


def _is_external(url: str) -> bool:
    if not url or url.startswith("bundle://"):
        return False
    return urlparse(url).netloc.lower() in _EXTERNAL_THUMB_HOSTS


def main() -> None:
    parser = argparse.ArgumentParser(description="回填 contents.thumbnail_url 到自己 storage")
    parser.add_argument("--dry-run", action="store_true", help="只打印将被处理的行，不下载、不写库")
    args = parser.parse_args()

    storage = get_storage()

    with SessionLocal() as db:
        rows = list(db.scalars(select(Content)))

        if args.dry_run:
            targets = [r for r in rows if _is_external(r.thumbnail_url or "")]
            for r in targets:
                print(f"[dry-run] {r.id}  {r.thumbnail_url}")
            print(f"\n--- dry-run ---  共 {len(rows)} 条，{len(targets)} 条待转存",
                  file=sys.stderr)
            return

        updated = 0
        for r in rows:
            old = r.thumbnail_url or ""
            if not _is_external(old):
                continue
            new = mirror_thumbnail(storage, old, r.id)
            if new == old:
                # mirror_thumbnail 下载失败时返回原 URL，已在内部打了日志
                continue
            r.thumbnail_url = new
            updated += 1
            print(f"{r.id}: {old} -> {new}")
        db.commit()

    print(f"\n--- 汇总 ---  共 {len(rows)} 条，已转存 {updated} 条", file=sys.stderr)


if __name__ == "__main__":
    main()
