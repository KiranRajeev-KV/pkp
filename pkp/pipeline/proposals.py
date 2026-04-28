"""Proposal generation engine for related-document suggestions."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pkp.config import get_config
from pkp.reranker import get_reranker
from pkp.storage.archive import ArchiveManager
from pkp.storage.db import db_context
from pkp.storage.models import Document, Proposal, SearchResult
from pkp.storage.qdrant import (
    qdrant_available,
    search_documents_hybrid,
    search_top_passage_for_document,
)

logger = logging.getLogger(__name__)

_BODY_QUERY_CHARS = 500
_FTS_QUERY_TOKEN_LIMIT = 20
_MIN_QUERY_TOKEN_LENGTH = 2
_PKP_FRONTMATTER_MARKER = 'pkp_version: "1"'
_EXTRACTED_METADATA_FRONTMATTER_KEYS = {
    "author",
    "date",
    "description",
    "hostname",
    "sitename",
    "source",
    "title",
    "url",
}


@dataclass
class ProposalCandidate:
    """Candidate proposal enriched with passage evidence and final score."""

    result: SearchResult
    score: float
    candidate_doc: Document | None
    passage_a: str | None
    passage_b: str | None


async def generate_proposals(doc_sha256: str) -> int:
    """Generate and persist missing proposals for one source document."""
    return await _generate_proposals(doc_sha256)


async def _generate_proposals(doc_sha256: str) -> int:
    """Run proposal generation for one source document."""
    config = get_config()
    if config.archive_path is None:
        logger.error("proposal archive path not configured doc_sha256=%s", doc_sha256)
        return 0

    async with db_context() as db:
        source_doc = await db.get_document(doc_sha256)

    if source_doc is None:
        logger.warning("proposal source document not found doc_sha256=%s", doc_sha256)
        return 0

    archive = ArchiveManager(config.archive_path)
    normalized_text = archive.read_normalized(doc_sha256)
    if normalized_text is None:
        logger.warning(
            "proposal normalized file missing doc_sha256=%s; using title-only query",
            doc_sha256,
        )

    raw_query = _build_query(source_doc.title, normalized_text)
    if not raw_query.strip():
        logger.warning(
            "proposal query empty doc_sha256=%s title=%r",
            doc_sha256,
            source_doc.title,
        )
        return 0

    qdrant_is_available = qdrant_available()
    fts_query = _build_fts_query(raw_query)
    search_limit = max(config.proposal_top_n * 3, config.proposal_top_n + 5)
    logger.info(
        "proposal query constructed doc_sha256=%s qdrant_available=%s query=%r",
        doc_sha256,
        qdrant_is_available,
        raw_query[:200],
    )

    results = await _search_candidates(
        doc_sha256=doc_sha256,
        raw_query=raw_query,
        fts_query=fts_query,
        limit=search_limit,
        qdrant_is_available=qdrant_is_available,
    )
    logger.info(
        "proposal candidates found doc_sha256=%s candidate_count=%s",
        doc_sha256,
        len(results),
    )

    inserted_count = 0
    async with db_context() as db:
        candidates: list[ProposalCandidate] = []
        for result in results:
            if result.doc_sha256 == doc_sha256:
                continue

            score = _score_search_result(result)
            if score < config.proposal_min_score:
                logger.info(
                    "proposal candidate skipped below threshold doc_sha256=%s candidate_sha256=%s score=%.4f",
                    doc_sha256,
                    result.doc_sha256,
                    score,
                )
                continue

            if await db.proposal_exists(doc_sha256, result.doc_sha256):
                logger.info(
                    "proposal candidate skipped existing pair doc_sha256=%s candidate_sha256=%s",
                    doc_sha256,
                    result.doc_sha256,
                )
                continue

            if await db.rejected_pair_exists(doc_sha256, result.doc_sha256):
                logger.info(
                    "proposal candidate skipped rejected pair doc_sha256=%s candidate_sha256=%s",
                    doc_sha256,
                    result.doc_sha256,
                )
                continue

            candidate_doc = await db.get_document(result.doc_sha256)
            if candidate_doc is None:
                logger.warning(
                    "proposal candidate document missing doc_a_sha256=%s doc_b_sha256=%s",
                    doc_sha256,
                    result.doc_sha256,
                )

            passage_a, passage_b = await _retrieve_passage_evidence(
                source_doc=source_doc,
                source_query=raw_query,
                candidate_doc=candidate_doc,
                archive=archive,
                qdrant_is_available=qdrant_is_available,
            )

            candidates.append(
                ProposalCandidate(
                    result=result,
                    score=score,
                    candidate_doc=candidate_doc,
                    passage_a=passage_a,
                    passage_b=passage_b,
                )
            )

        ranked_candidates = _rerank_candidates(candidates, config.reranker_min_score)

        for candidate in ranked_candidates[: config.proposal_top_n]:
            if candidate.candidate_doc is None:
                continue

            proposal = Proposal(
                proposal_id=_make_proposal_id(),
                doc_a_sha256=doc_sha256,
                doc_b_sha256=candidate.result.doc_sha256,
                score=candidate.score,
                rationale=_build_rationale(candidate.result.title, candidate.score),
                status="pending",
                created_at=datetime.now(UTC),
                passage_a=candidate.passage_a,
                passage_b=candidate.passage_b,
                reviewed_at=None,
                link_type="related",
            )
            await db.insert_proposal(proposal)
            inserted_count += 1
            logger.info(
                "proposal inserted proposal_id=%s doc_a_sha256=%s doc_b_sha256=%s score=%.4f",
                proposal.proposal_id,
                proposal.doc_a_sha256,
                proposal.doc_b_sha256,
                proposal.score,
            )

    logger.info(
        "proposal generation finished doc_sha256=%s inserted_count=%s",
        doc_sha256,
        inserted_count,
    )
    return inserted_count


def _strip_leading_frontmatter(text: str) -> str:
    """Remove PKP frontmatter and a known extracted-metadata block when present."""
    while True:
        updated = _strip_frontmatter_block_if(text, _is_pkp_frontmatter_block)
        if updated != text:
            text = updated
            continue

        updated = _strip_frontmatter_block_if(
            text,
            _is_extracted_metadata_frontmatter_block,
        )
        if updated != text:
            text = updated
            continue

        return text


def _strip_frontmatter_block_if(
    text: str,
    predicate: Callable[[str], bool],
) -> str:
    """Remove one leading frontmatter block when the block matches a predicate."""
    if not text.startswith("---\n"):
        return text

    end_index = text.find("\n---\n", 4)
    if end_index == -1:
        return text

    block = text[: end_index + 5]
    if not predicate(block):
        return text

    return text[end_index + 5 :].lstrip("\n")


def _frontmatter_keys(block: str) -> set[str]:
    """Extract YAML-like top-level keys from a frontmatter block."""
    keys: set[str] = set()
    for line in block.splitlines()[1:-1]:
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, _, _value = stripped.partition(":")
        key = key.strip()
        if key:
            keys.add(key)
    return keys


def _is_pkp_frontmatter_block(block: str) -> bool:
    """Return True when the block is PKP's injected leading frontmatter."""
    return _PKP_FRONTMATTER_MARKER in block


