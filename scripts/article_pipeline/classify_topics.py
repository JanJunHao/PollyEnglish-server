"""图文受控题材分类（Q4 图文线 · 阶段 1 收尾）。

给图文打上 app/taxonomy.py 受控词表里的 topic id（如 `science.space`）。

与 app/api/ai.py 的 classify_one() 区别：
- classify_one() 输出的是视频用的「自由形态标签」（daily_news/movie/...），不是受控题材；
- 这里要求 LLM 只能从 taxonomy 受控词表里选 topic id，输出再经 normalize_topics()
  过滤非法 id，保证落库的一定是合法 taxonomy 叶子节点。

复用 app/api/ai.py 同款 OpenAI 调用方式（JSON mode + 低温度）。
分类失败 best-effort 降级——返回空 topics 列表，不阻塞图文入库。
"""

from __future__ import annotations

import json
import logging

from openai import APIError, AsyncOpenAI

from app.config import get_settings
from app.taxonomy import TOPIC_TAXONOMY, normalize_topics, topic_label

log = logging.getLogger("polly.article_classify_topics")


def _build_taxonomy_menu() -> str:
    """把受控题材树渲染成给 LLM 看的清单文本。

    形如：
      science.space — 太空航天
      science.nature — 自然与环境
    只列叶子节点 id，强约束 LLM 只能从这些 id 里选。
    """
    lines: list[str] = []
    for parent, node in TOPIC_TAXONOMY.items():
        for child, label in node["children"].items():
            lines.append(f"- {parent}.{child} — {node['label']}/{label}")
    return "\n".join(lines)


def _classify_system() -> str:
    """构造受控题材分类的 system prompt。"""
    return (
        "你是 Polly 英语精读 App 的图文内容题材编辑。\n"
        "下面是受控题材清单（topic id — 中文含义），你只能从这个清单里选：\n\n"
        f"{_build_taxonomy_menu()}\n\n"
        "规则：\n"
        "- 根据文章标题与正文，选 1-3 个最贴合的 topic id；\n"
        "- 必须原样使用清单里的 topic id（形如 `science.space`），不得自创、不得改写、不得只写父类；\n"
        "- 按贴合度从高到低排列；\n"
        "- 实在无法归类时返回空数组。\n\n"
        '输出严格 JSON：{"topics": ["..."], "confidence": 0.0-1.0, "reason": "一句话"}'
    )


def _client() -> AsyncOpenAI:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY 未配置，无法做题材分类")
    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )


async def classify_article_topics(title: str, body: str) -> dict:
    """对一篇图文做受控题材分类。

    Args:
        title: 文章标题
        body: 文章正文（只取开头一段喂模型，省 token）

    Returns:
        {"topics": [合法 topic id...], "confidence": float, "reason": str}
        - topics 已过 normalize_topics()，保证全是合法 taxonomy 叶子 id；
        - 任意失败（无 key / API 错 / 非法 JSON）都返回 topics=[]，best-effort 降级，
          不抛错——不阻塞图文入库。
    """
    settings = get_settings()
    try:
        client = _client()
    except RuntimeError as exc:
        log.warning("题材分类跳过：%s", exc)
        return {"topics": [], "confidence": 0.0, "reason": str(exc)}

    user_prompt = (
        f"标题：{title}\n"
        f"正文开头：{body[:1500]}"
    )

    try:
        resp = await client.chat.completions.create(
            model=settings.openai_classify_model,
            messages=[
                {"role": "system", "content": _classify_system()},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except (APIError, json.JSONDecodeError) as exc:
        log.warning("题材分类失败，topics 留空降级：%s", exc)
        return {"topics": [], "confidence": 0.0, "reason": f"classify failed: {exc}"}

    raw_topics = data.get("topics", []) or []
    # 关键：normalize_topics() 过滤掉 LLM 可能产出的非法 / 自创 id，只留受控词表里的。
    topics = normalize_topics([str(t).strip() for t in raw_topics])
    dropped = [t for t in raw_topics if t not in topics]
    if dropped:
        log.info("题材分类丢弃非法 id：%s", dropped)

    return {
        "topics": topics,
        "confidence": float(data.get("confidence", 0.0) or 0.0),
        "reason": data.get("reason", ""),
        "labels": [topic_label(t) for t in topics],
    }
