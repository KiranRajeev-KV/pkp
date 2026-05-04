from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pkp.pipeline.notes import NOTE_FAILURE_PLACEHOLDER, generate_notes
from pkp.storage.archive import ArchiveManager


async def test_generate_notes_writes_failure_placeholder(
    monkeypatch: Any,
    test_config: Any,
    document_factory: Any,
) -> None:
    monkeypatch.setattr("pkp.pipeline.notes.get_config", lambda: test_config)

    async def fake_generate_document_notes(
        body_text: str,
        endpoint: str,
        model: str,
        timeout: float = 180.0,
    ) -> str | None:
        return None

    monkeypatch.setattr(
        "pkp.pipeline.notes.generate_document_notes",
        fake_generate_document_notes,
    )

    doc = document_factory(
        sha256="f" * 64,
        title="Failed Notes Source",
        archive_path=str(test_config.archive_path / ("f" * 64)),
        vault_path=None,
    )

    class FakeDB:
        async def get_document(self, sha256: str) -> Any:
            if sha256 == doc.sha256:
                return doc
            return None

        async def mark_document_vault_path(self, sha256: str, vault_path: str) -> None:
            if sha256 == doc.sha256:
                doc.vault_path = vault_path

    @asynccontextmanager
    async def fake_db_context() -> AsyncIterator[FakeDB]:
        yield FakeDB()

    monkeypatch.setattr("pkp.pipeline.notes.db_context", fake_db_context)

    assert test_config.archive_path is not None
    archive = ArchiveManager(test_config.archive_path)
    archive.write_normalized(
        doc.sha256,
        "---\n"
        'title: "Failed Notes Source"\n'
        'pkp_version: "1"\n'
        "---\n\n"
        "Body text that should be sent to note generation.",
    )

    written = await generate_notes(doc.sha256)

    assert written is False
    assert doc.vault_path is not None
    note_text = Path(doc.vault_path).read_text(encoding="utf-8")
    placeholder = NOTE_FAILURE_PLACEHOLDER.format(sha256=doc.sha256)
    notes_region = note_text[
        note_text.index("## Notes") : note_text.index("## Connections")
    ]
    assert placeholder in notes_region
    assert "## Source" in note_text
