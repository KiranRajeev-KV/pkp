"""FastAPI application for PKP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Response
from pydantic import BaseModel

from pkp import __version__
from pkp.config import get_config
from pkp.storage.db import Database, get_database


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
    "turso": "http://localhost:8080",
    "qdrant": "http://localhost:6333",
}

EXPECTED_STATUS = {
    "crawl4ai": 200,
    "turso": 404,
    "qdrant": 200,
}

SERVICE_PATHS = {
    "crawl4ai": "/health",
    "turso": "",
    "qdrant": "/",
}


async def check_service(name: str, url: str) -> tuple[str, ServiceHealth]:
    """Check a single service's health."""
    if name == "turso":
        return await _check_turso()
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
                version = "0.24.33"
            return name, ServiceHealth(status="ok", version=version)
    except httpx.TimeoutException:
        return name, ServiceHealth(status="error", message="timeout")
    except httpx.ConnectError:
        return name, ServiceHealth(status="error", message="connection failed")
    except Exception as e:
        return name, ServiceHealth(status="error", message=str(e)[:50])


async def _check_turso() -> tuple[str, ServiceHealth]:
    """Check Turso health via actual SQL query."""
    import libsql_client

    try:
        async with libsql_client.create_client("http://localhost:8080") as client:
            result = await client.execute("SELECT 1 as health_check")
            if result.rows and len(result.rows) > 0:
                return "turso", ServiceHealth(status="ok", version="0.24.33")
            return "turso", ServiceHealth(status="error", message="no rows returned")
    except Exception as e:
        return "turso", ServiceHealth(status="error", message=str(e)[:50])


class DatabaseDependency:
    """FastAPI dependency for database access."""

    def __init__(self) -> None:
        self._db: Database | None = None

    async def __call__(self) -> Database:
        """Get or create database connection."""
        if self._db is None:
            self._db = await get_database()
        return self._db

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None


_db_dep = DatabaseDependency()


async def _get_db() -> Database:
    """FastAPI dependency for database injection."""
    return await _db_dep()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    config = get_config()
    config.ensure_dirs()

    try:
        await _db_dep()
    except Exception as e:
        raise RuntimeError(f"Failed to initialize database: {e}") from e

    yield

    await _db_dep.close()


def get_application() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(
        title="PKP - Personal Knowledge Pipeline",
        description="Ingestion, retrieval, and proposal system",
        version=__version__,
        lifespan=lifespan,
    )

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
