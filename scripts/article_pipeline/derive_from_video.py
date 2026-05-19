"""视频转写改写成图文派生管线（Q5 数据复用「① 派生新内容」/ 阶段 3 深化）。

核心思路：把已有视频的逐字稿（字幕 JSON）用 LLM **改写成一篇连贯的图文精读文章**，
零成本把视频「一鱼多吃」成图文内容。

端到端链路：

  视频 content（kind='video'）
        ↓ 从 subtitle_url 取字幕 JSON（{"segments":[{"text":...},...]}）
        ↓ 逐句文本拼成全文
        ↓ LLM 改写成连贯文章（补连接词 / 分段 / 去口语碎片，保持原意与难度）
        ↓ 产出 RawArticle JSON（与 fetch_simple_wikipedia.py 同结构）
        ↓ 交给 article_ingest.ingest_one（CEFR / 切段 / 分类 / 翻译 / 标注全复用）
  [派生图文入库 contents(kind='article') + ArticleDetails]

派生文章可追溯来源：
- Content.attribution 写「改写自视频：<标题>」并附原视频来源；
- article_id 用 `derived-<video_id>` 前缀，便于识别与幂等判重。

不重写下游能力：CEFR 评级 / 切段 / 题材分类 / 翻译 / LLM 标注全部复用 article_ingest。

用法：
    # 单条
    python -m scripts.article_pipeline.derive_from_video --content-id 63KXWNeRx9c
    # 批量（所有有字幕的视频）
    python -m scripts.article_pipeline.derive_from_video --all --limit 2
    # 已存在时强制覆盖
    python -m scripts.article_pipeline.derive_from_video --content-id X --force
    # 跳过 LLM 标注（只跑改写 + 评级 + 切段 + 入库，调试用）
    python -m scripts.article_pipeline.derive_from_video --content-id X --no-annotate
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from openai import APIError, AsyncOpenAI

from app.config import get_settings
from app.db import SessionLocal
from app.db_bootstrap import ensure_schema_dev
from app.models import ArticleDetails, Content
from app.services.quality_scorer import load_subtitle_doc
from scripts.article_pipeline.article_ingest import ingest_one

# ---- 调参 ----
# 派生文章 id 前缀：便于识别来源与幂等判重
_DERIVED_PREFIX = "derived-"
# LLM 改写时单批喂入的字幕句数上限——长视频字幕可达数百句，
# 一次喂太多会超 token 且让模型「偷懒」漏内容，分批改写后拼接。
_REWRITE_BATCH_SENTENCES = 60
# 改写并发上限（与 article_ingest 标注并发对齐）
_REWRITE_CONCURRENCY = 4


# ============================================================
# 改写 prompt
# ============================================================

# 把口语逐字稿改写成书面图文精读文章。强调「改写」而非「拼接」：
# 补连接词、分段、去口语碎片，但不增删事实、不显著改变难度。
_REWRITE_SYSTEM = """你是一位英语编辑，正在把一段视频的口语逐字稿改写成一篇适合阅读的书面文章。

要求：
1. 这是「改写」不是「翻译」，也不是「总结」——输出仍是英文，保留逐字稿的全部信息与事实，不要新增观点。
2. 去掉口语碎片：填充词（um、you know、so 等）、重复、口播套话（如「Welcome to ...」「I'm X and I'm Y」之类的栏目开场/转场）、与内容无关的寒暄。
3. 把零散的口语短句整合成完整、连贯的书面句子，补上必要的连接词与过渡。
4. 按主题分段，段落之间用一个空行分隔。
5. 保持原文的英语难度大致不变——不要刻意拔高或简化用词。
6. 不要加标题、不要加 markdown 标记、不要加任何解释，只输出改写后的正文段落。

