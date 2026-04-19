"""CLI commands for PKP."""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import click
import httpx

from pkp import __version__
from pkp.config import get_config, load_config, save_config
from pkp.pipeline.extractor import (
    ExtractionError,
    ExtractorService,
    SourceRequest,
)
from pkp.pipeline.normalizer import NormalizerService
from pkp.storage.archive import ArchiveManager, DocumentMetadata
from pkp.storage.db import Document, IngestionMetric, db_context


@click.group()
@click.version_option(version=__version__)
def main() -> None:
    """PKP - Personal Knowledge Pipeline."""
    pass


@main.command()
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Data directory (default: ~/.pkp)",
)
@click.option(
    "--vault-path",
    type=click.Path(path_type=Path),
    default=None,
    help="Vault directory for Obsidian notes",
)
def init(data_dir: Path | None, vault_path: Path | None) -> None:
    """Initialize PKP configuration."""
    config = load_config()

    if data_dir:
        config.data_dir = data_dir
    if vault_path:
        config.vault_path = vault_path

    config.ensure_dirs()

    config_path = config.data_dir / "config.toml"
    save_config(config, config_path)

    click.echo(f"Initialized PKP at {config.data_dir}")
    click.echo(f"Archive: {config.archive_path}")
    if config.vault_path:
        click.echo(f"Vault: {config.vault_path}")

    asyncio.run(_init_database(config.db_path))  # type: ignore[arg-type]

    click.echo("\nChecking external services...")

    try:
        asyncio.run(_check_services())
    except Exception as e:
        click.echo(f"Service check failed: {e}", err=True)


async def _init_database(db_path: Path) -> None:
    """Initialize the database with schema."""
    from pkp.storage.db import Database

    try:
        db = Database(db_path)
        await db.connect()
        await db.close()
    except Exception as e:
        click.echo(f"Database initialization failed: {e}", err=True)


async def _check_services() -> None:
    crawl4ai = ("Crawl4AI", "http://localhost:11235", "/health", 200)
    qdrant = ("Qdrant", "http://localhost:6333", "/", 200)

    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, url, path, expected in [crawl4ai, qdrant]:
            try:
                response = await client.get(f"{url}{path}")
                if response.status_code != expected:
                    click.echo(f"{name}: ERROR - http {response.status_code}", err=True)
                    continue
                data = response.json() if response.text else {}
                version = (
                    data.get("version") or data.get("title", "").split()[-1]
                    if data
                    else "unknown"
                )
                click.echo(f"{name}: OK (v{version})")
            except httpx.ConnectError:
                click.echo(f"{name}: ERROR - connection failed", err=True)
            except httpx.TimeoutException:
                click.echo(f"{name}: ERROR - timeout", err=True)
            except Exception as e:
                click.echo(f"{name}: ERROR - {e}", err=True)

    config = get_config()
    try:
        if config.db_path is None or not config.db_path.exists():
            click.echo("SQLite: ERROR - database not configured", err=True)
        else:
            async with aiosqlite.connect(str(config.db_path)) as db:
                await db.execute("SELECT 1")
                click.echo("SQLite: OK (local file)")
    except FileNotFoundError:
        click.echo("SQLite: ERROR - database not found", err=True)
    except Exception as e:
        click.echo(f"SQLite: ERROR - {e}", err=True)


@main.command()
@click.argument("url")
@click.option(
    "--async",
    "run_async",
    is_flag=True,
    help="Run ingestion asynchronously",
)
@click.option(
    "--profile",
    "show_profile",
    is_flag=True,
    help="Show timing information",
)
def ingest_url(url: str, run_async: bool, show_profile: bool) -> None:
    """Ingest content from a URL."""
    if run_async:
        _ingest_url_async(url)
    else:
        asyncio.run(_ingest_url_sync(url, show_profile))


