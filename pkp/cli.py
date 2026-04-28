"""CLI commands for PKP."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiosqlite
import click
import httpx

from pkp import __version__
from pkp.config import get_config, load_config, save_config
from pkp.pipeline.extractor import extract_arxiv_id
from pkp.pipeline.ingest import (
    ExtractionError,
    fetch_arxiv_metadata,
)
from pkp.pipeline.ingest import (
    ingest_pdf as run_ingest_pdf,
)
from pkp.pipeline.ingest import (
    ingest_url as run_ingest_url,
)
from pkp.pipeline.ingest import (
    rebuild_index as run_rebuild_index,
)
from pkp.storage.archive import ArchiveManager
from pkp.storage.db import db_context


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
            result = await run_ingest_url(url, reporter=click.echo)
            if result.created:
                await _queue_generate_proposals_job(result.doc_sha256)
            if show_profile:
                _print_timing(result.timing())
    except TimeoutError:
        click.echo(f"Error: Timeout ingesting {url}", err=True)
        sys.exit(1)
    except ExtractionError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _ingest_url_async(url: str) -> None:
    """Async URL ingestion via job queue."""
    asyncio.run(_queue_ingest_job("ingest_url", {"url": url}))


def _print_timing(timing: dict[str, int]) -> None:
    """Print timing information."""
    if timing.get("extraction_time_ms"):
        click.echo(f"[TIMER] extraction: {timing['extraction_time_ms']}ms")
    if timing.get("normalization_time_ms"):
        click.echo(f"[TIMER] normalization: {timing['normalization_time_ms']}ms")
    if timing.get("archive_time_ms"):
        click.echo(f"[TIMER] archive: {timing['archive_time_ms']}ms")
    if timing.get("total_time_ms"):
        click.echo(f"[TIMER] total: {timing['total_time_ms']}ms")


async def _queue_ingest_job(job_type: str, payload: dict[str, str]) -> None:
    """Queue an ingestion job."""
    import uuid

    async with db_context() as db:
        job_id = f"{job_type}-{uuid.uuid4().hex[:8]}"
        await db.create_job(job_id, job_type, payload)
        click.echo(f"Queued job: {job_id}")


async def _queue_generate_proposals_job(doc_sha256: str) -> None:
    """Queue proposal generation for an ingested document."""
    await _queue_ingest_job("generate_proposals", {"doc_sha256": doc_sha256})


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
            result = await run_ingest_pdf(pdf_path, reporter=click.echo)
            if result.created:
                await _queue_generate_proposals_job(result.doc_sha256)
            if show_profile:
                _print_timing(result.timing())
    except TimeoutError:
        click.echo(f"Error: Timeout ingesting {pdf_path}", err=True)
        sys.exit(1)
    except ExtractionError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _ingest_pdf_async(pdf_path: Path) -> None:
    """Async PDF ingestion via job queue."""
    asyncio.run(_queue_ingest_job("ingest_pdf", {"pdf_path": str(pdf_path)}))


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
                    "reranker_score": r.reranker_score,
                }
                for r in results
            ]
            click.echo(json.dumps(output, indent=2))
            return

        if not results:
            click.echo("No results found.")
            return

        click.echo(f"{'TITLE':<25} {'URL':<30} {'CHUNKS':<8} {'SCORE'}")
        click.echo("-" * 75)

        for r in results:
            title = r.title[:24] if len(r.title) > 24 else r.title
            url = r.url or ""
            if len(url) > 29:
                url = url[:26] + "..."
            score = r.reranker_score if r.reranker_score is not None else r.best_rank
            click.echo(f"{title:<25} {url:<30} {r.match_count:<8} {score:.2f}")

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


@main.command("enrich-titles")
def enrich_titles() -> None:
    """Enrich PDF titles from arXiv metadata."""
    asyncio.run(_enrich_titles())


async def _enrich_titles() -> None:
    """Fetch arXiv metadata for archived PDFs and update titles."""
    config = get_config()
    if config.archive_path is None:
        click.echo("Archive path not configured", err=True)
        sys.exit(1)

    archive = ArchiveManager(config.archive_path)
    updated = 0
    no_id = 0
    failed = 0

    async with db_context() as db:
        documents = await db.get_all_documents()
        pdf_documents = [doc for doc in documents if doc.doc_type == "pdf"]

        for doc in pdf_documents:
            archived = archive.get_archived_document(doc.sha256)
            if archived is None:
                failed += 1
                click.echo(f"✗ Enrichment failed: {doc.title} — archive not found")
                continue

            candidates = [
                doc.title,
                archived.metadata.title,
                doc.archive_path,
            ]
            if archived.metadata.source_file:
                candidates.append(Path(archived.metadata.source_file).stem)

            arxiv_id = None
            for candidate in candidates:
                arxiv_id = extract_arxiv_id(candidate)
                if arxiv_id is not None:
                    break

            if arxiv_id is None:
                no_id += 1
                click.echo(f"✗ No arXiv ID found: {doc.title}")
                continue

            metadata = await fetch_arxiv_metadata(arxiv_id)
            if metadata is None:
                failed += 1
                click.echo(
                    f"✗ Enrichment failed: {doc.title} — metadata unavailable for {arxiv_id}"
                )
                continue

            old_title = doc.title
            new_title = metadata["title"]
            await db.update_document_title(doc.sha256, new_title)

            archived.metadata.title = new_title
            archived.metadata.arxiv_id = metadata["arxiv_id"]
            archived.metadata.arxiv_authors = metadata["authors"]
            archived.metadata.arxiv_abstract = metadata["abstract"]
            archive.write_metadata(doc.sha256, archived.metadata)

            updated += 1
            click.echo(f"✓ Updated: {old_title} → {new_title}")

    click.echo("")
    click.echo(f"Summary: {updated} updated, {no_id} with no arXiv ID, {failed} failed")
    click.echo(
        "Run pkp rebuild-index --all and re-enqueue generate_proposals jobs to refresh proposal rationales with corrected titles."
    )


@main.command(name="backfill-vault")
def backfill_vault() -> None:
    """Create missing vault notes for documents without a stored vault path."""
    failed = asyncio.run(_do_backfill_vault())
    if failed > 0:
        sys.exit(1)


async def _do_backfill_vault() -> int:
    """Backfill vault notes for documents whose SQLite vault_path is null."""
    config = get_config()
    if config.vault_path is None:
        click.echo("Backfill error: vault path not configured", err=True)
        return 1

    from pkp.vault.writer import VaultWriterError, create_document_note

    created = 0
    skipped = 0
    failed = 0

    async with db_context() as db:
        documents = await db.get_all_documents()
        for doc in documents:
            if doc.vault_path is not None:
                continue

            try:
                note_path = create_document_note(config.vault_path, doc)
                await db.mark_document_vault_path(doc.sha256, str(note_path))
                created += 1
                click.echo(f"✓ Created: {doc.title}")
            except VaultWriterError as exc:
                failed += 1
                click.echo(f"✗ Failed: {doc.title} — {exc}", err=True)
            except Exception as exc:
                failed += 1
                click.echo(f"✗ Failed: {doc.title} — {exc}", err=True)

    click.echo(
        f"Backfill complete: {created} created, {skipped} skipped, {failed} failed."
    )
    return failed


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
    result = await run_rebuild_index(
        filter_doc_type=filter_doc_type,
        rebuild_all=rebuild_all,
        reporter=click.echo,
    )
    click.echo(
        f"Done: {result.total_upserted} chunks indexed from {result.total_documents} documents"
    )


if __name__ == "__main__":
    main()
