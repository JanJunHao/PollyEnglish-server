"""派生测验题对外读取端点（Q5 数据复用「① 派生新内容」，阶段 3）。

测验题由 scripts/generate_quizzes.py 一次性预生成入 `quizzes` 表，本端点只读。
鉴权 / 限速与 /v1/contents 一致（require_api_key + rate_limit），只新增不改契约。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Content, Quiz
from app.schemas import ContentQuizzesOut, QuizOut
from app.security import rate_limit, require_api_key

router = APIRouter(
    prefix="/v1/contents",
    tags=["quizzes"],
    dependencies=[Depends(require_api_key), Depends(rate_limit("rate_limit_contents"))],
)


@router.get("/{content_id}/quizzes", response_model=ContentQuizzesOut)
def content_quizzes(
    content_id: str,
    kind: str | None = Query(default=None, description="按题型过滤：vocab_choice/grammar_choice/cloze"),
    db: Session = Depends(get_db),
) -> ContentQuizzesOut:
    """返回某内容的派生测验题。内容不存在 404；无题返回空列表。"""
    content = db.get(Content, content_id)
    if content is None:
        raise HTTPException(status_code=404, detail="content not found")

    stmt = select(Quiz).where(Quiz.content_id == content_id)
    if kind is not None:
        stmt = stmt.where(Quiz.kind == kind)
    stmt = stmt.order_by(Quiz.segment_id, Quiz.id)

    rows = db.execute(stmt).scalars().all()
    return ContentQuizzesOut(
        content_id=content_id,
        count=len(rows),
        quizzes=[QuizOut.model_validate(r) for r in rows],
    )
