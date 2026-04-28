"""Qdrant vector database layer for PKP."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from pkp.config import get_config
from pkp.reranker import get_reranker

from .db import db_context
from .models import SearchResult

PKP_POINT_NAMESPACE = uuid.UUID("95f9de35-3ce7-4912-89a4-cfe9f9c26798")
logger = logging.getLogger(__name__)


def _get_collection_name(doc_type: str) -> str:
    """Get the physical Qdrant collection name for a doc_type."""
    return f"{doc_type}_v1"


def _get_collection_alias(doc_type: str) -> str:
    """Get the stable alias name for a doc_type collection."""
    return f"{doc_type}_current"


def _get_qdrant_client() -> Any:
    """Get or create Qdrant client instance."""
    config = get_config()
    from qdrant_client import QdrantClient

    return QdrantClient(url=config.qdrant_url)


def _point_id_for_chunk(chunk_id: str) -> str:
    """Return a stable Qdrant point ID for a chunk."""
    return str(uuid.uuid5(PKP_POINT_NAMESPACE, chunk_id))


def _query_response_points(results: Any) -> list[Any]:
    """Return scored points from Qdrant query responses across client versions."""
    points = getattr(results, "points", None)
    if points is not None:
        return list(points)

    legacy_points = getattr(results, "result", None)
    if legacy_points is not None:
        return list(legacy_points)

    return []


def _sanitize_fts_query(query: str) -> str:
    """Convert free text into a SQLite FTS-safe OR query string."""
    query_terms: list[str] = []
    seen_tokens: set[str] = set()

    for token in re.findall(r"\w+", query.casefold()):
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        query_terms.append(f'"{token}"')

    return " OR ".join(query_terms)


def _list_collection_names() -> set[str]:
    """Return the set of collection names currently present in Qdrant."""
    client = _get_qdrant_client()
    return {collection.name for collection in client.get_collections().collections}


def _get_alias_mappings() -> dict[str, str]:
    """Return all Qdrant alias mappings."""
    client = _get_qdrant_client()
    response = client.get_aliases()
    return {alias.alias_name: alias.collection_name for alias in response.aliases}


def _get_upsert_collection_name(doc_type: str) -> str:
    """Return the best collection target for writes."""
    alias_name = _get_collection_alias(doc_type)
    physical_name = _get_collection_name(doc_type)

    try:
        alias_mappings = _get_alias_mappings()
        if alias_mappings.get(alias_name) == physical_name:
            return alias_name
    except Exception:
        return physical_name

    return physical_name


def _get_query_collection_name(doc_type: str) -> str | None:
    """Return the safe collection target for reads."""
    alias_name = _get_collection_alias(doc_type)
    physical_name = _get_collection_name(doc_type)

    try:
        alias_mappings = _get_alias_mappings()
    except Exception:
        alias_mappings = None

    if alias_mappings and alias_mappings.get(alias_name) == physical_name:
        return alias_name

    collection_names = _list_collection_names()

    if physical_name in collection_names:
        return physical_name

    return None


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

    client = _get_qdrant_client()
    collection_name = _get_collection_name(doc_type)
    alias_name = _get_collection_alias(doc_type)
    config = get_config()

    vector_size = config.embedding_dimension

    _warn_dimension_mismatch(vector_size)

    collection_names = _list_collection_names()
    exists = collection_name in collection_names

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

    try:
        alias_mappings = _get_alias_mappings()
        if alias_mappings.get(alias_name) != collection_name:
            operations: list[Any] = []
            if alias_name in alias_mappings:
                operations.append(
                    models.DeleteAliasOperation(
                        delete_alias=models.DeleteAlias(alias_name=alias_name)
                    )
                )
            operations.append(
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(
                        collection_name=collection_name,
                        alias_name=alias_name,
                    )
                )
            )
            client.update_collection_aliases(change_aliases_operations=operations)
    except Exception:
        # Alias APIs are optional for PKP; physical collections still work.
        pass

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

    collection_name = _get_upsert_collection_name(doc_type)
    client = _get_qdrant_client()

    points = []
    for i, chunk_id in enumerate(chunk_ids):
        sparse_indices, sparse_values = sparse_data[i]
        point_id = _point_id_for_chunk(chunk_id)

        if not sparse_indices:
            point = models.PointStruct(
                id=point_id,
                vector=dense_vectors[i],
                payload={**payloads[i], "chunk_id": chunk_id},
            )
        else:
            point = models.PointStruct(
                id=point_id,
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
        collection_name = _get_query_collection_name(doc_type)
        if collection_name is None:
            return []
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
        for point in _query_response_points(results):
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
                    doc_type=point.payload.get("doc_type"),
                    match_count=1,
                    best_rank=-point.score,
                    raw_score=point.score,
                )

        return list(seen_docs.values())[:limit]

    results_by_doc: dict[str, SearchResult] = {}

    for dtype in doc_types:
        collection_name = _get_query_collection_name(dtype)
        if collection_name is None:
            continue
        client = _get_qdrant_client()

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

        for point in _query_response_points(results):
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
                    doc_type=point.payload.get("doc_type"),
                    match_count=1,
                    best_rank=-point.score,
                    raw_score=point.score,
                )

    sorted_results = sorted(
        results_by_doc.values(),
        key=lambda r: r.best_rank,
    )
    return sorted_results[:limit]


async def _rerank_search_results(
    query: str,
    results: list[SearchResult],
) -> list[SearchResult]:
    """Rerank search results using each document's top matching passage."""
    if not results:
        return []

    reranker = get_reranker()
    if not reranker.enabled:
        return results

    scored_indexes: list[int] = []
    scored_results: list[SearchResult] = []
    pairs: list[tuple[str, str]] = []
    for index, result in enumerate(results):
        if result.doc_type is None:
            continue

        passage = await search_top_passage_for_document(
            query=query,
            target_doc_sha256=result.doc_sha256,
            doc_type=result.doc_type,
        )
        if passage is None:
            continue

        scored_indexes.append(index)
        scored_results.append(result)
        pairs.append((query, passage))

    if not scored_results:
        logger.warning("reranker unavailable, falling back to Qdrant score ordering")
        return results

    reranker_scores = reranker.compute_scores(pairs)
    if len(reranker_scores) != len(scored_results):
        logger.warning("reranker unavailable, falling back to Qdrant score ordering")
        return results

    for result, reranker_score in zip(scored_results, reranker_scores, strict=True):
        result.reranker_score = reranker_score
        result.raw_score = reranker_score
        result.best_rank = -reranker_score

    scored_results.sort(key=lambda result: result.reranker_score or 0, reverse=True)
    scored_index_set = set(scored_indexes)
    unscored_results = [
        result for index, result in enumerate(results) if index not in scored_index_set
    ]
    return scored_results + unscored_results


