"""图文采集管线主流程：把 article fetch JSON 走完整链路入库。

端到端链路（Q4 图文线，阶段 1）：

  采集的 article fetch JSON（fetch_simple_wikipedia.py 产出）
        ↓ cefr_grader.py 评 CEFR 难度
        ↓ segment.py 切段（段落 / 句子）
        ↓ 受控题材分类（classify_topics.py）→ Content.topics
        ↓ segment 逐句翻译（translation_pipeline.translate_segments）→ paragraphs[].translation
        ↓ LLM 标注：逐句句子讲解（Explanation）+ 查词卡（Word）
        ↓ 写库：contents(kind='article') + ArticleDetails
  [图文内容入库，精读层 Word/Explanation 缓存就绪]

复用现有能力，不重写：
- CEFR 评级：scripts.cc_pipeline.cefr_grader.CEFRGrader
- 受控题材分类：scripts.article_pipeline.classify_topics（落 app.taxonomy 受控词表）
- segment 翻译：app.services.translation_pipeline.translate_segments（与视频字幕共用）
- 句子讲解：app.api.ai 的 EXPLAIN_SYSTEM + 同款 OpenAI 调用，写 Explanation 表
- 查词卡：app.api.ai 的 WORD_SYSTEM，写 Word 表
- 存储抽象：app.storage.get_storage（配图转存用）

合规：CC BY-SA 内容的 attribution 写入 Content.attribution 列。

用法：
    python -m scripts.article_pipeline.article_ingest \
        --from-fetch cdn-staging/article-fetch/simplewiki.json
    python -m scripts.article_pipeline.article_ingest --from-fetch X --limit 1
    python -m scripts.article_pipeline.article_ingest --from-fetch X --no-annotate
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
from openai import APIError, AsyncOpenAI

from app.api.ai import EXPLAIN_SYSTEM, WORD_SYSTEM
from app.config import get_settings
from app.db import SessionLocal
from app.db_bootstrap import ensure_schema_dev
from app.models import ArticleDetails, Content, Explanation, Word
from app.services.translation_pipeline import translate_segments
from app.storage import get_storage, guess_content_type
from scripts.article_pipeline.classify_topics import classify_article_topics
from scripts.article_pipeline.segment import segment_article
from scripts.cc_pipeline.cefr_grader import CEFRGrader

# ---- 调参 ----
# 阅读速度估算：成人英语学习者约 150 wpm；偏保守用 130 让 reading_time 略宽。
_WORDS_PER_MINUTE = 130
# LLM 标注并发上限（与 pregenerate_explanations.py 默认值对齐）
_ANNOTATE_CONCURRENCY = 5
# CEFR grader 评 UNKNOWN 时的兜底等级（同 ingest.py 的 B1 兜底约定）
_CEFR_FALLBACK = "B1"
# 题材分类置信度阈值：类比视频侧 ingest.py 的 _CLASSIFY_CONFIDENCE_THRESHOLD = 0.7。
# 低于此阈值视为「弱贴合」（如《Photosynthesis》误标 science.space），
# 图文 status 落为 review_pending——不上首页、不删数据，等人工复审。
_CLASSIFY_CONFIDENCE_THRESHOLD = 0.7


# ============================================================
# 步骤 1：CEFR 评级
# ============================================================

def grade_cefr(body: str) -> tuple[str, dict]:
    """用 cc_pipeline 的 CEFRGrader 评文章难度。

    返回 (cefr_level, 完整评分结果)。文本过短 / 评为 UNKNOWN 时兜底 B1。
    """
    grader = CEFRGrader()
    result = grader.grade(body)
    cefr = result.get("cefr", "UNKNOWN")
    if cefr == "UNKNOWN":
        cefr = _CEFR_FALLBACK
    return cefr, result


# ============================================================
# 步骤 2：配图转存
# ============================================================

def _download_bytes(url: str) -> bytes | None:
    # Wikimedia 等图床会拒绝缺省 / 空 User-Agent 的请求，必须带标识。
    try:
        resp = httpx.get(
            url,
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "PollyArticlePipeline/0.1 (English reading-comprehension app)"},
        )
    except httpx.HTTPError:
        return None
    if resp.status_code == 200 and resp.content:
        return resp.content
    return None


def mirror_images(image_urls: list[str], article_id: str) -> list[str]:
    """把外部配图下载并转存到自己 storage，返回自托管 URL 列表。

    与 ingest.mirror_thumbnail 思路一致：App 端不依赖第三方图床。
    下载失败的图直接丢弃（图文配图非必需，不阻塞入库）。
    """
    storage = get_storage()
    out: list[str] = []
    for idx, url in enumerate(image_urls):
        if not url:
            continue
        data = _download_bytes(url)
        if data is None:
            print(f"[{article_id}] 配图下载失败，跳过：{url}", file=sys.stderr)
            continue
        # 推断扩展名，默认 .jpg
        suffix = Path(urlparse(url).path).suffix.lower() or ".jpg"
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".svg"}:
            suffix = ".jpg"
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name)
            key = f"article-images/{article_id}-{idx}{suffix}"
            out.append(storage.put(tmp_path, key, content_type=guess_content_type(tmp_path)))
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
    return out


# ============================================================
# 步骤 3：LLM 标注（句子讲解 + 查词卡）
# ============================================================

def _client() -> AsyncOpenAI:
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY 未配置，无法做 LLM 标注（可加 --no-annotate 跳过）")
    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )


def _explain_prompt(article: dict, cefr: str, sentence: str,
                    ctx_before: str | None, ctx_after: str | None) -> str:
    """构造句子讲解 prompt，与 app/api/ai.py / pregenerate_explanations.py 同款结构。"""
    parts = [f"原句：{sentence}"]
    parts.append(f"文章：{article['title']}（{article['author']}，{article['source']}）")
    parts.append(f"用户水平：{cefr}")
    if ctx_before:
        parts.append(f"上文：{ctx_before}")
    if ctx_after:
        parts.append(f"下文：{ctx_after}")
    return "\n".join(parts)


async def _explain_one(client: AsyncOpenAI, model: str, prompt: str) -> dict:
    """调 OpenAI 生成一句讲解。复用 EXPLAIN_SYSTEM。"""
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EXPLAIN_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    data["_model"] = resp.model
    return data


async def _word_one(client: AsyncOpenAI, model: str, word: str, context: str) -> dict:
    """调 OpenAI 生成一张查词卡。复用 WORD_SYSTEM。"""
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": WORD_SYSTEM},
            {"role": "user", "content": f"查询单词：{word}\n上下文：{context or '（无）'}"},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
        max_tokens=300,
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    data["_model"] = resp.model
    return data


async def annotate_article(article_id: str, article: dict, cefr: str,
                           segments: list[dict]) -> dict:
    """对一篇文章做完整 LLM 标注：逐句讲解 + 查词卡，写入 Explanation / Word 表。

    讲解：每个 segment 一条 Explanation（video_id=article_id, segment_id=句序号），
          与视频精读层 schema 完全一致——精读层形态无关。
    查词卡：从讲解返回的 key_vocab 聚合去重出生词，逐词生成 Word 缓存。
            Word 表按 lemma 全局共用，已存在的词跳过（省 token）。

    返回标注统计摘要。
    """
    settings = get_settings()
    model = settings.openai_explain_model
    client = _client()
    sem = asyncio.Semaphore(_ANNOTATE_CONCURRENCY)

    # ---- 3a. 逐句讲解 ----
    async def explain_worker(idx: int, seg: dict) -> tuple[dict, dict] | None:
        async with sem:
            ctx_before = segments[idx - 1]["text"] if idx > 0 else None
            ctx_after = segments[idx + 1]["text"] if idx + 1 < len(segments) else None
            prompt = _explain_prompt(article, cefr, seg["text"], ctx_before, ctx_after)
            try:
                data = await _explain_one(client, model, prompt)
                return seg, data
            except (APIError, json.JSONDecodeError) as exc:
                print(f"[{article_id}] seg {seg['id']} 讲解失败：{exc}", file=sys.stderr)
                return None

    explain_results = await asyncio.gather(
        *(explain_worker(i, s) for i, s in enumerate(segments))
    )

    # 入库 Explanation + 聚合生词
    vocab_words: dict[str, str] = {}  # lemma -> 一个出现它的句子（当 context）
    explanations_written = 0
    with SessionLocal() as db:
        for pair in explain_results:
            if pair is None:
                continue
            seg, data = pair
            db.merge(Explanation(
                video_id=article_id,        # 图文复用 video_id 列承载 content_id
                segment_id=seg["id"],
                sentence=seg["text"],
                natural_translation=data.get("natural_translation", ""),
                core_explanation=data.get("core_explanation", ""),
                key_vocab=data.get("key_vocab", []) or [],
                grammar_point=data.get("grammar_point"),
                cultural_note=data.get("cultural_note"),
                pronunciation_tip=data.get("pronunciation_tip"),
                similar_expressions=data.get("similar_expressions"),
                model=data.get("_model", model),
            ))
            explanations_written += 1
            for v in (data.get("key_vocab") or []):
                w = (v.get("word") or "").strip().lower()
                if w and w.isalpha() and w not in vocab_words:
                    vocab_words[w] = seg["text"]
        db.commit()

    # ---- 3b. 查词卡：只生成 Word 表里还没有的词 ----
    with SessionLocal() as db:
        missing = [w for w in vocab_words if db.get(Word, w) is None]

    async def word_worker(w: str) -> tuple[str, dict] | None:
        async with sem:
            try:
                data = await _word_one(client, model, w, vocab_words[w])
                return w, data
            except (APIError, json.JSONDecodeError) as exc:
                print(f"[{article_id}] word '{w}' 失败：{exc}", file=sys.stderr)
                return None

    word_results = await asyncio.gather(*(word_worker(w) for w in missing))

    words_written = 0
    with SessionLocal() as db:
        for pair in word_results:
            if pair is None:
                continue
            w, data = pair
            db.merge(Word(
                word=w,
                phonetic=data.get("phonetic", ""),
                level=data.get("level", ""),
                definitions=data.get("definitions", []) or [],
                model=data.get("_model", model),
                hit_count=0,
            ))
            words_written += 1
        db.commit()

    return {
        "explanations": explanations_written,
        "vocab_total": len(vocab_words),
        "words_new": words_written,
        "words_cached": len(vocab_words) - len(missing),
    }


# ============================================================
# 步骤 4：写库 contents(kind='article') + ArticleDetails
# ============================================================

def upsert_article(article: dict, cefr: str, segments: list[dict],
                   image_urls: list[str], topics: list[str],
                   status: str = "published",
                   classify_confidence: float | None = None) -> str:
    """upsert 一篇图文到 contents 基表 + article_details 明细表。

    Q2 统一内容模型：
    - contents 行 kind='article'，视频专属字段（duration/play_mode/video_url 等）留 NULL；
    - 图文特有字段（body/paragraphs/image_urls/word_count/reading_time）进 ArticleDetails；
    - topics 写受控题材 id 列表；attribution 写 CC BY-SA 署名。

    status / classify_confidence：题材分类置信度护栏的产出——
    分类置信度低于阈值时上游会传 status='review_pending'，此处如实落库。

    返回 'inserted' / 'updated'。
    """
    article_id = article["article_id"]
    word_count = len(article["body"].split())
    reading_time = max(1, round(word_count / _WORDS_PER_MINUTE * 60))

    # contents 基表共享元数据
    content_payload = {
        "id": article_id,
        "title": article["title"],
        "author": article["author"],
        "source": article["source"],
        "cefr_level": cefr,
        "kind": "article",
        "language": "en",
        "topics": topics,             # 受控题材 id 列表（已过 normalize_topics）
        "attribution": article["attribution"],
        # 视频专属字段对图文无意义，留 NULL
        "duration_seconds": None,
        "play_mode": None,
        "video_url": None,
        "youtube_id": None,
        # thumbnail：有配图用首图，无图给占位（基表 thumbnail_url 非空约束）
        "thumbnail_url": image_urls[0] if image_urls else "",
        "subtitle_url": None,
        "vocabulary_url": None,
        "explanation_url": None,
        # 图文统一归入「外刊」首页分类（iOS DiscoverTab.foreign）
        "categories": ["foreign"],
        "category_color_hex": 0x9B8CFF,
        "is_recommended": False,
        # classify_confidence：图文复用此列承载「题材分类置信度」（与视频侧同列同义）
        "classify_confidence": classify_confidence,
        # status：题材分类置信度护栏的产出——低置信度落 review_pending（不上首页）
        "status": status,
    }

    # article_details 明细
    details_payload = {
        "content_id": article_id,
        "body": article["body"],
        "paragraphs": segments,
        "image_urls": image_urls,
        "word_count": word_count,
        "reading_time_seconds": reading_time,
    }

    with SessionLocal() as db:
        existing = db.get(Content, article_id)
        if existing:
            for k, v in content_payload.items():
                setattr(existing, k, v)
            action = "updated"
        else:
            db.add(Content(**content_payload))
            action = "inserted"

        details = db.get(ArticleDetails, article_id)
        if details:
            for k, v in details_payload.items():
                setattr(details, k, v)
        else:
            db.add(ArticleDetails(**details_payload))
        db.commit()

    return action


# ============================================================
# 步骤 3.5：segment 逐句翻译（中英对照）
# ============================================================

async def translate_article_segments(article_id: str, segments: list[dict]) -> int:
    """对切段后的句子逐句翻译，原地填充每个 segment 的 translation 字段。

    复用视频字幕的 translate_segments()——图文 segment 与视频字幕 segment 同 schema
    （都含 id / text / translation），可直接共用，不重写翻译逻辑。

    best-effort 降级：翻译整体失败（无 OPENAI_API_KEY / API 错）只打日志，
    segment 保持 translation 留空，不抛错——与字幕流水线一致，不阻塞图文入库。
    （单批失败 translate_segments 内部已各自降级为空串。）

    返回成功填上译文的 segment 数。
    """
    if not segments:
        return 0
    try:
        translated = await translate_segments(segments, "zh-CN")
    except Exception as exc:  # noqa: BLE001
        print(f"[{article_id}] segment 翻译失败，translation 留空降级：{exc}", file=sys.stderr)
        return 0

    # translate_segments 保留原 segment 全部字段（含 paragraph）并新增 translation，
    # 顺序与入参一致，原地回填即可。
    filled = 0
    for seg, tr in zip(segments, translated):
        seg["translation"] = tr.get("translation", "") or ""
        if seg["translation"]:
            filled += 1
    return filled


# ============================================================
# 主流程
# ============================================================

async def ingest_one(article: dict, *, do_annotate: bool) -> dict:
    """单篇文章走完整管线。"""
    article_id = article["article_id"]
    body = article["body"]

    # 步骤 1：CEFR 评级
    cefr, grade_result = grade_cefr(body)

    # 步骤 2：切段
    segments = segment_article(body)

    # 步骤 3：配图转存
    image_urls = mirror_images(article.get("image_urls", []), article_id)

    # 步骤 3.5：受控题材分类 + segment 逐句翻译（均 best-effort，失败不阻塞）
    topics: list[str] = []
    translated_count = 0
    # 题材分类置信度护栏：默认 published；置信度低于阈值落 review_pending。
    status = "published"
    classify_confidence: float | None = None
    classify_reason = ""
    if do_annotate:
        topic_result = await classify_article_topics(article["title"], body)
        topics = topic_result["topics"]
        classify_confidence = topic_result.get("confidence")
        classify_reason = topic_result.get("reason", "")
        # 护栏判定（类比视频侧 ingest.py classify_confidence < 0.7 → review_pending）：
        # - 分类出了 topics 但置信度低 → 弱贴合，落 review_pending 等人工复审；
        # - 分类返回空 topics（无法归类 / API 失败降级）→ 同样落 review_pending，
        #   不让无题材的图文直接上首页。
        if not topics:
            status = "review_pending"
        elif (classify_confidence or 0.0) < _CLASSIFY_CONFIDENCE_THRESHOLD:
            status = "review_pending"
        translated_count = await translate_article_segments(article_id, segments)

    # 步骤 4：写库（segments 此时已带 translation；先入库，再标注）
    action = upsert_article(article, cefr, segments, image_urls, topics,
                            status=status, classify_confidence=classify_confidence)

    # 步骤 5：LLM 标注（可选；--no-annotate 跳过，用于纯采集调试）
    annotate_summary: dict = {}
    if do_annotate:
        annotate_summary = await annotate_article(article_id, article, cefr, segments)

    return {
        "article_id": article_id,
        "title": article["title"][:60],
        "action": action,
        "cefr": cefr,
        "cefr_composite": grade_result.get("composite_score"),
        "segments": len(segments),
        "topics": topics,
        "status": status,
        "classify_confidence": classify_confidence,
        "classify_reason": classify_reason,
        "translated_segments": translated_count,
        "images": len(image_urls),
        "word_count": len(body.split()),
        "annotate": annotate_summary or "skipped",
    }


async def ingest_from_fetch(items: list[dict], *, do_annotate: bool) -> None:
    """逐篇串行处理（图文量小、LLM 标注内部已并发，不需要篇级并发）。"""
    for item in items:
        try:
            result = await ingest_one(item, do_annotate=do_annotate)
            print(json.dumps(result, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({
                "article_id": item.get("article_id", "?"),
                "error": str(exc)[:300],
            }, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="图文采集管线：article fetch JSON → 入库")
    parser.add_argument("--from-fetch", required=True,
                        help="fetch_simple_wikipedia.py 产的 article fetch JSON 路径")
    parser.add_argument("--limit", type=int, help="只处理前 N 篇")
    parser.add_argument("--no-annotate", action="store_true",
                        help="跳过 LLM 标注（只采集 + 评级 + 切段 + 入库）")
    args = parser.parse_args()

    path = Path(args.from_fetch)
    if not path.exists():
        sys.exit(f"fetch 文件不存在：{path}")

    items = json.loads(path.read_text(encoding="utf-8"))
    if args.limit:
        items = items[:args.limit]

    ensure_schema_dev()
    print(f"准备入库 {len(items)} 篇图文", file=sys.stderr)
    asyncio.run(ingest_from_fetch(items, do_annotate=not args.no_annotate))


if __name__ == "__main__":
    main()
