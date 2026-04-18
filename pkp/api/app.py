"""FastAPI application for PKP."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from pkp import __version__
from pkp.config import get_config
from pkp.storage.db import Database, get_database


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str
    data_dir: Path


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
async def health_check() -> HealthResponse:
    """Health check endpoint."""
    config = get_config()
    return HealthResponse(
        status="ok",
        version=__version__,
        data_dir=config.data_dir,
    )


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "name": "PKP - Personal Knowledge Pipeline",
        "version": __version__,
    }
