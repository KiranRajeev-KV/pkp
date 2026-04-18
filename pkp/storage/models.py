"""Data models for PKP storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Document:
    """Document record from the documents table."""

    sha256: str
    title: str
    doc_type: str
    archive_path: str
    retrieved_at: datetime
    url: str | None = None
    indexed_at: datetime | None = None
    word_count: int | None = None
    vault_path: str | None = None
    tags: list[str] = field(default_factory=list)
    embedded_with: str = ""


@dataclass
class Chunk:
    """Chunk record from the chunks table."""

    chunk_id: str
    doc_sha256: str
    chunk_index: int
    char_start: int
    char_end: int
    token_count: int | None = None


@dataclass
class Proposal:
    """Proposal record from the proposals table."""

    proposal_id: str
    doc_a_sha256: str
    doc_b_sha256: str
    score: float
    rationale: str | None
    status: str
    created_at: datetime
    reviewed_at: datetime | None = None
    link_type: str | None = None


@dataclass
class Job:
    """Job record from the jobs table."""

    job_id: str
    job_type: str
    payload: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


@dataclass
class SearchResult:
    """Search result at document level."""

    doc_sha256: str
    title: str
    url: str | None
    match_count: int
    best_rank: float


@dataclass
class IngestionMetric:
    """Timing metrics for an ingestion."""

    doc_sha256: str = ""
    doc_type: str = ""
    source: str | None = None
    total_time_ms: int = 0
    extraction_time_ms: int | None = None
    normalization_time_ms: int | None = None
    archive_time_ms: int | None = None
    created_at: datetime | None = None
