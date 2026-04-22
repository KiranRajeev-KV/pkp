"""Async background worker for processing queued jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from pkp.pipeline.ingest import ingest_pdf, ingest_url, rebuild_index
from pkp.storage.db import Job, db_context

logger = logging.getLogger(__name__)


async def worker_loop(interval_seconds: float = 1.0) -> None:
    """Poll for pending jobs and process them in-process."""
    startup_reset_done = False

    while True:
        try:
            if not startup_reset_done:
                reset_count = await _reset_interrupted_jobs()
                if reset_count:
                    logger.info(
                        "worker reset %s interrupted job(s) to pending", reset_count
                    )
                startup_reset_done = True

            job = await _claim_next_job()
            if job is None:
                await asyncio.sleep(interval_seconds)
                continue

            await _process_job(job)
        except asyncio.CancelledError:
            logger.info("worker shutdown requested")
            raise
        except Exception:
            logger.exception(
                "worker loop error; retrying after %.1fs", interval_seconds
            )
            await asyncio.sleep(interval_seconds)


async def _reset_interrupted_jobs() -> int:
    """Reset stale running jobs at worker startup."""
    async with db_context() as db:
        return await db.reset_running_jobs()


async def _claim_next_job() -> Job | None:
    """Claim the next pending job."""
    async with db_context() as db:
        return await db.claim_next_pending_job()


async def _process_job(job: Job) -> None:
    """Dispatch one claimed job and persist its final state."""
    job_start = time.perf_counter()
    logger.info("worker claimed job_id=%s type=%s", job.job_id, job.job_type)

    try:
        payload = json.loads(job.payload)
        await _dispatch_job(job.job_type, payload)
    except asyncio.CancelledError:
        await asyncio.shield(_reset_job(job.job_id))
        duration_ms = int((time.perf_counter() - job_start) * 1000)
        logger.info(
            "worker interrupted job_id=%s type=%s duration_ms=%s -> pending",
            job.job_id,
            job.job_type,
            duration_ms,
        )
        raise
    except Exception as exc:
        await asyncio.shield(_complete_job(job.job_id, error=str(exc)))
        duration_ms = int((time.perf_counter() - job_start) * 1000)
        logger.exception(
            "worker failed job_id=%s type=%s duration_ms=%s",
            job.job_id,
            job.job_type,
            duration_ms,
        )
        return

    await asyncio.shield(_complete_job(job.job_id))
    duration_ms = int((time.perf_counter() - job_start) * 1000)
    logger.info(
        "worker completed job_id=%s type=%s duration_ms=%s",
        job.job_id,
        job.job_type,
        duration_ms,
    )


async def _dispatch_job(job_type: str, payload: dict[str, Any]) -> None:
    """Run the job handler for a claimed job."""
    logger.info("worker dispatch type=%s payload=%s", job_type, payload)

    if job_type == "ingest_url":
        result = await ingest_url(str(payload["url"]), reporter=_log_progress)
        if result.created:
            await _enqueue_job(
                "generate_proposals",
                {"doc_sha256": result.doc_sha256},
            )
        return

    if job_type == "ingest_pdf":
        result = await ingest_pdf(
            Path(str(payload["pdf_path"])), reporter=_log_progress
        )
        if result.created:
            await _enqueue_job(
                "generate_proposals",
                {"doc_sha256": result.doc_sha256},
            )
        return

    if job_type == "generate_proposals":
        logger.info(
            "worker proposals not yet implemented doc_sha256=%s",
            payload.get("doc_sha256"),
        )
        return

    if job_type == "rebuild_index":
        await rebuild_index(
            filter_doc_type=_optional_str(payload.get("doc_type")),
            rebuild_all=bool(payload.get("rebuild_all", False)),
            reporter=_log_progress,
        )
        return

    raise ValueError(f"Unsupported job type: {job_type}")


async def _complete_job(job_id: str, error: str | None = None) -> None:
    """Persist a final job state."""
    async with db_context() as db:
        await db.complete_job(job_id, error=error)


async def _reset_job(job_id: str) -> None:
    """Return an interrupted job to pending."""
    async with db_context() as db:
        await db.reset_job_to_pending(job_id)


async def _enqueue_job(job_type: str, payload: dict[str, Any]) -> str:
    """Create a pending job row."""
    job_id = f"{job_type}-{uuid.uuid4().hex[:8]}"
    async with db_context() as db:
        await db.create_job(job_id, job_type, payload)
    logger.info("worker enqueued job_id=%s type=%s", job_id, job_type)
    return job_id


def _log_progress(message: str) -> None:
    """Send shared pipeline progress messages to the worker logger."""
    logger.info("worker %s", message)


def _optional_str(value: Any) -> str | None:
    """Normalize an optional string payload field."""
    if value is None:
        return None
    return str(value)
