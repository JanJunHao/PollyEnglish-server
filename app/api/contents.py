import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Content, ContentFeedback
from app.schemas import ContentOut, ContentsLatestOut, FeedbackIn, FeedbackOut
from app.security import rate_limit, require_api_key

# 反馈累积到这个数量自动 review_pending（plan「AI 自动化护栏」）
FEEDBACK_THRESHOLD = 3

router = APIRouter(
    prefix="/v1/contents",
    tags=["contents"],
    dependencies=[Depends(require_api_key), Depends(rate_limit("rate_limit_contents"))],
)


def _client_fp(request: Request) -> str:
    """简易客户端指纹：IP + User-Agent 拼接 hash。
    不是真用户标识——只是合并同一来源短期内重复反馈。
    """
    xff = request.headers.get("x-forwarded-for")
    ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")
    ua = request.headers.get("user-agent", "")
    return hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:32]


@router.get("/latest", response_model=ContentsLatestOut)
def latest(
    since: datetime | None = Query(default=None, description="ISO 8601；只返回此时间之后更新的"),
    db: Session = Depends(get_db),
) -> ContentsLatestOut:
    # 硬性 gate：subtitle_url 缺失的视频永远不进首页。
    # Polly 的产品是「精读」，没字幕等于没产品价值；流水线层面也对应
    # scripts/ingest.py 的 status='review_pending' 降级逻辑。
    stmt = select(Content).where(
        Content.status == "published",
        Content.subtitle_url.isnot(None),
        Content.subtitle_url != "",
    )
    if since is not None:
        stmt = stmt.where(Content.updated_at > since)
    stmt = stmt.order_by(Content.is_recommended.desc(), Content.updated_at.desc())

    rows = db.execute(stmt).scalars().all()
    return ContentsLatestOut(
        server_time=datetime.now(timezone.utc),
        version=1,
        contents=[ContentOut.model_validate(r) for r in rows],
    )


@router.post("/{video_id}/feedback", response_model=FeedbackOut)
def submit_feedback(
    video_id: str,
    body: FeedbackIn,
    request: Request,
    db: Session = Depends(get_db),
) -> FeedbackOut:
    """用户反馈视频内容问题。
    累积达 FEEDBACK_THRESHOLD 次自动把视频 status 改成 review_pending，从首页摘掉。
    同一 client_fp + 同一 kind 短期内（24h）重复提交只算 1 次。
    """
    content = db.get(Content, video_id)
    if content is None:
        raise HTTPException(status_code=404, detail="video not found")

    fp = _client_fp(request)

    # 去重：同 fp + 同 kind 在最近 24h 提交过就不再计数
    one_day_ago = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    exists = db.execute(
        select(ContentFeedback).where(
            ContentFeedback.video_id == video_id,
            ContentFeedback.kind == body.kind,
            ContentFeedback.client_fp == fp,
            ContentFeedback.created_at >= one_day_ago,
        ).limit(1)
    ).scalar_one_or_none()

    if exists is None:
        db.add(ContentFeedback(
            video_id=video_id,
            kind=body.kind,
            note=body.note,
            client_fp=fp,
        ))
        db.flush()

    # 总反馈数（不去重）—— 简单阈值就用总数
    count = db.execute(
        select(func.count()).select_from(ContentFeedback).where(
            ContentFeedback.video_id == video_id,
            ContentFeedback.kind == body.kind,
        )
    ).scalar_one()

    if count >= FEEDBACK_THRESHOLD and content.status == "published":
        content.status = "review_pending"

    db.commit()
    return FeedbackOut(
        accepted=exists is None,
        feedback_count=count,
        status=content.status,
    )
