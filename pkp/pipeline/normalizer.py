"""Normalization service for extracted documents."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import tiktoken


@dataclass
class Chunk:
    """A chunk of text from a document."""

    chunk_id: str
    doc_sha256: str
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    token_count: int


@dataclass
class ChunkedDocument:
    """A document that has been normalized and chunked."""

    sha256: str
    title: str
    url: str | None
    doc_type: str
    frontmatter: dict[str, Any]
    normalized_text: str
    chunks: list[Chunk]
    word_count: int
    token_count: int
    retrieved_at: datetime
    normalization_time_ms: int = 0


class NormalizerService:
    """Normalizes extracted documents and chunks them."""

    def __init__(
        self, chunk_size_tokens: int = 512, chunk_overlap_tokens: int = 64
    ) -> None:
        """Initialize the normalizer with chunking parameters."""
        self.chunk_size_tokens = chunk_size_tokens
        self.chunk_overlap_tokens = chunk_overlap_tokens
        self._enc: tiktoken.Encoding | None = None

    @property
    def enc(self) -> tiktoken.Encoding:
        """Lazily load tiktoken encoding."""
        if self._enc is None:
            self._enc = tiktoken.get_encoding("cl100k_base")
        return self._enc

    def normalize(
        self,
        sha256: str,
        title: str,
        extracted_text: str,
        url: str | None = None,
        doc_type: str = "article",
        metadata: dict[str, Any] | None = None,
    ) -> ChunkedDocument:
        """Normalize extracted text and create chunks."""
        start_time = time.perf_counter()
        now = datetime.utcnow()

        cleaned_text = self._clean_markdown(extracted_text)

        frontmatter = self._build_frontmatter(
            title=title,
            url=url,
            doc_type=doc_type,
            metadata=metadata or {},
            retrieved_at=now,
        )

        normalized_text = self._add_frontmatter_to_text(frontmatter, cleaned_text)

        word_count = len(cleaned_text.split())
        token_count = len(self.enc.encode(cleaned_text))

        chunks = self._chunk_text(
            sha256=sha256,
            text=cleaned_text,
            frontmatter=frontmatter,
        )

        normalization_time_ms = int((time.perf_counter() - start_time) * 1000)

        return ChunkedDocument(
            sha256=sha256,
            title=title,
            url=url,
            doc_type=doc_type,
            frontmatter=frontmatter,
            normalized_text=normalized_text,
            chunks=chunks,
            word_count=word_count,
            token_count=token_count,
            retrieved_at=now,
            normalization_time_ms=normalization_time_ms,
        )

    def _clean_markdown(self, text: str) -> str:
        """Clean extracted markdown."""
        lines = text.split("\n")
        cleaned_lines: list[str] = []
        prev_line_empty = True

        for line in lines:
            stripped = line.rstrip()

            if stripped.startswith("```") and len(stripped) > 3:
                cleaned_lines.append(stripped)
                prev_line_empty = False
                continue

            if not stripped and prev_line_empty:
                continue

            cleaned_lines.append(stripped)
            prev_line_empty = not stripped

        while cleaned_lines and not cleaned_lines[-1]:
            cleaned_lines.pop()

        text = "\n".join(cleaned_lines)
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text.strip()

    def _build_frontmatter(
        self,
        title: str,
        url: str | None,
        doc_type: str,
        metadata: dict[str, Any],
        retrieved_at: datetime,
    ) -> dict[str, Any]:
        """Build YAML frontmatter."""
        frontmatter: dict[str, Any] = {
            "title": title,
            "doc_type": doc_type,
            "retrieved_at": retrieved_at.strftime("%Y-%m-%d"),
            "pkp_version": "1",
        }

        if url:
            frontmatter["source"] = url

        if metadata.get("author"):
            frontmatter["author"] = metadata["author"]

        if metadata.get("description"):
            frontmatter["description"] = metadata["description"]

        if frontmatter.get("date"):
            del frontmatter["date"]

        return frontmatter

    def _add_frontmatter_to_text(self, frontmatter: dict[str, Any], text: str) -> str:
        """Add YAML frontmatter to markdown text."""
        fm_lines = ["---"]
        for key, value in frontmatter.items():
            if isinstance(value, list):
                fm_lines.append(f"{key}: [{', '.join(repr(v) for v in value)}]")
            elif isinstance(value, bool):
                fm_lines.append(f"{key}: {str(value).lower()}")
            elif isinstance(value, int):
                fm_lines.append(f"{key}: {value}")
            else:
                fm_lines.append(f'{key}: "{value}"')
        fm_lines.append("---")
        fm_lines.append("")

        return "\n".join(fm_lines) + "\n" + text

    def _chunk_text(
        self, sha256: str, text: str, frontmatter: dict[str, Any]
    ) -> list[Chunk]:
        """Chunk text into fixed-size pieces with overlap."""
        tokens = self.enc.encode(text)
        total_tokens = len(tokens)

        if total_tokens <= self.chunk_size_tokens:
            chunk = Chunk(
                chunk_id=f"{sha256}:0",
                doc_sha256=sha256,
                chunk_index=0,
                text=text,
                char_start=0,
                char_end=len(text),
                token_count=total_tokens,
            )
            return [chunk]

        chunks: list[Chunk] = []
        start_idx = 0
        chunk_index = 0

        while start_idx < total_tokens:
            end_idx = min(start_idx + self.chunk_size_tokens, total_tokens)

            chunk_tokens = tokens[start_idx:end_idx]
            chunk_text = self.enc.decode(chunk_tokens)

            char_start = len(self.enc.decode(tokens[:start_idx]))
            char_end = char_start + len(chunk_text)

            chunk = Chunk(
                chunk_id=f"{sha256}:{chunk_index}",
                doc_sha256=sha256,
                chunk_index=chunk_index,
                text=chunk_text,
                char_start=char_start,
                char_end=char_end,
                token_count=len(chunk_tokens),
            )
            chunks.append(chunk)

            if end_idx >= total_tokens:
                break

            start_idx = end_idx - self.chunk_overlap_tokens
            chunk_index += 1

        return chunks

    def chunks_to_jsonl(self, chunks: list[Chunk]) -> list[dict[str, Any]]:
        """Convert chunks to JSONL format."""
        return [
            {
                "chunk_id": chunk.chunk_id,
                "doc_sha256": chunk.doc_sha256,
                "chunk_index": chunk.chunk_index,
                "content": chunk.text,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "token_count": chunk.token_count,
            }
            for chunk in chunks
        ]


def extract_title_from_markdown(text: str) -> str:
    """Extract title from markdown heading."""
    lines = text.strip().split("\n")
    for line in lines[:10]:
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled Document"


def classify_doc_type(text: str, url: str | None = None) -> str:
    """Classify document type based on content and URL."""
    if url:
        url_lower = url.lower()
        if "arxiv.org" in url_lower or "pdf" in url_lower:
            return "paper"
        if "github.com" in url_lower or "readme" in url_lower:
            return "documentation"

    text_lower = text.lower()
    if any(
        marker in text_lower
        for marker in ["references", "bibliography", "doi:", "arxiv:"]
    ):
        return "paper"
    if any(
        marker in text_lower
        for marker in ["api", "function", "class", "parameter", "usage"]
    ):
        return "documentation"

    return "article"
