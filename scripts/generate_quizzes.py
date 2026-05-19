"""派生测验题生成管线（Q5 数据复用「① 派生新内容」，阶段 3）。

按内容遍历其 `explanations` 行，基于已标注的 key_vocab / grammar_point / sentence，
用 LLM 派生测验题写入 `quizzes` 表。内容处理完即「独立且固定」，题目一次性预生成 +
永久缓存。

题型覆盖：
- vocab_choice  词义选择（给词选中文释义）
- grammar_choice 语法选择（针对句中语法点）
- cloze         完形填空（句中挖空选词）

用法：
  python -m scripts.generate_quizzes --content simplewiki-photosynthesis
  python -m scripts.generate_quizzes --all
  python -m scripts.generate_quizzes --content X --force        # 重新生成（先删旧题）
  python -m scripts.generate_quizzes --content X --per-sentence 2 --limit 10

幂等：默认跳过已有题目的内容；--force 时先删该内容旧题再写。

要求：
- .env 里 OPENAI_API_KEY 有效
- explanations 表已有对应内容的逐句讲解
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from openai import APIError, AsyncOpenAI

from app.config import get_settings
from app.db import SessionLocal
from app.models import Content, Explanation, Quiz

# 出题 system prompt：复用 app/api/ai.py 的严格 JSON 风格。
QUIZ_SYSTEM = """你是 Polly 英语精读 App 的出题老师，面向中文母语的英语学习者。
基于给定的英文句子及其已标注的词汇 / 语法点，出一组测验题。

题型（每种尽量出，素材不足可少出）：
- vocab_choice：词义选择。题干给一个词及其所在句子，4 个中文释义选项选正确的。
- grammar_choice：语法选择。针对句中的语法点出概念辨析题，4 个选项。
- cloze：完形填空。把句中的一个关键词挖空成 ____，4 个英文单词选项选填入空格的正确词。

必须做到：
- 准确，绝不杜撰；干扰项要合理（似是而非，不要明显荒谬）
- 每题 4 个选项，正确答案随机分布在不同下标，不要总是第 0 个
- rationale 用中文简明解释为什么对
- 输出严格 JSON，不要 markdown 代码块包裹

