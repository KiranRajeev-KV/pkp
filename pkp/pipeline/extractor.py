"""Extraction service for URLs and PDFs."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import trafilatura
from docling.document_converter import DocumentConverter

logger = logging.getLogger(__name__)


@dataclass
class SourceRequest:
    """Request for content extraction."""

    url: str | None = None
    pdf_path: Path | None = None


@dataclass
class ExtractedDocument:
    """Result of content extraction."""

    sha256: str
    url: str | None
    title: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    doc_type: str = "article"
    archive_path: Path | None = None


class ExtractionError(Exception):
    """Error during extraction."""

    pass


class NetworkError(ExtractionError):
    """Network-related extraction error."""

    pass


class ParserError(ExtractionError):
    """Parser-related extraction error."""

    pass


class TrafilaturaExtractor:
    """Extract content from URLs using Trafilatura."""

    def __init__(self, timeout: float = 30.0) -> None:
        """Initialize with timeout."""
        self.timeout = timeout

    async def extract(self, url: str) -> ExtractedDocument:
        """Extract content from a URL."""
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                html = response.text
        except httpx.HTTPError as e:
            raise NetworkError(f"Failed to fetch URL: {e}") from e

        result = trafilatura.extract(
            html,
            include_formatting=True,
            include_links=True,
            include_tables=True,
            output_format="markdown",
            with_metadata=True,
        )

        if not result or len(result.strip()) < 100:
            raise ParserError(
                f"Extraction returned insufficient content ({len(result) if result else 0} chars). "
                "The page may be paywalled, JavaScript-rendered, or unavailable."
            )

        docsha256 = hashlib.sha256(html.encode("utf-8")).hexdigest()

        metadata: dict[str, Any] = {}
        try:
            meta = trafilatura.extract_metadata(html)
            if meta:
                metadata = {
                    "title": getattr(meta, "title", "") or "",
                    "author": getattr(meta, "author", "") or "",
                    "date": getattr(meta, "date", "") or "",
                    "description": getattr(meta, "description", "") or "",
                }
        except Exception as e:
            logger.debug("Failed to extract metadata: %s", e)

        title = metadata.get("title") or self._extract_title_from_markdown(result)

        return ExtractedDocument(
            sha256=docsha256,
            url=url,
            title=title,
            text=result,
            metadata=metadata,
            doc_type="article",
        )

    def _extract_title_from_markdown(self, text: str) -> str:
        """Extract title from markdown content."""
        lines = text.strip().split("\n")
        for line in lines[:5]:
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
        return "Untitled Document"


class DoclingExtractor:
    """Extract content from PDFs using Docling."""

    def __init__(self) -> None:
        """Initialize the Docling extractor."""
        self.converter = DocumentConverter()

    async def extract(self, pdf_path: Path) -> ExtractedDocument:
        """Extract content from a PDF file."""
        if not pdf_path.exists():
            raise ParserError(f"PDF file not found: {pdf_path}")

        try:
            result = self.converter.convert(pdf_path)
            doc = result.document
        except Exception as e:
            raise ParserError(f"Failed to parse PDF: {e}") from e

        try:
            markdown = doc.export_to_markdown()
        except Exception as e:
            raise ParserError(f"Failed to export PDF to Markdown: {e}") from e

        if not markdown or len(markdown.strip()) < 100:
            raise ParserError(
                f"PDF extraction returned insufficient content ({len(markdown) if markdown else 0} chars). "
                "The PDF may be scanned or empty."
            )

        pdf_content = pdf_path.read_bytes()
        docsha256 = hashlib.sha256(pdf_content).hexdigest()

        title = self._extract_title(doc, pdf_path, markdown)

        metadata: dict[str, Any] = {
            "page_count": doc.num_pages if hasattr(doc, "num_pages") else 0,
        }

        return ExtractedDocument(
            sha256=docsha256,
            url=None,
            title=title,
            text=markdown,
            metadata=metadata,
            doc_type="pdf",
        )

    def _extract_title(self, doc: Any, pdf_path: Path, markdown: str) -> str:
        """Extract title from document."""
        if hasattr(doc, "name") and doc.name:
            title = doc.name
            if isinstance(title, str):
                return title

        lines = markdown.strip().split("\n")
        for line in lines[:10]:
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()

        return pdf_path.stem.replace("_", " ").replace("-", " ").strip().title()


class ExtractorService:
    """Unified extraction service for URLs and PDFs."""

    def __init__(self) -> None:
        """Initialize the extractor service."""
        self.url_extractor = TrafilaturaExtractor()
        self.pdf_extractor = DoclingExtractor()

    async def extract(self, request: SourceRequest) -> ExtractedDocument:
        """Extract content from a URL or PDF."""
        if request.url:
            return await self.url_extractor.extract(request.url)
        elif request.pdf_path:
            return await self.pdf_extractor.extract(request.pdf_path)
        else:
            raise ExtractionError("No URL or PDF path provided")

    async def extract_url(self, url: str) -> ExtractedDocument:
        """Extract content from a URL."""
        return await self.url_extractor.extract(url)

    async def extract_pdf(self, pdf_path: Path) -> ExtractedDocument:
        """Extract content from a PDF."""
        return await self.pdf_extractor.extract(pdf_path)
