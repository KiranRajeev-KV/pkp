from __future__ import annotations

from datetime import UTC, datetime

import pytest


@pytest.mark.asyncio
async def test_insert_document_round_trips_core_fields(
    db,
    document_factory,
) -> None:
    doc = document_factory(
        sha256="1" * 64,
        title="Round Trip Document",
        doc_type="pdf",
        archive_path="/tmp/archive/round-trip",
        retrieved_at=datetime(2026, 4, 26, tzinfo=UTC),
    )

    await db.insert_document(doc)
    stored = await db.get_document(doc.sha256)

    assert stored is not None
    assert stored.sha256 == doc.sha256
    assert stored.title == doc.title
    assert stored.doc_type == doc.doc_type


@pytest.mark.asyncio
async def test_claim_next_pending_job_claims_once_and_then_exhausts_queue(db) -> None:
    await db.create_job("job-1", "ingest_url", {"url": "https://example.com"})

    claimed = await db.claim_next_pending_job()
    remaining = await db.claim_next_pending_job()

    assert claimed is not None
    assert claimed.job_id == "job-1"
    assert claimed.job_type == "ingest_url"
    assert claimed.status == "running"
    assert claimed.started_at is not None
    assert remaining is None


@pytest.mark.asyncio
async def test_proposal_exists_is_true_for_both_pair_orderings(
    db,
    document_factory,
    proposal_factory,
) -> None:
    doc_a = document_factory(sha256="a" * 64, title="Document A")
    doc_b = document_factory(sha256="b" * 64, title="Document B")
    await db.insert_document(doc_a)
    await db.insert_document(doc_b)

    proposal = proposal_factory(
        proposal_id="proposal-abcd1234",
        doc_a_sha256=doc_a.sha256,
        doc_b_sha256=doc_b.sha256,
    )
    await db.insert_proposal(proposal)

    assert await db.proposal_exists(doc_a.sha256, doc_b.sha256) is True
    assert await db.proposal_exists(doc_b.sha256, doc_a.sha256) is True


@pytest.mark.asyncio
async def test_reject_proposal_records_rejected_pair_for_both_orderings(
    db,
    document_factory,
    proposal_factory,
) -> None:
    doc_a = document_factory(sha256="c" * 64, title="Document C")
    doc_b = document_factory(sha256="d" * 64, title="Document D")
    await db.insert_document(doc_a)
    await db.insert_document(doc_b)

    proposal = proposal_factory(
        proposal_id="proposal-reject1",
        doc_a_sha256=doc_a.sha256,
        doc_b_sha256=doc_b.sha256,
        status="pending",
    )
    await db.insert_proposal(proposal)

    rejected = await db.reject_proposal(
        proposal.proposal_id,
        proposal.doc_a_sha256,
        proposal.doc_b_sha256,
    )
    updated = await db.get_proposal(proposal.proposal_id)

    assert rejected is True
    assert updated is not None
    assert updated.status == "rejected"
    assert updated.reviewed_at is not None
    assert await db.rejected_pair_exists(doc_a.sha256, doc_b.sha256) is True
    assert await db.rejected_pair_exists(doc_b.sha256, doc_a.sha256) is True
