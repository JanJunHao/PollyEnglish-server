"""Simple English Wikipedia 文章采集连接器。

Q4 图文线第一阶段：只接一个来源——Simple English Wikipedia（CC BY-SA 许可）。
Simple English Wikipedia 本身就是简化英语写成，天然偏低难度，且有公开的 MediaWiki API。

与 cc_pipeline 的「平台连接器」定位一致：确定性脚本，只负责把外部 API 的结构化
数据拉下来、清洗、统一输出到下游 article_ingest.py 能吃的 fetch JSON。不含 LLM。

合规要点（CC BY-SA）：
- 必须保留署名与来源。本模块为每篇文章生成 attribution 字符串，含条目标题、
  来源站点、原文 URL、许可名。下游会写入 Content.attribution 列。

用法：
    # 按指定标题采集
    python -m scripts.article_pipeline.fetch_simple_wikipedia \
        --titles "Photosynthesis" "Solar System" \
        --out cdn-staging/article-fetch/simplewiki.json

    # 随机采集 N 篇（探索 / 跑通用）
    python -m scripts.article_pipeline.fetch_simple_wikipedia \
        --random 3 --out cdn-staging/article-fetch/simplewiki.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

# Simple English Wikipedia 的 MediaWiki API 端点
_API_BASE = "https://simple.wikipedia.org/w/api.php"
# 站点显示名与许可——写 attribution 用
_SITE_NAME = "Simple English Wikipedia"
_LICENSE = "CC BY-SA 4.0"
# 礼貌的 User-Agent（MediaWiki API 要求标识来源）
_USER_AGENT = "PollyArticlePipeline/0.1 (English reading-comprehension app; contact polly)"


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
    """条目的稳定 URL（用于 attribution 署名）。"""
    slug = title.replace(" ", "_")
    return f"https://simple.wikipedia.org/wiki/{slug}"


def _make_attribution(title: str) -> str:
    """生成 CC BY-SA 署名字符串。
    格式同时满足「保留来源」「保留许可」「指回原文」三项要求。
    """
    return (
        f'"{title}" — {_SITE_NAME}, {_page_url(title)}. '
        f"Licensed under {_LICENSE}."
    )


# 尾部章节标题：命中即截断其后全部内容（这些章节对精读无价值）
_TAIL_SECTIONS = {
    "references", "related pages", "other websites", "sources", "notes",
    "bibliography", "external links", "see also", "further reading",
    "footnotes", "citations", "gallery", "in popular culture",
}


def _clean_extract(text: str) -> str:
    """清洗 API extract 纯文本。

    explaintext 模式下 API 已去掉 wiki 标记，但复杂条目（含表格 / 列表 /
    信息框）仍会残留杂质：
    - 章节标题（独立成行的短行，无句末标点）
    - 表格 / 信息框拆出来的碎片行（无标点、纯键值、纯数字）
    - 列表项前缀（"* " "- " "• " 等）与列表碎句
    - 坐标 / 单位残片、过短的非句子行
    这里做强化清洗，只保留成句的正文段落（段落之间用空行分隔）。
    """
    out_paragraphs: list[str] = []
    for block in text.split("\n"):
        line = block.strip()
        if not line:
            continue

        # 去掉列表项前缀符号（* - • · –），统一成普通文本再判断
        line = re.sub(r"^[\*\-•·–—]\s+", "", line).strip()
        if not line:
            continue

        # 折叠条目内多余空白
        line = re.sub(r"[ \t]{2,}", " ", line)

        lower = line.lower().rstrip(":")

        # 命中尾部章节标题 → 截断后续全部内容
        if lower in _TAIL_SECTIONS:
            break

        # 跳过章节标题行：较短且不以句末标点结尾
        if len(line) < 60 and not re.search(r"[.!?\"')\]]$", line):
            continue

        # 跳过表格 / 信息框碎片：含字母但整行没有任何句末标点，
        # 且看起来不是一句话（词数很少或像键值对）。
        if not re.search(r"[.!?]", line):
            word_count = len(line.split())
            # 纯短碎片（坐标、单位、键值、列表残句）→ 丢弃
            if word_count < 8:
                continue
            # 含 "键: 值" 形态的信息框残行 → 丢弃
            if re.match(r"^[^.]{1,40}:\s*\S", line):
                continue

        # 跳过纯数字 / 纯符号行（表格数据残片）
        if not re.search(r"[A-Za-z]", line):
            continue

        out_paragraphs.append(line)
    return "\n\n".join(out_paragraphs)


def fetch_article(title: str) -> dict | None:
    """采集单篇文章。返回统一 RawArticle dict，失败返回 None。

    输出字段（下游 article_ingest.py 消费）：
      article_id    稳定 id，simplewiki- 前缀 + 规整标题
      title         条目标题
      author        固定 'Simple English Wikipedia contributors'（CC BY-SA 集体署名）
      source        'Simple English Wikipedia'
      body          清洗后的正文纯文本
      source_url    原文 URL
      attribution   CC BY-SA 署名字符串
      image_urls    条目主图 URL 列表（可能为空）
      license       许可名
    """
    # 1) 拉正文纯文本 + 主图。一次 API 调用拿 extracts + pageimages。
    data = _api_get({
        "action": "query",
        "prop": "extracts|pageimages",
        "titles": title,
        "explaintext": "1",       # 返回纯文本而非 HTML
        "exsectionformat": "plain",
        "piprop": "original",     # 取原始尺寸主图
        "redirects": "1",         # 自动跟随重定向
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
        # cefr_grader 要求至少 30 词；太短的条目（消歧页 / 小作品）直接淘汰
        print(f"[fetch] '{real_title}' 正文过短（<30 词），跳过", file=sys.stderr)
        return None

    image_urls: list[str] = []
    original = page.get("original")
    if original and original.get("source"):
        image_urls.append(original["source"])

    article_id = "simplewiki-" + re.sub(r"[^a-z0-9]+", "-", real_title.lower()).strip("-")

    return {
        "article_id": article_id,
        "title": real_title,
        # CC BY-SA 维基条目由社区集体编写，按惯例署名 "contributors"
        "author": "Simple English Wikipedia contributors",
        "source": _SITE_NAME,
        "body": body,
        "source_url": _page_url(real_title),
        "attribution": _make_attribution(real_title),
        "image_urls": image_urls,
        "license": _LICENSE,
    }


def fetch_random_titles(n: int) -> list[str]:
    """随机取 n 个主命名空间（namespace 0）的条目标题。用于探索 / 跑通验证。"""
    data = _api_get({
        "action": "query",
        "list": "random",
        "rnnamespace": "0",
        "rnlimit": str(min(n, 20)),
    })
    return [r["title"] for r in data.get("query", {}).get("random", [])]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="采集 Simple English Wikipedia 文章 → article fetch JSON"
    )
    parser.add_argument("--titles", nargs="*", help="指定条目标题列表")
    parser.add_argument("--random", type=int, help="随机采集 N 篇")
    parser.add_argument(
        "--out",
        default="cdn-staging/article-fetch/simplewiki.json",
        help="输出的 fetch JSON 路径",
    )
    args = parser.parse_args()

    if not args.titles and not args.random:
        parser.error("--titles 或 --random 至少给一个")

    titles: list[str] = list(args.titles or [])
    if args.random:
        titles += fetch_random_titles(args.random)

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
