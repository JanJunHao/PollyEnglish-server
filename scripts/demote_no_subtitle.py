"""一次性数据清理：把所有 status='published' 但 subtitle_url 为空的视频降级到 review_pending。

Why: Polly 的产品是英语精读，没字幕的视频本质不可用。早期 ingest 没把字幕当成
published 的硬前提，DB 里残留了 120+ 条「published 但无字幕」的脏数据。

How: 跑这个脚本一次，把这些行的 status 改成 review_pending。不删除，留着等
subtitle_pipeline 重新跑成功后可恢复 published（手动或后续批处理脚本）。

用法：
    cd polly-server
    python -m scripts.demote_no_subtitle           # dry run，只报数量
    python -m scripts.demote_no_subtitle --apply   # 实际执行
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import or_, select, update

from app.db import SessionLocal
from app.models import Content


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际执行；默认 dry-run")
    args = parser.parse_args()

    with SessionLocal() as db:
        # 先报当前现状
        rows = db.execute(
            select(Content.id, Content.title, Content.play_mode)
            .where(Content.status == "published")
            .where(or_(Content.subtitle_url.is_(None), Content.subtitle_url == ""))
        ).all()

        if not rows:
            print("没有需要降级的视频（subtitle_url 都齐了）。")
            return 0

        by_mode: dict[str, int] = {}
        for _id, _title, mode in rows:
            by_mode[mode] = by_mode.get(mode, 0) + 1

        print(f"找到 {len(rows)} 条 published 但无字幕的视频：")
        for mode, n in sorted(by_mode.items(), key=lambda x: -x[1]):
            print(f"  - {mode}: {n} 条")

        if not args.apply:
            print("\nDry-run，未实际改动。加 --apply 实际降级。")
            return 0

        result = db.execute(
            update(Content)
            .where(Content.status == "published")
            .where(or_(Content.subtitle_url.is_(None), Content.subtitle_url == ""))
            .values(status="review_pending")
        )
        db.commit()
        print(f"\n已降级 {result.rowcount} 条到 status='review_pending'。")
        print("subtitle_pipeline 重新跑成功后可批量恢复 published（手动改 SQL 或后续脚本）。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
