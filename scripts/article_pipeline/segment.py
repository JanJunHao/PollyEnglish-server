"""文章切段。

把采集到的正文纯文本切成「段落 / 句子」结构，对应视频字幕的 segments，
供精读交互（点词查卡、长按句子出讲解）复用。

切段策略：
- 一篇文章先按空行切成自然段落；
- 每个自然段落再按句子边界切成一个个句子；
- 每个句子是一个 segment，结构与 ArticleDetails.paragraphs 约定一致：
    {"id": 0, "text": "...", "translation": ""}
  （translation 留空，由翻译流程或前端按需补；与视频 segment 对齐）

为什么以「句子」为 segment 粒度而非「段落」：
精读交互（长按出讲解、点词查义）以句子为单位，视频字幕也是逐句的，
保持一致才能让 Word / Explanation 精读层形态无关地复用。
"""

from __future__ import annotations

import re

# 句子边界：句末标点 + 空白。用前瞻避免吞掉标点。
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")

# 常见缩写白名单：这些词后面的「. + 空格 + 大写」不是句子边界，不能切。
# 全部小写存储，匹配时大小写不敏感。
_ABBREVIATIONS = {
    # 称谓
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "rev", "gen",
    "hon", "messrs", "fr", "pres", "gov", "sen", "rep", "capt", "lt", "col",
    "sgt", "supt", "det",
    # 拉丁缩写 / 通用
    "etc", "vs", "viz", "al", "ca", "cf", "approx", "dept", "est", "fig",
    "no", "vol", "p", "pp", "ed", "eds", "trans", "min", "max",
    # 时间 / 度量
    "a.m", "p.m", "am", "pm", "inc", "ltd", "co", "corp",
    # 月份缩写
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
}

# 单个大写字母后跟点（人名缩写如 "George W. Bush" 的 "W."）也不该切。
_SINGLE_INITIAL = re.compile(r"\b[A-Z]$")


def _is_abbreviation(text_before_dot: str) -> bool:
    """判断一个「以点结尾的位置」之前的最后一个 token 是否为缩写。

    text_before_dot 是从段首到该点（不含点）的文本。取末尾连续字母 token，
    若命中缩写白名单或为单个大写首字母，则该点不是句子边界。
    """
    # 取末尾的字母 token（允许内部含点，如 "a.m"）
    m = re.search(r"([A-Za-z][A-Za-z.]*)$", text_before_dot)
    if not m:
        return False
    token = m.group(1).rstrip(".").lower()
    if token in _ABBREVIATIONS:
        return True
    # 单个大写字母首字母缩写（人名 middle initial）
    last_char = text_before_dot[-1:]
    if last_char.isupper() and (len(text_before_dot) < 2 or not text_before_dot[-2].isalpha()):
        return True
    return False


def split_sentences(paragraph: str) -> list[str]:
    """把一个自然段落切成句子列表。

    在正则候选边界基础上，逐个校验：若边界前的 token 是已知缩写
    （Mr. / Dr. / etc. / 月份 / 人名首字母等），则不在此处切句，
    把前后两半重新拼回，避免误切。
    """
    text = paragraph.strip()
    if not text:
        return []

    # 收集所有候选边界位置（match 的起始偏移，即句末标点之后、空白之前）。
    raw_parts: list[str] = []
    last = 0
    for m in _SENTENCE_BOUNDARY.finditer(text):
        boundary = m.start()  # 标点之后的位置
        before = text[:boundary]
        # 候选边界前一个字符是 '.' 时才需查缩写（'!' '?' 后基本是真边界）
        if before.endswith("."):
            if _is_abbreviation(before[:-1]):
                continue  # 缩写造成的假边界，跳过不切
        raw_parts.append(text[last:boundary].strip())
        last = m.end()
    raw_parts.append(text[last:].strip())

    return [p for p in raw_parts if p]


def segment_article(body: str) -> list[dict]:
    """把文章正文切成句子级 segment 列表。

    Args:
        body: 清洗后的正文纯文本，自然段之间用空行分隔。

    Returns:
        [{"id": 0, "text": "...", "translation": "", "paragraph": 0}, ...]
        - id：全局句序号，作为 Explanation 的 segment_id
        - paragraph：句子所属的自然段序号，前端按段渲染时用
    """
    segments: list[dict] = []
    seg_id = 0
    for para_idx, block in enumerate(body.split("\n\n")):
        block = block.strip()
        if not block:
            continue
        for sentence in split_sentences(block):
            segments.append({
                "id": seg_id,
                "text": sentence,
                "translation": "",  # 留空，与视频 segment 对齐；翻译按需补
                "paragraph": para_idx,
            })
            seg_id += 1
    return segments
