"""CLI commands for PKP."""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import click
import httpx
import libsql_client

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

    click.echo("\nChecking external services...")

    try:
        asyncio.run(_check_services())
    except Exception as e:
        click.echo(f"Service check failed: {e}", err=True)


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

    try:
        async with libsql_client.create_client("http://localhost:8080") as client:
            result = await client.execute("SELECT 1 as health")
            if result.rows and len(result.rows) > 0:
                click.echo("Turso: OK (v0.24.33)")
            else:
                click.echo("Turso: ERROR - no rows returned", err=True)
    except httpx.ConnectError:
        click.echo("Turso: ERROR - connection failed", err=True)
    except Exception as e:
        click.echo(f"Turso: ERROR - {e}", err=True)


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
        )
        await db.insert_document(doc)

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
            created_at=datetime.utcnow(),
        )
        await db.insert_metric(metric)

        click.echo(f"Archived: {extracted.sha256[:16]} at {doc_dir}")

        if show_profile:
            return {
                "extraction_time_ms": extraction_time_ms,
                "normalization_time_ms": chunked.normalization_time_ms,
                "archive_time_ms": archive_time_ms,
                "total_time_ms": total_time_ms,
            }
        return None


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
        )
        await db.insert_document(doc)

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
            created_at=datetime.utcnow(),
        )
        await db.insert_metric(metric)

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


if __name__ == "__main__":
    main()
