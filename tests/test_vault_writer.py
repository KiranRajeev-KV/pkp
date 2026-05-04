from __future__ import annotations

from pathlib import Path

import pytest

from pkp.vault.writer import (
    MARKER_END,
    MARKER_START,
    VaultWriterError,
    _sanitize_filename,
    append_connection,
    create_document_note,
    write_notes_section,
)


def test_create_document_note_preserves_existing_file(
    vault_dir: Path,
    document_factory,
) -> None:
    doc = document_factory(
        title="Existing File",
        sha256="1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
    )
    note_path = vault_dir / _sanitize_filename(doc.title, doc.sha256)
    original_bytes = (
        b'---\ntitle: "Existing File"\n---\n\n# User Note\n\nUser-owned content.\n'
    )
    note_path.write_bytes(original_bytes)

    returned_path = create_document_note(vault_dir, doc)

    assert returned_path == note_path.resolve()
    assert note_path.read_bytes() == original_bytes


def test_append_connection_only_changes_bytes_inside_markers(
    vault_dir: Path,
    document_factory,
    proposal_factory,
) -> None:
    doc_a = document_factory(title="Alpha Note", sha256="a" * 64)
    doc_b = document_factory(title="Beta Note", sha256="b" * 64)
    proposal = proposal_factory(
        doc_a_sha256=doc_a.sha256,
        doc_b_sha256=doc_b.sha256,
        rationale="Shared theme.",
        link_type="extends",
    )
    note_path = create_document_note(vault_dir, doc_a)
    original_text = note_path.read_text(encoding="utf-8")
    start_marker_idx = original_text.index(MARKER_START)
    start_content_idx = start_marker_idx + len(MARKER_START)
    end_marker_idx = original_text.index(MARKER_END)
    prefix_before = original_text[:start_content_idx]
    suffix_before = original_text[end_marker_idx:]

    append_connection(vault_dir, doc_a, doc_b, proposal)

    updated_text = note_path.read_text(encoding="utf-8")
    assert updated_text[:start_content_idx] == prefix_before
    assert updated_text.endswith(suffix_before)


def test_append_connection_is_idempotent(
    vault_dir: Path,
    document_factory,
    proposal_factory,
) -> None:
    doc_a = document_factory(title="Alpha Note", sha256="a" * 64)
    doc_b = document_factory(title="Beta Note", sha256="b" * 64)
    proposal = proposal_factory(
        doc_a_sha256=doc_a.sha256,
        doc_b_sha256=doc_b.sha256,
        rationale="Shared theme.",
        link_type="related",
    )
    note_path = create_document_note(vault_dir, doc_a)

    append_connection(vault_dir, doc_a, doc_b, proposal)
    append_connection(vault_dir, doc_a, doc_b, proposal)

    text = note_path.read_text(encoding="utf-8")
    assert text.count("[[beta-note-bbbbbbbb]] - *related*: Shared theme.") == 1


def test_append_connection_raises_without_markers_and_preserves_file(
    vault_dir: Path,
    document_factory,
    proposal_factory,
) -> None:
    doc_a = document_factory(title="Broken Note", sha256="c" * 64)
    doc_b = document_factory(title="Beta Note", sha256="b" * 64)
    proposal = proposal_factory(
        doc_a_sha256=doc_a.sha256,
        doc_b_sha256=doc_b.sha256,
    )
    note_path = vault_dir / _sanitize_filename(doc_a.title, doc_a.sha256)
    original_bytes = (
        b"---\n"
        b'title: "Broken Note"\n'
        b"---\n"
        b"\n"
        b"# Broken Note\n"
        b"\n"
        b"## Connections\n"
        b"- no markers here\n"
    )
    note_path.write_bytes(original_bytes)

    with pytest.raises(VaultWriterError):
        append_connection(vault_dir, doc_a, doc_b, proposal)

    assert note_path.read_bytes() == original_bytes


def test_write_notes_section_replaces_generated_headings_without_touching_other_sections(
    vault_dir: Path,
    document_factory,
) -> None:
    doc = document_factory(title="Notes Target", sha256="d" * 64)
    note_path = create_document_note(vault_dir, doc)
    original_text = note_path.read_text(encoding="utf-8")
    connections_before = original_text[
        original_text.index("## Connections") : original_text.index("## Source")
    ]
    source_before = original_text[original_text.index("## Source") :]

    first_notes = (
        "## Key Concepts\n"
        "First generated notes with normal Markdown subheadings.\n\n"
        "## Findings / Results\n"
        "Initial findings."
    )
    second_notes = (
        "## Key Concepts\n"
        "Replacement notes.\n\n"
        "## Questions and Follow-ups\n"
        "New follow-up."
    )

    write_notes_section(vault_dir, doc, first_notes)
    write_notes_section(vault_dir, doc, second_notes)

    updated_text = note_path.read_text(encoding="utf-8")
    notes_region = updated_text[
        updated_text.index("## Notes") : updated_text.index("## Connections")
    ]
    assert "First generated notes" not in notes_region
    assert "Replacement notes." in notes_region
    assert "## Questions and Follow-ups" in notes_region
    assert (
        updated_text[
            updated_text.index("## Connections") : updated_text.index("## Source")
        ]
        == connections_before
    )
    assert updated_text[updated_text.index("## Source") :] == source_before


def test_write_notes_section_anchors_connections_boundary_to_managed_marker(
    vault_dir: Path,
    document_factory,
) -> None:
    doc = document_factory(title="Notes With Fake Connections", sha256="e" * 64)
    note_path = create_document_note(vault_dir, doc)
    first_notes = (
        "## Key Concepts\n"
        "The source discusses the phrase below as content.\n\n"
        "## Connections\n"
        "This is generated note content, not the vault section."
    )

    write_notes_section(vault_dir, doc, first_notes)
    write_notes_section(vault_dir, doc, "## Key Concepts\nReplacement notes.")

    updated_text = note_path.read_text(encoding="utf-8")
    notes_region = updated_text[
        updated_text.index("## Notes") : updated_text.index("## Connections")
    ]
    assert "This is generated note content" not in notes_region
    assert "Replacement notes." in notes_region
    assert updated_text.count(MARKER_START) == 1
    assert updated_text.count(MARKER_END) == 1


def test_sanitize_filename_uses_expected_slug_and_sha_prefix() -> None:
    title = "Mixed CASE & Special!!! Title / With   Spaces"
    sha256 = "deadbeefcafebabe00112233445566778899aabbccddeeff0011223344556677"

    filename = _sanitize_filename(title, sha256)

    assert filename == "mixed-case-special-title-with-spaces-deadbeef.md"
