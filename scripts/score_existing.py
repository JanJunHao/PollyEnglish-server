"""Q1 质量打分器验证 / 回填脚本。

对已入库的视频跑 quality_scorer，打印质量分与分档，人工核对是否合理。
默认只打印不写库；加 --write 才把 audio_teachability / quality_score 写回 contents。

用法：
    python -m scripts.score_existing                # dry-run，打印全部
    python -m scripts.score_existing --limit 10     # 只跑前 10 条
    python -m scripts.score_existing --write        # 打分并写回数据库
"""

from __future__ import annotations

import argparse
import sys

from app.db import SessionLocal
from app.models import Content
from app.services.quality_scorer import load_subtitle_doc, score_from_subtitle_doc


def main() -> None:
    parser = argparse.ArgumentParser(description="对已入库视频跑 Q1 质量打分器")
    parser.add_argument("--limit", type=int, help="只处理前 N 条")
    parser.add_argument("--write", action="store_true",
                        help="把打分结果写回 contents（默认只 dry-run 打印）")
    args = parser.parse_args()

    with SessionLocal() as db:
        contents = db.query(Content).order_by(Content.created_at).all()
        if args.limit:
            contents = contents[:args.limit]

        print(f"{'ID':<14} {'L0':<4} {'L1':<7} {'L2':<7} {'综合':<7} "
              f"{'档':<8} {'WPM':<6} {'CEFR':<6} 标题")
        print("-" * 110)

        tiers = {"publish": 0, "review": 0, "reject": 0, "n/a": 0}
        scored = 0
        for c in contents:
            sub_doc = load_subtitle_doc(c.subtitle_url)
            if not sub_doc:
                print(f"{c.id:<14} {'--':<4} {'无字幕':<7}{'':<7}{'':<7}"
                      f"{'n/a':<8} {'':<6}{'':<6} {c.title[:40]}")
                tiers["n/a"] += 1
                continue

            q = score_from_subtitle_doc(
                sub_doc,
                duration_seconds=c.duration_seconds,
                source=c.source,
                kind=c.kind,
            )
            scored += 1
            tiers[q.tier] = tiers.get(q.tier, 0) + 1

            l1 = "-" if q.audio_teachability is None else f"{q.audio_teachability:.3f}"
            l2 = "-" if q.teaching_value is None else f"{q.teaching_value:.3f}"
            qs = "-" if q.quality_score is None else f"{q.quality_score:.3f}"
            wpm = q.l2_detail.get("wpm", "-")
            cefr = q.l2_detail.get("cefr", "-")
            l0 = "OK" if q.l0_passed else "FAIL"
            print(f"{c.id:<14} {l0:<4} {l1:<7} {l2:<7} {qs:<7} "
                  f"{q.tier:<8} {str(wpm):<6} {str(cefr):<6} {c.title[:40]}")
            if not q.l0_passed:
                print(f"   L0 未过: {q.l0_reasons}")

            if args.write:
                c.audio_teachability = q.audio_teachability
                c.quality_score = q.quality_score
                # status 只降级不升级
                rank = {"published": 0, "review_pending": 1}
                if rank.get(q.status, 1) > rank.get(c.status, 1):
                    c.status = q.status

        if args.write:
            db.commit()
            print("\n[已写回数据库]", file=sys.stderr)

        print(f"\n--- 汇总 ---", file=sys.stderr)
        print(f"总计 {len(contents)}  已打分 {scored}  "
              f"publish {tiers['publish']}  review {tiers['review']}  "
              f"reject {tiers['reject']}  无字幕 {tiers['n/a']}", file=sys.stderr)


if __name__ == "__main__":
    main()
