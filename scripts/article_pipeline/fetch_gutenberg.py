"""Project Gutenberg 文章采集连接器（Q4 图文线第 8 项 · 多来源接入）。

Project Gutenberg 收录大量公有领域（Public Domain）经典文学，适合做高难度
档的精读素材。本连接器走开源的 Gutendex API（https://gutendex.com）查元数据，
再下载纯文本电子书，截取一段成篇的「节选」供精读使用。

风格对齐 fetch_simple_wikipedia.py / fetch_wikinews.py：确定性脚本，不含 LLM，
输出统一 RawArticle JSON 供下游 article_ingest.py 消费。

为什么是「节选」而非整本书：
- 一本书几万词，远超单篇精读的合理长度；
- 这里从正文中段截取一个 _EXCERPT_WORDS 词左右、且按句子边界对齐的连续片段，
  作为一篇 article 入库。书名 + 作者写进 attribution / 标题。

合规要点（公有领域）：
- Gutenberg 正文本身是公有领域，无强制署名义务；但 Gutenberg 的「书目整理」
  受其商标条款约束。本连接器只取公有领域正文、剥掉 Gutenberg 的协议头尾，
  attribution 标注书名、作者、来源 Project Gutenberg、Public Domain。

用法：
    # 按 Gutenberg 电子书 id 采集（id 见 gutenberg.org 书页 URL）
    python -m scripts.article_pipeline.fetch_gutenberg \
        --ids 1342 11 --out cdn-staging/article-fetch/gutenberg.json

    # 按主题搜索抓 N 本（探索 / 跑通用）
    python -m scripts.article_pipeline.fetch_gutenberg \
        --search "fairy tales" --limit 2 \
        --out cdn-staging/article-fetch/gutenberg.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

_GUTENDEX_BASE = "https://gutendex.com/books"
_SITE_NAME = "Project Gutenberg"
_LICENSE = "Public Domain"
_USER_AGENT = "PollyArticlePipeline/0.1 (English reading-comprehension app; contact polly)"

# 节选目标词数：单篇精读的合理长度（与 Wikipedia 文章量级相当）
_EXCERPT_WORDS = 600
# Gutenberg 纯文本的协议头尾标记——截掉它们之间才是正文
_START_MARKERS = [
    "*** START OF THE PROJECT GUTENBERG",
    "*** START OF THIS PROJECT GUTENBERG",
    "***START OF THE PROJECT GUTENBERG",
]
_END_MARKERS = [
    "*** END OF THE PROJECT GUTENBERG",
    "*** END OF THIS PROJECT GUTENBERG",
    "***END OF THE PROJECT GUTENBERG",
]


def _http_get(url: str, *, is_json: bool = False):
    """通用 GET。is_json=True 时返回解析后的 JSON，否则返回文本。"""
    resp = httpx.get(
        url,
        headers={"User-Agent": _USER_AGENT},
        timeout=60,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.json() if is_json else resp.text


def _strip_gutenberg_wrapper(text: str) -> str:
    """剥掉 Gutenberg 纯文本的协议头尾，只留公有领域正文。"""
    upper = text.upper()
    start = 0
    for marker in _START_MARKERS:
        idx = upper.find(marker)
        if idx != -1:
            # 跳过该标记所在整行
            nl = text.find("\n", idx)
            start = nl + 1 if nl != -1 else idx + len(marker)
            break
    end = len(text)
    for marker in _END_MARKERS:
        idx = upper.find(marker, start)
        if idx != -1:
            end = idx
            break
    return text[start:end]


def _clean_text(text: str) -> str:
    """把 Gutenberg 正文整理成段落结构。

    Gutenberg 纯文本用「空行分段、段内硬换行」排版。这里：
    - 按空行切自然段；
    - 段内的硬换行折叠成空格（还原成连续文本）；
    - 丢掉全大写的章节标题行、装饰性分隔行。
    """
    # 先归一化换行符：Gutenberg 的 .txt.utf-8 用 CRLF，不归一化会导致空行
    # 切段正则匹配不到、整本书被当成一个段落。
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[str] = []
    for block in re.split(r"\n[ \t]*\n", text):
        block = block.strip()
        if not block:
            continue
        # 段内硬换行折叠为空格
        joined = re.sub(r"\s*\n\s*", " ", block).strip()
        joined = re.sub(r"[ \t]{2,}", " ", joined)
        if not joined:
            continue
        # 丢掉章节标题 / 装饰行：全大写、或太短且无句末标点
        if joined.isupper():
            continue
        if len(joined) < 40 and not re.search(r"[.!?\"')\]]$", joined):
            continue
        if not re.search(r"[A-Za-z]", joined):
            continue
        paragraphs.append(joined)
    return "\n\n".join(paragraphs)


def _take_excerpt(body: str, target_words: int = _EXCERPT_WORDS) -> str:
    """从正文中段取一段约 target_words 词、按整段对齐的连续节选。

    从中段开始（跳过开头的版权页 / 目录 / 献词等噪声），按自然段累加，
    直到词数达标——保证节选不会从句子中间断开。
    """
    paras = [p for p in body.split("\n\n") if p.strip()]
    if not paras:
        return ""
    # 从全文约 1/5 处起，避开卷首杂项
    start_idx = len(paras) // 5
    picked: list[str] = []
    words = 0
    for para in paras[start_idx:]:
        picked.append(para)
        words += len(para.split())
        if words >= target_words:
            break
    return "\n\n".join(picked)


def _plain_text_url(book: dict) -> str | None:
    """从 Gutendex book 记录里挑一个纯文本格式的下载 URL。"""
    formats = book.get("formats", {})
    # 优先 utf-8 纯文本
    for mime, url in formats.items():
        if mime.startswith("text/plain") and "utf-8" in mime:
            return url
    for mime, url in formats.items():
        if mime.startswith("text/plain"):
            return url
    return None


def _book_to_article(book: dict) -> dict | None:
    """把一条 Gutendex book 记录采集成统一 RawArticle dict，失败返回 None。"""
    book_id = book.get("id")
    title = (book.get("title") or "").strip().replace("\n", " ")
    authors = book.get("authors") or []
    author_name = authors[0]["name"] if authors else "Unknown"

    txt_url = _plain_text_url(book)
    if not txt_url:
        print(f"[fetch] '{title}' 无纯文本格式，跳过", file=sys.stderr)
        return None

    try:
        raw = _http_get(txt_url)
    except httpx.HTTPError as exc:
        print(f"[fetch] '{title}' 正文下载失败：{exc}", file=sys.stderr)
        return None

    inner = _strip_gutenberg_wrapper(raw)
    cleaned = _clean_text(inner)
    body = _take_excerpt(cleaned)
    if len(body.split()) < 30:
        print(f"[fetch] '{title}' 节选过短（<30 词），跳过", file=sys.stderr)
        return None

    source_url = f"https://www.gutenberg.org/ebooks/{book_id}"
    # 节选标题标明是节选，避免误以为是全书
    display_title = f"{title} (excerpt)"
    attribution = (
        f'"{title}" by {author_name} — {_SITE_NAME}, {source_url}. '
        f"{_LICENSE}."
    )
    article_id = f"gutenberg-{book_id}"

    return {
        "article_id": article_id,
        "title": display_title,
        "author": author_name,
        "source": _SITE_NAME,
        "body": body,
        "source_url": source_url,
        "attribution": attribution,
        "image_urls": [],  # Gutenberg 纯文本电子书无配图
        "license": _LICENSE,
    }


def fetch_by_ids(ids: list[int]) -> list[dict]:
    """按 Gutenberg 电子书 id 采集。"""
    out: list[dict] = []
    for book_id in ids:
        try:
            book = _http_get(f"{_GUTENDEX_BASE}/{book_id}", is_json=True)
        except httpx.HTTPError as exc:
            print(f"[fetch] id={book_id} 查询失败：{exc}", file=sys.stderr)
            continue
        article = _book_to_article(book)
        if article is not None:
            out.append(article)
            print(f"[fetch] OK '{article['title']}'  {len(article['body'].split())} 词",
                  file=sys.stderr)
    return out


def fetch_by_search(query: str, limit: int) -> list[dict]:
    """按关键词搜索采集（只取 languages=en 的公有领域书）。"""
    try:
        data = _http_get(
            f"{_GUTENDEX_BASE}?search={httpx.QueryParams({'q': query})['q']}"
            f"&languages=en&mime_type=text%2Fplain",
            is_json=True,
        )
    except httpx.HTTPError as exc:
        print(f"[fetch] 搜索 '{query}' 失败：{exc}", file=sys.stderr)
        return []
    out: list[dict] = []
    for book in data.get("results", []):
        if len(out) >= limit:
            break
        article = _book_to_article(book)
        if article is not None:
            out.append(article)
            print(f"[fetch] OK '{article['title']}'  {len(article['body'].split())} 词",
                  file=sys.stderr)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="采集 Project Gutenberg 公有领域文学节选 → article fetch JSON"
    )
    parser.add_argument("--ids", nargs="*", type=int, help="Gutenberg 电子书 id 列表")
    parser.add_argument("--search", help="按关键词搜索")
    parser.add_argument("--limit", type=int, default=3, help="搜索模式下最多采集本数")
    parser.add_argument(
        "--out",
        default="cdn-staging/article-fetch/gutenberg.json",
        help="输出的 fetch JSON 路径",
    )
    args = parser.parse_args()

    if not args.ids and not args.search:
        parser.error("--ids 或 --search 至少给一个")

    out: list[dict] = []
    if args.ids:
        out += fetch_by_ids(args.ids)
    if args.search:
        out += fetch_by_search(args.search, args.limit)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"采集完成 → 入选 {len(out)} 篇，已写出 {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
