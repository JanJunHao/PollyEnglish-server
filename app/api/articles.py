"""图文内容对外读取端点（Q4 图文线，阶段 1）。

与 app/api/contents.py 的 /v1/contents/latest 平行：
- /contents/latest 服务 iOS 视频首页（kind='video'）；
- /articles/latest 服务 iOS 图文列表（kind='article'）。
两者鉴权 / 限速方式一致（require_api_key + rate_limit），互不影响。

为什么单独建端点而不扩展 /contents/latest：contents.py 注释明确写了
「/contents/latest 只返回视频形态，图文走后续独立端点」——不改其对外契约。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ArticleDetails, Content
from app.schemas import ArticleOut, ArticleSegment, ArticlesLatestOut
from app.security import rate_limit, require_api_key

# 复用 contents 的限速配置（图文与视频列表同档）
router = APIRouter(
    prefix="/v1/articles",
    tags=["articles"],
    dependencies=[Depends(require_api_key), Depends(rate_limit("rate_limit_contents"))],
)


def _to_article_out(content: Content, details: ArticleDetails) -> ArticleOut:
    """把 contents 基表行 + article_details 明细行合成对外 ArticleOut。"""
    return ArticleOut(
        id=content.id,
        kind=content.kind,
        title=content.title,
        author=content.author,
        source=content.source,
        cefr_level=content.cefr_level,
        categories=content.categories or [],
        topics=content.topics or [],
        attribution=content.attribution,
        thumbnail_url=content.thumbnail_url or "",
        body=details.body,
        paragraphs=[ArticleSegment(**seg) for seg in (details.paragraphs or [])],
        image_urls=details.image_urls or [],
        word_count=details.word_count,
        reading_time_seconds=details.reading_time_seconds,
        updated_at=content.updated_at,
    )


@router.get("/latest", response_model=ArticlesLatestOut)
def latest(
    since: datetime | None = Query(default=None, description="ISO 8601；只返回此时间之后更新的"),
    db: Session = Depends(get_db),
) -> ArticlesLatestOut:
    """图文首页：返回已发布的图文内容（kind='article'），按更新时间倒序。

    与 /contents/latest 对位——只返回 kind='article' 且 status='published' 的行，
    联 article_details 取正文与切段。
    """
    stmt = (
        select(Content, ArticleDetails)
        .join(ArticleDetails, ArticleDetails.content_id == Content.id)
        .where(
            Content.kind == "article",
            Content.status == "published",
        )
    )
    if since is not None:
        stmt = stmt.where(Content.updated_at > since)
    stmt = stmt.order_by(Content.is_recommended.desc(), Content.updated_at.desc())

    rows = db.execute(stmt).all()
    return ArticlesLatestOut(
        server_time=datetime.now(timezone.utc),
        version=1,
        articles=[_to_article_out(c, d) for c, d in rows],
    )


@router.get("/{article_id}", response_model=ArticleOut)
def get_article(article_id: str, db: Session = Depends(get_db)) -> ArticleOut:
    """按 id 取单篇图文（含完整正文 + 切段）。"""
    content = db.get(Content, article_id)
    if content is None or content.kind != "article":
        raise HTTPException(status_code=404, detail="article not found")
    details = db.get(ArticleDetails, article_id)
    if details is None:
        raise HTTPException(status_code=404, detail="article details missing")
    return _to_article_out(content, details)
