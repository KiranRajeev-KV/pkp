"""BGE-M3 embedding module for PKP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import numpy as np

from pkp.config import get_config


@dataclass
class EmbeddingResult:
    """Result from embedding operation."""

    dense: np.ndarray
    sparse: list[dict[str, float]]


class BGEEmbedder:
    """BGE-M3 embedder with lazy model loading."""

    def __init__(
        self, model_name: str | None = None, batch_size: int | None = None
    ) -> None:
        """Initialize embedder with optional model name and batch size."""
        config = get_config()
        self.model_name = model_name or config.embedding_model
        self.batch_size = batch_size or config.embed_batch_size
        self._model: Any = None
        self._tokenizer: Any = None

    @property
    def model(self) -> Any:
        """Lazily load the BGE-M3 model."""
        if self._model is None:
            from FlagEmbedding import BGEM3FlagModel

            self._model = BGEM3FlagModel(self.model_name, use_fp16=True)
        return self._model

    @property
    def tokenizer(self) -> Any:
        """Lazily get the tokenizer."""
        if self._tokenizer is None:
            self._tokenizer = self.model.tokenizer
        return self._tokenizer

    def embed_chunks(self, texts: list[str]) -> list[EmbeddingResult]:
        """Embed a list of text chunks, returning dense + sparse vectors.

        Args:
            texts: List of text strings to embed.

        Returns:
            List of EmbeddingResult, one per input text.
        """
        if not texts:
            return []

        output = self.model.encode(
            texts,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
            batch_size=self.batch_size,
        )

        dense_vectors = output["dense_vecs"]
        lexical_weights = output["lexical_weights"]

        results: list[EmbeddingResult] = []
        for dense, sparse in zip(dense_vectors, lexical_weights, strict=True):
            results.append(
                EmbeddingResult(
                    dense=np.array(dense, dtype=np.float32),
                    sparse=sparse if isinstance(sparse, list) else [sparse],
                )
            )

        return results

    def embed_query(self, text: str) -> np.ndarray:
        """Embed a query string for dense search only.

        Args:
            text: Query text to embed.

        Returns:
            Dense embedding vector (1024 dimensions).
        """
        output = self.model.encode(
            [text],
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
            batch_size=1,
        )

        return np.array(output["dense_vecs"][0], dtype=np.float32)

    def embed_query_sparse(self, text: str) -> dict[str, float]:  # type: ignore[no-any-return]
        """Embed a query string for sparse (lexical) search.

        Args:
            text: Query text to embed.

        Returns:
            Dictionary mapping tokens to weights.
        """
        output = self.model.encode(
            [text],
            return_dense=False,
            return_sparse=True,
            return_colbert_vecs=False,
            batch_size=1,
        )

        lexical: Any = output["lexical_weights"]
        if isinstance(lexical, list):
            return cast(dict[str, float], lexical[0])
        return cast(dict[str, float], lexical)

    def tokens_to_indices(
        self, text: str, lexical_weights: dict[str, float]
    ) -> tuple[list[int], list[float]]:
        """Convert token weights to Qdrant sparse vector format.

        Args:
            text: Original text that was encoded.
            lexical_weights: Dictionary of {token_id: weight} from BGE-M3.
            Keys are token IDs (strings like '3034'), not string tokens.

        Returns:
            Tuple of (indices, values) for Qdrant SparseVector.
        """
        tokens = self.tokenizer(
            [text],
            padding=True,
            truncation=True,
            max_length=8192,
            return_tensors=None,
        )

        input_ids = tokens["input_ids"]
        attention_mask = tokens["attention_mask"]

        token_weights: dict[int, float] = {}
        for i, token_id in enumerate(input_ids[0]):
            if attention_mask[0][i] == 0:
                break

            token_id_str = str(token_id)
            if token_id_str in lexical_weights:
                weight = float(lexical_weights[token_id_str])
                if weight > 0:
                    if token_id in token_weights:
                        token_weights[token_id] = max(token_weights[token_id], weight)
                    else:
                        token_weights[token_id] = weight

        indices = list(token_weights.keys())
        values = list(token_weights.values())
        return indices, values


_embedder_instance: BGEEmbedder | None = None


def get_embedder() -> BGEEmbedder:
    """Get the global embedder instance."""
    global _embedder_instance
    if _embedder_instance is None:
        _embedder_instance = BGEEmbedder()
    return _embedder_instance