def _is_extracted_metadata_frontmatter_block(block: str) -> bool:
    """Return True for the known extractor-inserted metadata frontmatter shape."""
    keys = _frontmatter_keys(block)
    if not keys:
        return False
    if not keys.issubset(_EXTRACTED_METADATA_FRONTMATTER_KEYS):
        return False
    return "url" in keys or "hostname" in keys or "sitename" in keys


def _build_query(title: str, normalized_text: str | None) -> str:
    """Build the proposal search query from title and normalized content."""
    if normalized_text is None:
        return title.strip()

    body_text = _strip_leading_frontmatter(normalized_text).strip()
    body_preview = body_text[:_BODY_QUERY_CHARS].strip()
    if not body_preview:
        return title.strip()

    return f"{title.strip()}\n\n{body_preview}"


def _build_document_query(doc: Document, archive: ArchiveManager) -> str:
    """Build a retrieval query for one document using archived normalized text."""
    normalized_text = archive.read_normalized(doc.sha256)
    return _build_query(doc.title, normalized_text)


def _build_fts_query(raw_query: str) -> str:
    """Convert raw query text into an FTS-safe OR query."""
    query_terms: list[str] = []
    seen_tokens: set[str] = set()

    for token in re.findall(r"\w+", raw_query.casefold()):
        if len(token) < _MIN_QUERY_TOKEN_LENGTH:
            continue
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        query_terms.append(f'"{token}"')
        if len(query_terms) >= _FTS_QUERY_TOKEN_LIMIT:
            break

    return " OR ".join(query_terms)


def _score_from_best_rank(best_rank: float) -> float:
    """Transform an FTS search rank into a higher-is-better proposal score."""
    return 1.0 / (1.0 + abs(best_rank))


def _score_search_result(result: SearchResult) -> float:
    """Return the proposal score using raw Qdrant score when available."""
    if result.raw_score is not None:
        return result.raw_score
    return _score_from_best_rank(result.best_rank)


