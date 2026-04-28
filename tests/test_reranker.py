from __future__ import annotations

from unittest.mock import Mock

from pkp.reranker import BGEReranker


def test_reranker_returns_empty_when_disabled(monkeypatch, test_config) -> None:
    test_config.reranker = "none"
    monkeypatch.setattr("pkp.reranker.get_config", lambda: test_config)

    reranker = BGEReranker()

    assert reranker.enabled is False
    assert reranker.compute_scores([("query", "passage")]) == []


def test_reranker_scores_pairs_with_mock_model(monkeypatch, test_config) -> None:
    test_config.reranker = "local"
    monkeypatch.setattr("pkp.reranker.get_config", lambda: test_config)

    reranker = BGEReranker()
    reranker._model = Mock()
    reranker._model.compute_score.return_value = [0.81, 0.12]

    scores = reranker.compute_scores(
        [
            ("what is RAG?", "RAG combines retrieval and generation"),
            ("what is RAG?", "Samurai were warriors"),
        ]
    )

    assert scores == [0.81, 0.12]
    reranker._model.compute_score.assert_called_once_with(
        [
            ["what is RAG?", "RAG combines retrieval and generation"],
            ["what is RAG?", "Samurai were warriors"],
        ],
        normalize=True,
    )


def test_reranker_returns_empty_on_model_failure(monkeypatch, test_config) -> None:
    test_config.reranker = "local"
    monkeypatch.setattr("pkp.reranker.get_config", lambda: test_config)

    reranker = BGEReranker()
    reranker._model = Mock()
    reranker._model.compute_score.side_effect = RuntimeError("model failure")

    assert reranker.compute_scores([("query", "passage")]) == []
