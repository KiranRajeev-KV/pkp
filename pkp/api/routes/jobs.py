"""Job API routes."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from pkp.api.deps import get_db
from pkp.storage.db import Database
from pkp.storage.models import Job

router = APIRouter()


class JobResponse(BaseModel):
    """Job response."""

    job_id: str
    job_type: str
    payload: dict[str, Any]
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None


def _job_response(job: Job) -> JobResponse:
    """Build an API response from a job record."""
    payload = json.loads(job.payload)
    return JobResponse(
        job_id=job.job_id,
        job_type=job.job_type,
        payload=payload,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error=job.error,
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    db: Annotated[Database, Depends(get_db)],
) -> JobResponse:
    """Get one job by job_id."""
    job = await db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_response(job)
