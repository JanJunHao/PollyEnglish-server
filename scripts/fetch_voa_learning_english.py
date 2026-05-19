"""抓 VOA Learning English 官网（learningenglish.voanews.com）文章元数据 + 逐字稿。

为什么抓官网而不是其 YouTube 频道：
- VOA 是美国政府出版物，**公有领域**（17 U.S.C. §105），合规零风险。
- 官网文章自带**完整逐字稿**（lesson 正文即逐字稿），且语言为专门面向英语学习者
  的"特别英语 / Learning English"——天然适合精读，无需再跑 ASR。
- 官网文章带原生 CDN 媒体（视频 mp4 / 音频 mp3），可走 Polly 的 native 播放模式。

数据流：
  本脚本（抓元数据 + 逐字稿）
    → 产出 fetch JSON（与 scripts/ingest.py --from-fetch 兼容）
    → ingest.py 把 transcript 转成 Polly 字幕 JSON、入库、Q1 打分

发现来源（两种，均在 robots.txt 允许范围内）：
  1) 站点地图 sitemap_428_1..4.xml.gz —— 列出全部文章 slug URL（默认）。
  2) --url 显式指定一个或多个文章 URL（调试 / 精选用）。

VOA 文章页结构（Pangea CMS）：
  - <div id="article-content"> 内 <div class="wsw"> 是正文逐字稿，<p> 分段。
  - <script type="application/ld+json"> 给 headline / description / datePublished。
  - 媒体：正文 / 播放器里嵌 voa-video-ns.akamaized.net 的 .mp4，
          或 voa-audio.voanews.eu 的 .mp3。
  - og:image 给封面图。

每条产出（与 fetch_nasa.py 同形 + transcript 字段）：
  {
    "video_id": "voa-<articleid>",
    "title": "...",
    "author": "VOA Learning English",
    "source": "VOA Learning English",
    "duration_seconds": 312,            # ffprobe 探媒体；探不到按词数估
    "thumbnail_url": "https://...png",
    "description": "...",
    "video_url": "https://...mp4 或 .mp3",  # native 模式 AVPlayer 播
    "play_mode": "native",
    "cefr_level": "B1",                 # VOA Learning English 整体 ~B1
    "categories_hint": ["daily_news"],
    "transcript": ["第一段...", "第二段...", ...],  # 逐字稿段落，ingest 转字幕
    "attribution": "Voice of America (public domain)"
  }

无逐字稿 / 逐字稿过短 / 非英语（站点混入法语等课程）的条目直接丢——Polly 是精读应用。

用法：
  python -m scripts.fetch_voa_learning_english --limit 3
  python -m scripts.fetch_voa_learning_english --url https://learningenglish.voanews.com/a/xxx/123.html
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import html as ihtml
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx

# ---- 站点常量 ----
_BASE = "https://learningenglish.voanews.com"
# 主站点地图：1..4 是全量文章归档（偏老内容），sitemap_428_latest 是近期内容。
# 默认走全量归档分片（命中率高、逐字稿稳）；
# 用 --sitemap latest 可改用近期分片——但近期分片多为纯数字 ID 短视频页，
# 逐字稿命中率低，需配合下面的 URL 过滤兜底（见 _discover_urls）。
_SITEMAP_ARCHIVE_SHARDS = [
    f"{_BASE}/sitemap_428_1.xml.gz",
    f"{_BASE}/sitemap_428_2.xml.gz",
    f"{_BASE}/sitemap_428_3.xml.gz",
    f"{_BASE}/sitemap_428_4.xml.gz",
]
_SITEMAP_LATEST_SHARDS = [
    f"{_BASE}/sitemap_428_latest.xml.gz",
]
# 兼容旧名引用
_SITEMAP_SHARDS = _SITEMAP_ARCHIVE_SHARDS
# 伪装常规浏览器 UA —— VOA 对默认 httpx UA 偶尔返简化页
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

# 逐字稿词数下限：VOA 站内混有大量纯短视频（VOA60 / Everyday Grammar），
# 这类页面没有正文逐字稿或正文只有一两句。精读应用需要成篇逐字稿，
# 200 词约 1.5 分钟有效内容，足够保守地把短视频和真正的 lesson 分开。
_MIN_TRANSCRIPT_WORDS = 200

# 时长上限：超 45 分钟的长节目先不收（移动端单次学习难跟完）。
_MAX_DURATION_SECONDS = 45 * 60

# slug 形式的英文文章 URL（/a/<slug>/<id>.html）——归档分片基本都是这种形式，
# slug 段含语义关键词，逐字稿命中率高。
_ARTICLE_URL_RE = re.compile(r"https://learningenglish\.voanews\.com/a/[a-z0-9-]+/\d+\.html")
# 纯数字 ID 形式（/a/<id>.html）——latest 近期分片几乎全是这种形式，
# 多为短视频页、逐字稿命中率低；仅在 --sitemap latest 时才纳入候选，
# 再靠 _parse_article 的逐字稿词数闸（_MIN_TRANSCRIPT_WORDS）兜底滤掉短视频。
_NUMERIC_ID_URL_RE = re.compile(r"https://learningenglish\.voanews\.com/a/\d+\.html")


# =========================================================================
# 站点地图：发现候选文章 URL
# =========================================================================

async def _discover_urls(
    client: httpx.AsyncClient, pool: int, sitemap: str = "archive"
) -> list[str]:
    """从站点地图分片里收集 slug 形式文章 URL，去重后返回 pool 条。

    sitemap：
      - 'archive'（默认）→ 全量归档分片 sitemap_428_1..4，偏老但逐字稿命中率高，
                           只收 slug 形式 URL。
      - 'latest'         → 近期分片 sitemap_428_latest。这份几乎全是纯数字 ID 页
                           （/a/<id>.html，多为短视频）；slug 形式与数字 ID 形式
                           都纳入候选，靠后续 _parse_article 的逐字稿词数闸
                           （_MIN_TRANSCRIPT_WORDS）兜底滤掉逐字稿过短的短视频。
    """
    shards = _SITEMAP_LATEST_SHARDS if sitemap == "latest" else _SITEMAP_ARCHIVE_SHARDS
    urls: list[str] = []
    seen: set[str] = set()
    for shard in shards:
        if len(urls) >= pool:
            break
        try:
            r = await client.get(shard, timeout=25.0)
            r.raise_for_status()
            raw = r.content
            # latest 分片有时不带 .gz 压缩；gzip 解不开就按明文处理
            try:
                xml = gzip.decompress(raw).decode("utf-8", errors="replace")
            except (OSError, EOFError, gzip.BadGzipFile):
                xml = raw.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            print(f"  站点地图 {shard} 拉取失败，跳过: {exc}", file=sys.stderr)
            continue
        # archive：只收 slug 形式（命中率高）。
        # latest：slug 形式优先，再补纯数字 ID 形式——latest 分片几乎全是后者，
        #         否则会一条候选都收不到；逐字稿过短的短视频后续会被词数闸滤掉。
        patterns = [_ARTICLE_URL_RE]
        if sitemap == "latest":
            patterns.append(_NUMERIC_ID_URL_RE)
        for pat in patterns:
            for m in pat.finditer(xml):
                u = m.group(0)
                if u in seen:
                    continue
                seen.add(u)
                urls.append(u)
                if len(urls) >= pool:
                    break
            if len(urls) >= pool:
                break
    return urls


# =========================================================================
# 单篇文章解析
# =========================================================================

def _article_id(url: str) -> str:
    """从 URL 末段取数字文章 ID 当 slug 基。"""
    m = re.search(r"/(\d+)\.html", url)
    return m.group(1) if m else url.rstrip("/").split("/")[-1]


def _extract_ld_json(html: str) -> dict:
    """取 ld+json（NewsArticle / VideoObject），失败返回空 dict。"""
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _extract_transcript(html: str) -> list[str]:
    """从 <div class="wsw"> 里抽逐字稿段落（清洗 HTML 标签 + 反转义）。

    只保留 > 3 词的段落，滤掉"图说""作者署名"之类一句话碎片中明显非正文的。
    """
    s = html.find('class="wsw"')
    if s < 0:
        return []
    # wsw 块通常在 article-content 内；取足够大的窗口再按 </div><!-- 收尾兜底
    block = html[s:s + 60000]
    paragraphs: list[str] = []
    for raw in re.findall(r"<p>(.*?)</p>", block, re.S):
        txt = re.sub(r"<[^>]+>", "", ihtml.unescape(raw))
        txt = re.sub(r"\s+", " ", txt).strip()
        if len(txt.split()) > 3:
            paragraphs.append(txt)
    return paragraphs


def _extract_media(html: str) -> tuple[str | None, str]:
    """抽媒体 URL。返回 (url, media_kind)，media_kind ∈ {'video','audio'}。

    优先视频 mp4（学习者更易跟读口型 / 画面）；无视频再回退音频 mp3。
    VOA 播放器把媒体清单塞在 HTML 里、用 &quot; 转义，先还原再匹配。
    """
    h = html.replace("&quot;", '"')
    # 视频：优先 _mobile.mp4（移动端友好、省流量），其次普通 mp4
    vids = re.findall(r'https://voa-video[^"<\s]+\.mp4', h)
    if vids:
        for v in vids:
            if "_mobile.mp4" in v:
                return v, "video"
        return vids[0], "video"
    # 音频：优先非 _hq（省流量）
    auds = re.findall(r'https://voa-audio[^"<\s]+\.mp3', h)
    if auds:
        for a in auds:
            if "_hq.mp3" not in a:
                return a, "audio"
        return auds[0], "audio"
    return None, ""


def _extract_thumbnail(html: str, ld: dict) -> str:
    """封面图：优先 ld+json.thumbnailUrl，其次 og:image。"""
    thumb = ld.get("thumbnailUrl")
    if isinstance(thumb, list):
        thumb = thumb[0] if thumb else None
    if isinstance(thumb, str) and thumb.startswith("http"):
        return thumb
    m = re.search(r'<meta[^>]*property="og:image"[^>]*content="([^"]*)"', html)
    if m and m.group(1).startswith("http"):
        return m.group(1)
    m = re.search(r'<meta[^>]*content="([^"]*)"[^>]*property="og:image"', html)
    return m.group(1) if m and m.group(1).startswith("http") else ""


def _extract_title(html: str, ld: dict) -> str:
    head = ld.get("headline") or ld.get("name")
    if isinstance(head, str) and head.strip():
        return ihtml.unescape(head).strip()
    m = re.search(r"<title>([^<]*)</title>", html)
    return ihtml.unescape(m.group(1)).strip() if m else "(untitled)"


# 非英语文章过滤：VOA 站点混入法语 / 西语等学习课程（如 "Leçon 40"），
# 这些不是 Polly 的目标语言。按常见非英语线索粗筛。
_NON_ENGLISH_HINTS = re.compile(
    r"(le[çc]on\b|lección|aprende inglés|apprendre l'anglais)", re.I
)


def _looks_english(title: str, transcript: list[str]) -> bool:
    if _NON_ENGLISH_HINTS.search(title):
        return False
    sample = " ".join(transcript[:3])
    if _NON_ENGLISH_HINTS.search(sample):
        return False
    return True


def _probe_duration(url: str) -> int:
    """用 ffprobe 探媒体时长（秒）。探不到返回 0，由上层按词数估。

    只读流头部，不下整文件；ffprobe 自带超时保护，再包一层 timeout。
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", url],
            capture_output=True, text=True, timeout=40,
        )
        if out.returncode == 0 and out.stdout.strip():
            return int(float(out.stdout.strip()))
    except Exception:  # noqa: BLE001
        pass
    return 0


