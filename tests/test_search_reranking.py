from __future__ import annotations

from unittest.mock import Mock

import pytest

from pkp.storage.models import SearchResult
from pkp.storage.qdrant import _rerank_search_results


@pytest.mark.asyncio
async def test_rerank_search_results_orders_by_reranker_score(monkeypatch) -> None:
    async def fake_search_top_passage_for_document(
        query: str,
        target_doc_sha256: str,
        doc_type: str,
    ) -> str:
        del query, doc_type
        return f"passage:{target_doc_sha256[:6]}"

    mock_reranker = Mock(enabled=True)
    mock_reranker.compute_scores.return_value = [0.12, 0.91]

    results = [
        SearchResult(
            doc_sha256="a" * 64,
            title="Low",
            url="https://example.com/low",
            doc_type="article",
            match_count=1,
            best_rank=-0.8,
            raw_score=0.8,
        ),
        SearchResult(
            doc_sha256="b" * 64,
            title="High",
            url="https://example.com/high",
            doc_type="article",
            match_count=1,
            best_rank=-0.7,
            raw_score=0.7,
        ),
    ]

    monkeypatch.setattr(
        "pkp.storage.qdrant.search_top_passage_for_document",
        fake_search_top_passage_for_document,
    )
    monkeypatch.setattr("pkp.storage.qdrant.get_reranker", lambda: mock_reranker)

    reranked = await _rerank_search_results("chunking strategies for RAG", results)

    assert [result.title for result in reranked] == ["High", "Low"]
    assert reranked[0].reranker_score == pytest.approx(0.91)
    assert reranked[0].best_rank == pytest.approx(-0.91)
    assert reranked[1].reranker_score == pytest.approx(0.12)


@pytest.mark.asyncio
async def test_rerank_search_results_preserves_unscored_results_after_reranked_subset(
    monkeypatch,
) -> None:
    async def fake_search_top_passage_for_document(
        query: str,
        target_doc_sha256: str,
        doc_type: str,
    ) -> str | None:
        del query, doc_type
        if target_doc_sha256.startswith("a"):
            return "passage:a"
        return None

    mock_reranker = Mock(enabled=True)
    mock_reranker.compute_scores.return_value = [0.88]

    results = [
        SearchResult(
            doc_sha256="a" * 64,
            title="Scored",
            url="https://example.com/scored",
            doc_type="article",
            match_count=1,
            best_rank=-0.9,
            raw_score=0.9,
        ),
        SearchResult(
            doc_sha256="b" * 64,
            title="Unscored One",
            url="https://example.com/unscored-one",
            doc_type="article",
            match_count=1,
            best_rank=-0.8,
            raw_score=0.8,
        ),
        SearchResult(
            doc_sha256="c" * 64,
            title="Unscored Two",
            url="https://example.com/unscored-two",
            doc_type=None,
            match_count=1,
            best_rank=-0.7,
            raw_score=0.7,
        ),
    ]

    monkeypatch.setattr(
        "pkp.storage.qdrant.search_top_passage_for_document",
        fake_search_top_passage_for_document,
    )
    monkeypatch.setattr("pkp.storage.qdrant.get_reranker", lambda: mock_reranker)

    reranked = await _rerank_search_results("chunking strategies for RAG", results)

    assert [result.title for result in reranked] == [
        "Scored",
        "Unscored One",
        "Unscored Two",
    ]
    assert reranked[0].reranker_score == pytest.approx(0.88)


@pytest.mark.asyncio
async def test_rerank_search_results_returns_original_when_disabled(
    monkeypatch,
) -> None:
    results = [
        SearchResult(
            doc_sha256="a" * 64,
            title="Original First",
            url="https://example.com/first",
            doc_type="article",
            match_count=1,
            best_rank=-0.8,
            raw_score=0.8,
        ),
        SearchResult(
            doc_sha256="b" * 64,
            title="Original Second",
            url="https://example.com/second",
            doc_type="article",
            match_count=1,
            best_rank=-0.7,
            raw_score=0.7,
        ),
    ]

    monkeypatch.setattr("pkp.storage.qdrant.get_reranker", lambda: Mock(enabled=False))

    reranked = await _rerank_search_results("chunking strategies for RAG", results)

    assert reranked == results
