"""词语真实语境例句端点（Q5 数据复用「③ 内容自我增强」，阶段 3）。

词→内容反向索引由 scripts/build_word_index.py 预构建入 `word_occurrences` 表，
本端点只读，让查词卡能给出某个词的多个真实语境例句。
鉴权 / 限速与 /v1/contents 一致，只新增不改契约。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import WordOccurrence
from app.schemas import WordExample, WordExamplesOut
from app.security import rate_limit, require_api_key

router = APIRouter(
    prefix="/v1/words",
    tags=["words"],
    dependencies=[Depends(require_api_key), Depends(rate_limit("rate_limit_contents"))],
)


@router.get("/{word}/examples", response_model=WordExamplesOut)
def word_examples(
    word: str,
    limit: int = Query(default=10, ge=1, le=50, description="最多返回几条例句"),
    db: Session = Depends(get_db),
) -> WordExamplesOut:
    """按词返回真实语境例句。word 自动小写归一；无命中返回空列表（200）。

    注意：传入的词会按 build_word_index.py 同款方式小写处理后匹配，
    索引存的是 lemma，建议查词卡传词原形或 lemma。
    """
    key = word.strip().lower()
    stmt = (
        select(WordOccurrence)
        .where(WordOccurrence.word == key)
        .order_by(WordOccurrence.content_id, WordOccurrence.segment_id)
        .limit(limit)
    )
    rows = db.execute(stmt).scalars().all()
    return WordExamplesOut(
        word=key,
        count=len(rows),
        examples=[WordExample.model_validate(r) for r in rows],
    )