async def _parse_article(client: httpx.AsyncClient, url: str) -> dict | None:
    """抓单篇文章，解析成 fetch JSON 条目。不合格返回 None。"""
    try:
        r = await client.get(url, timeout=25.0, follow_redirects=True)
        r.raise_for_status()
        html = r.text
    except Exception as exc:  # noqa: BLE001
        print(f"  skip {url}: 抓取失败 {exc}", file=sys.stderr)
        return None

    ld = _extract_ld_json(html)
    transcript = _extract_transcript(html)
    title = _extract_title(html, ld)

    # 逐字稿硬性卡：精读必须有成篇逐字稿
    word_count = sum(len(p.split()) for p in transcript)
    if word_count < _MIN_TRANSCRIPT_WORDS:
        print(f"  skip {url}: 逐字稿仅 {word_count} 词（<{_MIN_TRANSCRIPT_WORDS}）",
              file=sys.stderr)
        return None

    # 语言卡：滤掉法语 / 西语等非英语课程
    if not _looks_english(title, transcript):
        print(f"  skip {url}: 疑似非英语内容（{title[:40]}）", file=sys.stderr)
        return None

    # 媒体卡：必须有可播的原生媒体（视频或音频）
    media_url, media_kind = _extract_media(html)
    if not media_url:
        print(f"  skip {url}: 无可播媒体（{title[:40]}）", file=sys.stderr)
        return None

    # 时长：ffprobe 探；探不到按 VOA Learning English 平均语速 ~110 wpm 估
    duration = _probe_duration(media_url)
    if duration <= 0:
        duration = int(word_count / 110 * 60)
    if duration > _MAX_DURATION_SECONDS:
        print(f"  skip {url}: 时长 {duration/60:.0f} 分钟（>{_MAX_DURATION_SECONDS//60}）",
              file=sys.stderr)
        return None

    art_id = _article_id(url)
    description = ""
    if isinstance(ld.get("description"), str):
        description = ld["description"][:500]

    print(f"  ✓ voa-{art_id} （{media_kind}，逐字稿 {word_count} 词，{duration}s）",
          file=sys.stderr)
    return {
        "video_id": f"voa-{art_id}",
        "title": title,
        "author": "VOA Learning English",
        "source": "VOA Learning English",
        "duration_seconds": duration,
        "thumbnail_url": _extract_thumbnail(html, ld),
        "description": description,
        "video_url": media_url,
        "play_mode": "native",
        # VOA Learning English 全站定位为英语学习者，整体难度 ~B1；
        # ingest 端的 Q1 打分器 + cefr_grader 会据逐字稿实测细化。
        "cefr_level": "B1",
        "categories_hint": ["daily_news"],
        # 逐字稿原文段落 —— ingest --from-fetch 会转成 Polly 字幕 JSON。
        "transcript": transcript,
        # 公有领域，但仍按规划文档要求记 attribution。
        "attribution": "Voice of America (public domain)",
        "source_url": url,
    }


