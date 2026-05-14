import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from openai import APIError, AsyncOpenAI
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Explanation, Word
from app.quota import consume
from app.schemas import ClassifyIn, ClassifyOut, ExplainIn, ExplainOut, WordIn, WordOut
from app.security import rate_limit, require_api_key

router = APIRouter(
    prefix="/v1/ai",
    tags=["ai"],
    dependencies=[Depends(require_api_key), Depends(rate_limit("rate_limit_ai"))],
)
log = logging.getLogger("polly.ai")


def _client() -> AsyncOpenAI:
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured")
    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )


EXPLAIN_SYSTEM = """你是一位幽默、博学的英语老师，正在帮助一位中文母语的英语学习者理解一句来自影视剧或演讲的台词。

讲解风格：
- 像朋友在旁边低声讲解，不像课本
- 优先讲地道意思和使用场景，不优先讲语法
- 文化背景比语法规则更重要
- 用具体例子代替抽象规则
- 简洁，不啰嗦

必须做到：
- 准确，绝不杜撰
- 不确定时直说"这里来源不太确定"
- 输出严格 JSON，不要带 markdown 代码块包裹

绝对不要：
- 输出免责声明、寒暄
- 解释作为 AI 如何如何
- 把简单句子复杂化

输出 JSON 字段：
{
  "natural_translation": "地道翻译",
  "core_explanation": "1-2 句话讲透真正在表达什么",
  "key_vocab": [{"word": "...", "meaning": "...", "register": "正式|口语|俚语|null", "examples": ["英文例句"]}],
  "grammar_point": "值得讲的语法点（简单句填 null）",
  "cultural_note": "文化梗（无可填 null）",
  "pronunciation_tip": "连读重音（无可填 null）",
  "similar_expressions": ["类似表达 1", "类似表达 2"]
}

总输出不超过 500 字符。"""


CLASSIFY_SYSTEM = """你是 Polly 内容分类编辑。从分类清单中选 1-3 个最贴合的标签：
- daily_news（每日快讯/新闻）
- movie（电影/电视剧）
- discovery（探索/科普/纪录片）
- ted（TED / TED-Ed 演讲）
- street_interview（街头采访/真人对话）
- highlights（精彩片段/亮点剪辑）

输出严格 JSON：
{"categories": ["..."], "confidence": 0.0-1.0, "reason": "一句话"}

低置信度（< 0.7）的情况：跨多领域、字幕噪声大、来源不在已知列表。"""


