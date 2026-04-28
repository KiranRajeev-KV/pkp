from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from pkp.pipeline.proposals import (
    ProposalCandidate,
    _rerank_candidates,
    _strip_leading_frontmatter,
    generate_proposals,
)
from pkp.storage.models import Document, Proposal, SearchResult

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


class FakeProposalDB:
    """Fake proposal database for testing."""

    def __init__(self, documents: list[Document]) -> None:
        self.documents = {document.sha256: document for document in documents}
        self.proposals: list[Proposal] = []
        self.rejected_pairs: set[tuple[str, str]] = set()

    async def get_document(self, sha256: str) -> Document | None:
        """Get a document by its SHA256."""
        return self.documents.get(sha256)

    async def proposal_exists(self, doc_a_sha256: str, doc_b_sha256: str) -> bool:
        """Check if a proposal exists for the given document pair."""
        return any(
            {
                proposal.doc_a_sha256,
                proposal.doc_b_sha256,
            }
            == {doc_a_sha256, doc_b_sha256}
            for proposal in self.proposals
        )

    async def rejected_pair_exists(self, doc_a_sha256: str, doc_b_sha256: str) -> bool:
        """Check if a rejected pair exists."""
        return (doc_a_sha256, doc_b_sha256) in self.rejected_pairs or (
            doc_b_sha256,
            doc_a_sha256,
        ) in self.rejected_pairs

    async def insert_proposal(self, proposal: Proposal) -> None:
        """Insert a new proposal."""
        self.proposals.append(proposal)

    async def get_proposals_by_status(
        self,
        status: str,
        limit: int,
    ) -> list[Proposal]:
        """Get proposals filtered by status."""
        return [proposal for proposal in self.proposals if proposal.status == status][
            :limit
        ]


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
    monkeypatch,
    test_config,
    document_factory,
) -> None:
    assert test_config.archive_path is not None
    test_config.reranker = "none"

    async def fake_search_top_passage_for_document(
        *args: object, **kwargs: object
    ) -> None:
        del args, kwargs
        return None

    async def fake_search_documents_hybrid(
        query: str,
        limit: int,
        *,
        rerank: bool = True,
    ) -> list[SearchResult]:
        del query, limit, rerank
        return [
            SearchResult(
                doc_sha256=high_raw_doc.sha256,
                title=high_raw_doc.title,
                url=high_raw_doc.url,
                doc_type=high_raw_doc.doc_type,
                match_count=1,
                best_rank=-100.0,
                raw_score=0.9,
            ),
            SearchResult(
                doc_sha256=low_raw_doc.sha256,
                title=low_raw_doc.title,
                url=low_raw_doc.url,
                doc_type=low_raw_doc.doc_type,
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
    database = FakeProposalDB([source_doc, high_raw_doc, low_raw_doc])

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

    @asynccontextmanager
    async def fake_db_context() -> object:
        yield database

    monkeypatch.setattr("pkp.pipeline.proposals.db_context", fake_db_context)
    monkeypatch.setattr(
        "pkp.pipeline.proposals.search_documents_hybrid",
        fake_search_documents_hybrid,
    )
    monkeypatch.setattr(
        "pkp.pipeline.proposals.search_top_passage_for_document",
        fake_search_top_passage_for_document,
    )

    inserted_count = await generate_proposals(source_doc.sha256)
    proposals = await database.get_proposals_by_status("pending", limit=10)
    scores_by_target = {proposal.doc_b_sha256: proposal.score for proposal in proposals}

    assert inserted_count == 2
    assert scores_by_target[high_raw_doc.sha256] == pytest.approx(0.9)
    assert scores_by_target[low_raw_doc.sha256] == pytest.approx(0.2)
    assert scores_by_target[high_raw_doc.sha256] > scores_by_target[low_raw_doc.sha256]


@pytest.mark.asyncio
async def test_generate_proposals_is_deduplicated_on_second_run(
    monkeypatch,
    test_config,
    document_factory,
) -> None:
    assert test_config.archive_path is not None
    test_config.reranker = "none"

    async def fake_search_top_passage_for_document(
        *args: object, **kwargs: object
    ) -> None:
        del args, kwargs
        return None

    async def fake_search_documents_hybrid(
        query: str,
        limit: int,
        *,
        rerank: bool = True,
    ) -> list[SearchResult]:
        del query, limit, rerank
        return [
            SearchResult(
                doc_sha256=candidate_one.sha256,
                title=candidate_one.title,
                url=candidate_one.url,
                doc_type=candidate_one.doc_type,
                match_count=1,
                best_rank=-1.0,
                raw_score=0.8,
            ),
            SearchResult(
                doc_sha256=candidate_two.sha256,
                title=candidate_two.title,
                url=candidate_two.url,
                doc_type=candidate_two.doc_type,
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
    database = FakeProposalDB([source_doc, candidate_one, candidate_two])

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

    @asynccontextmanager
    async def fake_db_context() -> object:
        yield database

    monkeypatch.setattr("pkp.pipeline.proposals.db_context", fake_db_context)
    monkeypatch.setattr(
        "pkp.pipeline.proposals.search_documents_hybrid",
        fake_search_documents_hybrid,
    )
    monkeypatch.setattr(
        "pkp.pipeline.proposals.search_top_passage_for_document",
        fake_search_top_passage_for_document,
    )

    first_inserted = await generate_proposals(source_doc.sha256)
    proposals_after_first = await database.get_proposals_by_status("pending", limit=10)
    second_inserted = await generate_proposals(source_doc.sha256)
    proposals_after_second = await database.get_proposals_by_status("pending", limit=10)

    assert first_inserted == 2
    assert len(proposals_after_first) == 2
    assert second_inserted == 0
    assert len(proposals_after_second) == 2


def test_rerank_candidates_orders_by_reranker_score_and_drops_below_threshold(
    monkeypatch,
    document_factory,
) -> None:
    candidate_doc = document_factory(sha256="7" * 64, title="Candidate Document")
    unscored_candidate = ProposalCandidate(
        result=SearchResult(
            doc_sha256="8" * 64,
            title="Unscored Candidate",
            url="https://example.com/high",
            doc_type="article",
            match_count=1,
            best_rank=-0.5,
            raw_score=0.5,
        ),
        score=0.5,
        candidate_doc=candidate_doc,
        passage_a=None,
        passage_b=None,
    )
    high_candidate = ProposalCandidate(
        result=SearchResult(
            doc_sha256="7" * 64,
            title="High Candidate",
            url="https://example.com/high",
            doc_type="article",
            match_count=1,
            best_rank=-0.45,
            raw_score=0.45,
        ),
        score=0.45,
        candidate_doc=candidate_doc,
        passage_a="source high",
        passage_b="candidate high",
    )
    low_candidate = ProposalCandidate(
        result=SearchResult(
            doc_sha256="9" * 64,
            title="Low Candidate",
            url="https://example.com/low",
            doc_type="article",
            match_count=1,
            best_rank=-0.4,
            raw_score=0.4,
        ),
        score=0.4,
        candidate_doc=candidate_doc,
        passage_a="source low",
        passage_b="candidate low",
    )

    mock_reranker = Mock(enabled=True)
    mock_reranker.compute_scores.return_value = [0.82, 0.009]
    monkeypatch.setattr(
        "pkp.pipeline.proposals.get_reranker",
        lambda: mock_reranker,
    )

    ranked = _rerank_candidates(
        [low_candidate, high_candidate, unscored_candidate],
        reranker_min_score=0.01,
    )

    assert [candidate.result.title for candidate in ranked] == [
        "Low Candidate",
        "Unscored Candidate",
    ]
    assert ranked[0].score == pytest.approx(0.82)


@pytest.mark.asyncio
async def test_generate_proposals_uses_reranker_scores_and_drops_low_candidates(
    monkeypatch,
    test_config,
    document_factory,
) -> None:
    assert test_config.archive_path is not None
    test_config.reranker = "local"
    test_config.reranker_min_score = 0.5

    async def fake_search_documents_hybrid(
        query: str,
        limit: int,
        *,
        rerank: bool = True,
    ) -> list[SearchResult]:
        del query, limit
        assert rerank is False
        return [
            SearchResult(
                doc_sha256=first_candidate.sha256,
                title=first_candidate.title,
                url=first_candidate.url,
                doc_type=first_candidate.doc_type,
                match_count=1,
                best_rank=-1.0,
                raw_score=0.6,
            ),
            SearchResult(
                doc_sha256=second_candidate.sha256,
                title=second_candidate.title,
                url=second_candidate.url,
                doc_type=second_candidate.doc_type,
                match_count=1,
                best_rank=-2.0,
                raw_score=0.55,
            ),
            SearchResult(
                doc_sha256=third_candidate.sha256,
                title=third_candidate.title,
                url=third_candidate.url,
                doc_type=third_candidate.doc_type,
                match_count=1,
                best_rank=-3.0,
                raw_score=0.52,
            ),
        ]

    async def fake_search_top_passage_for_document(
        query: str,
        target_doc_sha256: str,
        doc_type: str,
    ) -> str:
        del query, doc_type
        return f"passage:{target_doc_sha256[:6]}"

    source_doc = document_factory(
        sha256="1" * 64,
        title="Source Document",
        archive_path=str(test_config.archive_path / ("1" * 64)),
    )
    first_candidate = document_factory(
        sha256="2" * 64,
        title="First Candidate",
        archive_path=str(test_config.archive_path / ("2" * 64)),
    )
    second_candidate = document_factory(
        sha256="3" * 64,
        title="Second Candidate",
        archive_path=str(test_config.archive_path / ("3" * 64)),
    )
    third_candidate = document_factory(
        sha256="4" * 64,
        title="Third Candidate",
        archive_path=str(test_config.archive_path / ("4" * 64)),
    )
    database = FakeProposalDB(
        [source_doc, first_candidate, second_candidate, third_candidate]
    )
    for doc in (source_doc, first_candidate, second_candidate, third_candidate):
        _write_normalized_archive(
            test_config.archive_path,
            doc.sha256,
            f"# {doc.title}\n\nBody for {doc.title}.",
        )

    mock_reranker = Mock(enabled=True)
    mock_reranker.compute_scores.return_value = [0.65, 0.91, 0.2]

    monkeypatch.setattr("pkp.pipeline.proposals.get_config", lambda: test_config)
    monkeypatch.setattr("pkp.storage.db.get_config", lambda: test_config)
    monkeypatch.setattr("pkp.pipeline.proposals.qdrant_available", lambda: True)

    @asynccontextmanager
    async def fake_db_context() -> object:
        yield database

    monkeypatch.setattr("pkp.pipeline.proposals.db_context", fake_db_context)
    monkeypatch.setattr(
        "pkp.pipeline.proposals.search_documents_hybrid",
        fake_search_documents_hybrid,
    )
    monkeypatch.setattr(
        "pkp.pipeline.proposals.search_top_passage_for_document",
        fake_search_top_passage_for_document,
    )
    monkeypatch.setattr(
        "pkp.pipeline.proposals.get_reranker",
        lambda: mock_reranker,
    )

    inserted_count = await generate_proposals(source_doc.sha256)
    proposals = await database.get_proposals_by_status("pending", limit=10)

    assert inserted_count == 2
    assert [proposal.doc_b_sha256 for proposal in proposals] == [
        second_candidate.sha256,
        first_candidate.sha256,
    ]
    assert [proposal.score for proposal in proposals] == [
        pytest.approx(0.91),
        pytest.approx(0.65),
    ]
    assert third_candidate.sha256 not in {
        proposal.doc_b_sha256 for proposal in proposals
    }


@pytest.mark.asyncio
async def test_generate_proposals_preserves_candidates_without_passages(
    monkeypatch,
    test_config,
    document_factory,
) -> None:
    assert test_config.archive_path is not None
    test_config.reranker = "local"
    test_config.reranker_min_score = 0.5

    async def fake_search_documents_hybrid(
        query: str,
        limit: int,
        *,
        rerank: bool = True,
    ) -> list[SearchResult]:
        del query, limit
        assert rerank is False
        return [
            SearchResult(
                doc_sha256=scored_candidate.sha256,
                title=scored_candidate.title,
                url=scored_candidate.url,
                doc_type=scored_candidate.doc_type,
                match_count=1,
                best_rank=-1.0,
                raw_score=0.7,
            ),
            SearchResult(
                doc_sha256=unscored_candidate.sha256,
                title=unscored_candidate.title,
                url=unscored_candidate.url,
                doc_type=unscored_candidate.doc_type,
                match_count=1,
                best_rank=-0.9,
                raw_score=0.6,
            ),
        ]

    async def fake_search_top_passage_for_document(
        query: str,
        target_doc_sha256: str,
        doc_type: str,
    ) -> str | None:
        del query, doc_type
        if target_doc_sha256 in {source_doc.sha256, scored_candidate.sha256}:
            return f"passage:{target_doc_sha256[:6]}"
        return None

    source_doc = document_factory(
        sha256="a" * 64,
        title="Source Document",
        archive_path=str(test_config.archive_path / ("a" * 64)),
    )
    scored_candidate = document_factory(
        sha256="b" * 64,
        title="Scored Candidate",
        archive_path=str(test_config.archive_path / ("b" * 64)),
    )
    unscored_candidate = document_factory(
        sha256="c" * 64,
        title="Unscored Candidate",
        archive_path=str(test_config.archive_path / ("c" * 64)),
    )
    database = FakeProposalDB([source_doc, scored_candidate, unscored_candidate])

    for doc in (source_doc, scored_candidate, unscored_candidate):
        _write_normalized_archive(
            test_config.archive_path,
            doc.sha256,
            f"# {doc.title}\n\nBody for {doc.title}.",
        )

    mock_reranker = Mock(enabled=True)
    mock_reranker.compute_scores.return_value = [0.87]

    monkeypatch.setattr("pkp.pipeline.proposals.get_config", lambda: test_config)
    monkeypatch.setattr("pkp.storage.db.get_config", lambda: test_config)
    monkeypatch.setattr("pkp.pipeline.proposals.qdrant_available", lambda: True)

    @asynccontextmanager
    async def fake_db_context() -> object:
        yield database

    monkeypatch.setattr("pkp.pipeline.proposals.db_context", fake_db_context)
    monkeypatch.setattr(
        "pkp.pipeline.proposals.search_documents_hybrid",
        fake_search_documents_hybrid,
    )
    monkeypatch.setattr(
        "pkp.pipeline.proposals.search_top_passage_for_document",
        fake_search_top_passage_for_document,
    )
    monkeypatch.setattr(
        "pkp.pipeline.proposals.get_reranker",
        lambda: mock_reranker,
    )

    inserted_count = await generate_proposals(source_doc.sha256)
    proposals = await database.get_proposals_by_status("pending", limit=10)

    assert inserted_count == 2
    assert [proposal.doc_b_sha256 for proposal in proposals] == [
        scored_candidate.sha256,
        unscored_candidate.sha256,
    ]
    assert proposals[0].score == pytest.approx(0.87)
    assert proposals[1].score == pytest.approx(0.6)
