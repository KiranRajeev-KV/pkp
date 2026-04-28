"""Shared ingest and index rebuild workflows."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from defusedxml import ElementTree as ET  # noqa: N817

from pkp.config import get_config
from pkp.pipeline.extractor import (
    ExtractedDocument,
    ExtractionError,
    ExtractorService,
    SourceRequest,
    extract_arxiv_id,
)
from pkp.pipeline.normalizer import Chunk, NormalizerService
from pkp.storage.archive import ArchiveManager, DocumentMetadata
from pkp.storage.db import Chunk as DBChunk
from pkp.storage.db import Document, IngestionMetric, db_context

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

    from pkp.storage.db import Database

logger = logging.getLogger(__name__)
ProgressReporter = Callable[[str], None]


@dataclass
class IngestResult:
    """Result of a shared ingest workflow."""

    doc_sha256: str
    title: str
    doc_type: str
    archive_path: Path
    created: bool
    extraction_time_ms: int
    normalization_time_ms: int
    archive_time_ms: int
    total_time_ms: int

    def timing(self) -> dict[str, int]:
        """Return CLI-compatible timing information."""
        return {
            "extraction_time_ms": self.extraction_time_ms,
            "normalization_time_ms": self.normalization_time_ms,
            "archive_time_ms": self.archive_time_ms,
            "total_time_ms": self.total_time_ms,
        }


@dataclass
class RebuildIndexResult:
    """Summary of an index rebuild."""

    total_upserted: int
    total_documents: int


@dataclass
class QdrantIndexingResult:
    """Summary of one Qdrant indexing operation."""

    indexed_chunks: int
    citation_chunks_skipped: int


_CITATION_CHUNK_THRESHOLD = 0.50
_CITATION_FILTER_NOTE = (
    "Citation chunk filtering is now active. Run 'pkp rebuild-index --all' to "
    "remove citation chunks from existing Qdrant collections."
)
_CITATION_CHUNK_PATTERNS = (
    re.compile(
        r"\[(?:doi|arxiv|bibcode|pmid|pmc|issn|isbn|s2cid|oclc|jstor|hdl)\]"
        r"\(https://en\.wikipedia\.org/wiki/[^)]+_\(identifier\)\)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\[\^.*?\]\(https://en\.wikipedia\.org#cite_(?:ref|note)[^)]+\)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\[\[\d+\]\]\(https://en\.wikipedia\.org#cite_note[^)]+\)",
        re.IGNORECASE,
    ),
    re.compile(r"10\.\d{4,9}/[^\s)\]>]+", re.IGNORECASE),
    re.compile(
        r"https?://(?:doi\.org|arxiv\.org/abs|ui\.adsabs\.harvard\.edu/abs|"
        r"pubmed\.ncbi\.nlm\.nih\.gov|api\.semanticscholar\.org/CorpusID:?|"
        r"search\.worldcat\.org/oclc/|www\.jstor\.org/stable/|hdl\.handle\.net/|"
        r"www\.ncbi\.nlm\.nih\.gov/pmc/articles/|science\.org/doi/)[^\s)\]]*",
        re.IGNORECASE,
    ),
    re.compile(r"Special:BookSources/[^\s)\]]+", re.IGNORECASE),
    re.compile(r"en\.wikipedia\.org/wiki/[^\s)\]]+_\(identifier\)", re.IGNORECASE),
    re.compile(
        r"\b(?:S2CID|OCLC|PMID|JSTOR|ISSN|ISBN|PMC|Bibcode|arXiv|doi|hdl)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b"),
)

_ARXIV_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


async def fetch_arxiv_metadata(arxiv_id: str) -> dict[str, Any] | None:
    """Fetch title, authors, and abstract for an arXiv paper."""
    url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"

    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
    except Exception as exc:
        logger.warning(
            "arxiv metadata fetch failed arxiv_id=%s url=%s: %s",
            arxiv_id,
            url,
            exc,
        )
        return None

    try:
        root = ET.fromstring(response.text)
        entry = root.find("atom:entry", _ARXIV_ATOM_NS)
        if entry is None:
            logger.warning("arxiv metadata missing entry arxiv_id=%s", arxiv_id)
            return None

        title = entry.findtext("atom:title", default="", namespaces=_ARXIV_ATOM_NS)
        summary = entry.findtext(
            "atom:summary",
            default="",
            namespaces=_ARXIV_ATOM_NS,
        )
        authors = [
            name.text.strip()
            for name in entry.findall("atom:author/atom:name", _ARXIV_ATOM_NS)
            if name.text and name.text.strip()
        ]
        cleaned_title = " ".join(title.split())
        cleaned_summary = " ".join(summary.split())
        if not cleaned_title:
            logger.warning("arxiv metadata missing title arxiv_id=%s", arxiv_id)
            return None

        return {
            "title": cleaned_title,
            "authors": authors,
            "abstract": cleaned_summary,
            "arxiv_id": arxiv_id,
        }
    except Exception as exc:
        logger.warning("arxiv metadata parse failed arxiv_id=%s: %s", arxiv_id, exc)
        return None


def _detect_arxiv_id_for_document(
    extracted: ExtractedDocument,
    source_file: str | None = None,
) -> str | None:
    """Detect an arXiv ID from available document metadata."""
    candidates = [extracted.title]
    if source_file:
        candidates.append(Path(source_file).stem)
    candidates.extend(extracted.text.splitlines()[:20])

    for candidate in candidates:
        arxiv_id = extract_arxiv_id(candidate)
        if arxiv_id:
            return arxiv_id
    return None


async def _maybe_enrich_arxiv_metadata(
    *,
    extracted: ExtractedDocument,
    source_file: str | None,
    archive: ArchiveManager,
    db: Database,
    reporter: ProgressReporter,
) -> str:
    """Update archive metadata and DB title when arXiv metadata is available."""
    arxiv_id = _detect_arxiv_id_for_document(extracted, source_file=source_file)
    if arxiv_id is None:
        return extracted.title

    metadata = await fetch_arxiv_metadata(arxiv_id)
    if metadata is None:
        logger.warning(
            "arxiv enrichment unavailable sha256=%s arxiv_id=%s",
            extracted.sha256,
            arxiv_id,
        )
        return extracted.title

    enriched_title = metadata["title"]
    await db.update_document_title(extracted.sha256, enriched_title)

    archived = archive.get_archived_document(extracted.sha256)
    if archived is not None:
        archived.metadata.title = enriched_title
        archived.metadata.arxiv_id = metadata["arxiv_id"]
        archived.metadata.arxiv_authors = metadata["authors"]
        archived.metadata.arxiv_abstract = metadata["abstract"]
        archive.write_metadata(extracted.sha256, archived.metadata)

    reporter(f"Enriched title via arXiv: {enriched_title}")
    return str(enriched_title)


def _citation_match_ratio(text: str) -> float:
    """Return the matched-character ratio for citation-style patterns."""
    if not text:
        return 0.0

    spans: list[tuple[int, int]] = []
    for pattern in _CITATION_CHUNK_PATTERNS:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end()))

    if not spans:
        return 0.0

    spans.sort()
    merged_spans: list[tuple[int, int]] = []
    start, end = spans[0]
    for next_start, next_end in spans[1:]:
        if next_start <= end:
            end = max(end, next_end)
            continue
        merged_spans.append((start, end))
        start, end = next_start, next_end
    merged_spans.append((start, end))

    matched_chars = sum(span_end - span_start for span_start, span_end in merged_spans)
    return matched_chars / len(text)


def is_citation_chunk(text: str, threshold: float = _CITATION_CHUNK_THRESHOLD) -> bool:
    """Return True when a chunk is predominantly reference/citation content."""
    return _citation_match_ratio(text) > threshold


async def ingest_url(
    url: str,
    reporter: ProgressReporter | None = None,
) -> IngestResult:
    """Run the full URL ingest pipeline."""
    reporter = reporter or _noop_reporter
    total_start_ns = time.perf_counter_ns()
    config = get_config()

    reporter(f"Extracting {url}...")
    extractor = ExtractorService(
        user_agent=config.user_agent,
        crawl4ai_timeout=config.crawl4ai_timeout,
        crawl4ai_browser_type=config.crawl4ai_browser_type,
        crawl4ai_headless=config.crawl4ai_headless,
        fallback_word_count_threshold=config.fallback_word_count_threshold,
    )
    extracted = await extractor.extract(SourceRequest(url=url))
    if extracted.raw_html is None:
        raise RuntimeError(f"URL extraction returned no raw HTML for {url}")

    reporter(f"Extracted: {extracted.title} ({extracted.sha256[:16]}...)")
    return await _finish_ingest(
        extracted=extracted,
        original_content=extracted.raw_html,
        original_extension=".html",
        metric_source=url,
        reporter=reporter,
        total_start_ns=total_start_ns,
    )


async def ingest_pdf(
    pdf_path: Path,
    reporter: ProgressReporter | None = None,
) -> IngestResult:
    """Run the full PDF ingest pipeline."""
    reporter = reporter or _noop_reporter
    total_start_ns = time.perf_counter_ns()
    config = get_config()

    reporter(f"Extracting {pdf_path}...")
    extractor = ExtractorService(
        user_agent=config.user_agent,
        crawl4ai_timeout=config.crawl4ai_timeout,
        crawl4ai_browser_type=config.crawl4ai_browser_type,
        crawl4ai_headless=config.crawl4ai_headless,
        fallback_word_count_threshold=config.fallback_word_count_threshold,
    )
    extracted = await extractor.extract(SourceRequest(pdf_path=pdf_path))

    reporter(f"Extracted: {extracted.title} ({extracted.sha256[:16]}...)")
    return await _finish_ingest(
        extracted=extracted,
        original_content=pdf_path.read_bytes(),
        original_extension=".pdf",
        metric_source=str(pdf_path),
        source_file=str(pdf_path),
        reporter=reporter,
        total_start_ns=total_start_ns,
    )


async def rebuild_index(
    filter_doc_type: str | None,
    rebuild_all: bool = False,
    reporter: ProgressReporter | None = None,
) -> RebuildIndexResult:
    """Rebuild the Qdrant index from archived chunks."""
    reporter = reporter or _noop_reporter

    from pkp.storage.qdrant import _get_qdrant_client, qdrant_available

    config = get_config()
    if config.archive_path is None:
        raise RuntimeError("Archive path not configured")

    if not qdrant_available():
        raise RuntimeError("Qdrant not available")

    client = _get_qdrant_client()

    async with db_context() as db:
        documents = await db.get_all_documents()
        doc_types = await db.get_distinct_doc_types() if rebuild_all else []

    await _setup_collections(
        client=client,
        doc_types=doc_types,
        filter_doc_type=filter_doc_type,
        rebuild_all=rebuild_all,
        reporter=reporter,
    )

    total_upserted, total_docs = await _index_documents(
        client=client,
        documents=documents,
        filter_doc_type=filter_doc_type,
        reporter=reporter,
    )
    reporter(_CITATION_FILTER_NOTE)
    return RebuildIndexResult(total_upserted=total_upserted, total_documents=total_docs)


async def _finish_ingest(
    extracted: ExtractedDocument,
    original_content: bytes,
    original_extension: str,
    metric_source: str,
    reporter: ProgressReporter,
    total_start_ns: int,
    source_file: str | None = None,
) -> IngestResult:
    """Persist extracted content, database rows, and vector index entries."""
    config = get_config()
    if config.archive_path is None:
        raise RuntimeError("Archive path not configured")

    archive = ArchiveManager(config.archive_path)
    if archive.document_exists(extracted.sha256):
        await _repair_unindexed_document(extracted.sha256, reporter)
        reporter(f"Document already archived: {extracted.sha256[:16]}")
        return IngestResult(
            doc_sha256=extracted.sha256,
            title=extracted.title,
            doc_type=extracted.doc_type,
            archive_path=archive.get_document_dir(extracted.sha256),
            created=False,
            extraction_time_ms=extracted.extraction_time_ms,
            normalization_time_ms=0,
            archive_time_ms=0,
            total_time_ms=_elapsed_ms(total_start_ns),
        )

    normalizer = NormalizerService(
        chunk_size_tokens=config.chunk_size_tokens,
        chunk_overlap_tokens=config.chunk_overlap_tokens,
    )
    chunked = normalizer.normalize(
        sha256=extracted.sha256,
        title=extracted.title,
        extracted_text=extracted.text,
        url=extracted.url,
        doc_type=extracted.doc_type,
        metadata=extracted.metadata,
    )
    reporter(f"Normalized: {len(chunked.chunks)} chunks, {chunked.word_count} words")

    doc_dir = archive.get_document_dir(extracted.sha256)
    doc_dir.mkdir(parents=True, exist_ok=True)

    archive_start_ns = time.perf_counter_ns()

    archive.write_original(extracted.sha256, original_content, original_extension)
    archive.write_extracted(extracted.sha256, extracted.text)
    archive.write_normalized(extracted.sha256, chunked.normalized_text)
    archive.write_chunks(extracted.sha256, normalizer.chunks_to_jsonl(chunked.chunks))

    metadata = DocumentMetadata(
        url=extracted.url,
        sha256=extracted.sha256,
        doc_type=extracted.doc_type,
        title=extracted.title,
        retrieved_at=chunked.retrieved_at.isoformat(),
        content_type=extracted.metadata.get("content_type"),
        source_file=source_file,
        extraction_status="success",
        word_count=chunked.word_count,
    )
    archive.write_metadata(extracted.sha256, metadata)

    async with db_context() as db:
        doc = Document(
            sha256=extracted.sha256,
            url=extracted.url,
            title=extracted.title,
            doc_type=extracted.doc_type,
            retrieved_at=chunked.retrieved_at,
            word_count=chunked.word_count,
            archive_path=str(doc_dir),
            tags=[],
            embedded_with=config.embedding_model,
        )
        await db.insert_document(doc)
        final_title = extracted.title
        if extracted.doc_type == "pdf":
            try:
                final_title = await _maybe_enrich_arxiv_metadata(
                    extracted=extracted,
                    source_file=source_file,
                    archive=archive,
                    db=db,
                    reporter=reporter,
                )
            except Exception as exc:
                logger.warning(
                    "arxiv enrichment failed unexpectedly sha256=%s: %s",
                    extracted.sha256,
                    exc,
                )
            else:
                extracted.title = final_title
                doc.title = final_title

        if config.vault_path is not None and config.auto_vault_on_ingest:
            try:
                from pkp.vault.writer import VaultWriterError, create_document_note

                note_path = create_document_note(config.vault_path, doc)
                await db.mark_document_vault_path(doc.sha256, str(note_path))
            except VaultWriterError as exc:
                logger.warning(
                    "vault note creation failed sha256=%s: %s",
                    doc.sha256,
                    exc,
                )
                reporter(f"Vault note creation failed for {doc.sha256[:16]}: {exc}")
            except Exception as exc:
                logger.warning(
                    "vault note creation failed unexpectedly sha256=%s: %s",
                    doc.sha256,
                    exc,
                )
                reporter(f"Vault note creation failed for {doc.sha256[:16]}: {exc}")

        for idx, chunk in enumerate(chunked.chunks):
            chunk_record = DBChunk(
                chunk_id=f"{extracted.sha256}:{idx}",
                doc_sha256=extracted.sha256,
                chunk_index=idx,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                token_count=chunk.token_count,
            )
            await db.insert_chunk(chunk_record, chunk.text)

        archive_time_ms = _elapsed_ms(archive_start_ns)
        total_time_ms = _elapsed_ms(total_start_ns)

        metric = IngestionMetric(
            doc_sha256=extracted.sha256,
            doc_type=extracted.doc_type,
            source=metric_source,
            total_time_ms=total_time_ms,
            extraction_time_ms=extracted.extraction_time_ms,
            normalization_time_ms=chunked.normalization_time_ms,
            archive_time_ms=archive_time_ms,
            created_at=datetime.now(timezone.utc),
        )
        await db.insert_metric(metric)

        indexing_result = await _upsert_to_qdrant(
            sha256=extracted.sha256,
            doc_type=extracted.doc_type,
            title=final_title,
            url=extracted.url,
            chunks=chunked.chunks,
            reporter=reporter,
        )
        if indexing_result is not None and indexing_result.indexed_chunks > 0:
            await db.mark_document_indexed(extracted.sha256)
            reporter(
                f"Indexed {indexing_result.indexed_chunks} chunks "
                f"({indexing_result.citation_chunks_skipped} citation chunks skipped)"
            )
            reporter(_CITATION_FILTER_NOTE)
        elif indexing_result is not None:
            reporter(
                f"Indexed 0 chunks ({indexing_result.citation_chunks_skipped} citation chunks skipped)"
            )

    reporter(f"Archived: {extracted.sha256[:16]} at {doc_dir}")

    return IngestResult(
        doc_sha256=extracted.sha256,
        title=final_title,
        doc_type=extracted.doc_type,
        archive_path=doc_dir,
        created=True,
        extraction_time_ms=extracted.extraction_time_ms,
        normalization_time_ms=chunked.normalization_time_ms,
        archive_time_ms=archive_time_ms,
        total_time_ms=total_time_ms,
    )


async def _upsert_to_qdrant(
    sha256: str,
    doc_type: str,
    title: str,
    url: str | None,
    chunks: list[Chunk],
    reporter: ProgressReporter,
) -> QdrantIndexingResult | None:
    """Embed chunks and upsert them into Qdrant."""
    from pkp.embedder import get_embedder
    from pkp.storage.qdrant import ensure_collection, qdrant_available, upsert_vectors

    if not qdrant_available():
        reporter(f"Vector index unavailable for {sha256[:16]}; skipping indexing")
        return None

    try:
        ensure_collection(doc_type)
    except Exception as exc:
        reporter(f"Vector collection setup failed for {doc_type}: {exc}")
        return None

    indexable_chunks = [chunk for chunk in chunks if not is_citation_chunk(chunk.text)]
    skipped_chunks = len(chunks) - len(indexable_chunks)
    if not indexable_chunks:
        return QdrantIndexingResult(
            indexed_chunks=0,
            citation_chunks_skipped=skipped_chunks,
        )

    for attempt in range(1, 3):
        try:
            embedder = get_embedder()
            chunk_texts = [chunk.text for chunk in indexable_chunks]
            embeddings = embedder.embed_chunks(chunk_texts)

            vectors: list[list[float]] = []
            sparse_data: list[tuple[list[int], list[float]]] = []
            payloads: list[dict[str, Any]] = []

            for idx, emb in enumerate(embeddings):
                chunk = indexable_chunks[idx]
                payloads.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "doc_sha256": sha256,
                        "chunk_index": chunk.chunk_index,
                        "content": chunk.text,
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                        "token_count": chunk.token_count,
                        "title": title,
                        "url": url,
                        "doc_type": doc_type,
                    }
                )
                vectors.append(emb.dense.tolist())
                indices, values = embedder.tokens_to_indices(chunk.text, emb.sparse[0])
                sparse_data.append((indices, values))

            chunk_ids = [chunk.chunk_id for chunk in indexable_chunks]
            upsert_vectors(
                doc_type=doc_type,
                chunk_ids=chunk_ids,
                dense_vectors=vectors,
                sparse_data=sparse_data,
                payloads=payloads,
            )
            return QdrantIndexingResult(
                indexed_chunks=len(indexable_chunks),
                citation_chunks_skipped=skipped_chunks,
            )
        except Exception as exc:
            if attempt == 1:
                reporter(f"Vector indexing failed for {sha256[:16]} (retrying): {exc}")
                continue
            reporter(f"Vector indexing failed for {sha256[:16]}: {exc}")
            return None

    return None


async def _repair_unindexed_document(sha256: str, reporter: ProgressReporter) -> None:
    """Re-attempt indexing for archived documents that never reached Qdrant."""
    async with db_context() as db:
        doc = await db.get_document(sha256)
    if doc is None or doc.indexed_at is not None:
        return

    reporter(f"Document archived but not indexed: {sha256[:16]}; retrying")
    indexed_chunk_count = await _index_existing_document(doc, reporter)
    if indexed_chunk_count is None:
        return

    if indexed_chunk_count > 0:
        async with db_context() as db:
            await db.mark_document_indexed(sha256)
        reporter(_CITATION_FILTER_NOTE)


async def _index_existing_document(
    doc: Document, reporter: ProgressReporter
) -> int | None:
    """Index one archived document without requiring a rebuild."""
    doc_dir = Path(doc.archive_path)
    chunks_file = doc_dir / "chunks.jsonl"
    if not chunks_file.exists():
        reporter(f"Skipping {doc.sha256[:16]}: no chunks file")
        return None

    chunks = _load_chunks(chunks_file)
    if not chunks:
        reporter(f"Skipping {doc.sha256[:16]}: no archived chunks")
        return None

    indexing_result = await _upsert_archived_chunks(
        doc=doc,
        chunks=chunks,
        reporter=reporter,
    )
    if indexing_result is None:
        return None

    reporter(
        f"Indexed {doc.sha256[:16]}: {indexing_result.indexed_chunks} chunks "
        f"({indexing_result.citation_chunks_skipped} citation chunks skipped)"
    )
    return indexing_result.indexed_chunks


async def _upsert_archived_chunks(
    doc: Document,
    chunks: list[dict[str, Any]],
    reporter: ProgressReporter,
) -> QdrantIndexingResult | None:
    """Embed archived chunks and upsert them into Qdrant."""
    from pkp.embedder import get_embedder
    from pkp.storage.qdrant import ensure_collection, qdrant_available, upsert_vectors

    if not qdrant_available():
        reporter(f"Vector index unavailable for {doc.sha256[:16]}; skipping indexing")
        return None

    try:
        ensure_collection(doc.doc_type)
    except Exception as exc:
        reporter(f"Vector collection setup failed for {doc.doc_type}: {exc}")
        return None

    indexable_chunks = [
        chunk for chunk in chunks if not is_citation_chunk(chunk["content"])
    ]
    skipped_chunks = len(chunks) - len(indexable_chunks)
    if not indexable_chunks:
        return QdrantIndexingResult(
            indexed_chunks=0,
            citation_chunks_skipped=skipped_chunks,
        )

    for attempt in range(1, 3):
        try:
            embedder = get_embedder()
            chunk_texts = [chunk["content"] for chunk in indexable_chunks]
            embeddings = embedder.embed_chunks(chunk_texts)

            vectors: list[list[float]] = []
            sparse_data: list[tuple[list[int], list[float]]] = []
            payloads: list[dict[str, Any]] = []

            for idx, emb in enumerate(embeddings):
                chunk = indexable_chunks[idx]
                payloads.append(
                    {
                        "chunk_id": chunk["chunk_id"],
                        "doc_sha256": doc.sha256,
                        "chunk_index": chunk["chunk_index"],
                        "content": chunk["content"],
                        "char_start": chunk.get("char_start", 0),
                        "char_end": chunk.get("char_end", 0),
                        "token_count": chunk.get("token_count"),
                        "title": doc.title,
                        "url": doc.url,
                        "doc_type": doc.doc_type,
                    }
                )
                vectors.append(emb.dense.tolist())
                indices, values = embedder.tokens_to_indices(
                    chunk["content"], emb.sparse[0]
                )
                sparse_data.append((indices, values))

            chunk_ids = [chunk["chunk_id"] for chunk in indexable_chunks]
            upsert_vectors(
                doc_type=doc.doc_type,
                chunk_ids=chunk_ids,
                dense_vectors=vectors,
                sparse_data=sparse_data,
                payloads=payloads,
            )
            return QdrantIndexingResult(
                indexed_chunks=len(indexable_chunks),
                citation_chunks_skipped=skipped_chunks,
            )
        except Exception as exc:
            if attempt == 1:
                reporter(
                    f"Vector indexing failed for {doc.sha256[:16]} (retrying): {exc}"
                )
                continue
            reporter(f"Vector indexing failed for {doc.sha256[:16]}: {exc}")
            return None

    return None


async def _setup_collections(
    client: QdrantClient,
    doc_types: list[str],
    filter_doc_type: str | None,
    rebuild_all: bool,
    reporter: ProgressReporter,
) -> None:
    """Drop and recreate collections when running a full rebuild."""
    from pkp.storage.qdrant import _get_collection_name, ensure_collection

    if not rebuild_all or not doc_types:
        return

    reporter("Dropping all collections for rebuild...")
    for dtype in doc_types:
        if filter_doc_type and dtype != filter_doc_type:
            continue
        collection_name = _get_collection_name(dtype)
        try:
            client.delete_collection(collection_name=collection_name)
            reporter(f"Dropped {collection_name}")
        except Exception:
            continue

    for dtype in doc_types:
        if filter_doc_type and dtype != filter_doc_type:
            continue
        ensure_collection(dtype)
        reporter(f"Created collection: {_get_collection_name(dtype)}")


async def _index_documents(
    client: QdrantClient,
    documents: list[Document],
    filter_doc_type: str | None,
    reporter: ProgressReporter,
) -> tuple[int, int]:
    """Index archived documents into Qdrant."""
    total_upserted = 0
    total_docs = 0

    for doc in documents:
        if filter_doc_type and doc.doc_type != filter_doc_type:
            continue

        result = await _index_single_document(client, doc, reporter)
        if result is None:
            continue
        total_docs += 1
        total_upserted += result

    return total_upserted, total_docs


async def _index_single_document(
    client: QdrantClient,
    doc: Document,
    reporter: ProgressReporter,
) -> int | None:
    """Index one archived document into Qdrant."""
    from pkp.storage.qdrant import _get_collection_name, ensure_collection

    doc_dir = Path(doc.archive_path)
    chunks_file = doc_dir / "chunks.jsonl"
    if not chunks_file.exists():
        reporter(f"Skipping {doc.sha256[:16]}: no chunks file")
        return None

    collection_name = _get_collection_name(doc.doc_type)
    try:
        client.scroll(collection_name=collection_name, limit=1, with_payload=False)
    except Exception:
        try:
            ensure_collection(doc.doc_type)
        except Exception as exc:
            reporter(f"Skipping {doc.doc_type}: {exc}")
            return None

    return await _index_existing_document(doc, reporter)


def _load_chunks(chunks_file: Path) -> list[dict[str, Any]]:
    """Load chunk payloads from disk."""
    chunks: list[dict[str, Any]] = []
    with open(chunks_file, encoding="utf-8") as file_obj:
        for line in file_obj:
            if line.strip():
                chunks.append(json.loads(line))
    return chunks


def _noop_reporter(_: str) -> None:
    """Stub reporter function for shared workflows."""


def _elapsed_ms(start_ns: int) -> int:
    """Return elapsed milliseconds from a perf_counter_ns start value."""
    return (time.perf_counter_ns() - start_ns) // 1_000_000


__all__ = [
    "IngestResult",
    "RebuildIndexResult",
    "ingest_pdf",
    "ingest_url",
    "rebuild_index",
    "ExtractionError",
]