async def _ingest_url_sync(url: str, show_profile: bool = False) -> None:
    """Sync URL ingestion."""
    try:
        async with asyncio.timeout(120):
            timing = await _do_ingest_url(url, show_profile)
            if timing and show_profile:
                _print_timing(timing)
    except TimeoutError:
        click.echo(f"Error: Timeout ingesting {url}", err=True)
        sys.exit(1)
    except ExtractionError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _ingest_url_async(url: str) -> None:
    """Async URL ingestion via job queue."""
    asyncio.run(_queue_ingest_job("ingest_url", {"url": url}))


def _print_timing(timing: dict) -> None:
    """Print timing information."""
    if timing.get("extraction_time_ms"):
        click.echo(f"[TIMER] extraction: {timing['extraction_time_ms']}ms")
    if timing.get("normalization_time_ms"):
        click.echo(f"[TIMER] normalization: {timing['normalization_time_ms']}ms")
    if timing.get("archive_time_ms"):
        click.echo(f"[TIMER] archive: {timing['archive_time_ms']}ms")
    if timing.get("total_time_ms"):
        click.echo(f"[TIMER] total: {timing['total_time_ms']}ms")


async def _do_ingest_url(url: str, show_profile: bool = False) -> dict | None:
    """Perform URL ingestion."""
    total_start = time.perf_counter()
    config = get_config()
    if config.archive_path is None:
        raise RuntimeError("Archive path not configured")
    archive = ArchiveManager(config.archive_path)

    click.echo(f"Extracting {url}...")

    extractor = ExtractorService()
    extracted = await extractor.extract(SourceRequest(url=url))

    extraction_time_ms = extracted.extraction_time_ms

    click.echo(f"Extracted: {extracted.title} ({extracted.sha256[:16]}...)")

    if archive.document_exists(extracted.sha256):
        click.echo(f"Document already archived: {extracted.sha256[:16]}")
        total_time_ms = int((time.perf_counter() - total_start) * 1000)
        if show_profile:
            return {
                "extraction_time_ms": extraction_time_ms,
                "normalization_time_ms": 0,
                "archive_time_ms": 0,
                "total_time_ms": total_time_ms,
            }
        return None

    chunk_size = config.chunk_size_tokens
    chunk_overlap = config.chunk_overlap_tokens
    normalizer = NormalizerService(
        chunk_size_tokens=chunk_size,
        chunk_overlap_tokens=chunk_overlap,
    )

    chunked = normalizer.normalize(
        sha256=extracted.sha256,
        title=extracted.title,
        extracted_text=extracted.text,
        url=extracted.url,
        doc_type=extracted.doc_type,
        metadata=extracted.metadata,
    )

    click.echo(f"Normalized: {len(chunked.chunks)} chunks, {chunked.word_count} words")

    doc_dir = archive.get_document_dir(extracted.sha256)
    doc_dir.mkdir(parents=True, exist_ok=True)

    if extracted.url:
        original_content = extracted.text.encode("utf-8")
        extension = ".html"
    else:
        raise ExtractionError("PDF source requires file path, not URL-based extraction")
    archive.write_original(extracted.sha256, original_content, extension)

    archive.write_extracted(extracted.sha256, extracted.text)
    archive.write_normalized(extracted.sha256, chunked.normalized_text)

    chunks_json = normalizer.chunks_to_jsonl(chunked.chunks)
    archive.write_chunks(extracted.sha256, chunks_json)

    archive_start = time.perf_counter()

    metadata = DocumentMetadata(
        url=extracted.url,
        sha256=extracted.sha256,
        doc_type=extracted.doc_type,
        title=extracted.title,
        retrieved_at=chunked.retrieved_at.isoformat(),
        content_type=extracted.metadata.get("content_type"),
        extraction_status="success",
        word_count=chunked.word_count,
    )
    archive.write_metadata(extracted.sha256, metadata)

    config = get_config()
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

        from pkp.storage.db import Chunk as DBChunk

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

        archive_time_ms = int((time.perf_counter() - archive_start) * 1000)
        total_time_ms = int((time.perf_counter() - total_start) * 1000)

        metric = IngestionMetric(
            doc_sha256=extracted.sha256,
            doc_type=extracted.doc_type,
            source=url,
            total_time_ms=total_time_ms,
            extraction_time_ms=extraction_time_ms,
            normalization_time_ms=chunked.normalization_time_ms,
            archive_time_ms=archive_time_ms,
            created_at=datetime.now(timezone.utc),
        )
        await db.insert_metric(metric)

        await _upsert_to_qdrant(
            extracted.sha256,
            extracted.doc_type,
            extracted.title,
            extracted.url,
            chunked.chunks,
        )

        await db.mark_document_indexed(extracted.sha256)

        click.echo(f"Archived: {extracted.sha256[:16]} at {doc_dir}")

        if show_profile:
            return {
                "extraction_time_ms": extraction_time_ms,
                "normalization_time_ms": chunked.normalization_time_ms,
                "archive_time_ms": archive_time_ms,
                "total_time_ms": total_time_ms,
            }
        return None