async def fetch(urls: list[str] | None, limit: int,
                sitemap: str = "archive") -> list[dict]:
    """主流程：发现候选 → 逐篇解析 → 凑满 limit 条合格条目即停。

    sitemap：'archive'（默认全量归档）或 'latest'（近期分片，命中率低需多拉候选）。
    """
    out: list[dict] = []
    async with httpx.AsyncClient(headers=_HEADERS) as client:
        if urls:
            candidates = urls
        else:
            # latest 分片逐字稿命中率低，候选池放大些以凑够 limit
            pool_mult = 10 if sitemap == "latest" else 6
            candidates = await _discover_urls(
                client, pool=max(limit * pool_mult, 30), sitemap=sitemap
            )
        print(f"  候选 {len(candidates)} 篇（sitemap={sitemap}），目标 {limit} 条",
              file=sys.stderr)
        for url in candidates:
            if len(out) >= limit:
                break
            item = await _parse_article(client, url)
            if item:
                out.append(item)
    return out[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="抓 VOA Learning English 官网文章（含逐字稿）")
    parser.add_argument("--limit", type=int, default=10,
                        help="最终产出合格条数上限")
    parser.add_argument("--url", action="append",
                        help="显式指定文章 URL（可多次）；不给则走站点地图发现")
    parser.add_argument(
        "--sitemap", choices=["archive", "latest"], default="archive",
        help="站点地图来源：archive=全量归档（默认，偏老但命中率高）；"
             "latest=近期分片（新内容，但多短视频页、逐字稿命中率低）",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "cdn-staging" / "voa-web-fetch.json"),
    )
    args = parser.parse_args()

    print(f"VOA Learning English 官网抓取，目标 {args.limit} 条"
          f"（sitemap={args.sitemap}）…", file=sys.stderr)
    items = asyncio.run(fetch(args.url, args.limit, args.sitemap))
    print(f"实际拿到 {len(items)} 条（有媒体 + 成篇英文逐字稿）", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
