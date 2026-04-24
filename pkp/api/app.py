"""FastAPI application for PKP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pkp import __version__
from pkp.api.deps import db_dependency, get_db
from pkp.api.routes.ingest import router as ingest_router
from pkp.api.routes.jobs import router as jobs_router
from pkp.api.routes.proposals import router as proposals_router
from pkp.api.routes.queue import router as queue_router
from pkp.config import get_config
from pkp.storage.qdrant import (
    qdrant_available,
    search_documents_hybrid,
)


class ServiceHealth(BaseModel):
    """Individual service health status."""

    status: str
    message: str | None = None
    version: str | None = None


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str
    data_dir: Path
    services: dict[str, ServiceHealth]


SERVICES = {
    "crawl4ai": "http://localhost:11235",
    "qdrant": "http://localhost:6333",
}

EXPECTED_STATUS = {
    "crawl4ai": 200,
    "qdrant": 200,
}

SERVICE_PATHS = {
    "crawl4ai": "/health",
    "qdrant": "/",
}


async def check_service(name: str, url: str) -> tuple[str, ServiceHealth]:
    """Check a single service's health."""
    path = SERVICE_PATHS.get(name, "/health")
    expected = EXPECTED_STATUS.get(name, 200)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url}{path}")
            if response.status_code != expected:
                return name, ServiceHealth(
                    status="error", message=f"http {response.status_code}"
                )
            data = response.json() if response.text else {}
            if data:
                version = (
                    data.get("version")
                    or data.get("title", "").split()[-1]
                    or "unknown"
                )
            else:
                version = data.get("version", "") or "unknown"
            return name, ServiceHealth(status="ok", version=version)
    except httpx.TimeoutException:
        return name, ServiceHealth(status="error", message="timeout")
    except httpx.ConnectError:
        return name, ServiceHealth(status="error", message="connection failed")
    except Exception as e:
        return name, ServiceHealth(status="error", message=str(e)[:50])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    config = get_config()
    config.ensure_dirs()
    worker_task: asyncio.Task[None] | None = None

    try:
        await db_dependency()
    except Exception as e:
        raise RuntimeError(f"Failed to initialize database: {e}") from e

    from pkp.api.worker import worker_loop

    worker_task = asyncio.create_task(worker_loop(), name="pkp-worker")

    try:
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task
        await db_dependency.close()


def get_application() -> FastAPI:
    """Create the FastAPI application."""
    static_dir = Path(__file__).resolve().parent / "static"
    app = FastAPI(
        title="PKP - Personal Knowledge Pipeline",
        description="Ingestion, retrieval, and proposal system",
        version=__version__,
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(proposals_router)
    app.include_router(jobs_router)
    app.include_router(ingest_router)
    app.include_router(queue_router)

    return app


app = get_application()


@app.get("/health", response_model=HealthResponse)
async def health_check(response: Response) -> HealthResponse:
    """Health check endpoint - verifies all external services."""
    config = get_config()

    results = await asyncio.gather(
        *[check_service(name, url) for name, url in SERVICES.items()]
    )

    services = dict(results)

    healthy_count = sum(1 for h in services.values() if h.status == "ok")
    if healthy_count == len(SERVICES):
        status = "ok"
    elif healthy_count > 0:
        status = "degraded"
        response.status_code = 200
    else:
        status = "error"
        response.status_code = 503

    return HealthResponse(
        status=status,
        version=__version__,
        data_dir=config.data_dir,
        services=services,
    )


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "name": "PKP - Personal Knowledge Pipeline",
        "version": __version__,
    }


class SearchResult(BaseModel):
    """Search result at document level."""

    doc_sha256: str
    title: str
    url: str | None
    match_count: int
    best_rank: float


class SearchResponse(BaseModel):
    """Search response."""

    query: str
    results: list[SearchResult]
    total: int


@app.get("/search", response_model=SearchResponse)
async def search(q: str, limit: int = 10) -> SearchResponse:
    """Search archived documents using Qdrant (hybrid) or FTS5."""
    try:
        if qdrant_available():
            results = await search_documents_hybrid(q, limit)
        else:
            db = await get_db()
            results = await db.search_documents(q, limit)
    except Exception:
        db = await get_db()
        results = await db.search_documents(q, limit)

    return SearchResponse(
        query=q,
        results=[
            SearchResult(
                doc_sha256=r.doc_sha256,
                title=r.title,
                url=r.url,
                match_count=r.match_count,
                best_rank=r.best_rank,
            )
            for r in results
        ],
        total=len(results),
    )
