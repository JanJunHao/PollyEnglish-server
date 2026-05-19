"""抓 MIT OpenCourseWare 讲座视频元数据 + 逐字稿。

为什么用 MIT OCW：
- 全部 **CC BY-NC-SA** 授权（合规，规划文档 Q3 白名单内）。
- 讲座视频**自带完整逐字稿**——无需跑 ASR，逐字稿质量高（人工校对）。
- 题材是大学课程（科学 / 工程 / 经济 / 人文），适合中高难度精读档。

发现来源：MIT Open Learning 的公开 API（learn.mit.edu / OCW 前身的检索后端）。
  GET https://api.learn.mit.edu/api/v1/learning_resources/
      ?resource_type=video&platform=ocw&limit=<n>&offset=<m>
  返回每条视频资源，关键字段：
    - readable_id        → YouTube 视频 ID（OCW 视频托管在 YouTube）
    - title / description
    - url                → OCW 课程页 URL
    - license_cc         → 是否 CC 授权（只收 True 的）
    - video.duration     → ISO 8601 时长（如 "PT1H43S"）
    - image.url          → 缩略图
    - content_files[].content → **整篇逐字稿纯文本**（最关键，省去抓页面 / 跑 ASR）

每条产出（与 fetch_voa_learning_english.py 同形——带 transcript 字段）：
  {
    "video_id": "<youtube_id>",       # OCW 视频即 YouTube 视频
    "title": "...",
    "author": "MIT OpenCourseWare",
    "source": "MIT OpenCourseWare",
    "duration_seconds": 3643,
    "thumbnail_url": "https://i.ytimg.com/vi/<id>/hqdefault.jpg",
    "description": "...",
    "play_mode": "youtube_embed",     # 托管在 YouTube；不暴露下载 URL
    "cefr_level": "C1",               # 大学讲座，整体偏高难度
    "categories_hint": ["discovery"],
    "transcript": ["段落1", "段落2", ...],   # ingest --from-fetch 转成字幕
    "attribution": "MIT OpenCourseWare (CC BY-NC-SA)",
    "source_url": "https://ocw.mit.edu/courses/.../"
  }

逐字稿过短 / 无 CC 授权 / 时长超限的条目直接丢。

用法：
  python -m scripts.fetch_mit_ocw --limit 3
  python -m scripts.fetch_mit_ocw --limit 5 --offset 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

# Open Learning 检索 API（OCW 视频资源都在这里）
_API = "https://api.learn.mit.edu/api/v1/learning_resources/"
_UA = "Mozilla/5.0 polly-fetcher (educational content sourcing)"

# 逐字稿词数下限：大学讲座普遍很长，但仍设下限滤掉异常空条目。
_MIN_TRANSCRIPT_WORDS = 300
# 时长上限：整节大课普遍 >60 分钟，是高价值精读素材，不应被时长闸误拒。
# 与 quality_scorer.LONG_LECTURE_MAX_DURATION_SECONDS（3 小时）对齐——
# 大学单节课极少超此值，仍能挡掉异常超长条目。
_MAX_DURATION_SECONDS = 3 * 60 * 60

# 逐字稿里的非语音标记：MITOCW 逐字稿开头有文件名行，且夹杂
# [SQUEAKING] / [RUSTLING] / [CLICKING] / [APPLAUSE] 等环境音标记——
# 精读用不上，清洗掉。
_SOUND_MARKER_RE = re.compile(r"\[[A-Z][A-Z \-]*\]")
_FILENAME_HEADER_RE = re.compile(r"^MITOCW\s*\|.*$", re.M)


def _parse_iso_duration(s: str | None) -> int:
    """ISO 8601 时长（如 PT1H43S / PT47M12S）→ 秒。解析不了返回 0。"""
    if not s:
        return 0
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s.strip())
    if not m:
        return 0
    h, mn, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mn * 60 + sec


def _clean_transcript(raw: str) -> list[str]:
    """MITOCW 逐字稿纯文本 → 清洗后的段落列表。

    - 去掉首部 "MITOCW | xxx.mp4" 文件名行。
    - 去掉 [SQUEAKING] 类环境音标记。
    - 按空行分段；过短的碎片段并入相邻段（讲座逐字稿换行较随意）。
    """
    if not raw:
        return []
    txt = _FILENAME_HEADER_RE.sub("", raw)
    txt = _SOUND_MARKER_RE.sub("", txt)
    # 按空行切成块，每块内部把软换行拼成连续文本
    blocks: list[str] = []
    for blk in re.split(r"\n\s*\n", txt):
        line = re.sub(r"\s+", " ", blk).strip()
        if line:
            blocks.append(line)
    # 合并过短碎片（< 12 词）到上一段，避免字幕段碎片化
    merged: list[str] = []
    for b in blocks:
        if merged and len(b.split()) < 12:
            merged[-1] = merged[-1] + " " + b
        else:
            merged.append(b)
    return [m for m in merged if len(m.split()) > 3]


async def _fetch_page(client: httpx.AsyncClient, limit: int, offset: int) -> list[dict]:
    """拉一页 OCW 视频资源。"""
    r = await client.get(
        _API,
        params={"resource_type": "video", "platform": "ocw",
                "limit": str(limit), "offset": str(offset)},
        timeout=25.0, follow_redirects=True,
    )
    r.raise_for_status()
    return r.json().get("results", []) or []


def _parse_resource(res: dict) -> dict | None:
    """单条 OCW 视频资源 → fetch JSON 条目。不合格返回 None。"""
    youtube_id = res.get("readable_id") or ""
    title = (res.get("title") or "").strip() or "(untitled)"

    # CC 授权卡：只收明确标 CC 的（合规）
    if not res.get("license_cc"):
        print(f"  skip {youtube_id}: 非 CC 授权（{title[:40]}）", file=sys.stderr)
        return None

    # readable_id 应是 11 位 YouTube ID；不像就跳过（保守）
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", youtube_id):
        print(f"  skip: readable_id 非 YouTube ID（{youtube_id}）", file=sys.stderr)
        return None

    # 逐字稿：从 content_files[].content 取（OCW API 直接内嵌纯文本）
    transcript: list[str] = []
    for cf in res.get("content_files") or []:
        content = cf.get("content")
        if content and isinstance(content, str):
            transcript = _clean_transcript(content)
            if transcript:
                break
    word_count = sum(len(p.split()) for p in transcript)
    if word_count < _MIN_TRANSCRIPT_WORDS:
        print(f"  skip {youtube_id}: 逐字稿仅 {word_count} 词（<{_MIN_TRANSCRIPT_WORDS}）",
              file=sys.stderr)
        return None

    # 时长：video.duration 是 ISO 8601
    video = res.get("video") or {}
    duration = _parse_iso_duration(video.get("duration"))
    if duration <= 0:  # 兜底：按 ~130 wpm 估
        duration = int(word_count / 130 * 60)
    if duration > _MAX_DURATION_SECONDS:
        print(f"  skip {youtube_id}: 时长 {duration/60:.0f} 分钟（>{_MAX_DURATION_SECONDS//60}）",
              file=sys.stderr)
        return None

    image = res.get("image") or {}
    thumb = image.get("url") or f"https://i.ytimg.com/vi/{youtube_id}/hqdefault.jpg"

    print(f"  ✓ {youtube_id} （逐字稿 {word_count} 词，{duration}s）", file=sys.stderr)
    return {
        "video_id": youtube_id,
        "title": title,
        "author": "MIT OpenCourseWare",
        "source": "MIT OpenCourseWare",
        "duration_seconds": duration,
        "thumbnail_url": thumb,
        "description": (res.get("description") or "")[:500],
        # OCW 视频托管在 YouTube，走 embed；不暴露下载 URL（合规）
        "play_mode": "youtube_embed",
        # 大学讲座整体偏难，给 C1 初值；ingest 的 cefr_grader 会据逐字稿实测细化
        "cefr_level": "C1",
        "categories_hint": ["discovery"],
        "transcript": transcript,
        "attribution": "MIT OpenCourseWare (CC BY-NC-SA)",
        "source_url": res.get("url") or "",
    }


async def fetch(limit: int, offset: int) -> list[dict]:
    """主流程：分页拉取 OCW 视频资源，逐条解析，凑满 limit 即停。"""
    out: list[dict] = []
    async with httpx.AsyncClient(headers={"User-Agent": _UA}) as client:
        page_size = max(limit * 4, 20)  # 超量拉，应对逐字稿过短 / 非 CC 被滤
        cur = offset
        # 最多翻 5 页，避免逐字稿命中率低时无限翻页
        for _ in range(5):
            if len(out) >= limit:
                break
            try:
                results = await _fetch_page(client, page_size, cur)
            except Exception as exc:  # noqa: BLE001
                print(f"  API 第 offset={cur} 页拉取失败: {exc}", file=sys.stderr)
                break
            if not results:
                break
            for res in results:
                if len(out) >= limit:
                    break
                item = _parse_resource(res)
                if item:
                    out.append(item)
            cur += page_size
    return out[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description="抓 MIT OpenCourseWare 讲座视频")
    parser.add_argument("--limit", type=int, default=10, help="最终产出条数上限")
    parser.add_argument("--offset", type=int, default=0, help="API 起始偏移（翻页用）")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "cdn-staging" / "mit-ocw-fetch.json"),
    )
    args = parser.parse_args()

    print(f"MIT OCW 抓取，目标 {args.limit} 条（offset={args.offset}）…", file=sys.stderr)
    items = asyncio.run(fetch(args.limit, args.offset))
    print(f"实际拿到 {len(items)} 条（CC 授权 + 成篇逐字稿）", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
