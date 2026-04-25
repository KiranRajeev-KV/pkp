"""Write PKP-managed notes into an Obsidian vault."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from pkp.storage.models import Document, Proposal

logger = logging.getLogger(__name__)
MARKER_START = "<!-- pkp:connections:start -->"
MARKER_END = "<!-- pkp:connections:end -->"
PKP_VERSION = "1"


class VaultWriterError(Exception):
    """Raised when PKP cannot safely update a vault note."""


def _sanitize_filename(title: str, sha256: str) -> str:
    """Build a stable vault filename from a title and document hash."""
    slug = re.sub(r"[^a-z0-9-]", "-", title.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    slug = slug[:60].strip("-") or "document"
    return f"{slug}-{sha256[:8]}.md"


def _atomic_write(target: Path, content: str) -> None:
    """Write text to a file atomically using a sibling temp file and rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f"{target.name}.tmp"
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(target)


def _scaffold(doc: Document) -> str:
    """Build the initial vault note scaffold for one document."""
    source_value = (
        "null" if doc.url is None else json.dumps(doc.url, ensure_ascii=False)
    )
    return (
        "---\n"
        f"title: {json.dumps(doc.title, ensure_ascii=False)}\n"
        f"source: {source_value}\n"
        f'retrieved: "{doc.retrieved_at.date().isoformat()}"\n'
        f'doc_type: "{doc.doc_type}"\n'
        f'sha256: "{doc.sha256}"\n'
        f'pkp_version: "{PKP_VERSION}"\n'
        "---\n\n"
        f"# {doc.title}\n\n"
        "## Notes\n\n\n"
        "## Connections\n"
        f"{MARKER_START}\n"
        f"{MARKER_END}\n\n"
        "## Source\n\n"
        f"- Archive: {doc.archive_path}\n"
    )


def _note_path_for_document(vault_path: Path, doc: Document) -> Path:
    """Resolve the current note path for a document, preferring stored state."""
    if doc.vault_path:
        note_path = Path(doc.vault_path)
        if not note_path.is_absolute():
            note_path = vault_path / note_path
        return note_path.resolve()

    return (vault_path / _sanitize_filename(doc.title, doc.sha256)).resolve()


def _link_target_for_document(vault_path: Path, doc: Document) -> str:
    """Build an Obsidian wikilink target from the document's current note path."""
    note_path = _note_path_for_document(vault_path, doc)
    vault_root = vault_path.resolve()
    try:
        relative_path = note_path.relative_to(vault_root)
        return relative_path.with_suffix("").as_posix()
    except ValueError:
        return note_path.stem


def _format_connection_line(
    vault_path: Path, doc_b: Document, proposal: Proposal
) -> str:
    """Build one markdown connection line for an approved proposal."""
    link_target = _link_target_for_document(vault_path, doc_b)
    link_type = proposal.link_type or "related"
    rationale = (proposal.rationale or "").strip() or "Connection approved."
    return f"- [[{link_target}]] - *{link_type}*: {rationale}"


def create_document_note(vault_path: Path, doc: Document) -> Path:
    """Create a new vault note for a document if it does not already exist."""
    vault_path.mkdir(parents=True, exist_ok=True)
    note_path = (vault_path / _sanitize_filename(doc.title, doc.sha256)).resolve()
    if note_path.exists():
        logger.info("vault note exists sha256=%s path=%s", doc.sha256, note_path)
        return note_path

    _atomic_write(note_path, _scaffold(doc))
    logger.info("vault note created sha256=%s path=%s", doc.sha256, note_path)
    return note_path


def append_connection(
    vault_path: Path, doc_a: Document, doc_b: Document, proposal: Proposal
) -> Path:
    """Append one approved connection line inside an existing note's markers."""
    note_path = _note_path_for_document(vault_path, doc_a)
    if not note_path.exists():
        message = f"vault note missing for doc_a sha256={doc_a.sha256} path={note_path}"
        logger.error(message)
        raise VaultWriterError(message)

    text = note_path.read_text(encoding="utf-8")
    start_marker_idx = text.find(MARKER_START)
    end_marker_idx = text.find(MARKER_END)
    if start_marker_idx == -1:
        message = f"connection start marker missing path={note_path}"
        logger.error(message)
        raise VaultWriterError(message)
    if end_marker_idx == -1:
        message = f"connection end marker missing path={note_path}"
        logger.error(message)
        raise VaultWriterError(message)

    start_content_idx = start_marker_idx + len(MARKER_START)
    if end_marker_idx < start_content_idx:
        message = f"connection marker order invalid path={note_path}"
        logger.error(message)
        raise VaultWriterError(message)

    prefix = text[:start_content_idx]
    existing = text[start_content_idx:end_marker_idx]
    suffix = text[end_marker_idx:]
    connection_line = _format_connection_line(vault_path, doc_b, proposal)
    if connection_line in existing.splitlines():
        logger.info(
            "vault connection exists proposal_id=%s path=%s",
            proposal.proposal_id,
            note_path,
        )
        return note_path

    body = existing.rstrip()
    new_existing = f"{body}\n{connection_line}\n" if body else f"\n{connection_line}\n"
    new_text = f"{prefix}{new_existing}{suffix}"

    assert new_text[: len(prefix)] == prefix
    if suffix:
        assert new_text[-len(suffix) :] == suffix

    _atomic_write(note_path, new_text)
    logger.info(
        "vault connection appended proposal_id=%s path=%s",
        proposal.proposal_id,
        note_path,
    )
    return note_path
