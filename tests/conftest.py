from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

from pkp.config import PKPConfig
from pkp.storage.db import Database
from pkp.storage.models import Document, Proposal


@pytest.fixture
def test_config(tmp_path: Path) -> PKPConfig:
    config = PKPConfig(
        data_dir=tmp_path / "data",
        vault_path=tmp_path / "vault",
        db_path=tmp_path / "db" / "metadata.db",
        archive_path=tmp_path / "archive",
        auto_vault_on_ingest=False,
    )
    config.ensure_dirs()
    return config


@pytest_asyncio.fixture
async def db(test_config: PKPConfig) -> Iterator[Database]:
    assert test_config.db_path is not None
    database = Database(test_config.db_path)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def vault_dir(test_config: PKPConfig) -> Path:
    assert test_config.vault_path is not None
    test_config.vault_path.mkdir(parents=True, exist_ok=True)
    return test_config.vault_path


@pytest.fixture
def document_factory() -> Callable[..., Document]:
    def factory(**overrides: object) -> Document:
        defaults: dict[str, object] = {
            "sha256": "a" * 64,
            "title": "Test Document",
            "doc_type": "article",
            "archive_path": "/tmp/archive/a",
            "retrieved_at": datetime(2026, 4, 25, tzinfo=UTC),
            "url": "https://example.com/test-document",
            "indexed_at": None,
            "word_count": 123,
            "vault_path": None,
            "tags": [],
            "embedded_with": "BAAI/bge-m3",
        }
        defaults.update(overrides)
        return Document(**defaults)

    return factory


@pytest.fixture
def proposal_factory() -> Callable[..., Proposal]:
    def factory(**overrides: object) -> Proposal:
        defaults: dict[str, object] = {
            "proposal_id": "proposal-12345678",
            "doc_a_sha256": "a" * 64,
            "doc_b_sha256": "b" * 64,
            "score": 0.85,
            "rationale": "Documents discuss related ideas.",
            "status": "pending",
            "created_at": datetime(2026, 4, 25, tzinfo=UTC),
            "reviewed_at": None,
            "link_type": "related",
        }
        defaults.update(overrides)
        return Proposal(**defaults)

    return factory
