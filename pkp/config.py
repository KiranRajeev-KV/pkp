"""Configuration management for PKP."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import toml


@dataclass
class PKPConfig:
    """PKP configuration settings."""

    data_dir: Path = field(default_factory=lambda: Path.home() / ".pkp")
    vault_path: Path | None = None
    db_path: Path | None = None
    archive_path: Path | None = None
    index_path: Path | None = None

    embedding_model: str = "nomic-embed-text"
    embedding_endpoint: str = "http://localhost:11434"
    embedding_dimension: int = 768

    llm_provider: str = "ollama"
    llm_model: str = "mistral-nemo"

    chunk_size_tokens: int = 512
    chunk_overlap_tokens: int = 64

    proposal_top_n: int = 10
    reranker: str = "none"

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
            "embedding_model": self.embedding_model,
            "embedding_endpoint": self.embedding_endpoint,
            "embedding_dimension": self.embedding_dimension,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "chunk_size_tokens": self.chunk_size_tokens,
            "chunk_overlap_tokens": self.chunk_overlap_tokens,
            "proposal_top_n": self.proposal_top_n,
            "reranker": self.reranker,
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
            embedding_model=data.get("embedding_model", "nomic-embed-text"),
            embedding_endpoint=data.get("embedding_endpoint", "http://localhost:11434"),
            embedding_dimension=data.get("embedding_dimension", 768),
            llm_provider=data.get("llm_provider", "ollama"),
            llm_model=data.get("llm_model", "mistral-nemo"),
            chunk_size_tokens=data.get("chunk_size_tokens", 512),
            chunk_overlap_tokens=data.get("chunk_overlap_tokens", 64),
            proposal_top_n=data.get("proposal_top_n", 10),
            reranker=data.get("reranker", "none"),
        )


def load_config(config_path: Path | None = None) -> PKPConfig:
    """Load configuration from file or return defaults."""
    if config_path is None:
        config_path = Path.home() / ".pkp" / "config.toml"

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
