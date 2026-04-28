"""BGE reranker module for PKP."""

from __future__ import annotations

import logging
from typing import Any

from pkp.config import get_config

logger = logging.getLogger(__name__)

_LOCAL_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


class BGEReranker:
    """BGE reranker with lazy model loading."""

    def __init__(self, model_name: str | None = None) -> None:
        """Initialize reranker with optional model name."""
        self.model_name = model_name or _LOCAL_RERANKER_MODEL
        self._model: Any = None

    @property
    def enabled(self) -> bool:
        """Return whether reranking is enabled."""
        return get_config().reranker == "local"

    @property
    def model(self) -> Any:
        """Lazily load the BGE reranker model."""
        if self._model is None:
            from FlagEmbedding import FlagReranker

            self._model = FlagReranker(self.model_name, use_fp16=True)
        return self._model

    def compute_scores(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Compute normalized reranker scores for query-passage pairs."""
        if not self.enabled or not pairs:
            return []

        try:
            raw_scores = self.model.compute_score(
                [[query, passage] for query, passage in pairs],
                normalize=True,
            )
        except Exception:
            logger.warning(
                "reranker unavailable, falling back to Qdrant score ordering",
                exc_info=True,
            )
            return []

        if isinstance(raw_scores, int | float):
            return [float(raw_scores)]

        return [float(score) for score in raw_scores]


_reranker_instance: BGEReranker | None = None


def get_reranker() -> BGEReranker:
    """Get the global reranker instance."""
    global _reranker_instance
    if _reranker_instance is None:
        _reranker_instance = BGEReranker()
    return _reranker_instance