直接输出改写后的英文文章正文。"""


def _rewrite_prompt(title: str, source: str, chunk_text: str,
                     is_first: bool, is_last: bool) -> str:
    """构造单批改写 prompt。"""
    parts = [f"视频标题：{title}", f"来源：{source}"]
    if not is_first:
        parts.append("（这是逐字稿的中间部分，承接上文，开头不要写引入语。）")
    if not is_last:
        parts.append("（这不是逐字稿的结尾，结尾不要写总结收束语。）")
    parts.append("")
    parts.append("逐字稿片段：")
    parts.append(chunk_text)
    return "\n".join(parts)


# ============================================================
# 字幕 → 全文改写
# ============================================================

def _subtitle_sentences(doc: dict) -> list[str]:
    """从 SubtitleDocument 取出逐句英文文本，过滤空句。"""
    out: list[str] = []
    for seg in doc.get("segments", []) or []:
        text = (seg.get("text") or "").strip()
        if text:
            out.append(text)
    return out


def _chunk_sentences(sentences: list[str], size: int) -> list[list[str]]:
    """把句子列表按 size 切成多批。"""
    return [sentences[i:i + size] for i in range(0, len(sentences), size)]


def _client() -> AsyncOpenAI:
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY 未配置，无法做视频改写")
    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )


async def _rewrite_chunk(client: AsyncOpenAI, model: str, title: str, source: str,
                         chunk: list[str], is_first: bool, is_last: bool) -> str:
    """改写单批字幕句子，返回改写后的英文段落文本。"""
    prompt = _rewrite_prompt(title, source, " ".join(chunk), is_first, is_last)
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _REWRITE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
    )
    return (resp.choices[0].message.content or "").strip()


async def rewrite_transcript(title: str, source: str, sentences: list[str]) -> str:
    """把整段逐字稿改写成连贯文章正文。

    长视频按 _REWRITE_BATCH_SENTENCES 分批并发改写，再按原顺序拼接，
    批与批之间用空行分隔（保持段落结构）。
    """
    settings = get_settings()
    model = settings.openai_explain_model
    client = _client()

    chunks = _chunk_sentences(sentences, _REWRITE_BATCH_SENTENCES)
    sem = asyncio.Semaphore(_REWRITE_CONCURRENCY)

    async def worker(idx: int, chunk: list[str]) -> tuple[int, str]:
        async with sem:
            text = await _rewrite_chunk(
                client, model, title, source, chunk,
                is_first=(idx == 0), is_last=(idx == len(chunks) - 1),
            )
            return idx, text

    results = await asyncio.gather(*(worker(i, c) for i, c in enumerate(chunks)))
    # 按原批次顺序拼接
    ordered = [text for _, text in sorted(results, key=lambda x: x[0])]
    return "\n\n".join(t for t in ordered if t)


# ============================================================
# 构造 RawArticle（与 fetch_simple_wikipedia.py 输出同结构）
# ============================================================

def build_raw_article(video: Content, body: str) -> dict:
    """把改写后的正文包成下游 article_ingest 能吃的 RawArticle dict。

    article_id 用 derived-<video_id> 前缀，便于识别来源与幂等判重。
    attribution 标注「改写自视频」，可追溯回原视频。
    """
    article_id = _DERIVED_PREFIX + video.id
    attribution = (
        f'改写自视频：《{video.title}》（来源：{video.source}）。'
        f'本文由该视频逐字稿经 AI 改写为图文精读版。'
    )
    return {
        "article_id": article_id,
        "title": video.title,
        # 派生内容不归个人作者，署原视频来源方
        "author": video.author or video.source or "Polly",
        "source": video.source or "Polly derived",
        "body": body,
        "source_url": video.video_url or "",
        "attribution": attribution,
        # 派生文章不带配图（视频帧版权 / 提取成本另议，留 TODO）
        "image_urls": [],
        "license": "derived",
    }


# ============================================================
# 单条派生
# ============================================================

async def derive_one(content_id: str, *, do_annotate: bool, force: bool) -> dict:
    """对一个视频 content 跑视频→图文派生。"""
    with SessionLocal() as db:
        video = db.get(Content, content_id)
        if video is None:
            return {"content_id": content_id, "error": "content 不存在"}
        if video.kind != "video":
            return {"content_id": content_id, "error": f"不是视频（kind={video.kind}）"}
        if not video.subtitle_url:
            return {"content_id": content_id, "error": "无 subtitle_url，无法派生"}
        # 提前取出需要的字段，避免 session 关闭后访问
        subtitle_url = video.subtitle_url
        video_detached = video

    article_id = _DERIVED_PREFIX + content_id

    # 幂等：派生文章已存在则跳过（除非 --force）
    with SessionLocal() as db:
        if db.get(Content, article_id) is not None and not force:
            return {
                "content_id": content_id,
                "article_id": article_id,
                "action": "skipped",
                "reason": "派生文章已存在（加 --force 覆盖）",
            }

    # 1) 取字幕 JSON
    doc = load_subtitle_doc(subtitle_url)
    if doc is None:
        return {"content_id": content_id, "error": f"字幕取回失败：{subtitle_url}"}
    sentences = _subtitle_sentences(doc)
    if len(sentences) < 5:
        return {"content_id": content_id, "error": f"字幕句子过少（{len(sentences)} 句）"}

    # 2) LLM 改写成连贯文章
    body = await rewrite_transcript(
        video_detached.title, video_detached.source or "", sentences,
    )
    if len(body.split()) < 30:
        # 下游 cefr_grader 要求 ≥30 词
        return {"content_id": content_id, "error": f"改写后正文过短（{len(body.split())} 词）"}

    # 3) 包成 RawArticle，交给现有 article_ingest 主流程入库
    raw_article = build_raw_article(video_detached, body)
    result = await ingest_one(raw_article, do_annotate=do_annotate)

    return {
        "content_id": content_id,
        "video_title": (video_detached.title or "")[:50],
        "transcript_sentences": len(sentences),
        "article": result,
    }


# ============================================================
# 主流程
# ============================================================

def _list_subtitled_videos(limit: int | None) -> list[str]:
    """列出所有有字幕的视频 content_id。"""
    with SessionLocal() as db:
        q = (
            db.query(Content.id)
            .filter(Content.kind == "video", Content.subtitle_url.isnot(None))
            .order_by(Content.created_at.desc())
        )
        if limit:
            q = q.limit(limit)
        return [row[0] for row in q.all()]


async def run(content_ids: list[str], *, do_annotate: bool, force: bool) -> None:
    """逐条串行派生（改写 + 标注内部已并发，不需要条级并发；
    也避免多条同时写 SQLite 引发并发写冲突）。"""
    for cid in content_ids:
        try:
            result = await derive_one(cid, do_annotate=do_annotate, force=force)
        except Exception as exc:  # noqa: BLE001
            result = {"content_id": cid, "error": str(exc)[:300]}
        print(json.dumps(result, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="视频转写改写成图文派生管线（Q5 数据复用）"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--content-id", help="单个视频 content_id")
    group.add_argument("--all", action="store_true", help="批量处理所有有字幕的视频")
    parser.add_argument("--limit", type=int, help="--all 时只处理前 N 条")
    parser.add_argument("--force", action="store_true",
                        help="派生文章已存在时强制覆盖")
    parser.add_argument("--no-annotate", action="store_true",
                        help="跳过 LLM 标注（只改写 + 评级 + 切段 + 入库）")
    args = parser.parse_args()

    ensure_schema_dev()

    if args.all:
        content_ids = _list_subtitled_videos(args.limit)
        print(f"批量派生：{len(content_ids)} 个有字幕的视频", file=sys.stderr)
    else:
        content_ids = [args.content_id]

    asyncio.run(run(content_ids, do_annotate=not args.no_annotate, force=args.force))


if __name__ == "__main__":
    main()
