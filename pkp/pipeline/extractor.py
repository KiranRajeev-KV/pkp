"""Extraction service for URLs and PDFs."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import trafilatura
from docling.document_converter import DocumentConverter

logger = logging.getLogger(__name__)

_ARXIV_ID_PATTERNS = (
    re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b"),
    re.compile(r"\b([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?\b"),
)


def extract_arxiv_id(text: str) -> str | None:
    """Extract a normalized arXiv ID from text."""
    for pattern in _ARXIV_ID_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return match.group(1)
    return None


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
    raw_html: bytes | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    doc_type: str = "article"
    archive_path: Path | None = None
    extraction_time_ms: int = 0


@dataclass
class CrawlResult:
    """Result from Crawl4AI extraction."""

    fit_markdown: str
    raw_markdown: str
    html: str
    word_count: int
    success: bool
    error_message: str | None = None


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

    def __init__(self, timeout: float = 30.0, user_agent: str | None = None) -> None:
        """Initialize with timeout and user agent."""
        self.timeout = timeout
        self.user_agent = (
            user_agent or "PKP/0.1.0 (https://github.com/KiranRajeev-KV/pkp)"
        )

    async def extract(self, url: str) -> ExtractedDocument:
        """Extract content from a URL."""
        start_time = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                raw_html = response.content
                html = response.text
        except httpx.HTTPStatusError as e:
            raise NetworkError(
                f"Failed to fetch URL {url}: HTTP {e.response.status_code} - {e}"
            ) from e
        except httpx.HTTPError as e:
            raise NetworkError(f"Failed to fetch URL {url}: {e}") from e

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

        docsha256 = hashlib.sha256(raw_html).hexdigest()

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

        extraction_time_ms = int((time.perf_counter() - start_time) * 1000)

        return ExtractedDocument(
            sha256=docsha256,
            url=url,
            title=title,
            text=result,
            raw_html=raw_html,
            metadata=metadata,
            doc_type="article",
            extraction_time_ms=extraction_time_ms,
        )

    def _extract_title_from_markdown(self, text: str) -> str:
        """Extract title from markdown content."""
        lines = text.strip().split("\n")
        for line in lines[:5]:
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
        return "Untitled Document"

    def extract_from_html(
        self, html: str, url: str, raw_html: bytes | None = None
    ) -> ExtractedDocument | None:
        """Extract content from pre-fetched HTML (fallback from Crawl4AI).

        Processes HTML that was already fetched by another extractor,
        avoiding a second network request.
        """
        import trafilatura

        result = trafilatura.extract(
            html,
            url=url,
            include_formatting=True,
            include_links=True,
            include_tables=True,
            output_format="markdown",
            with_metadata=True,
        )

        if not result or len(result.strip()) < 50:
            return None

        html_bytes = raw_html or html.encode("utf-8")
        docsha256 = hashlib.sha256(html_bytes).hexdigest()

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
        except Exception:
            pass

        title = metadata.get("title") or self._extract_title_from_markdown(result)

        return ExtractedDocument(
            sha256=docsha256,
            url=url,
            title=title,
            text=result,
            raw_html=html_bytes,
            metadata=metadata,
            doc_type="article",
            extraction_time_ms=0,
        )


class Crawl4AIExtractor:
    """Extract content from URLs using Crawl4AI."""

    def __init__(
        self,
        timeout: float = 30.0,
        headless: bool = True,
        browser_type: str = "chromium",
        user_agent: str | None = None,
    ) -> None:
        """Initialize with timeout and browser settings."""
        self.timeout = timeout
        self.headless = headless
        self.browser_type = browser_type
        self.user_agent = (
            user_agent or "PKP/0.1.0 (https://github.com/KiranRajeev-KV/pkp)"
        )

    async def extract(self, url: str) -> CrawlResult:
        """Extract content from a URL using Crawl4AI."""
        try:
            from crawl4ai import (
                AsyncWebCrawler,
                BrowserConfig,
                CacheMode,
                CrawlerRunConfig,
            )
        except ImportError as e:
            raise ExtractionError(f"crawl4ai not installed: {e}") from e

        browser_cfg = BrowserConfig(
            browser_type=self.browser_type,
            headless=self.headless,
            user_agent=self.user_agent,
        )
        run_cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            word_count_threshold=10,
            page_timeout=int(self.timeout * 1000),
        )

        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            result = await crawler.arun(url=url, config=run_cfg)

            fit_md = ""
            raw_md = ""

            if hasattr(result, "markdown") and result.markdown:
                fit_md = (
                    result.markdown.fit_markdown
                    if hasattr(result.markdown, "fit_markdown")
                    else str(result.markdown)
                )
                raw_md = (
                    result.markdown.raw_markdown
                    if hasattr(result.markdown, "raw_markdown")
                    else str(result.markdown)
                )
            elif hasattr(result, "text") and result.text:
                fit_md = str(result.text)
                raw_md = str(result.text)

            word_count = len(fit_md.split()) if fit_md else 0

            return CrawlResult(
                fit_markdown=fit_md,
                raw_markdown=raw_md,
                html=result.html if hasattr(result, "html") else "",
                word_count=word_count,
                success=result.success if hasattr(result, "success") else bool(fit_md),
                error_message=result.error_message
                if hasattr(result, "error_message")
                else None,
            )


class DoclingExtractor:
    """Extract content from PDFs using Docling."""

    def __init__(self) -> None:
        """Initialize the Docling extractor."""
        self.converter = DocumentConverter()

    async def extract(self, pdf_path: Path) -> ExtractedDocument:
        """Extract content from a PDF file."""
        start_time = time.perf_counter()

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

        extraction_time_ms = int((time.perf_counter() - start_time) * 1000)

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
            extraction_time_ms=extraction_time_ms,
        )

    def _extract_title(self, doc: Any, pdf_path: Path, markdown: str) -> str:
        """Extract title from document."""
        heading_title = self._extract_title_from_markdown(markdown)
        if heading_title:
            return heading_title

        if hasattr(doc, "name") and isinstance(doc.name, str):
            doc_name = doc.name.strip()
            if self._looks_like_real_title(doc_name, pdf_path):
                return doc_name

        return self._prettify_filename(pdf_path.stem)

    def _extract_title_from_markdown(self, markdown: str) -> str | None:
        """Extract a title-like heading from the opening markdown."""
        lines = markdown.strip().split("\n")
        for line in lines[:20]:
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
            if stripped.startswith("## "):
                return stripped[3:].strip()
        return None

    def _looks_like_real_title(self, title: str, pdf_path: Path) -> bool:
        """Return True when Docling provided a better title than the filename."""
        normalized = title.strip().casefold()
        if not normalized or normalized == "original":
            return False

        stem = pdf_path.stem.strip().casefold()
        return normalized != stem

    def _prettify_filename(self, stem: str) -> str:
        """Return a readable fallback title from the filename stem."""
        return stem.replace("_", " ").replace("-", " ").strip().title()


class ExtractorService:
    """Unified extraction service for URLs and PDFs."""

    def __init__(
        self,
        user_agent: str | None = None,
        use_crawl4ai: bool = True,
        crawl4ai_timeout: float = 30.0,
        crawl4ai_browser_type: str | None = None,
        crawl4ai_headless: bool = True,
        fallback_word_count_threshold: int = 150,
    ) -> None:
        """Initialize the extractor service."""
        if user_agent is None:
            from pkp import __version__

            user_agent = f"PKP/{__version__} (https://github.com/KiranRajeev-KV/pkp)"

        if crawl4ai_browser_type is None:
            from pkp.config import get_config

            crawl4ai_browser_type = get_config().crawl4ai_browser_type

        self.use_crawl4ai = use_crawl4ai
        self.crawl4ai_timeout = crawl4ai_timeout
        self.crawl4ai_browser_type = crawl4ai_browser_type
        self.crawl4ai_headless = crawl4ai_headless
        self.fallback_word_count_threshold = fallback_word_count_threshold
        self._user_agent = user_agent

        self.url_extractor = TrafilaturaExtractor(user_agent=user_agent)
        self.pdf_extractor = DoclingExtractor()
        self._crawl4ai_extractor: Crawl4AIExtractor | None = None

    @property
    def crawl4ai_extractor(self) -> Crawl4AIExtractor | None:
        """Lazy-load Crawl4AI extractor."""
        if self._crawl4ai_extractor is None and self.use_crawl4ai:
            try:
                self._crawl4ai_extractor = Crawl4AIExtractor(
                    timeout=self.crawl4ai_timeout,
                    headless=self.crawl4ai_headless,
                    browser_type=self.crawl4ai_browser_type,
                    user_agent=self._user_agent,
                )
            except Exception as e:
                logger.warning("Failed to initialize Crawl4AI extractor: %s", e)
        return self._crawl4ai_extractor

    async def extract(self, request: SourceRequest) -> ExtractedDocument:
        """Extract content from a URL or PDF."""
        if request.url:
            return await self.extract_url(request.url)
        elif request.pdf_path:
            return await self.extract_pdf(request.pdf_path)
        else:
            raise ExtractionError("No URL or PDF path provided")

    async def extract_url(self, url: str) -> ExtractedDocument:
        """Extract content from a URL.

        Strategy:
        1. Try Crawl4AI first (JS rendering, better for modern sites)
        2. If word count < threshold, try Trafilatura on the same HTML (no second request)
        3. If both fail or Crawl4AI unavailable, fall back to Trafilatura network fetch
        """
        import logging
        import time

        logger = logging.getLogger(__name__)
        start_time = time.perf_counter()

        if self.use_crawl4ai and self.crawl4ai_extractor:
            try:
                crawl_result = await self.crawl4ai_extractor.extract(url)

                if crawl_result.success and crawl_result.word_count > 0:
                    html_content = crawl_result.html
                    raw_html = html_content.encode("utf-8")

                    if crawl_result.word_count >= self.fallback_word_count_threshold:
                        docsha256 = hashlib.sha256(raw_html).hexdigest()

                        metadata: dict[str, Any] = {}
                        try:
                            import trafilatura

                            meta = trafilatura.extract_metadata(html_content)
                            if meta:
                                metadata = {
                                    "title": getattr(meta, "title", "") or "",
                                    "author": getattr(meta, "author", "") or "",
                                    "date": getattr(meta, "date", "") or "",
                                    "description": getattr(meta, "description", "")
                                    or "",
                                }
                        except Exception:
                            pass

                        title = metadata.get(
                            "title"
                        ) or self.url_extractor._extract_title_from_markdown(
                            crawl_result.fit_markdown
                        )

                        extraction_time_ms = int(
                            (time.perf_counter() - start_time) * 1000
                        )

                        return ExtractedDocument(
                            sha256=docsha256,
                            url=url,
                            title=title,
                            text=crawl_result.fit_markdown,
                            raw_html=raw_html,
                            metadata=metadata,
                            doc_type="article",
                            extraction_time_ms=extraction_time_ms,
                        )

                    if html_content and len(html_content.strip()) > 100:
                        fallback_result = self.url_extractor.extract_from_html(
                            html_content, url, raw_html=raw_html
                        )
                        if fallback_result and len(fallback_result.text) > len(
                            crawl_result.fit_markdown
                        ):
                            fallback_result.extraction_time_ms = int(
                                (time.perf_counter() - start_time) * 1000
                            )
                            fallback_result.sha256 = hashlib.sha256(
                                raw_html
                            ).hexdigest()
                            fallback_result.raw_html = raw_html
                            return fallback_result

                    logger.debug(
                        "Crawl4AI returned low word count (%d), "
                        "Trafilatura fallback also insufficient",
                        crawl_result.word_count,
                    )

            except ExtractionError:
                logger.debug("Crawl4AI failed, falling back to Trafilatura")

        result = await self.url_extractor.extract(url)
        result.extraction_time_ms = int((time.perf_counter() - start_time) * 1000)
        return result

    async def extract_pdf(self, pdf_path: Path) -> ExtractedDocument:
        """Extract content from a PDF."""
        return await self.pdf_extractor.extract(pdf_path)
