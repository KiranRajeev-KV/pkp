"""FastAPI dependencies for PKP API routes."""

from __future__ import annotations

from pkp.storage.db import Database, get_database


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


db_dependency = DatabaseDependency()


async def get_db() -> Database:
    """FastAPI dependency for database injection."""
    return await db_dependency()
