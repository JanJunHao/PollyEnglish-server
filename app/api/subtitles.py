"""字幕生成 jobs API。"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import SubtitleJob
from app.schemas import SubtitleJobIn, SubtitleJobOut
from app.security import rate_limit, require_api_key
from app.services.subtitle_pipeline import enqueue_job, run_job

router = APIRouter(
    prefix="/v1/subtitles",
    tags=["subtitles"],
    dependencies=[Depends(require_api_key), Depends(rate_limit("rate_limit_ai"))],
)


def _to_out(job: SubtitleJob) -> SubtitleJobOut:
    return SubtitleJobOut(
        id=job.id,
        youtube_id=job.youtube_id,
        status=job.status,
        result_subtitle_url=job.result_subtitle_url,
        segments_count=job.segments_count,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _spawn(job_id: str) -> None:
    """BackgroundTasks 调度：sync 函数包一层 asyncio.run。"""
    import asyncio
    asyncio.run(run_job(job_id))


@router.post("/jobs", response_model=SubtitleJobOut)
def create_job(
    body: SubtitleJobIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> SubtitleJobOut:
    job = enqueue_job(db, youtube_id=body.youtube_id)
    background.add_task(_spawn, job.id)
    return _to_out(job)


@router.get("/jobs/{job_id}", response_model=SubtitleJobOut)
def get_job(job_id: str, db: Session = Depends(get_db)) -> SubtitleJobOut:
    job = db.get(SubtitleJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _to_out(job)
