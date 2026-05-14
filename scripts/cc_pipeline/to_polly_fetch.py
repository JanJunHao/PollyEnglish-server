"""把 yt_cc_scraper.py 产出的 manifest.json 转成 scripts/ingest.py 能吃的 fetch JSON。

字段映射：
  youtube_id        → video_id
  channel_title     → author (作为 fallback) / source
  duration_seconds  → duration_seconds（直接透传）
  cefr_estimate     → categories_hint 同形（让 ingest 拿到难度，写入 contents.cefr_level）
  has_english_subtitle + subtitle_source → 过滤条件
  description       → description

过滤策略：默认只保留
  - license_verified == True（yt-dlp 二次验证通过）
  - has_english_subtitle == True
  - subtitle_source == 'manual'（人工字幕；auto 字幕错字多，不适合精读）

如果要更宽松（auto 字幕也要），加 --allow-auto。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# YouTube 默认 maxresdefault 缩略图 URL
def _thumbnail_url(youtube_id: str) -> str:
    return f"https://i.ytimg.com/vi/{youtube_id}/maxresdefault.jpg"


def convert(
    manifest_path: Path,
    out_path: Path,
    allow_auto_subtitle: bool = False,
    min_cefr: str | None = None,
    max_cefr: str | None = None,
    default_category: str = "discovery",
) -> int:
    """返回写出条目数。"""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    items_in = data.get("videos", [])

    cefr_order = ["A1", "A2", "B1", "B2", "C1", "C2"]
    min_idx = cefr_order.index(min_cefr) if min_cefr else 0
    max_idx = cefr_order.index(max_cefr) if max_cefr else len(cefr_order) - 1

    out: list[dict] = []
    rejected_reasons: dict[str, int] = {}

    def _reject(reason: str) -> None:
        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1

    # 字幕长度下限：VOA 上有作者只传开头几秒就断掉的视频（08yf0dxq3EI 是已知样本，
    # 30 分钟节目只有 29 词的自报家门），CEFR grader 会正确地评成 UNKNOWN，
    # 但 cefr_level 字段会兜底为 B1 进库，用户点进去拿到的字幕只有 7 秒——产品体验灾难。
    # 500 词阈值：30 分钟按 100 wpm 即使只录满 5 分钟也有 500 词，足够保守。
    MIN_WORD_COUNT = 500

    for v in items_in:
        if not v.get("license_verified"):
            _reject("license_not_verified")
            continue
        if not v.get("has_english_subtitle"):
            _reject("no_subtitle")
            continue
        if v.get("subtitle_source") == "auto" and not allow_auto_subtitle:
            _reject("auto_subtitle_only")
            continue
        if (v.get("word_count") or 0) < MIN_WORD_COUNT:
            _reject(f"subtitle_too_short(<{MIN_WORD_COUNT}_words)")
            continue
        cefr = v.get("cefr_estimate") or ""
        if cefr in cefr_order:
            idx = cefr_order.index(cefr)
            if idx < min_idx or idx > max_idx:
                _reject(f"cefr_out_of_range({cefr})")
                continue

        out.append({
            "video_id": v["youtube_id"],
            "title": v["title"],
            "author": v.get("channel_title") or "",
            "source": v.get("channel_title") or "",
            "duration_seconds": int(v.get("duration_seconds") or 0),
            "play_mode": "youtube_embed",  # CC 视频走 YouTube iFrame，合规且免 host
            "thumbnail_url": _thumbnail_url(v["youtube_id"]),
            "description": v.get("description") or "",
            # 把 CEFR 喂给 ingest——ingest 已经有 cefr_level 列，scripts 端默认从这里取
            "cefr_level": cefr or "B1",
            # 用 channel 的常见类型猜默认 category；ingest 的 gpt-4o 分类会覆盖
            "categories_hint": [default_category],
            # 把 wpm 也带上，给后续做听力难度二维筛选（plan 文档 04.15 提的特性）
            "wpm": v.get("wpm"),
            "attribution": v.get("attribution") or "",
        })

    out_path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"输入 {len(items_in)} 条 → 输出 {len(out)} 条")
    if rejected_reasons:
        print("过滤掉的原因分布：")
        for k, n in sorted(rejected_reasons.items(), key=lambda x: -x[1]):
            print(f"  - {k}: {n}")
    print(f"已写出 → {out_path}")
    return len(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", help="yt_cc_scraper 产的 manifest.json 路径")
    parser.add_argument("--out", default="cdn-staging/cc-fetch.json",
                        help="输出的 ingest fetch JSON 路径")
    parser.add_argument("--allow-auto", action="store_true",
                        help="允许 auto 字幕的视频（默认只要 manual）")
    parser.add_argument("--min-cefr", choices=["A1", "A2", "B1", "B2", "C1", "C2"])
    parser.add_argument("--max-cefr", choices=["A1", "A2", "B1", "B2", "C1", "C2"])
    parser.add_argument("--default-category", default="discovery",
                        choices=["daily_news", "movie", "discovery", "ted",
                                 "street_interview", "highlights"])
    args = parser.parse_args()

    convert(
        manifest_path=Path(args.manifest),
        out_path=Path(args.out),
        allow_auto_subtitle=args.allow_auto,
        min_cefr=args.min_cefr,
        max_cefr=args.max_cefr,
        default_category=args.default_category,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