@router.post("/explain", response_model=ExplainOut, dependencies=[Depends(consume("ai_explain"))])
async def explain(body: ExplainIn, db: Session = Depends(get_db)) -> ExplainOut:
    settings = get_settings()

    # 先查预生成缓存 (video_id, segment_id)。命中走 <50ms 返回，不烧 token。
    if body.video_id is not None and body.segment_id is not None:
        cached = db.get(Explanation, (body.video_id, body.segment_id))
        if cached is not None:
            return ExplainOut(
                sentence=cached.sentence,
                natural_translation=cached.natural_translation,
                core_explanation=cached.core_explanation,
                key_vocab=cached.key_vocab or [],
                grammar_point=cached.grammar_point,
                cultural_note=cached.cultural_note,
                pronunciation_tip=cached.pronunciation_tip,
                similar_expressions=cached.similar_expressions,
                model=cached.model,
                cached=True,
            )

    client = _client()

    parts = [
        f"原句：{body.sentence}",
    ]
    if body.video_title or body.video_author or body.video_source:
        meta = f"视频：{body.video_title or '?'}（{body.video_author or '?'}，{body.video_source or '?'}）"
        parts.append(meta)
    if body.cefr_level:
        parts.append(f"用户水平：{body.cefr_level}")
    if body.context_before:
        parts.append(f"上文：{body.context_before}")
    if body.context_after:
        parts.append(f"下文：{body.context_after}")
    user_prompt = "\n".join(parts)

    try:
        resp = await client.chat.completions.create(
            model=settings.openai_explain_model,
            messages=[
                {"role": "system", "content": EXPLAIN_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
    except APIError as exc:
        log.exception("openai explain failed")
        raise HTTPException(status_code=502, detail=f"openai: {exc}") from exc

    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="openai returned non-JSON") from exc

    out = ExplainOut(
        sentence=body.sentence,
        natural_translation=data.get("natural_translation", ""),
        core_explanation=data.get("core_explanation", ""),
        key_vocab=data.get("key_vocab", []) or [],
        grammar_point=data.get("grammar_point"),
        cultural_note=data.get("cultural_note"),
        pronunciation_tip=data.get("pronunciation_tip"),
        similar_expressions=data.get("similar_expressions"),
        model=resp.model,
        cached=False,
    )

    # 实时调用产生的结果也回写预生成缓存，下次同 (video_id, segment_id) 直接命中。
    if body.video_id is not None and body.segment_id is not None:
        try:
            row = Explanation(
                video_id=body.video_id,
                segment_id=body.segment_id,
                sentence=out.sentence,
                natural_translation=out.natural_translation,
                core_explanation=out.core_explanation,
                key_vocab=[v.model_dump() for v in out.key_vocab],
                grammar_point=out.grammar_point,
                cultural_note=out.cultural_note,
                pronunciation_tip=out.pronunciation_tip,
                similar_expressions=out.similar_expressions,
                model=out.model,
            )
            db.merge(row)
            db.commit()
        except Exception as exc:  # 缓存失败不影响主流程
            log.warning("explain cache write failed: %s", exc)
            db.rollback()

    return out


WORD_SYSTEM = """你是英语词典编辑。返回严格 JSON 格式的中文释义。
- 只输出 JSON，绝不输出 markdown 包裹或额外解释
- 释义简洁准确，1-3 条
- level 字段使用 CEFR 等级
- phonetic 使用 IPA 国际音标，斜杠包裹

输出结构：
{
  "phonetic": "/IPA/",
  "level": "A1|A2|B1|B2|C1|C2",
  "definitions": [{"pos": "n.|v.|adj.|...", "meaning": "中文释义"}]
}"""


@router.post("/word", response_model=WordOut, dependencies=[Depends(consume("ai_word"))])
async def word(body: WordIn, db: Session = Depends(get_db)) -> WordOut:
    settings = get_settings()
    key = body.word.strip().lower()

    # 先查缓存
    cached = db.get(Word, key)
    if cached is not None:
        cached.hit_count += 1
        db.commit()
        return WordOut(
            word=body.word,
            phonetic=cached.phonetic,
            level=cached.level,
            definitions=cached.definitions or [],
            model=cached.model,
            cached=True,
        )

    client = _client()
    user_prompt = f"查询单词：{body.word}\n上下文：{body.context or '（无）'}"

    try:
        resp = await client.chat.completions.create(
            model=settings.openai_explain_model,
            messages=[
                {"role": "system", "content": WORD_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=300,
        )
    except APIError as exc:
        log.exception("openai word failed")
        raise HTTPException(status_code=502, detail=f"openai: {exc}") from exc

    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="openai returned non-JSON") from exc

    out = WordOut(
        word=body.word,
        phonetic=data.get("phonetic", ""),
        level=data.get("level", ""),
        definitions=data.get("definitions", []) or [],
        model=resp.model,
        cached=False,
    )

    # 回写缓存
    try:
        db.merge(Word(
            word=key,
            phonetic=out.phonetic,
            level=out.level,
            definitions=[d.model_dump() for d in out.definitions],
            model=out.model,
            hit_count=0,
        ))
        db.commit()
    except Exception as exc:
        log.warning("word cache write failed: %s", exc)
        db.rollback()

    return out


async def classify_one(
    title: str,
    author: str | None = None,
    source: str | None = None,
    subtitle_excerpt: str = "",
) -> ClassifyOut:
    """gpt-4o-mini 自动打标分类，纯函数版本。
    ingest pipeline 直接 import 调用，跳过 HTTP / 鉴权 / 配额。
    """
    settings = get_settings()
    client = _client()

    user_prompt = (
        f"标题：{title}\n"
        f"作者：{author or '未知'}\n"
        f"来源：{source or '未知'}\n"
        f"字幕开头：{subtitle_excerpt[:1000]}"
    )

    try:
        resp = await client.chat.completions.create(
            model=settings.openai_classify_model,
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
    except APIError as exc:
        log.exception("openai classify failed")
        raise HTTPException(status_code=502, detail=f"openai: {exc}") from exc

    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="openai returned non-JSON") from exc

    return ClassifyOut(
        categories=data.get("categories", []) or [],
        confidence=float(data.get("confidence", 0.0)),
        reason=data.get("reason", ""),
        model=resp.model,
    )


@router.post("/classify", response_model=ClassifyOut)
async def classify(body: ClassifyIn) -> ClassifyOut:
    """HTTP 薄壳：真实逻辑在 classify_one()。"""
    return await classify_one(
        title=body.title,
        author=body.author,
        source=body.source,
        subtitle_excerpt=body.subtitle_excerpt,
    )
