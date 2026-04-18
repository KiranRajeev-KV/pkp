"""SQLite database layer for PKP."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from pkp.config import get_config


@dataclass
class Document:
    """Document record from the documents table."""

    sha256: str
    title: str
    doc_type: str
    archive_path: str
    retrieved_at: datetime
    url: str | None = None
    indexed_at: datetime | None = None
    word_count: int | None = None
    vault_path: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Chunk:
    """Chunk record from the chunks table."""

    chunk_id: str
    doc_sha256: str
    chunk_index: int
    char_start: int
    char_end: int
    token_count: int | None = None


@dataclass
class Proposal:
    """Proposal record from the proposals table."""

    proposal_id: str
    doc_a_sha256: str
    doc_b_sha256: str
    score: float
    rationale: str | None
    status: str
    created_at: datetime
    reviewed_at: datetime | None = None
    link_type: str | None = None


@dataclass
class Job:
    """Job record from the jobs table."""

    job_id: str
    job_type: str
    payload: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


@dataclass
class SearchResult:
    """Search result at document level."""

    doc_sha256: str
    title: str
    url: str | None
    match_count: int
    best_rank: float


@dataclass
class IngestionMetric:
    """Timing metrics for an ingestion."""

    doc_sha256: str = ""
    doc_type: str = ""
    source: str | None = None
    total_time_ms: int = 0
    extraction_time_ms: int | None = None
    normalization_time_ms: int | None = None
    archive_time_ms: int | None = None
    created_at: datetime | None = None


SCHEMA = """
-- Core document registry
CREATE TABLE IF NOT EXISTS documents (
    sha256 TEXT PRIMARY KEY,
    url TEXT,
    title TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    indexed_at TEXT,
    word_count INTEGER,
    archive_path TEXT NOT NULL,
    vault_path TEXT,
    tags TEXT
);

-- Chunk registry (for provenance, not for content storage)
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_sha256 TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    token_count INTEGER,
    FOREIGN KEY (doc_sha256) REFERENCES documents(sha256)
);

-- BM25 full-text index over chunk content and title (title weighted 10x)
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    doc_sha256 UNINDEXED,
    title,
    content,
    tokenize = 'porter unicode61'
);

-- Connection proposals
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    doc_a_sha256 TEXT NOT NULL,
    doc_b_sha256 TEXT NOT NULL,
    score REAL NOT NULL,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    link_type TEXT,
    FOREIGN KEY (doc_a_sha256) REFERENCES documents(sha256),
    FOREIGN KEY (doc_b_sha256) REFERENCES documents(sha256)
);

-- Job queue
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error TEXT
);

-- Rejected pairs (to prevent reproposing)
CREATE TABLE IF NOT EXISTS rejected_pairs (
    doc_a_sha256 TEXT NOT NULL,
    doc_b_sha256 TEXT NOT NULL,
    rejected_at TEXT NOT NULL,
    PRIMARY KEY (doc_a_sha256, doc_b_sha256)
);

-- Ingestion timing metrics
CREATE TABLE IF NOT EXISTS ingestion_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_sha256 TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    source TEXT,
    total_time_ms INTEGER NOT NULL,
    extraction_time_ms INTEGER,
    normalization_time_ms INTEGER,
    archive_time_ms INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (doc_sha256) REFERENCES documents(sha256)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_doc_a ON proposals(doc_a_sha256);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_sha256);
