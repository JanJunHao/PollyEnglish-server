"""字幕翻译 jobs API。"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import SubtitleTranslationJob
from app.schemas import TranslationJobIn, TranslationJobOut
from app.security import rate_limit, require_api_key
from app.services.translation_pipeline import enqueue_job, run_job

router = APIRouter(
    prefix="/v1/translations",
    tags=["translations"],
    dependencies=[Depends(require_api_key), Depends(rate_limit("rate_limit_ai"))],
)


def _to_out(job: SubtitleTranslationJob) -> TranslationJobOut:
    return TranslationJobOut(
        id=job.id,
        source_subtitle_url=job.source_subtitle_url,
        target_lang=job.target_lang,
        status=job.status,
        result_subtitle_url=job.result_subtitle_url,
        segments_count=job.segments_count,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _spawn(job_id: str) -> None:
    import asyncio
    asyncio.run(run_job(job_id))


@router.post("/jobs", response_model=TranslationJobOut)
def create_job(
    body: TranslationJobIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> TranslationJobOut:
    job = enqueue_job(db, source_subtitle_url=body.source_subtitle_url, target_lang=body.target_lang)
    background.add_task(_spawn, job.id)
    return _to_out(job)


@router.get("/jobs/{job_id}", response_model=TranslationJobOut)
def get_job(job_id: str, db: Session = Depends(get_db)) -> TranslationJobOut:
    job = db.get(SubtitleTranslationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _to_out(job)
