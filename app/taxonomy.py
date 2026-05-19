"""受控题材分类树（taxonomy）。

Q2 统一内容模型——内容识别采用「两轴」：
  1. modality（内容形态）：video / article / audio，由 `Content.kind` 承载，入库时确定。
  2. topic（题材）：受控的分层标签，由 `Content.topics` 承载（topic id 列表）。

为什么用受控词表而不是自由 tag：自由字符串（旧 `Content.categories`）拼写不一、
无法做稳定的分面（faceted）筛选。这里固定一棵两层树，AI 分类只能往这棵树里落。

topic id 约定：`<父类>.<子类>`，如 `science.space`。只存叶子节点 id。
旧 `categories` 字段保留不动，由后续分类流水线逐步映射到 `topics`。
"""

from __future__ import annotations

# 两层题材树：父类 -> {label, children: {子类 key -> 中文 label}}
TOPIC_TAXONOMY: dict[str, dict] = {
    "news": {
        "label": "新闻时事",
        "children": {
            "world": "国际",
            "society": "社会民生",
            "business": "商业财经",
            "politics": "政治",
        },
    },
    "science": {
        "label": "科学",
        "children": {
            "space": "太空航天",
            "nature": "自然与环境",
            "technology": "科技",
            "health": "健康医学",
            "psychology": "心理",
        },
    },
    "culture": {
        "label": "人文",
        "children": {
            "history": "历史",
            "art": "艺术",
            "film": "影视",
            "literature": "文学",
            "language": "语言",
        },
    },
    "daily_life": {
        "label": "日常生活",
        "children": {
            "travel": "旅行",
            "food": "美食",
            "work": "职场",
            "relationships": "人际关系",
            "hobbies": "兴趣爱好",
        },
    },
    "education": {
        "label": "教育学习",
        "children": {
            "academic_lecture": "学术讲座",
            "how_to": "技能教学",
            "personal_growth": "个人成长",
        },
    },
}


def all_topic_ids() -> set[str]:
    """所有合法叶子 topic id 的集合，形如 `science.space`。"""
    return {
        f"{parent}.{child}"
        for parent, node in TOPIC_TAXONOMY.items()
        for child in node["children"]
    }


def is_valid_topic(topic_id: str) -> bool:
    """topic id 是否在受控词表内。"""
    return topic_id in all_topic_ids()


def topic_label(topic_id: str) -> str | None:
    """返回 topic id 的中文 label，如 `science.space` -> `太空航天`；非法 id 返回 None。"""
    parent, _, child = topic_id.partition(".")
    node = TOPIC_TAXONOMY.get(parent)
    if node is None:
        return None
    return node["children"].get(child)


def normalize_topics(topics: list[str]) -> list[str]:
    """过滤掉不在受控词表里的 topic id，去重并保持顺序。入库前调用。"""
    seen: set[str] = set()
    out: list[str] = []
    for t in topics:
        if t in seen:
            continue
        if is_valid_topic(t):
            seen.add(t)
            out.append(t)
    return out
