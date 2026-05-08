# Setup and Reproducibility

PKP runs as a local Python application with SQLite for durable state and optional services and models for richer retrieval and enrichment.

## Requirements

| Requirement | Role |
| --- | --- |
| Python 3.12+ | Required by the project. |
| `uv` | Installs the locked Python environment and runs commands. |
| Qdrant | Dense+sparse hybrid retrieval, passage evidence, and index rebuilds. |
| BGE-M3 | Token-based chunking plus dense embeddings and sparse lexical weights. |
| BGE reranker | Optional local second-stage ranking. |
| Docling | PDF-to-Markdown extraction. |
| Crawl4AI and Trafilatura | Primary and fallback URL extraction. |
| Ollama | Optional proposal explanations and generated research notes. |
| OpenAI or Anthropic credentials | Optional hosted proposal explanations. |

Embedding, tokenizer, and reranker assets may download on first use through their underlying model libraries. PKP does not pin model-artifact revisions separately, so model availability and revision are part of a reproducible run record.

## Installation

```bash
git clone https://github.com/KiranRajeev-KV/pkp.git
cd pkp
uv sync
uv run pkp init
```

`uv.lock` pins the Python dependency graph. Use the source checkout rather than `pip install pkp`; that name is already used by an unrelated PyPI package.

`pkp init` creates the default local directories and SQLite database, then reports the configured local service health checks. `--data-dir` changes `data_dir` after the configuration object has already derived its default database and archive paths, so inspect or explicitly set those paths when moving the data directory.

## Configuration

PKP stores configuration in `~/.pkp/config.toml`, under `[pkp]`. Most settings are TOML values rather than environment variables.

| Location | Default |
| --- | --- |
| Data directory | `~/.pkp` |
| Archive | `~/.pkp/archive` |
| SQLite database | `~/.pkp/db/metadata.db` |
| Obsidian vault | `~/.pkp/vault` |

The main settings control paths (`data_dir`, `vault_path`, `db_path`, `archive_path`), BGE-M3 and Qdrant (`embedding_model`, `embedding_dimension`, `embed_batch_size`, `qdrant_url`), chunking, Crawl4AI extraction, proposal thresholds, reranking, LLM endpoint/model, and `auto_vault_on_ingest`. See the [reference](reference.md#configuration) for defaults and the full field list.

PKP loads a `.env` file beside the configuration. For hosted proposal explanations:

```bash
cp .env.example ~/.pkp/.env
```

Only these credentials are read:

```text
OPENAI_API_KEY
ANTHROPIC_API_KEY
```

Ollama does not need either key.

## Running PKP

1. Initialize local state:

   ```bash
   uv run pkp init
   ```

2. Start Qdrant for hybrid retrieval:

   ```bash
   docker compose up -d qdrant
   ```

3. Ingest a URL or PDF:

   ```bash
   uv run pkp ingest-url https://example.com/article
   uv run pkp ingest-pdf /path/to/paper.pdf
   ```

4. Serve the local API, review UI, and polling worker:

   ```bash
   uv run pkp serve
   ```

   Open `http://127.0.0.1:8000/queue` to review relationship proposals.

5. Search the corpus:

   ```bash
   uv run pkp search "retrieval systems"
   uv run pkp search --fts "retrieval systems"
   ```

6. Rebuild the vector index when needed:

   ```bash
   uv run pkp rebuild-index
   ```

Without Qdrant, PKP still persists archive and SQLite state and uses FTS5 for lexical search and proposal candidates. Ingestion still needs the configured tokenizer; URL ingestion needs extraction libraries and source access, and PDF ingestion needs Docling. Full hybrid retrieval requires Qdrant and BGE-M3. The local reranker is optional.

Proposal explanations work with Ollama, OpenAI, or Anthropic. Generated research notes are Ollama-only and require the configured Ollama endpoint and model.

`docker-compose.yml` provides Qdrant. It also defines a Crawl4AI HTTP service, but URL extraction imports the Crawl4AI Python package directly; the HTTP service is used by CLI and API health checks rather than the extraction path.

## Rebuilding the index

Qdrant is derived retrieval state. `pkp rebuild-index` reads SQLite document records and archived `chunks.jsonl`, creates or checks the relevant collections, generates BGE-M3 vectors, and upserts every matching archived document without deleting existing collections.

`pkp rebuild-index --all` drops the selected physical collections before recreating and indexing them. Both modes need SQLite metadata, archived chunks, Qdrant, and BGE-M3. Archive artifacts are written atomically per file; metadata can later be enriched and rewritten.

## Reproducibility notes

- `uv.lock` pins Python dependencies.
- Model artifacts can change unless their revisions are recorded separately for a run.
- Remote webpages and arXiv metadata can change or be unavailable.
- Local and hosted LLM output can vary.
- Hardware affects inference latency.

For commands, configuration, and API details, see the [reference](reference.md). For the data flow, see the [architecture](architecture.md).
