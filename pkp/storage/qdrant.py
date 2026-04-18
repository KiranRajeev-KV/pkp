"""Qdrant vector database layer for PKP."""

from __future__ import annotations

from typing import Any

from pkp.config import get_config

from .db import db_context
from .models import SearchResult


def _get_collection_name(doc_type: str) -> str:
    """Get Qdrant collection name for a doc_type."""
    return f"{doc_type}_v1"


def _get_qdrant_client() -> Any:
    """Get or create Qdrant client instance."""
    config = get_config()
    from qdrant_client import QdrantClient

    return QdrantClient(url=config.qdrant_url)


def _warn_dimension_mismatch(config_vector_size: int) -> None:
    """Warn if actual embedding dimension differs from config."""
    import click

    try:
        from pkp.embedder import get_embedder

        embedder = get_embedder()
        actual_dim = len(embedder.embed_query("test"))
        if actual_dim != config_vector_size:
            click.echo(
                f"WARNING: embedding dimension mismatch: config={config_vector_size}, actual={actual_dim}. "
                f"Using config value. Update config.embedding_dimension to {actual_dim} to silence this warning.",
                err=True,
            )
    except Exception:
        pass


def ensure_collection(doc_type: str) -> bool:
    """Ensure Qdrant collection exists for doc_type, create if needed.

    Returns:
        True if collection was created, False if it already existed.
    """
    from qdrant_client import models

    config = get_config()
    collection_name = _get_collection_name(doc_type)
    client = _get_qdrant_client()

    vector_size = config.embedding_dimension

    _warn_dimension_mismatch(vector_size)

    collections = client.get_collections().collections
    exists = any(c.name == collection_name for c in collections)

    if not exists:
        client.create_collection(
            collection_name=collection_name,
            vectors_config={
                "dense": models.VectorParams(
                    size=vector_size,
                    distance=models.Distance.COSINE,
                )
            },
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )
    return not exists


def qdrant_available() -> bool:
    """Check if Qdrant is available."""
    try:
        client = _get_qdrant_client()
        client.get_collections()
        return True
    except Exception:
        return False


def upsert_vectors(
    doc_type: str,
    chunk_ids: list[str],
    dense_vectors: list[list[float]],
    sparse_data: list[tuple[list[int], list[float]]],
    payloads: list[dict[str, Any]],
) -> None:
    """Upsert vectors into Qdrant.

    Args:
        doc_type: Document type.
        chunk_ids: List of chunk IDs.
        dense_vectors: Dense vectors.
        sparse_data: List of (indices, values) tuples.
        payloads: List of payload dicts.
    """
    from qdrant_client import models

    collection_name = _get_collection_name(doc_type)
    client = _get_qdrant_client()

    points = []
    for i, chunk_id in enumerate(chunk_ids):
        sparse_indices, sparse_values = sparse_data[i]

        if not sparse_indices:
            point = models.PointStruct(
                id=i,
                vector=dense_vectors[i],
                payload={**payloads[i], "chunk_id": chunk_id},
            )
        else:
            point = models.PointStruct(
                id=i,
                vector={
                    "dense": dense_vectors[i],
                    "sparse": models.SparseVector(
                        indices=sparse_indices,
                        values=sparse_values,
                    ),
                },
                payload={**payloads[i], "chunk_id": chunk_id},
            )
        points.append(point)

    client.upsert(
        collection_name=collection_name,
        points=points,
    )


async def search_qdrant(
    query_dense: list[float],
    query_sparse: tuple[list[int], list[float]],
    limit: int = 10,
    doc_type: str | None = None,
) -> list[SearchResult]:
    """Search Qdrant using hybrid RRF.

    Args:
        query_dense: Dense query vector.
        query_sparse: (indices, values) for sparse query.
        limit: Max results.
        doc_type: Optional doc_type to filter, or None for all.

    Returns:
        List of SearchResult.
    """
    from qdrant_client import models

    async with db_context() as db:
        if doc_type is not None:
            doc_types = [doc_type]
        else:
            doc_types = await db.get_distinct_doc_types()

    prefetch_limit = min(limit * 2, 100)

    if doc_type is not None:
        collection_name = _get_collection_name(doc_type)
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="doc_type",
                    match=models.MatchValue(value=doc_type),
                )
            ]
        )

        client = _get_qdrant_client()
        query_sparse_vec = models.SparseVector(
            indices=query_sparse[0],
            values=query_sparse[1],
        )

        dense_prefetch = models.Prefetch(
            query=query_dense,
            using="dense",
            limit=prefetch_limit,
            filter=query_filter,
        )

        sparse_prefetch = models.Prefetch(
            query=query_sparse_vec,
            using="sparse",
            limit=prefetch_limit,
            filter=query_filter,
        )

        results = client.query_points(
            collection_name=collection_name,
            prefetch=[dense_prefetch, sparse_prefetch],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            with_payload=True,
            limit=limit,
        )

        seen_docs: dict[str, SearchResult] = {}
        for point in results.result:
            doc_sha = point.payload.get("doc_sha256", "")
            if not doc_sha:
                continue

            if doc_sha in seen_docs:
                seen_docs[doc_sha].match_count += 1
            else:
                seen_docs[doc_sha] = SearchResult(
                    doc_sha256=doc_sha,
                    title=point.payload.get("title", ""),
                    url=point.payload.get("url"),
                    match_count=1,
                    best_rank=-point.score,
                )

        return list(seen_docs.values())[:limit]

    results_by_doc: dict[str, SearchResult] = {}

    for dtype in doc_types:
        try:
            collection_name = _get_collection_name(dtype)
            client = _get_qdrant_client()
        except Exception:
            break

        try:
            query_sparse_vec = models.SparseVector(
                indices=query_sparse[0],
                values=query_sparse[1],
            )

            dense_prefetch = models.Prefetch(
                query=query_dense,
                using="dense",
                limit=prefetch_limit,
            )

            sparse_prefetch = models.Prefetch(
                query=query_sparse_vec,
                using="sparse",
                limit=prefetch_limit,
            )

            results = client.query_points(
                collection_name=collection_name,
                prefetch=[dense_prefetch, sparse_prefetch],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                with_payload=True,
                limit=prefetch_limit,
            )
        except Exception:
            continue

        for point in results.result:
            doc_sha = point.payload.get("doc_sha256", "")
            if not doc_sha:
                continue

            if doc_sha in results_by_doc:
                results_by_doc[doc_sha].match_count += 1
            else:
                results_by_doc[doc_sha] = SearchResult(
                    doc_sha256=doc_sha,
                    title=point.payload.get("title", ""),
                    url=point.payload.get("url"),
                    match_count=1,
                    best_rank=-point.score,
                )

    sorted_results = sorted(
        results_by_doc.values(),
        key=lambda r: r.best_rank,
    )
    return sorted_results[:limit]


async def search_documents_hybrid(query: str, limit: int = 10) -> list[SearchResult]:
    """Try Qdrant first, fallback to FTS5 on connection error.

    Args:
        query: Search query.
        limit: Max results.

    Returns:
        List of SearchResult.
    """
    if not qdrant_available():
        async with db_context() as db:
            return await db.search_documents(query, limit)

    try:
        from pkp.embedder import get_embedder

        embedder = get_embedder()

        dense_vec = embedder.embed_query(query)
        sparse_weights = embedder.embed_query_sparse(query)

        indices, values = embedder.tokens_to_indices(query, sparse_weights)

        return await search_qdrant(
            query_dense=dense_vec.tolist(),
            query_sparse=(indices, values),
            limit=limit,
        )
    except Exception:
        async with db_context() as db:
            return await db.search_documents(query, limit)