async def _upsert_to_qdrant(
    sha256: str,
    doc_type: str,
    title: str,
    url: str | None,
    chunks: list,
) -> None:
    """Embed chunks and upsert to Qdrant.

    Args:
        sha256: Document SHA256.
        doc_type: Document type.
        title: Document title.
        url: Document URL.
        chunks: List of Chunk objects.
    """
    from pkp.embedder import get_embedder
    from pkp.pipeline.normalizer import Chunk
    from pkp.storage.qdrant import ensure_collection, qdrant_available, upsert_vectors

    if not qdrant_available():
        return

    try:
        ensure_collection(doc_type)
    except Exception:
        return

    embedder = get_embedder()

    chunk_texts = [chunk.text for chunk in chunks]
    embeddings = embedder.embed_chunks(chunk_texts)

    vectors: list[list[float]] = []
    sparse_data: list[tuple[list[int], list[float]]] = []
    payloads: list[dict] = []

    for idx, emb in enumerate(embeddings):
        chunk = chunks[idx]
        if isinstance(chunk, Chunk):
            payload = {
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
        else:
            payload = {
                "chunk_id": f"{sha256}:{idx}",
                "doc_sha256": sha256,
                "chunk_index": idx,
                "content": chunk.text,
                "title": title,
                "url": url,
                "doc_type": doc_type,
            }

        vectors.append(emb.dense.tolist())

        indices, values = embedder.tokens_to_indices(chunk.text, emb.sparse[0])
        sparse_data.append((indices, values))

        payloads.append(payload)

    try:
        chunk_ids = [c.chunk_id for c in chunks]
        upsert_vectors(
            doc_type=doc_type,
            chunk_ids=chunk_ids,
            dense_vectors=vectors,
            sparse_data=sparse_data,
            payloads=payloads,
        )
    except Exception as e:
        click.echo(f"Error upserting to Qdrant: {e}", err=True)
        try:
            upsert_vectors(
                doc_type=doc_type,
                chunk_ids=chunk_ids,
                dense_vectors=vectors,
                sparse_data=sparse_data,
                payloads=payloads,
            )
        except Exception as e2:
            click.echo(f"Retry failed: {e2}", err=True)


async def _queue_ingest_job(job_type: str, payload: dict) -> None:
    """Queue an ingestion job."""
    import uuid

    async with db_context() as db:
        job_id = f"{job_type}-{uuid.uuid4().hex[:8]}"
        await db.create_job(job_id, job_type, payload)
        click.echo(f"Queued job: {job_id}")


@main.command()
@click.argument("pdf_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--async",
    "run_async",
    is_flag=True,
    help="Run ingestion asynchronously",
)
@click.option(
    "--profile",
    "show_profile",
    is_flag=True,
    help="Show timing information",
)
def ingest_pdf(pdf_path: Path, run_async: bool, show_profile: bool) -> None:
    """Ingest content from a PDF file."""
    if run_async:
        _ingest_pdf_async(pdf_path)
    else:
        asyncio.run(_ingest_pdf_sync(pdf_path, show_profile))


async def _ingest_pdf_sync(pdf_path: Path, show_profile: bool = False) -> None:
    """Sync PDF ingestion."""
    try:
        async with asyncio.timeout(300):
            timing = await _do_ingest_pdf(pdf_path, show_profile)
            if timing and show_profile:
                _print_timing(timing)
    except TimeoutError:
        click.echo(f"Error: Timeout ingesting {pdf_path}", err=True)
        sys.exit(1)
    except ExtractionError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _ingest_pdf_async(pdf_path: Path) -> None:
    """Async PDF ingestion via job queue."""
    asyncio.run(_queue_ingest_job("ingest_pdf", {"pdf_path": str(pdf_path)}))


async def _do_ingest_pdf(pdf_path: Path, show_profile: bool = False) -> dict | None:
    """Perform PDF ingestion."""
    total_start = time.perf_counter()
    config = get_config()
    if config.archive_path is None:
        raise RuntimeError("Archive path not configured")
    archive = ArchiveManager(config.archive_path)

    click.echo(f"Extracting {pdf_path}...")

    extractor = ExtractorService()
    extracted = await extractor.extract(SourceRequest(pdf_path=pdf_path))

    original_content = pdf_path.read_bytes()

    extraction_time_ms = extracted.extraction_time_ms

    click.echo(f"Extracted: {extracted.title} ({extracted.sha256[:16]}...)")

    if archive.document_exists(extracted.sha256):
        click.echo(f"Document already archived: {extracted.sha256[:16]}")
        total_time_ms = int((time.perf_counter() - total_start) * 1000)
        if show_profile:
            return {
                "extraction_time_ms": extraction_time_ms,
                "normalization_time_ms": 0,
                "archive_time_ms": 0,
                "total_time_ms": total_time_ms,
            }
        return None

    chunk_size = config.chunk_size_tokens
    chunk_overlap = config.chunk_overlap_tokens
    normalizer = NormalizerService(
        chunk_size_tokens=chunk_size,
        chunk_overlap_tokens=chunk_overlap,
    )

    chunked = normalizer.normalize(
        sha256=extracted.sha256,
        title=extracted.title,
        extracted_text=extracted.text,
        url=extracted.url,
        doc_type=extracted.doc_type,
        metadata=extracted.metadata,
    )

    click.echo(f"Normalized: {len(chunked.chunks)} chunks, {chunked.word_count} words")

    doc_dir = archive.get_document_dir(extracted.sha256)
    doc_dir.mkdir(parents=True, exist_ok=True)

    archive.write_original(extracted.sha256, original_content, ".pdf")

    archive.write_extracted(extracted.sha256, extracted.text)
    archive.write_normalized(extracted.sha256, chunked.normalized_text)

    chunks_json = normalizer.chunks_to_jsonl(chunked.chunks)
    archive.write_chunks(extracted.sha256, chunks_json)

    archive_start = time.perf_counter()

    metadata = DocumentMetadata(
        url=extracted.url,
        sha256=extracted.sha256,
        doc_type=extracted.doc_type,
        title=extracted.title,
        retrieved_at=chunked.retrieved_at.isoformat(),
        source_file=str(pdf_path),
        extraction_status="success",
        word_count=chunked.word_count,
    )
    archive.write_metadata(extracted.sha256, metadata)

    config = get_config()
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

        from pkp.storage.db import Chunk as DBChunk

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

        archive_time_ms = int((time.perf_counter() - archive_start) * 1000)
        total_time_ms = int((time.perf_counter() - total_start) * 1000)

        metric = IngestionMetric(
            doc_sha256=extracted.sha256,
            doc_type=extracted.doc_type,
            source=str(pdf_path),
            total_time_ms=total_time_ms,
            extraction_time_ms=extraction_time_ms,
            normalization_time_ms=chunked.normalization_time_ms,
            archive_time_ms=archive_time_ms,
            created_at=datetime.now(timezone.utc),
        )
        await db.insert_metric(metric)

        await _upsert_to_qdrant(
            extracted.sha256,
            extracted.doc_type,
            extracted.title,
            extracted.url,
            chunked.chunks,
        )

        await db.mark_document_indexed(extracted.sha256)

        click.echo(f"Archived: {extracted.sha256[:16]} at {doc_dir}")

        if show_profile:
            return {
                "extraction_time_ms": extraction_time_ms,
                "normalization_time_ms": chunked.normalization_time_ms,
                "archive_time_ms": archive_time_ms,
                "total_time_ms": total_time_ms,
            }
        return None


@main.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8000, help="Port to bind to")
def serve(host: str, port: int) -> None:
    """Start the PKP API server."""
    import uvicorn

    from pkp.api.app import app

    click.echo(f"Starting PKP API server at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


@main.command()
@click.argument("query")
@click.option("--limit", "-n", default=10, help="Max results")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--fts", "use_fts", is_flag=True, help="Force FTS5 search")
def search(query: str, limit: int, json_output: bool, use_fts: bool) -> None:
    """Search archived documents using Qdrant (hybrid) or FTS5."""
    asyncio.run(_do_search(query, limit, json_output, use_fts))


async def _do_search(
    query: str, limit: int, json_output: bool, use_fts: bool = False
) -> None:
    """Perform document search."""
    from pkp.storage.db import db_context
    from pkp.storage.qdrant import qdrant_available, search_documents_hybrid

    try:
        if use_fts:
            async with db_context() as db:
                results = await db.search_documents(query, limit)
        else:
            if qdrant_available():
                results = await search_documents_hybrid(query, limit)
            else:
                async with db_context() as db:
                    results = await db.search_documents(query, limit)

            if json_output:
                import json

                output = [
                    {
                        "doc_sha256": r.doc_sha256,
                        "title": r.title,
                        "url": r.url,
                        "match_count": r.match_count,
                        "best_rank": r.best_rank,
                    }
                    for r in results
                ]
                click.echo(json.dumps(output, indent=2))
                return

            if not results:
                click.echo("No results found.")
                return

            click.echo(f"{'TITLE':<25} {'URL':<30} {'CHUNKS':<8} {'RANK'}")
            click.echo("-" * 75)

            for r in results:
                title = r.title[:24] if len(r.title) > 24 else r.title
                url = r.url or ""
                if len(url) > 29:
                    url = url[:26] + "..."
                click.echo(
                    f"{title:<25} {url:<30} {r.match_count:<8} {r.best_rank:.2f}"
                )

    except Exception as e:
        click.echo(f"Search error: {e}", err=True)


@main.command()
def status() -> None:
    """Show PKP status."""
    config = get_config()

    click.echo(f"PKP Version: {__version__}")
    click.echo(f"Data Directory: {config.data_dir}")
    click.echo(f"Archive: {config.archive_path}")
    click.echo(f"Database: {config.db_path}")

    if config.vault_path:
        click.echo(f"Vault: {config.vault_path}")

    if config.archive_path is None:
        click.echo("Archive path not configured")
        return
    archive = ArchiveManager(config.archive_path)
    documents = archive.list_all_documents()
    click.echo(f"Documents: {len(documents)}")


@main.command()
@click.option(
    "--all",
    "rebuild_all",
    is_flag=True,
    default=False,
    help="Rebuild all documents (not just repair mode)",
)
@click.option("--doc-type", default=None, help="Filter by doc_type (article, pdf, etc)")
def rebuild_index(rebuild_all: bool, doc_type: str | None) -> None:
    """Rebuild Qdrant index from archived chunks.

    Repair mode (default): Skip documents already indexed in Qdrant.
    --all: Drop all collections and re-index everything fresh.
    """
    asyncio.run(_do_rebuild_index(doc_type, rebuild_all))


async def _do_rebuild_index(
    filter_doc_type: str | None, rebuild_all: bool = False
) -> None:
    """Rebuild Qdrant index from archived chunks."""
    from pkp.config import get_config
    from pkp.storage.db import db_context
    from pkp.storage.qdrant import (
        _get_qdrant_client,
        qdrant_available,
    )

    config = get_config()
    if config.archive_path is None:
        click.echo("Archive path not configured")
        return

    if not qdrant_available():
        click.echo("Qdrant not available")
        return

    client = _get_qdrant_client()

    async with db_context() as db:
        documents = await db.get_all_documents()

        if rebuild_all:
            doc_types = await db.get_distinct_doc_types()
        else:
            doc_types = []

    await _setup_collections(client, doc_types, filter_doc_type, rebuild_all)

    total_upserted, total_docs = await _index_documents(
        client, documents, filter_doc_type
    )

    click.echo(f"Done: {total_upserted} chunks indexed from {total_docs} documents")


async def _setup_collections(
    client: Any,
    doc_types: list[str],
    filter_doc_type: str | None,
    rebuild_all: bool,
) -> None:
    """Set up or drop collections based on rebuild mode."""
    from pkp.storage.qdrant import _get_collection_name, ensure_collection

    if not rebuild_all or not doc_types:
        return

    click.echo("Dropping all collections for rebuild...")
    for dtype in doc_types:
        if filter_doc_type and dtype != filter_doc_type:
            continue
        collection_name = _get_collection_name(dtype)
        try:
            client.delete_collection(collection_name=collection_name)
            click.echo(f"Dropped {collection_name}")
        except Exception:
            pass

    for dtype in doc_types:
        if filter_doc_type and dtype != filter_doc_type:
            continue
        ensure_collection(dtype)
        click.echo(f"Created collection: {dtype}_v1")


async def _index_documents(
    client: Any, documents: list[Any], filter_doc_type: str | None
) -> tuple[int, int]:
    """Index documents into Qdrant."""
    total_upserted = 0
    total_docs = 0

    for doc in documents:
        if filter_doc_type and doc.doc_type != filter_doc_type:
            continue

        result = await _index_single_document(client, doc, filter_doc_type)
        if result:
            total_docs += 1
            total_upserted += result

    return total_upserted, total_docs


async def _index_single_document(
    client: Any, doc: Any, filter_doc_type: str | None
) -> int | None:
    """Index a single document into Qdrant."""
    import json
    from pathlib import Path

    from pkp.embedder import get_embedder
    from pkp.storage.qdrant import (
        _get_collection_name,
        ensure_collection,
        upsert_vectors,
    )

    doc_dir = Path(doc.archive_path)
    chunks_file = doc_dir / "chunks.jsonl"

    if not chunks_file.exists():
        click.echo(f"Skipping {doc.sha256[:16]}: no chunks file")
        return None

    collection_name = _get_collection_name(doc.doc_type)

    try:
        client.scroll(collection_name=collection_name, limit=1, with_payload=False)
    except Exception:
        try:
            ensure_collection(doc.doc_type)
        except Exception as e:
            click.echo(f"Skipping {doc.doc_type}: {e}")
            return None

    chunks: list[dict] = []
    with open(chunks_file, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                chunks.append(json.loads(line))

    if not chunks:
        return None

    try:
        embedder = get_embedder()
        chunk_texts = [c["content"] for c in chunks]
        embeddings = embedder.embed_chunks(chunk_texts)

        vectors: list[list[float]] = []
        sparse_data: list[tuple[list[int], list[float]]] = []
        payloads: list[dict] = []

        for idx, emb in enumerate(embeddings):
            chunk = chunks[idx]
            payload = {
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

            vectors.append(emb.dense.tolist())

            indices, values = embedder.tokens_to_indices(
                chunk["content"], emb.sparse[0]
            )
            sparse_data.append((indices, values))

            payloads.append(payload)

        chunk_ids = [c["chunk_id"] for c in chunks]
        upsert_vectors(
            doc_type=doc.doc_type,
            chunk_ids=chunk_ids,
            dense_vectors=vectors,
            sparse_data=sparse_data,
            payloads=payloads,
        )

        click.echo(f"Indexed {doc.sha256[:16]}: {len(chunks)} chunks")
        return len(chunks)

    except Exception as e:
        click.echo(f"Error indexing {doc.sha256[:16]}: {e}")
        return None


if __name__ == "__main__":
    main()
