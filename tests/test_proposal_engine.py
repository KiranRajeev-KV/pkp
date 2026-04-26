from __future__ import annotations

from pathlib import Path

import pytest

from pkp.pipeline.proposals import _strip_leading_frontmatter, generate_proposals
from pkp.storage.models import SearchResult

DOUBLE_FRONTMATTER_TEXT = """---
title: Antarctica - Wikipedia
author: Authority control databases
url: https://en.wikipedia.org/wiki/Antarctica
hostname: wikipedia.org
---
---
doc_type: article
retrieved_at: "2026-04-25"
pkp_version: "1"
---

# Antarctica

Antarctica is, on average, the coldest, driest, and windiest of the continents.
"""


def _write_normalized_archive(
    archive_root: Path,
    sha256: str,
    content: str,
) -> None:
    doc_dir = archive_root / sha256
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "normalized.md").write_text(content, encoding="utf-8")


def test_strip_leading_frontmatter_removes_both_leading_yaml_blocks() -> None:
    stripped = _strip_leading_frontmatter(DOUBLE_FRONTMATTER_TEXT)

    assert stripped.startswith("# Antarctica")
    assert not stripped.startswith("---")


@pytest.mark.asyncio
async def test_generate_proposals_stores_raw_qdrant_score_without_inversion(
    db,
    monkeypatch,
    test_config,
    document_factory,
) -> None:
    assert test_config.archive_path is not None

    async def fake_search_documents_hybrid(
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        del query, limit
        return [
            SearchResult(
                doc_sha256=high_raw_doc.sha256,
                title=high_raw_doc.title,
                url=high_raw_doc.url,
                match_count=1,
                best_rank=-100.0,
                raw_score=0.9,
            ),
            SearchResult(
                doc_sha256=low_raw_doc.sha256,
                title=low_raw_doc.title,
                url=low_raw_doc.url,
                match_count=1,
                best_rank=-0.01,
                raw_score=0.2,
            ),
        ]

    source_doc = document_factory(
        sha256="1" * 64,
        title="Source Document",
        archive_path=str(test_config.archive_path / ("1" * 64)),
    )
    high_raw_doc = document_factory(
        sha256="2" * 64,
        title="High Raw Candidate",
        archive_path=str(test_config.archive_path / ("2" * 64)),
    )
    low_raw_doc = document_factory(
        sha256="3" * 64,
        title="Low Raw Candidate",
        archive_path=str(test_config.archive_path / ("3" * 64)),
    )

    for doc in (source_doc, high_raw_doc, low_raw_doc):
        await db.insert_document(doc)

    _write_normalized_archive(
        test_config.archive_path,
        source_doc.sha256,
        (
            "---\n"
            'doc_type: "article"\n'
            'retrieved_at: "2026-04-25"\n'
            'pkp_version: "1"\n'
            "---\n\n"
            "# Source Document\n\n"
            "This body is used to build a proposal query."
        ),
    )

    monkeypatch.setattr("pkp.pipeline.proposals.get_config", lambda: test_config)
    monkeypatch.setattr("pkp.storage.db.get_config", lambda: test_config)
    monkeypatch.setattr("pkp.pipeline.proposals.qdrant_available", lambda: True)
    monkeypatch.setattr(
        "pkp.pipeline.proposals.search_documents_hybrid",
        fake_search_documents_hybrid,
    )

    inserted_count = await generate_proposals(source_doc.sha256)
    proposals = await db.get_proposals_by_status("pending", limit=10)
    scores_by_target = {proposal.doc_b_sha256: proposal.score for proposal in proposals}

    assert inserted_count == 2
    assert scores_by_target[high_raw_doc.sha256] == pytest.approx(0.9)
    assert scores_by_target[low_raw_doc.sha256] == pytest.approx(0.2)
    assert scores_by_target[high_raw_doc.sha256] > scores_by_target[low_raw_doc.sha256]


@pytest.mark.asyncio
async def test_generate_proposals_is_deduplicated_on_second_run(
    db,
    monkeypatch,
    test_config,
    document_factory,
) -> None:
    assert test_config.archive_path is not None

    async def fake_search_documents_hybrid(
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        del query, limit
        return [
            SearchResult(
                doc_sha256=candidate_one.sha256,
                title=candidate_one.title,
                url=candidate_one.url,
                match_count=1,
                best_rank=-1.0,
                raw_score=0.8,
            ),
            SearchResult(
                doc_sha256=candidate_two.sha256,
                title=candidate_two.title,
                url=candidate_two.url,
                match_count=1,
                best_rank=-2.0,
                raw_score=0.7,
            ),
        ]

    source_doc = document_factory(
        sha256="4" * 64,
        title="Dedup Source",
        archive_path=str(test_config.archive_path / ("4" * 64)),
    )
    candidate_one = document_factory(
        sha256="5" * 64,
        title="Candidate One",
        archive_path=str(test_config.archive_path / ("5" * 64)),
    )
    candidate_two = document_factory(
        sha256="6" * 64,
        title="Candidate Two",
        archive_path=str(test_config.archive_path / ("6" * 64)),
    )

    for doc in (source_doc, candidate_one, candidate_two):
        await db.insert_document(doc)

    _write_normalized_archive(
        test_config.archive_path,
        source_doc.sha256,
        (
            "---\n"
            'doc_type: "article"\n'
            'retrieved_at: "2026-04-25"\n'
            'pkp_version: "1"\n'
            "---\n\n"
            "# Dedup Source\n\n"
            "Proposal generation should not create duplicate rows."
        ),
    )

    monkeypatch.setattr("pkp.pipeline.proposals.get_config", lambda: test_config)
    monkeypatch.setattr("pkp.storage.db.get_config", lambda: test_config)
    monkeypatch.setattr("pkp.pipeline.proposals.qdrant_available", lambda: True)
    monkeypatch.setattr(
        "pkp.pipeline.proposals.search_documents_hybrid",
        fake_search_documents_hybrid,
    )

    first_inserted = await generate_proposals(source_doc.sha256)
    proposals_after_first = await db.get_proposals_by_status("pending", limit=10)
    second_inserted = await generate_proposals(source_doc.sha256)
    proposals_after_second = await db.get_proposals_by_status("pending", limit=10)

    assert first_inserted == 2
    assert len(proposals_after_first) == 2
    assert second_inserted == 0
    assert len(proposals_after_second) == 2