输出 JSON 结构：
{
  "quizzes": [
    {
      "kind": "vocab_choice|grammar_choice|cloze",
      "question": "题干（cloze 题干须含 ____）",
      "options": ["选项1", "选项2", "选项3", "选项4"],
      "answer_index": 0,
      "rationale": "中文解析"
    }
  ]
}"""


def _build_user_prompt(exp: Explanation) -> str:
    """把一条 explanation 的结构化标注拼成出题 prompt。"""
    parts = [f"英文句子：{exp.sentence}"]
    if exp.natural_translation:
        parts.append(f"参考翻译：{exp.natural_translation}")
    vocab = exp.key_vocab or []
    if vocab:
        vlines = [
            f"- {v.get('word', '')}：{v.get('meaning', '')}"
            for v in vocab
            if v.get("word")
        ]
        if vlines:
            parts.append("已标注词汇：\n" + "\n".join(vlines))
    if exp.grammar_point:
        parts.append(f"语法点：{exp.grammar_point}")
    return "\n".join(parts)


async def _quiz_one(client: AsyncOpenAI, model: str, prompt: str, per_sentence: int) -> dict:
    """对单句调一次 LLM，返回解析后的 JSON。"""
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": QUIZ_SYSTEM},
            {
                "role": "user",
                "content": f"{prompt}\n\n请出 {per_sentence} 道题，题型尽量分散。",
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.4,
    )
    raw = resp.choices[0].message.content or "{}"
    data = json.loads(raw)
    data["_model"] = resp.model
    return data


_VALID_KINDS = {"vocab_choice", "grammar_choice", "cloze", "comprehension"}


def _sanitize(item: dict) -> dict | None:
    """校验单题结构；不合规返回 None 跳过。"""
    kind = str(item.get("kind", "")).strip()
    question = str(item.get("question", "")).strip()
    options = item.get("options") or []
    answer_index = item.get("answer_index")
    rationale = str(item.get("rationale", "")).strip()
    if kind not in _VALID_KINDS:
        return None
    if not question or not rationale:
        return None
    if not isinstance(options, list) or len(options) < 2:
        return None
    options = [str(o).strip() for o in options]
    if not isinstance(answer_index, int) or not (0 <= answer_index < len(options)):
        return None
    return {
        "kind": kind,
        "question": question[:2048],
        "options": options,
        "answer_index": answer_index,
        "rationale": rationale[:2048],
    }


async def process_content(
    content_id: str, *, force: bool, limit: int | None, per_sentence: int, concurrency: int
) -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        sys.exit("OPENAI_API_KEY not configured")

    with SessionLocal() as db:
        content = db.get(Content, content_id)
        if content is None:
            print(f"[{content_id}] 跳过：contents 表无此内容")
            return
        exps = (
            db.query(Explanation)
            .filter(Explanation.video_id == content_id)
            .order_by(Explanation.segment_id)
            .all()
        )
        if not exps:
            print(f"[{content_id}] 跳过：无 explanations")
            return

        existing = db.query(Quiz).filter(Quiz.content_id == content_id).count()
        if existing and not force:
            print(f"[{content_id}] 跳过：已有 {existing} 道题（--force 可重建）")
            return
        # 把 ORM 对象转成普通 dict，离开会话后仍可用
        exp_data = [
            {
                "segment_id": e.segment_id,
                "sentence": e.sentence,
                "natural_translation": e.natural_translation,
                "key_vocab": e.key_vocab,
                "grammar_point": e.grammar_point,
            }
            for e in exps
        ]

    # 优先挑选有标注（key_vocab 或 grammar_point）的句子——素材足才出得好题
    candidates = [
        e for e in exp_data if (e["key_vocab"] or e["grammar_point"])
    ]
    if not candidates:
        candidates = exp_data
    if limit:
        candidates = candidates[:limit]

    print(
        f"[{content_id}] explanations={len(exp_data)} 可出题句={len(candidates)} "
        f"每句 {per_sentence} 题"
    )

    client = AsyncOpenAI(
        api_key=settings.openai_api_key, base_url=settings.openai_base_url or None
    )
    sem = asyncio.Semaphore(concurrency)

    async def worker(e: dict):
        async with sem:
            # 复用 Explanation 的属性壳调 _build_user_prompt
            shim = Explanation(
                video_id=content_id,
                segment_id=e["segment_id"],
                sentence=e["sentence"],
                natural_translation=e["natural_translation"] or "",
                core_explanation="",
                key_vocab=e["key_vocab"],
                grammar_point=e["grammar_point"],
                model="",
            )
            prompt = _build_user_prompt(shim)
            try:
                data = await _quiz_one(
                    client, settings.openai_explain_model, prompt, per_sentence
                )
                return e["segment_id"], data
            except APIError as exc:
                print(f"[{content_id}] seg {e['segment_id']} FAILED: {exc}")
                return None
            except json.JSONDecodeError as exc:
                print(f"[{content_id}] seg {e['segment_id']} bad JSON: {exc}")
                return None

    results = await asyncio.gather(*[worker(e) for e in candidates])

    # 入库：幂等——force 时先删旧题
    with SessionLocal() as db:
        if force:
            deleted = db.query(Quiz).filter(Quiz.content_id == content_id).delete()
            if deleted:
                print(f"[{content_id}] 已删除旧题 {deleted} 道")
        wrote = 0
        for pair in results:
            if pair is None:
                continue
            segment_id, data = pair
            model = data.get("_model", settings.openai_explain_model)
            for raw_item in data.get("quizzes", []) or []:
                clean = _sanitize(raw_item)
                if clean is None:
                    continue
                db.add(
                    Quiz(
                        content_id=content_id,
                        segment_id=segment_id,
                        kind=clean["kind"],
                        question=clean["question"],
                        options=clean["options"],
                        answer_index=clean["answer_index"],
                        rationale=clean["rationale"],
                        source_model=model,
                    )
                )
                wrote += 1
        db.commit()
        print(f"[{content_id}] 写入 {wrote} 道测验题")


def main() -> None:
    parser = argparse.ArgumentParser(description="派生测验题生成管线（Q5 阶段 3）")
    parser.add_argument("--content", help="单个内容 id")
    parser.add_argument("--all", action="store_true", help="处理所有有 explanations 的内容")
    parser.add_argument("--force", action="store_true", help="重建：先删旧题再写")
    parser.add_argument("--limit", type=int, help="每内容只取前 N 句出题（调试用）")
    parser.add_argument("--per-sentence", type=int, default=2, help="每句生成几题")
    parser.add_argument("--concurrency", type=int, default=5, help="并发 OpenAI 调用数")
    args = parser.parse_args()

    if not args.content and not args.all:
        parser.error("--content 或 --all 二选一")

    if args.all:
        with SessionLocal() as db:
            ids = [
                r[0]
                for r in db.query(Explanation.video_id).distinct().all()
            ]
    else:
        ids = [args.content]

    for cid in ids:
        try:
            asyncio.run(
                process_content(
                    cid,
                    force=args.force,
                    limit=args.limit,
                    per_sentence=args.per_sentence,
                    concurrency=args.concurrency,
                )
            )
        except SystemExit as e:
            print(f"[{cid}] skipped: {e}")


if __name__ == "__main__":
    main()
