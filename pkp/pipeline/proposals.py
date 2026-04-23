"""Proposal generation engine for related-document suggestions."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

from pkp.config import get_config
from pkp.storage.archive import ArchiveManager
from pkp.storage.db import db_context
from pkp.storage.models import Proposal, SearchResult
from pkp.storage.qdrant import qdrant_available, search_documents_hybrid

logger = logging.getLogger(__name__)

_BODY_QUERY_CHARS = 500
_FTS_QUERY_TOKEN_LIMIT = 20
_MIN_QUERY_TOKEN_LENGTH = 2


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
        for result in results:
            if inserted_count >= config.proposal_top_n:
                break

            if result.doc_sha256 == doc_sha256:
                continue

            score = _score_from_best_rank(result.best_rank)
            if score < 0.1:
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

            proposal = Proposal(
                proposal_id=_make_proposal_id(),
                doc_a_sha256=doc_sha256,
                doc_b_sha256=result.doc_sha256,
                score=score,
                rationale=_build_rationale(result.title, score),
                status="pending",
                created_at=datetime.now(UTC),
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
    """Remove only the first leading YAML frontmatter block."""
    if not text.startswith("---\n"):
        return text

    end_index = text.find("\n---\n", 4)
    if end_index == -1:
        return text

    return text[end_index + 5 :].lstrip("\n")


def _build_query(title: str, normalized_text: str | None) -> str:
    """Build the proposal search query from title and normalized content."""
    if normalized_text is None:
        return title.strip()

    body_text = _strip_leading_frontmatter(normalized_text).strip()
    body_preview = body_text[:_BODY_QUERY_CHARS].strip()
    if not body_preview:
        return title.strip()

    return f"{title.strip()}\n\n{body_preview}"


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
    """Transform a search rank into a higher-is-better proposal score."""
    return 1.0 / (1.0 + abs(best_rank))


def _build_rationale(candidate_title: str, score: float) -> str:
    """Build a template rationale for a candidate document."""
    return f'Related to "{candidate_title}" (score: {score:.2f}).'


def _make_proposal_id() -> str:
    """Create a proposal identifier using the existing short UUID pattern."""
    return f"proposal-{uuid.uuid4().hex[:8]}"


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
            return await search_documents_hybrid(raw_query, limit=limit)
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