def _build_rationale(candidate_title: str, score: float) -> str:
    """Build a template rationale for a candidate document."""
    return f'Related to "{candidate_title}" (score: {score:.2f}).'


def _rerank_candidates(
    candidates: list[ProposalCandidate],
    reranker_min_score: float,
) -> list[ProposalCandidate]:
    """Rerank proposal candidates using passage evidence when available."""
    if not candidates:
        return []

    reranker = get_reranker()
    if not reranker.enabled:
        return candidates

    scored_indexes = [
        index
        for index, candidate in enumerate(candidates)
        if candidate.passage_a and candidate.passage_b
    ]
    scored_candidates = [candidates[index] for index in scored_indexes]
    if not scored_candidates:
        logger.warning("reranker unavailable, falling back to Qdrant score ordering")
        return candidates

    pairs = [
        (candidate.passage_a, candidate.passage_b)
        for candidate in scored_candidates
        if candidate.passage_a and candidate.passage_b
    ]
    reranker_scores = reranker.compute_scores(pairs)
    if len(reranker_scores) != len(scored_candidates):
        logger.warning("reranker unavailable, falling back to Qdrant score ordering")
        return candidates

    for candidate, reranker_score in zip(
        scored_candidates, reranker_scores, strict=True
    ):
        candidate.score = reranker_score

    reranked_candidates = [
        candidate
        for candidate in scored_candidates
        if candidate.score >= reranker_min_score
    ]
    scored_index_set = set(scored_indexes)
    preserved_candidates = [
        candidate
        for index, candidate in enumerate(candidates)
        if index not in scored_index_set
    ]

    if not reranked_candidates:
        if preserved_candidates:
            return preserved_candidates
        logger.info(
            "proposal candidates dropped by reranker threshold reranker_min_score=%.4f",
            reranker_min_score,
        )
        return []

    reranked_candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return reranked_candidates + preserved_candidates


def _make_proposal_id() -> str:
    """Create a proposal identifier using the existing short UUID pattern."""
    return f"proposal-{uuid.uuid4().hex[:8]}"


async def _retrieve_passage_evidence(
    source_doc: Document,
    source_query: str,
    candidate_doc: Document | None,
    archive: ArchiveManager,
    qdrant_is_available: bool,
) -> tuple[str | None, str | None]:
    """Return the best matching passage pair for a proposal candidate."""
    if not qdrant_is_available or candidate_doc is None:
        return None, None

    candidate_query = _build_document_query(candidate_doc, archive)
    source_passage: str | None = None
    candidate_passage: str | None = None

    try:
        candidate_passage = await search_top_passage_for_document(
            query=source_query,
            target_doc_sha256=candidate_doc.sha256,
            doc_type=candidate_doc.doc_type,
        )
    except Exception as exc:
        logger.warning(
            "proposal evidence retrieval failed doc_a_sha256=%s doc_b_sha256=%s direction=source_to_candidate: %s",
            source_doc.sha256,
            candidate_doc.sha256,
            exc,
        )

    try:
        source_passage = await search_top_passage_for_document(
            query=candidate_query,
            target_doc_sha256=source_doc.sha256,
            doc_type=source_doc.doc_type,
        )
    except Exception as exc:
        logger.warning(
            "proposal evidence retrieval failed doc_a_sha256=%s doc_b_sha256=%s direction=candidate_to_source: %s",
            source_doc.sha256,
            candidate_doc.sha256,
            exc,
        )

    return source_passage, candidate_passage


async def _search_candidates(
    doc_sha256: str,
    raw_query: str,
    fts_query: str,
    limit: int,
    qdrant_is_available: bool,
) -> list[SearchResult]:
    """Search for candidate documents using hybrid search with explicit FTS fallback."""
    if qdrant_is_available:
        try:
            return await search_documents_hybrid(raw_query, limit=limit, rerank=False)
        except Exception as exc:
            logger.warning(
                "proposal hybrid search failed doc_sha256=%s; using FTS fallback: %s",
                doc_sha256,
                exc,
            )
    else:
        logger.warning(
            "proposal search using FTS fallback because Qdrant is unavailable doc_sha256=%s",
            doc_sha256,
        )

    if not fts_query:
        logger.warning(
            "proposal FTS query empty after sanitization doc_sha256=%s",
            doc_sha256,
        )
        return []

    logger.info(
        "proposal FTS query constructed doc_sha256=%s query=%r",
        doc_sha256,
        fts_query,
    )
    async with db_context() as db:
        return await db.search_documents(fts_query, limit)
