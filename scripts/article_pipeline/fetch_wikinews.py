"""Wikinews 文章采集连接器（Q4 图文线第 8 项 · 多来源接入）。

Wikinews 是维基媒体旗下的协作新闻站，正文以 CC BY 2.5 许可发布。
与 Simple English Wikipedia 同样走 MediaWiki API，风格对齐
fetch_simple_wikipedia.py：确定性脚本，不含 LLM，输出统一 RawArticle JSON
供下游 article_ingest.py 消费。

合规要点（CC BY 2.5）：
- CC BY 只要求保留署名（不要求同源协议），仍需保留来源、许可、指回原文 URL。
- 本模块为每篇文章生成 attribution 字符串，写入 Content.attribution。

用法：
    # 按指定标题采集
    python -m scripts.article_pipeline.fetch_wikinews \
        --titles "Some news headline" \
        --out cdn-staging/article-fetch/wikinews.json

    # 抓最近发布的 N 篇（推荐：跑通 / 探索用）
    python -m scripts.article_pipeline.fetch_wikinews \
        --latest 3 --out cdn-staging/article-fetch/wikinews.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

# 英文 Wikinews 的 MediaWiki API 端点
_API_BASE = "https://en.wikinews.org/w/api.php"
_SITE_NAME = "Wikinews"
# Wikinews 正文采用 CC BY 2.5（注意：不是 BY-SA，与 Wikipedia 不同）
_LICENSE = "CC BY 2.5"
_USER_AGENT = "PollyArticlePipeline/0.1 (English reading-comprehension app; contact polly)"

# Wikinews 文章惯用尾部章节，命中即截断
_TAIL_SECTIONS = {
    "sources", "related news", "external links", "references",
    "sister links", "see also", "notes",
}


def _api_get(params: dict) -> dict:
    """调用 MediaWiki API，统一注入 format=json。"""
    params = {**params, "format": "json", "formatversion": "2"}
    resp = httpx.get(
        _API_BASE,
        params=params,
        headers={"User-Agent": _USER_AGENT},
        timeout=30,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.json()


def _page_url(title: str) -> str:
    """条目稳定 URL（用于 attribution 署名）。"""
    slug = title.replace(" ", "_")
    return f"https://en.wikinews.org/wiki/{slug}"


def _make_attribution(title: str) -> str:
    """生成 CC BY 2.5 署名字符串：保留来源、许可、指回原文。"""
    return (
        f'"{title}" — {_SITE_NAME}, {_page_url(title)}. '
        f"Licensed under {_LICENSE}."
    )


def _clean_extract(text: str) -> str:
    """清洗 Wikinews extract 纯文本。

    复用 fetch_simple_wikipedia 同款思路：去章节标题、去碎片行、
    截断尾部「Sources / Related news」等无价值章节。
    """
    out_paragraphs: list[str] = []
    for block in text.split("\n"):
        line = block.strip()
        if not line:
            continue
        line = re.sub(r"^[\*\-•·–—]\s+", "", line).strip()
        if not line:
            continue
        line = re.sub(r"[ \t]{2,}", " ", line)
        lower = line.lower().rstrip(":")
        if lower in _TAIL_SECTIONS:
            break
        # 章节标题：短行 + 无句末标点
        if len(line) < 60 and not re.search(r"[.!?\"')\]]$", line):
            continue
        # 无句末标点的短碎片（表格 / 列表残片）
        if not re.search(r"[.!?]", line) and len(line.split()) < 8:
            continue
        if not re.search(r"[A-Za-z]", line):
            continue
        out_paragraphs.append(line)
    return "\n\n".join(out_paragraphs)


def fetch_article(title: str) -> dict | None:
    """采集单篇 Wikinews 文章。返回统一 RawArticle dict，失败返回 None。

    输出字段与 fetch_simple_wikipedia.fetch_article 完全一致，
    保证下游 article_ingest.py 可无差别消费。
    """
    data = _api_get({
        "action": "query",
        "prop": "extracts|pageimages",
        "titles": title,
        "explaintext": "1",
        "exsectionformat": "plain",
        "piprop": "original",
        "redirects": "1",
    })
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        print(f"[fetch] '{title}' 无结果", file=sys.stderr)
        return None
    page = pages[0]
    if page.get("missing"):
        print(f"[fetch] '{title}' 条目不存在", file=sys.stderr)
        return None

    real_title = page.get("title", title)
    raw_extract = page.get("extract", "") or ""
    body = _clean_extract(raw_extract)
    if len(body.split()) < 30:
        # cefr_grader 要求至少 30 词；过短的（草稿 / 占位）淘汰
        print(f"[fetch] '{real_title}' 正文过短（<30 词），跳过", file=sys.stderr)
        return None

    image_urls: list[str] = []
    original = page.get("original")
    if original and original.get("source"):
        image_urls.append(original["source"])

    article_id = "wikinews-" + re.sub(r"[^a-z0-9]+", "-", real_title.lower()).strip("-")

    return {
        "article_id": article_id,
        "title": real_title,
        # Wikinews 文章由社区集体编写，按惯例集体署名
        "author": "Wikinews contributors",
        "source": _SITE_NAME,
        "body": body,
        "source_url": _page_url(real_title),
        "attribution": _make_attribution(real_title),
        "image_urls": image_urls,
        "license": _LICENSE,
    }


def fetch_latest_titles(n: int) -> list[str]:
    """取最近发布的 n 篇 Wikinews 文章标题。

    走「Published」分类（Wikinews 已发布文章都归入此分类），按最新排列。
    """
    data = _api_get({
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:Published",
        "cmnamespace": "0",
        "cmsort": "timestamp",
        "cmdir": "desc",
        "cmlimit": str(min(n, 50)),
    })
    members = data.get("query", {}).get("categorymembers", [])
    return [m["title"] for m in members]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="采集 Wikinews 文章 → article fetch JSON"
    )
    parser.add_argument("--titles", nargs="*", help="指定条目标题列表")
    parser.add_argument("--latest", type=int, help="抓最近发布的 N 篇")
    parser.add_argument(
        "--out",
        default="cdn-staging/article-fetch/wikinews.json",
        help="输出的 fetch JSON 路径",
    )
    args = parser.parse_args()

    if not args.titles and not args.latest:
        parser.error("--titles 或 --latest 至少给一个")

    titles: list[str] = list(args.titles or [])
    if args.latest:
        titles += fetch_latest_titles(args.latest)

    out: list[dict] = []
    for t in titles:
        article = fetch_article(t)
        if article is not None:
            out.append(article)
            print(f"[fetch] OK '{article['title']}'  {len(article['body'].split())} 词",
                  file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"采集 {len(titles)} 篇 → 入选 {len(out)} 篇，已写出 {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
