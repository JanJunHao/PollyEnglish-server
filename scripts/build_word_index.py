"""词 → 内容反向索引构建器（Q5 数据复用「③ 内容自我增强」，阶段 3）。

遍历内容的句子，把每个词（小写 + 简单 lemma 归一）记一条 `word_occurrences`，
让查词卡能给出某个词的多个真实语境例句。

句子来源（最简单可靠）：
- `explanations.sentence`——视频字幕句 / 图文段落句，已是切好的句子。
- 图文若无 explanations，回退用 `article_details.paragraphs`。

分词参考 scripts/cc_pipeline/cefr_grader.py 的 `_tokenize`（正则小写分词），
额外做轻量 lemma 归一（复数 / 时态后缀）。纯派生表，可随时按全部内容重建。

用法：
  python -m scripts.build_word_index --content simplewiki-photosynthesis
  python -m scripts.build_word_index --all

幂等：每内容重建前先清该内容旧索引。
"""

from __future__ import annotations

import argparse
import re

from app.db import SessionLocal
from app.models import ArticleDetails, Content, Explanation, WordOccurrence

# 常见功能词，不值得做反向索引（查词卡不会查它们）。
_STOPWORDS = {
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it",
    "for", "not", "on", "with", "he", "as", "you", "do", "at", "this", "but",
    "his", "by", "from", "they", "we", "say", "her", "she", "or", "an",
    "will", "my", "one", "all", "would", "there", "their", "what", "so",
    "up", "out", "if", "about", "who", "which", "go", "me", "when", "can",
    "no", "just", "him", "is", "are", "was", "were", "been", "am", "its",
    "had", "has", "did", "does", "s", "t", "ll", "re", "ve", "d", "m",
    "your", "our", "us", "them", "then", "than", "too", "very", "also",
}


def _tokenize(text: str) -> list[str]:
    """小写分词。与 cefr_grader._tokenize 同款正则。"""
    return re.findall(r"[a-zA-Z']+", text.lower())


def _lemmatize(word: str) -> str:
    """轻量 lemma 归一：去常见复数 / 时态后缀。
    不引入 spaCy/nltk，保持脚本零重依赖；够查词卡聚合用。
    """
    w = word.strip("'")
    if len(w) <= 3:
        return w
    # 进行时 -ing
    if w.endswith("ing") and len(w) > 5:
        base = w[:-3]
        # running -> run（去重叠尾辅音）
        if len(base) > 2 and base[-1] == base[-2]:
            return base[:-1]
        return base + "e" if base.endswith(("at", "iz", "us")) else base
    # 过去式 -ed
    if w.endswith("ed") and len(w) > 4:
        base = w[:-2]
        if len(base) > 2 and base[-1] == base[-2]:
            return base[:-1]
        return base
    # 复数 / 三单 -es / -s
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("es") and len(w) > 4 and w[-3] in "sxzo":
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _sentences_for(db, content: Content) -> list[tuple[int, str]]:
    """取一条内容的句子列表 [(segment_id, sentence)]。
    优先 explanations.sentence；图文无讲解时回退 article_details.paragraphs。
    """
    exps = (
        db.query(Explanation)
        .filter(Explanation.video_id == content.id)
        .order_by(Explanation.segment_id)
        .all()
    )
    if exps:
        return [(e.segment_id, e.sentence) for e in exps]

    if content.kind == "article":
        details = db.get(ArticleDetails, content.id)
        if details and details.paragraphs:
            out = []
            for seg in details.paragraphs:
                sid = seg.get("id")
                txt = seg.get("text", "")
                if sid is not None and txt:
                    out.append((int(sid), txt))
            return out
    return []


def process_content(content_id: str) -> int:
    """重建单条内容的反向索引，返回写入条数。"""
    with SessionLocal() as db:
        content = db.get(Content, content_id)
        if content is None:
            print(f"[{content_id}] 跳过：contents 表无此内容")
            return 0

        sentences = _sentences_for(db, content)
        if not sentences:
            print(f"[{content_id}] 跳过：无可索引句子")
            return 0

        # 幂等：先清旧索引
        deleted = (
            db.query(WordOccurrence)
            .filter(WordOccurrence.content_id == content_id)
            .delete()
        )
        if deleted:
            print(f"[{content_id}] 清除旧索引 {deleted} 条")

        wrote = 0
        for segment_id, sentence in sentences:
            # 同一句里同一 lemma 只记一次，避免重复例句
            seen: set[str] = set()
            for tok in _tokenize(sentence):
                if "'" in tok:
                    tok = tok.replace("'", "")
                if not tok or tok in _STOPWORDS:
                    continue
                lemma = _lemmatize(tok)
                if not lemma or lemma in _STOPWORDS or len(lemma) < 2:
                    continue
                if lemma in seen:
                    continue
                seen.add(lemma)
                db.add(
                    WordOccurrence(
                        word=lemma,
                        content_id=content_id,
                        segment_id=segment_id,
                        sentence=sentence[:2048],
                    )
                )
                wrote += 1
        db.commit()
        print(f"[{content_id}] 句子 {len(sentences)} 句，写入 {wrote} 条词索引")
        return wrote


def main() -> None:
    parser = argparse.ArgumentParser(description="词→内容反向索引构建器（Q5 阶段 3）")
    parser.add_argument("--content", help="单个内容 id")
    parser.add_argument("--all", action="store_true", help="处理所有内容")
    args = parser.parse_args()

    if not args.content and not args.all:
        parser.error("--content 或 --all 二选一")

    if args.all:
        with SessionLocal() as db:
            ids = [c.id for c in db.query(Content).all()]
    else:
        ids = [args.content]

    total = 0
    for cid in ids:
        total += process_content(cid)
    print(f"完成：共写入 {total} 条 word_occurrences")


if __name__ == "__main__":
    main()
