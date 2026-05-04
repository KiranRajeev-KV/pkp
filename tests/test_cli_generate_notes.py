from __future__ import annotations

from pathlib import Path
from typing import Any

from pkp.cli import _document_needs_generated_notes
from pkp.pipeline.notes import NOTE_FAILURE_PLACEHOLDER


def _write_note(path: Path, notes: str) -> None:
    path.write_text(
        "---\n"
        'title: "Bulk Selection"\n'
        "---\n\n"
        "# Bulk Selection\n\n"
        "## Notes\n"
        f"{notes}\n\n"
        "## Connections\n"
        "<!-- pkp:connections:start -->\n"
        "<!-- pkp:connections:end -->\n\n"
        "## Source\n\n"
        "- Archive: /tmp/archive\n",
        encoding="utf-8",
    )


def test_document_needs_generated_notes_selects_missing_empty_and_failed(
    tmp_path: Path,
    document_factory: Any,
) -> None:
    missing_doc = document_factory(sha256="1" * 64, vault_path=str(tmp_path / "x.md"))
    assert _document_needs_generated_notes(missing_doc) is True

    empty_path = tmp_path / "empty.md"
    _write_note(empty_path, "")
    empty_doc = document_factory(sha256="2" * 64, vault_path=str(empty_path))
    assert _document_needs_generated_notes(empty_doc) is True

    failed_sha = "3" * 64
    failed_path = tmp_path / "failed.md"
    _write_note(failed_path, NOTE_FAILURE_PLACEHOLDER.format(sha256=failed_sha))
    failed_doc = document_factory(sha256=failed_sha, vault_path=str(failed_path))
    assert _document_needs_generated_notes(failed_doc) is True

    nonempty_path = tmp_path / "nonempty.md"
    _write_note(nonempty_path, "## Key Concepts\nUser or generated notes.")
    nonempty_doc = document_factory(sha256="4" * 64, vault_path=str(nonempty_path))
    assert _document_needs_generated_notes(nonempty_doc) is False


def test_document_needs_generated_notes_does_not_select_broken_notes_region(
    tmp_path: Path,
    document_factory: Any,
) -> None:
    note_path = tmp_path / "broken.md"
    note_path.write_text("# Broken\n\n## Notes\nNo terminator.\n", encoding="utf-8")
    doc = document_factory(sha256="5" * 64, vault_path=str(note_path))

    assert _document_needs_generated_notes(doc) is False
