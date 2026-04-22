"""Shared ingest and index rebuild workflows."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pkp.config import get_config
from pkp.pipeline.extractor import (
    ExtractedDocument,
    ExtractionError,
    ExtractorService,
    SourceRequest,
)
from pkp.pipeline.normalizer import Chunk, NormalizerService
from pkp.storage.archive import ArchiveManager, DocumentMetadata
from pkp.storage.db import Chunk as DBChunk
from pkp.storage.db import Document, IngestionMetric, db_context

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

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

        indexed = await _upsert_to_qdrant(
            sha256=extracted.sha256,
            doc_type=extracted.doc_type,
            title=extracted.title,
            url=extracted.url,
            chunks=chunked.chunks,
            reporter=reporter,
        )
        if indexed:
            await db.mark_document_indexed(extracted.sha256)

    reporter(f"Archived: {extracted.sha256[:16]} at {doc_dir}")

    return IngestResult(
        doc_sha256=extracted.sha256,
        title=extracted.title,
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
) -> bool:
    """Embed chunks and upsert them into Qdrant."""
    from pkp.embedder import get_embedder
    from pkp.storage.qdrant import ensure_collection, qdrant_available, upsert_vectors

    if not qdrant_available():
        reporter(f"Vector index unavailable for {sha256[:16]}; skipping indexing")
        return False

    try:
        ensure_collection(doc_type)
    except Exception as exc:
        reporter(f"Vector collection setup failed for {doc_type}: {exc}")
        return False

    for attempt in range(1, 3):
        try:
            embedder = get_embedder()
            chunk_texts = [chunk.text for chunk in chunks]
            embeddings = embedder.embed_chunks(chunk_texts)

            vectors: list[list[float]] = []
            sparse_data: list[tuple[list[int], list[float]]] = []
            payloads: list[dict[str, Any]] = []

            for idx, emb in enumerate(embeddings):
                chunk = chunks[idx]
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

            chunk_ids = [chunk.chunk_id for chunk in chunks]
            upsert_vectors(
                doc_type=doc_type,
                chunk_ids=chunk_ids,
                dense_vectors=vectors,
                sparse_data=sparse_data,
                payloads=payloads,
            )
            return True
        except Exception as exc:
            if attempt == 1:
                reporter(f"Vector indexing failed for {sha256[:16]} (retrying): {exc}")
                continue
            reporter(f"Vector indexing failed for {sha256[:16]}: {exc}")
            return False

    return False


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

    async with db_context() as db:
        await db.mark_document_indexed(sha256)


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

    success = await _upsert_archived_chunks(
        doc=doc,
        chunks=chunks,
        reporter=reporter,
    )
    if not success:
        return None

    reporter(f"Indexed {doc.sha256[:16]}: {len(chunks)} chunks")
    return len(chunks)


async def _upsert_archived_chunks(
    doc: Document,
    chunks: list[dict[str, Any]],
    reporter: ProgressReporter,
) -> bool:
    """Embed archived chunks and upsert them into Qdrant."""
    from pkp.embedder import get_embedder
    from pkp.storage.qdrant import ensure_collection, qdrant_available, upsert_vectors

    if not qdrant_available():
        reporter(f"Vector index unavailable for {doc.sha256[:16]}; skipping indexing")
        return False

    try:
        ensure_collection(doc.doc_type)
    except Exception as exc:
        reporter(f"Vector collection setup failed for {doc.doc_type}: {exc}")
        return False

    for attempt in range(1, 3):
        try:
            embedder = get_embedder()
            chunk_texts = [chunk["content"] for chunk in chunks]
            embeddings = embedder.embed_chunks(chunk_texts)

            vectors: list[list[float]] = []
            sparse_data: list[tuple[list[int], list[float]]] = []
            payloads: list[dict[str, Any]] = []

            for idx, emb in enumerate(embeddings):
                chunk = chunks[idx]
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

            chunk_ids = [chunk["chunk_id"] for chunk in chunks]
            upsert_vectors(
                doc_type=doc.doc_type,
                chunk_ids=chunk_ids,
                dense_vectors=vectors,
                sparse_data=sparse_data,
                payloads=payloads,
            )
            return True
        except Exception as exc:
            if attempt == 1:
                reporter(
                    f"Vector indexing failed for {doc.sha256[:16]} (retrying): {exc}"
                )
                continue
            reporter(f"Vector indexing failed for {doc.sha256[:16]}: {exc}")
            return False

    return False


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
