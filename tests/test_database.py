from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pkp.storage.db import Database


class FakeCursor:
    """Fake cursor for testing database operations."""

    def __init__(
        self, *, row: dict[str, object] | None = None, rowcount: int = 0
    ) -> None:
        self._row = row
        self.rowcount = rowcount
        self.closed = False

    async def fetchone(self) -> dict[str, object] | None:
        """Fetch the next row."""
        return self._row

    async def close(self) -> None:
        """Close the cursor."""
        self.closed = True


class FakeConnection:
    """Fake connection for testing database operations."""

    def __init__(self, cursors: list[FakeCursor]) -> None:
        self._cursors = cursors
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.commit_count = 0
        self.rollback_count = 0

    async def execute(self, sql: str, params: tuple[object, ...] = ()) -> FakeCursor:
        """Execute a SQL statement."""
        self.execute_calls.append((sql, params))
        if not self._cursors:
            raise AssertionError("Unexpected execute call with no remaining cursors")
        return self._cursors.pop(0)

    async def commit(self) -> None:
        """Commit the current transaction."""
        self.commit_count += 1

    async def rollback(self) -> None:
        """Rollback the current transaction."""
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_insert_document_serializes_core_fields(
    monkeypatch,
    document_factory,
) -> None:
    db = Database(Path("/tmp/test.db"))
    fake_conn = FakeConnection([])
    db._conn = fake_conn
    captured: dict[str, object] = {}

    async def fake_exec(sql: str, params: tuple[object, ...]) -> None:
        captured["sql"] = sql
        captured["params"] = params

    monkeypatch.setattr(db, "_exec", fake_exec)

    doc = document_factory(
        sha256="1" * 64,
        title="Round Trip Document",
        doc_type="pdf",
        archive_path="/tmp/archive/round-trip",
        retrieved_at=datetime(2026, 4, 26, tzinfo=UTC),
    )

    await db.insert_document(doc)

    assert "INSERT OR REPLACE INTO documents" in str(captured["sql"])
    assert captured["params"] == (
        doc.sha256,
        doc.url,
        doc.title,
        doc.doc_type,
        doc.retrieved_at.isoformat(),
        None,
        doc.word_count,
        doc.archive_path,
        None,
        "[]",
        doc.embedded_with,
    )
    assert fake_conn.commit_count == 1


@pytest.mark.asyncio
async def test_claim_next_pending_job_claims_once_and_then_exhausts_queue() -> None:
    now = "2026-04-26T00:00:00+00:00"
    row = {
        "job_id": "job-1",
        "job_type": "ingest_url",
        "payload": '{"url": "https://example.com"}',
        "status": "running",
        "created_at": now,
        "started_at": now,
        "completed_at": None,
        "error": None,
    }

    db = Database(Path("/tmp/test.db"))
    fake_conn = FakeConnection([FakeCursor(row=row, rowcount=1), FakeCursor(row=None)])
    db._conn = fake_conn

    claimed = await db.claim_next_pending_job()
    remaining = await db.claim_next_pending_job()

    assert claimed is not None
    assert claimed.job_id == "job-1"
    assert claimed.job_type == "ingest_url"
    assert claimed.status == "running"
    assert claimed.started_at is not None
    assert remaining is None
    assert fake_conn.commit_count == 2


@pytest.mark.asyncio
async def test_proposal_exists_is_true_for_both_pair_orderings(monkeypatch) -> None:
    db = Database(Path("/tmp/test.db"))
    captured: dict[str, tuple[object, ...]] = {}

    async def fake_fetch_one(sql: str, params: tuple[object, ...]) -> dict[str, int]:
        del sql
        captured["params"] = params
        return {"exists": 1}

    monkeypatch.setattr(db, "_fetch_one", fake_fetch_one)

    exists = await db.proposal_exists("a" * 64, "b" * 64)

    assert exists is True
    assert captured["params"] == ("a" * 64, "b" * 64, "b" * 64, "a" * 64)


@pytest.mark.asyncio
async def test_reject_proposal_records_rejected_pair_for_both_orderings() -> None:
    db = Database(Path("/tmp/test.db"))
    fake_conn = FakeConnection([FakeCursor(), FakeCursor(rowcount=1), FakeCursor()])
    db._conn = fake_conn

    rejected = await db.reject_proposal("proposal-reject1", "c" * 64, "d" * 64)

    assert rejected is True
    assert fake_conn.commit_count == 1
    assert fake_conn.rollback_count == 0
    assert fake_conn.execute_calls[0][0] == "BEGIN"
    assert "UPDATE proposals" in fake_conn.execute_calls[1][0]
    assert "INSERT OR REPLACE INTO rejected_pairs" in fake_conn.execute_calls[2][0]


@pytest.mark.asyncio
async def test_update_proposal_explanation_persists_rationale_and_link_type(
    monkeypatch,
) -> None:
    db = Database(Path("/tmp/test.db"))
    fake_conn = FakeConnection([])
    db._conn = fake_conn
    captured: dict[str, object] = {}

    async def fake_exec(sql: str, params: tuple[object, ...]) -> None:
        captured["sql"] = sql
        captured["params"] = params

    monkeypatch.setattr(db, "_exec", fake_exec)

    await db.update_proposal_explanation(
        "proposal-explain1",
        "Document E lays the conceptual groundwork that Document F applies.",
        "prerequisite",
    )

    assert "UPDATE proposals" in str(captured["sql"])
    assert captured["params"] == (
        "Document E lays the conceptual groundwork that Document F applies.",
        "prerequisite",
        "proposal-explain1",
    )
    assert fake_conn.commit_count == 1
