"""Ingestion API routes."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pkp.storage.db import db_context

router = APIRouter()


class IngestUrlRequest(BaseModel):
    """Request to enqueue URL ingestion."""

    url: str


class IngestPdfRequest(BaseModel):
    """Request to enqueue PDF ingestion from a local path."""

    path: Path


class EnqueueJobResponse(BaseModel):
    """Response for an enqueued job."""

    job_id: str
    job_type: str
    status: str


@router.post("/ingest/url", response_model=EnqueueJobResponse)
async def ingest_url(request: IngestUrlRequest) -> EnqueueJobResponse:
    """Enqueue a URL ingestion job."""
    job_type = "ingest_url"
    job_id = _make_job_id(job_type)
    async with db_context() as db:
        await db.create_job(job_id, job_type, {"url": request.url})
    return EnqueueJobResponse(job_id=job_id, job_type=job_type, status="pending")


@router.post("/ingest/pdf", response_model=EnqueueJobResponse)
async def ingest_pdf(request: IngestPdfRequest) -> EnqueueJobResponse:
    """Enqueue a PDF ingestion job from a local filesystem path."""
    if not request.path.exists():
        raise HTTPException(status_code=422, detail="PDF file does not exist")

    job_type = "ingest_pdf"
    job_id = _make_job_id(job_type)
    async with db_context() as db:
        await db.create_job(job_id, job_type, {"pdf_path": str(request.path)})
    return EnqueueJobResponse(job_id=job_id, job_type=job_type, status="pending")


def _make_job_id(job_type: str) -> str:
    """Create a job identifier using the existing short UUID pattern."""
    return f"{job_type}-{uuid.uuid4().hex[:8]}"
