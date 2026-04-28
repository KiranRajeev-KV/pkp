"""Configuration management for PKP."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import toml
from dotenv import load_dotenv

DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434"
LEGACY_LLM_ENDPOINTS = {
    "ollama": DEFAULT_OLLAMA_ENDPOINT,
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
}


def _load_env_file(data_dir: Path) -> None:
    """Load .env file from data directory."""
    env_path = data_dir / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def _coerce_llm_endpoint(value: Any) -> str | None:
    """Normalize configured or legacy provider values into a base endpoint."""
    if not isinstance(value, str):
        return None

    if value.startswith(("http://", "https://")):
        return value

    return LEGACY_LLM_ENDPOINTS.get(value)


def _resolve_llm_endpoint(data: dict[str, Any]) -> str:
    """Resolve llm endpoint with compatibility for legacy provider configs."""
    endpoint = _coerce_llm_endpoint(data.get("llm_endpoint"))
    if endpoint is not None:
        return endpoint

    legacy_provider = _coerce_llm_endpoint(data.get("llm_provider"))
    if legacy_provider is not None:
        return legacy_provider

    return DEFAULT_OLLAMA_ENDPOINT


def _resolve_llm_model(data: dict[str, Any]) -> str:
    """Resolve llm model without rewriting explicit saved selections."""
    model = data.get("llm_model")
    if model:
        return str(model)

    return "qwen3:4b"


@dataclass
class PKPConfig:
    """PKP configuration settings."""

    data_dir: Path = field(default_factory=lambda: Path.home() / ".pkp")
    vault_path: Path | None = None
    db_path: Path | None = None
    archive_path: Path | None = None
    index_path: Path | None = None

    user_agent: str = "PKP/0.1.0 (https://github.com/KiranRajeev-KV/pkp)"

    embedding_model: str = "BAAI/bge-m3"
    embedding_endpoint: str = "http://localhost:11434"
    embedding_dimension: int = 1024
    embed_batch_size: int = 32
    qdrant_url: str = "http://localhost:6333"

    llm_endpoint: str = DEFAULT_OLLAMA_ENDPOINT
    llm_model: str = "qwen3:4b"

    chunk_size_tokens: int = 512
    chunk_overlap_tokens: int = 64

    crawl4ai_timeout: float = 30.0
    crawl4ai_browser_type: str = "chromium"
    crawl4ai_headless: bool = True
    fallback_word_count_threshold: int = 150

    proposal_top_n: int = 10
    proposal_min_score: float = 0.15
    reranker: str = "local"
    reranker_min_score: float = 0.01
    auto_vault_on_ingest: bool = True

    def __post_init__(self) -> None:
        """Derive default paths from data_dir."""
        if self.vault_path is None:
            self.vault_path = self.data_dir / "vault"
        if self.db_path is None:
            self.db_path = self.data_dir / "db" / "metadata.db"
        if self.archive_path is None:
            self.archive_path = self.data_dir / "archive"
        if self.index_path is None:
            self.index_path = self.data_dir / "index"

    def ensure_dirs(self) -> None:
        """Ensure all required directories exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.db_path:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.archive_path:
            self.archive_path.mkdir(parents=True, exist_ok=True)
        if self.vault_path:
            self.vault_path.mkdir(parents=True, exist_ok=True)
        if self.index_path:
            self.index_path.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict[str, Any]:
        """Convert config to dictionary for serialization."""
        return {
            "data_dir": str(self.data_dir),
            "vault_path": str(self.vault_path) if self.vault_path else None,
            "db_path": str(self.db_path) if self.db_path else None,
            "archive_path": str(self.archive_path) if self.archive_path else None,
            "index_path": str(self.index_path) if self.index_path else None,
            "user_agent": self.user_agent,
            "embedding_model": self.embedding_model,
            "embedding_endpoint": self.embedding_endpoint,
            "embedding_dimension": self.embedding_dimension,
            "embed_batch_size": self.embed_batch_size,
            "qdrant_url": self.qdrant_url,
            "llm_endpoint": self.llm_endpoint,
            "llm_model": self.llm_model,
            "chunk_size_tokens": self.chunk_size_tokens,
            "chunk_overlap_tokens": self.chunk_overlap_tokens,
            "crawl4ai_timeout": self.crawl4ai_timeout,
            "crawl4ai_browser_type": self.crawl4ai_browser_type,
            "crawl4ai_headless": self.crawl4ai_headless,
            "fallback_word_count_threshold": self.fallback_word_count_threshold,
            "proposal_top_n": self.proposal_top_n,
            "proposal_min_score": self.proposal_min_score,
            "reranker": self.reranker,
            "reranker_min_score": self.reranker_min_score,
            "auto_vault_on_ingest": self.auto_vault_on_ingest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PKPConfig:
        """Create config from dictionary."""
        return cls(
            data_dir=Path(data.get("data_dir", str(Path.home() / ".pkp"))),
            vault_path=Path(data["vault_path"]) if data.get("vault_path") else None,
            db_path=Path(data["db_path"]) if data.get("db_path") else None,
            archive_path=Path(data["archive_path"])
            if data.get("archive_path")
            else None,
            index_path=Path(data["index_path"]) if data.get("index_path") else None,
            user_agent=data.get(
                "user_agent", "PKP/0.1.0 (https://github.com/KiranRajeev-KV/pkp)"
            ),
            embedding_model=data.get("embedding_model", "BAAI/bge-m3"),
            embedding_endpoint=data.get("embedding_endpoint", "http://localhost:11434"),
            embedding_dimension=data.get("embedding_dimension", 1024),
            embed_batch_size=data.get("embed_batch_size", 32),
            qdrant_url=data.get("qdrant_url", "http://localhost:6333"),
            llm_endpoint=_resolve_llm_endpoint(data),
            llm_model=_resolve_llm_model(data),
            chunk_size_tokens=data.get("chunk_size_tokens", 512),
            chunk_overlap_tokens=data.get("chunk_overlap_tokens", 64),
            crawl4ai_timeout=data.get("crawl4ai_timeout", 30.0),
            crawl4ai_browser_type=data.get("crawl4ai_browser_type", "chromium"),
            crawl4ai_headless=data.get("crawl4ai_headless", True),
            fallback_word_count_threshold=data.get(
                "fallback_word_count_threshold", 150
            ),
            proposal_top_n=data.get("proposal_top_n", 10),
            proposal_min_score=data.get("proposal_min_score", 0.15),
            reranker=data.get("reranker", "local"),
            reranker_min_score=data.get("reranker_min_score", 0.01),
            auto_vault_on_ingest=data.get("auto_vault_on_ingest", True),
        )


def load_config(config_path: Path | None = None) -> PKPConfig:
    """Load configuration from file or return defaults."""
    if config_path is None:
        config_path = Path.home() / ".pkp" / "config.toml"

    env_data_dir = config_path.parent
    _load_env_file(env_data_dir)

    if not config_path.exists():
        config = PKPConfig()
        config.ensure_dirs()
        save_config(config, config_path)
        return config

    with open(config_path, encoding="utf-8") as f:
        data = toml.load(f)

    if "pkp" in data:
        data = data["pkp"]

    return PKPConfig.from_dict(data)


def save_config(config: PKPConfig, config_path: Path | None = None) -> None:
    """Save configuration to file."""
    if config_path is None:
        config_path = config.data_dir / "config.toml"

    config.ensure_dirs()

    toml_data = {"pkp": config.to_dict()}

    temp_path = config_path.with_suffix(".toml.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        toml.dump(toml_data, f)

    temp_path.replace(config_path)


def get_config() -> PKPConfig:
    """Get the global configuration instance."""
    return load_config()