CREATE INDEX IF NOT EXISTS idx_metrics_doc ON ingestion_metrics(doc_sha256);
"""

MIGRATIONS: list[str] = []


class Database:
    """Async SQLite database connection manager."""

    def __init__(self, db_path: Path) -> None:
        """Initialize database connection."""
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Connect to the database and run migrations."""
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row

        statements = [s.strip() for s in SCHEMA.split(";") if s.strip()]
        for stmt in statements:
            await self._conn.execute(stmt)
        await self._conn.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(
        self,
    ) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Context manager for database transactions."""
        if self._conn is None:
            raise RuntimeError("Database not connected")
        async with self._conn:
            yield self._conn

    async def _exec(self, sql: str, params: tuple | dict[str, Any] = ()) -> None:
        """Execute a SQL statement."""
        if self._conn is None:
            raise RuntimeError("Database not connected")
        await self._conn.execute(sql, params)

    async def _fetch_one(
        self, sql: str, params: tuple | dict[str, Any] = ()
    ) -> aiosqlite.Row | None:
        """Fetch one row from the database."""
        if self._conn is None:
            raise RuntimeError("Database not connected")
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchone()

    async def _fetch_all(
        self, sql: str, params: tuple | dict[str, Any] = ()
    ) -> list[aiosqlite.Row]:
        """Fetch all rows from the database."""
        if self._conn is None:
            raise RuntimeError("Database not connected")
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        return list(rows)

    async def get_document(self, sha256: str) -> Document | None:
        """Get a document by sha256."""
        row = await self._fetch_one(
            "SELECT * FROM documents WHERE sha256 = ?", (sha256,)
        )
        if row is None:
            return None
        return self._row_to_document(row)

    async def insert_document(self, doc: Document) -> None:
        """Insert a new document."""
        await self._exec(
            """INSERT OR REPLACE INTO documents
            (sha256, url, title, doc_type, retrieved_at, indexed_at, word_count, archive_path, vault_path, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.sha256,
                doc.url,
                doc.title,
                doc.doc_type,
                doc.retrieved_at.isoformat(),
                doc.indexed_at.isoformat() if doc.indexed_at else None,
                doc.word_count,
                doc.archive_path,
                doc.vault_path,
                json.dumps(doc.tags),
            ),
        )
        assert self._conn is not None
        await self._conn.commit()

    def _row_to_document(self, row: aiosqlite.Row) -> Document:
        """Convert a database row to a Document instance."""
        tags = []
        if row["tags"]:
            tags = json.loads(row["tags"])
        return Document(
            sha256=row["sha256"],
            url=row["url"],
            title=row["title"],
            doc_type=row["doc_type"],
            retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
            indexed_at=datetime.fromisoformat(row["indexed_at"])
            if row["indexed_at"]
            else None,
            word_count=row["word_count"],
            archive_path=row["archive_path"],
            vault_path=row["vault_path"],
            tags=tags,
        )

    async def get_jobs_by_status(self, status: str, limit: int = 1) -> list[Job]:
        """Get jobs by status."""
        rows = await self._fetch_all(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC LIMIT ?",
            (status, limit),
        )
        return [self._row_to_job(row) for row in rows]

    async def claim_job(self, job_id: str) -> Job | None:
        """Claim a pending job for processing."""
        now = datetime.utcnow().isoformat()
        await self._exec(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE job_id = ? AND status = 'pending'",
            (now, job_id),
        )
        assert self._conn is not None
        await self._conn.commit()
        row = await self._fetch_one("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        if row is None:
            return None
        return self._row_to_job(row)

    async def complete_job(self, job_id: str, error: str | None = None) -> None:
        """Mark a job as completed."""
        now = datetime.utcnow().isoformat()
        status = "failed" if error else "done"
        await self._exec(
            "UPDATE jobs SET status = ?, completed_at = ?, error = ? WHERE job_id = ?",
            (status, now, error, job_id),
        )
        assert self._conn is not None
        await self._conn.commit()

    async def create_job(
        self, job_id: str, job_type: str, payload: dict[str, Any]
    ) -> None:
        """Create a new job."""
        now = datetime.utcnow().isoformat()
        await self._exec(
            "INSERT INTO jobs (job_id, job_type, payload, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (job_id, job_type, json.dumps(payload), now),
        )
        assert self._conn is not None
        await self._conn.commit()

    def _row_to_job(self, row: aiosqlite.Row) -> Job:
        """Convert a database row to a Job instance."""
        return Job(
            job_id=row["job_id"],
            job_type=row["job_type"],
            payload=row["payload"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=datetime.fromisoformat(row["started_at"])
            if row["started_at"]
            else None,
            completed_at=datetime.fromisoformat(row["completed_at"])
            if row["completed_at"]
            else None,
            error=row["error"],
        )

    async def insert_chunk(self, chunk: Chunk, fts_content: str) -> None:
        """Insert a chunk and add to FTS index."""
        await self._exec(
            """INSERT OR REPLACE INTO chunks
            (chunk_id, doc_sha256, chunk_index, char_start, char_end, token_count)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                chunk.chunk_id,
                chunk.doc_sha256,
                chunk.chunk_index,
                chunk.char_start,
                chunk.char_end,
                chunk.token_count,
            ),
        )

        doc_row = await self._fetch_one(
            "SELECT title FROM documents WHERE sha256 = ?",
            (chunk.doc_sha256,),
        )
        title = doc_row["title"] if doc_row else ""

        await self._exec(
            "INSERT INTO chunks_fts (chunk_id, doc_sha256, title, content) VALUES (?, ?, ?, ?)",
            (chunk.chunk_id, chunk.doc_sha256, title, fts_content),
        )
        assert self._conn is not None
        await self._conn.commit()

    async def search_chunks_fts(
        self, query: str, limit: int = 50
    ) -> list[tuple[str, float]]:
        """Search chunks using BM25."""
        rows = await self._fetch_all(
            """SELECT chunk_id, bm25(chunks_fts) as rank
            FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, limit),
        )
        return [(row["chunk_id"], row["rank"]) for row in rows]

    async def search_documents(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Search documents using FTS5 with title weighting (10x), single SQL query."""
        rows = await self._fetch_all(
            """WITH fts_results AS (
                SELECT doc_sha256, bm25(chunks_fts, 10.0, 1.0) as rank
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT 100
            )
            SELECT
                d.sha256,
                d.title,
                d.url,
                COUNT(*) as match_count,
                MIN(r.rank) as best_rank
            FROM fts_results r
            JOIN documents d ON d.sha256 = r.doc_sha256
            GROUP BY d.sha256
            ORDER BY best_rank
            LIMIT ?""",
            (query, limit),
        )

        return [
            SearchResult(
                doc_sha256=row["sha256"],
                title=row["title"],
                url=row["url"],
                match_count=row["match_count"],
                best_rank=row["best_rank"],
            )
            for row in rows
        ]

    async def get_all_documents(self) -> list[Document]:
        """Get all documents."""
        rows = await self._fetch_all(
            "SELECT * FROM documents ORDER BY retrieved_at DESC"
        )
        return [self._row_to_document(row) for row in rows]

    async def insert_proposal(self, proposal: Proposal) -> None:
        """Insert a new proposal."""
        await self._exec(
            """INSERT OR REPLACE INTO proposals
            (proposal_id, doc_a_sha256, doc_b_sha256, score, rationale, status, created_at, reviewed_at, link_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                proposal.proposal_id,
                proposal.doc_a_sha256,
                proposal.doc_b_sha256,
                proposal.score,
                proposal.rationale,
                proposal.status,
                proposal.created_at.isoformat(),
                proposal.reviewed_at.isoformat() if proposal.reviewed_at else None,
                proposal.link_type,
            ),
        )
        assert self._conn is not None
        await self._conn.commit()

    async def get_proposals_by_status(
        self, status: str, limit: int = 50
    ) -> list[Proposal]:
        """Get proposals by status."""
        rows = await self._fetch_all(
            "SELECT * FROM proposals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
        return [self._row_to_proposal(row) for row in rows]

    def _row_to_proposal(self, row: aiosqlite.Row) -> Proposal:
        """Convert a database row to a Proposal instance."""
        return Proposal(
            proposal_id=row["proposal_id"],
            doc_a_sha256=row["doc_a_sha256"],
            doc_b_sha256=row["doc_b_sha256"],
            score=row["score"],
            rationale=row["rationale"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            reviewed_at=datetime.fromisoformat(row["reviewed_at"])
            if row["reviewed_at"]
            else None,
            link_type=row["link_type"],
        )

    async def update_proposal_status(
        self, proposal_id: str, status: str, reviewed_at: datetime | None = None
    ) -> None:
        """Update proposal status."""
        await self._exec(
            "UPDATE proposals SET status = ?, reviewed_at = ? WHERE proposal_id = ?",
            (status, reviewed_at.isoformat() if reviewed_at else None, proposal_id),
        )
        assert self._conn is not None
        await self._conn.commit()

    async def mark_document_indexed(self, sha256: str) -> None:
        """Mark a document as indexed."""
        now = datetime.utcnow().isoformat()
        await self._exec(
            "UPDATE documents SET indexed_at = ? WHERE sha256 = ?",
            (now, sha256),
        )
        assert self._conn is not None
        await self._conn.commit()

    async def mark_document_vault_path(self, sha256: str, vault_path: str) -> None:
        """Mark a document's vault path."""
        await self._exec(
            "UPDATE documents SET vault_path = ? WHERE sha256 = ?",
            (vault_path, sha256),
        )
        assert self._conn is not None
        await self._conn.commit()

    async def insert_metric(self, metric: IngestionMetric) -> None:
        """Insert ingestion timing metric."""
        created_at = metric.created_at or datetime.utcnow()
        await self._exec(
            """INSERT INTO ingestion_metrics
            (doc_sha256, doc_type, source, total_time_ms, extraction_time_ms, normalization_time_ms, archive_time_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                metric.doc_sha256,
                metric.doc_type,
                metric.source,
                metric.total_time_ms,
                metric.extraction_time_ms,
                metric.normalization_time_ms,
                metric.archive_time_ms,
                created_at.isoformat(),
            ),
        )
        assert self._conn is not None
        await self._conn.commit()

    async def get_metrics(self) -> list[IngestionMetric]:
        """Get all ingestion metrics."""
        rows = await self._fetch_all(
            "SELECT * FROM ingestion_metrics ORDER BY created_at DESC"
        )
        return [self._row_to_metric(row) for row in rows]

    def _row_to_metric(self, row: aiosqlite.Row) -> IngestionMetric:
        """Convert a database row to IngestionMetric."""
        return IngestionMetric(
            doc_sha256=row["doc_sha256"],
            doc_type=row["doc_type"],
            source=row["source"],
            total_time_ms=row["total_time_ms"],
            extraction_time_ms=row["extraction_time_ms"],
            normalization_time_ms=row["normalization_time_ms"],
            archive_time_ms=row["archive_time_ms"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


_db_instance: Database | None = None


async def get_database() -> Database:
    """Get the global database instance."""
    global _db_instance
    if _db_instance is None:
        config = get_config()
        if config.db_path is None:
            raise RuntimeError("Database path not configured")
        _db_instance = Database(config.db_path)
        await _db_instance.connect()
    return _db_instance


async def close_database() -> None:
    """Close the global database instance."""
    global _db_instance
    if _db_instance:
        await _db_instance.close()
        _db_instance = None


@asynccontextmanager
async def db_context() -> AsyncGenerator[Database, None]:
    """Context manager for database - handles connect/close lifecycle.

    Usage:
        async with db_context() as db:
            await db.insert_document(doc)
            await db.insert_metric(metric)
        # Connection automatically closed
    """
    config = get_config()
    if config.db_path is None:
        raise RuntimeError("Database path not configured")

    db = Database(config.db_path)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()