async def search_documents_hybrid(
    query: str,
    limit: int = 10,
    *,
    rerank: bool = True,
) -> list[SearchResult]:
    """Try Qdrant first, fallback to FTS5 on connection error.

    Args:
        query: Search query.
        limit: Max results.
        rerank: Whether to rerank the final result set.

    Returns:
        List of SearchResult.
    """
    safe_query = _sanitize_fts_query(query)

    if not qdrant_available():
        async with db_context() as db:
            return await db.search_documents(safe_query or query, limit)

    try:
        from pkp.embedder import get_embedder

        embedder = get_embedder()

        dense_vec = embedder.embed_query(query)
        sparse_weights = embedder.embed_query_sparse(query)

        indices, values = embedder.tokens_to_indices(query, sparse_weights)

        results = await search_qdrant(
            query_dense=dense_vec.tolist(),
            query_sparse=(indices, values),
            limit=limit,
        )
        if not rerank:
            return results
        try:
            reranked_results = await _rerank_search_results(query, results)
        except Exception:
            logger.warning(
                "reranker unavailable, falling back to Qdrant score ordering",
                exc_info=True,
            )
            return results
        return reranked_results[:limit]
    except Exception:
        async with db_context() as db:
            return await db.search_documents(safe_query or query, limit)


async def search_top_passage_for_document(
    query: str,
    target_doc_sha256: str,
    doc_type: str,
) -> str | None:
    """Return the top matching chunk content for one target document."""
    if not query.strip():
        return None

    collection_name = _get_query_collection_name(doc_type)
    if collection_name is None:
        return None

    from qdrant_client import models

    from pkp.embedder import get_embedder

    embedder = get_embedder()
    dense_vec = embedder.embed_query(query)
    sparse_weights = embedder.embed_query_sparse(query)
    indices, values = embedder.tokens_to_indices(query, sparse_weights)

    query_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="doc_type",
                match=models.MatchValue(value=doc_type),
            ),
            models.FieldCondition(
                key="doc_sha256",
                match=models.MatchValue(value=target_doc_sha256),
            ),
        ]
    )

    dense_prefetch = models.Prefetch(
        query=dense_vec.tolist(),
        using="dense",
        limit=1,
        filter=query_filter,
    )
    sparse_prefetch = models.Prefetch(
        query=models.SparseVector(indices=indices, values=values),
        using="sparse",
        limit=1,
        filter=query_filter,
    )

    client = _get_qdrant_client()
    results = client.query_points(
        collection_name=collection_name,
        prefetch=[dense_prefetch, sparse_prefetch],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        with_payload=True,
        limit=1,
    )

    for point in _query_response_points(results):
        content = point.payload.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    return None
