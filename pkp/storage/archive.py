"""Archive manager for immutable source storage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DocumentMetadata:
    """Metadata for an archived document."""

    url: str | None = None
    sha256: str = ""
    doc_type: str = "article"
    title: str = ""
    retrieved_at: str = ""
    content_type: str | None = None
    source_file: str | None = None
    error: str | None = None
    extraction_status: str = "pending"
    word_count: int | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class ArchivedDocument:
    """A document stored in the archive."""

    sha256: str
    original_path: Path
    extracted_path: Path
    normalized_path: Path
    chunks_path: Path
    meta_path: Path
    metadata: DocumentMetadata


class ArchiveManager:
    """Manages the immutable source archive.

    All writes are atomic (temp file + rename) to ensure no partial state.
    Once written, files are never modified.
    """

    def __init__(self, archive_path: Path) -> None:
        """Initialize with the archive base path."""
        self.archive_path = archive_path
        self.archive_path.mkdir(parents=True, exist_ok=True)

    def get_document_dir(self, sha256: str) -> Path:
        """Get the directory for a document by sha256."""
        return self.archive_path / sha256

    def document_exists(self, sha256: str) -> bool:
        """Check if a document exists in the archive."""
        doc_dir = self.get_document_dir(sha256)
        return doc_dir.exists() and (doc_dir / "original.html").exists()

    def write_original(
        self, sha256: str, content: bytes, extension: str = ".html"
    ) -> Path:
        """Write the original source content atomically."""
        doc_dir = self.get_document_dir(sha256)
        doc_dir.mkdir(parents=True, exist_ok=True)

        original_path = doc_dir / f"original{extension}"
        temp_path = doc_dir / f"original{extension}.tmp"

        with open(temp_path, "wb") as f:
            f.write(content)

        temp_path.rename(original_path)
        return original_path

    def write_extracted(self, sha256: str, content: str) -> Path:
        """Write the extracted Markdown content atomically."""
        doc_dir = self.get_document_dir(sha256)
        doc_dir.mkdir(parents=True, exist_ok=True)

        extracted_path = doc_dir / "extracted.md"
        temp_path = doc_dir / "extracted.md.tmp"

        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)

        temp_path.rename(extracted_path)
        return extracted_path

    def write_normalized(self, sha256: str, content: str) -> Path:
        """Write the normalized Markdown content atomically."""
        doc_dir = self.get_document_dir(sha256)
        doc_dir.mkdir(parents=True, exist_ok=True)

        normalized_path = doc_dir / "normalized.md"
        temp_path = doc_dir / "normalized.md.tmp"

        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)

        temp_path.rename(normalized_path)
        return normalized_path

    def write_chunks(self, sha256: str, chunks: list[dict[str, Any]]) -> Path:
        """Write the chunks JSONL file atomically."""
        doc_dir = self.get_document_dir(sha256)
        doc_dir.mkdir(parents=True, exist_ok=True)

        chunks_path = doc_dir / "chunks.jsonl"
        temp_path = doc_dir / "chunks.jsonl.tmp"

        with open(temp_path, "w", encoding="utf-8") as f:
            for chunk in chunks:
                f.write(json.dumps(chunk, ensure_ascii=False) + "\n")

        temp_path.rename(chunks_path)
        return chunks_path

    def write_metadata(self, sha256: str, metadata: DocumentMetadata) -> Path:
        """Write the metadata JSON file atomically."""
        doc_dir = self.get_document_dir(sha256)
        doc_dir.mkdir(parents=True, exist_ok=True)

        meta_path = doc_dir / "meta.json"
        temp_path = doc_dir / "meta.json.tmp"

        metadata.sha256 = sha256

        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(metadata.__dict__, f, ensure_ascii=False, indent=2)

        temp_path.rename(meta_path)
        return meta_path

    def get_archived_document(self, sha256: str) -> ArchivedDocument | None:
        """Get an archived document by sha256."""
        doc_dir = self.get_document_dir(sha256)

        if not doc_dir.exists():
            return None

        original_path = doc_dir / "original.html"
        if not original_path.exists():
            original_path = doc_dir / "original.pdf"
            if not original_path.exists():
                return None

        extracted_path = doc_dir / "extracted.md"
        normalized_path = doc_dir / "normalized.md"
        chunks_path = doc_dir / "chunks.jsonl"
        meta_path = doc_dir / "meta.json"

        metadata = DocumentMetadata()
        if meta_path.exists():
            with open(meta_path, encoding="utf-8") as f:
                data = json.load(f)
                metadata = DocumentMetadata(
                    **{
                        k: v
                        for k, v in data.items()
                        if k in DocumentMetadata.__dataclass_fields__
                    }
                )

        return ArchivedDocument(
            sha256=sha256,
            original_path=original_path,
            extracted_path=extracted_path,
            normalized_path=normalized_path,
            chunks_path=chunks_path,
            meta_path=meta_path,
            metadata=metadata,
        )

    def read_normalized(self, sha256: str) -> str | None:
        """Read the normalized Markdown content."""
        doc_dir = self.get_document_dir(sha256)
        normalized_path = doc_dir / "normalized.md"

        if not normalized_path.exists():
            return None

        with open(normalized_path, encoding="utf-8") as f:
            return f.read()

    def read_chunks(self, sha256: str) -> list[dict[str, Any]]:
        """Read the chunks JSONL file."""
        doc_dir = self.get_document_dir(sha256)
        chunks_path = doc_dir / "chunks.jsonl"

        if not chunks_path.exists():
            return []

        chunks = []
        with open(chunks_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    chunks.append(json.loads(line))
        return chunks

    def list_all_documents(self) -> list[str]:
        """List all document sha256s in the archive."""
        if not self.archive_path.exists():
            return []

        sha256s = []
        for doc_dir in self.archive_path.iterdir():
            if doc_dir.is_dir():
                sha256s.append(doc_dir.name)
        return sorted(sha256s)


def compute_sha256(content: bytes | str) -> str:
    """Compute SHA256 hash of content."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def compute_sha256_file(path: Path) -> str:
    """Compute SHA256 hash of a file."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()
