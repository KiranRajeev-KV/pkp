# Reproducibility

This guide describes the setup and execution paths present in the checked-in source. It is intended to make a fresh checkout repeatable without overstating what this cleanup verified.

## Current reproducibility status

The Python dependency graph is locked in `uv.lock`, with `pyproject.toml` declaring Python 3.12 or newer. This cleanup used source inspection and static document checks; it did not perform a fresh installation or a complete runtime validation. PKP therefore cannot currently be claimed as freshly reproduced end to end from this environment.

The runtime-heavy paths depend on local models and, for hybrid retrieval, a running Qdrant service. URL and PDF extraction also depend on their respective libraries and input material. A lock file makes Python package resolution reproducible; it does not make remote webpages, model registries, external APIs, or local hardware identical.

## Requirements

| Requirement | Role in PKP |
| --- | --- |
| Python 3.12+ | Required by the project metadata and lock file. |
| `uv` | Resolves the locked dependencies and runs the project commands below. The repository does not pin a `uv` executable version. |
| SQLite | Local durable state and FTS5 fallback. It is accessed through the installed Python dependencies; no separate server is configured. |
| Qdrant | Derived vector index for dense+sparse hybrid retrieval, passage evidence lookup, and vector-index rebuilds. It is not authoritative storage. |
| BGE-M3 (`BAAI/bge-m3`) | Token-based normalization uses its tokenizer. Qdrant indexing and hybrid retrieval use its dense embeddings and learned sparse weights. |
| BGE reranker (`BAAI/bge-reranker-v2-m3`) | Optional local second-stage ranking when `reranker = "local"`. |
| Docling | PDF-to-Markdown extraction. |
| Crawl4AI Python package and Trafilatura | URL extraction first attempts Crawl4AI, with Trafilatura fallback behavior described below. |
| Ollama | Optional local proposal explanations and the only supported provider for generated research notes. |
| OpenAI or Anthropic credentials | Optional hosted proposal explanations when the configured LLM endpoint selects that provider. They are not needed for ingestion or retrieval. |

The embedding and reranker wrappers load their FlagEmbedding models lazily. The normalizer separately calls `AutoTokenizer.from_pretrained` for the configured embedding model during ingestion. If the required tokenizer or model weights are not already available locally, the first use may download them through the underlying model libraries. PKP does not pin a separate model-artifact revision or document model-size requirements, so this guide does not assert model sizes, GPU requirements, or memory requirements.

## Installation

The repository defines the `pkp` console script and its `justfile` uses `uv sync` for dependency installation. From a fresh checkout, the smallest source-install flow is:

```bash
git clone https://github.com/KiranRajeev-KV/pkp.git
cd pkp
uv sync
uv run pkp init
```

`uv sync` uses the checked-in lock file. `uv run pkp init` initializes SQLite and creates the default configuration and directories. Do not use `pip install pkp`: that package name is already used by an unrelated PyPI project.

The default initialization path is the most directly supported one. The `init` command also exposes `--data-dir` and `--vault-path`. Current source loads the default configuration before applying `--data-dir`, so do not assume that option also moves already-derived `db_path` and `archive_path`; inspect the resulting TOML and set those paths explicitly when using a custom location.

## Configuration

By default, PKP reads `~/.pkp/config.toml`. The file contains a `[pkp]` table and is created when configuration is first loaded or when `pkp init` runs. Default derived locations are:

| Location | Default |
| --- | --- |
| Data directory | `~/.pkp` |
| Archive | `~/.pkp/archive` |
| SQLite database | `~/.pkp/db/metadata.db` |
| Obsidian vault | `~/.pkp/vault` |

The configuration fields are TOML settings, not process-environment settings. Edit the generated file to change a setting. The current fields group as follows.

| Category | Current fields |
| --- | --- |
| Paths and storage | `data_dir`, `vault_path`, `db_path`, `archive_path` |
| General and extraction | `user_agent`, `crawl4ai_timeout`, `crawl4ai_browser_type`, `crawl4ai_headless`, `fallback_word_count_threshold` |
| Embeddings and retrieval | `embedding_model`, `embedding_dimension`, `embed_batch_size`, `qdrant_url`, `chunk_size_tokens`, `chunk_overlap_tokens` |
| Proposals and reranking | `proposal_top_n`, `proposal_min_score`, `reranker`, `reranker_min_score` |
| LLM | `llm_endpoint`, `llm_model` |
| Vault behavior | `auto_vault_on_ingest` |

The defaults point to `BAAI/bge-m3`, a 1024-dimensional embedding configuration, Qdrant at `http://localhost:6333`, a local reranker, and Ollama at `http://localhost:11434` with model `qwen3:4b`. Setting `reranker` to a value other than `local`, such as `none`, disables the local reranker in current source.

## Environment variables

PKP loads a `.env` file from the directory containing its configuration. With the default configuration location, copy the checked-in example there if hosted proposal explanations will be used:

```bash
cp .env.example ~/.pkp/.env
```

The current application code reads only these hosted-provider credentials:

```text
OPENAI_API_KEY
ANTHROPIC_API_KEY
```

They are read only when a proposal explanation is configured to call the corresponding provider. Local Ollama use needs neither key.

## Running modes

### Local/basic state

Without Qdrant, PKP can retain archive and SQLite state, populate SQLite FTS5 during ingestion, and search through FTS5. The CLI `search --fts` forces that lexical path; ordinary search also uses FTS5 when Qdrant is unavailable. Proposal candidate retrieval can use FTS5, though vector-backed passage evidence is unavailable.

This is not a dependency-free mode. Ingestion still normalizes and tokenizes documents with the configured embedding tokenizer, so the tokenizer must be available. URL ingest also needs the extraction libraries and network access to the source; PDF ingest needs Docling. With Qdrant absent, vector indexing is skipped rather than replaced with local dense retrieval.

### Full retrieval

Full retrieval requires Qdrant reachable at `qdrant_url` and the local BGE-M3 model available to FlagEmbedding. PKP indexes chunks with BGE-M3 dense vectors and learned sparse lexical weights. At query time Qdrant runs dense and sparse prefetches and fuses them with Reciprocal Rank Fusion; PKP aggregates chunk hits into document results.

The BGE reranker is a separate, optional second stage. Its current local model is `BAAI/bge-reranker-v2-m3`, loaded only when scoring is required and `reranker = "local"`. If it is disabled or fails, PKP retains first-stage ordering or candidates according to the relevant search or proposal path.

### Optional LLM features

Proposal explanations are requested after a proposal already exists. They support local Ollama and hosted OpenAI or Anthropic endpoints selected from `llm_endpoint`; hosted endpoints require the matching environment variable above. The LLM does not generate first-stage retrieval candidates or approve relationships.

Generated research notes are different: `generate_document_notes` currently accepts only an Ollama endpoint. Configuring OpenAI or Anthropic can support proposal explanations, but it makes document-note generation return no notes. Ollama and the configured `llm_model` must therefore be available for `pkp generate-notes`, worker note jobs, or automatic note generation to succeed.

## Services

`docker-compose.yml` pins Qdrant to `qdrant/qdrant:v1.17.1`, publishes ports 6333 and 6334, and persists its data in the `qdrant_data` Compose volume. For the default configuration, start only that service for hybrid retrieval:

```bash
docker compose up -d qdrant
```

The same Compose file defines a Crawl4AI HTTP service on port 11235. That service is used by the CLI and API health checks, but the extraction implementation itself imports and invokes the Crawl4AI Python package directly. It is not the URL extraction path, and the Crawl4AI container is therefore not a prerequisite for extraction according to current source. When Crawl4AI is unavailable, unsuccessful, or raises its handled extraction error, Trafilatura can fetch the URL directly; short Crawl4AI output first gets a Trafilatura attempt on the already-fetched HTML.

`pkp init` checks both the Crawl4AI HTTP endpoint and Qdrant after initializing SQLite. A Crawl4AI health-check error can therefore be reported during initialization even though the package-based URL extractor remains the active extraction path.

Ollama is not defined in the Compose file. Run a compatible Ollama endpoint separately when using local explanations or generated notes, and set `llm_endpoint` and `llm_model` in TOML if its address or model differs from the defaults.

## Typical workflow

The following commands map directly to current Click commands. They assume default configuration and that Qdrant has been started if hybrid retrieval is desired.

1. Initialize the local state:

   ```bash
   uv run pkp init
   ```

2. Start Qdrant for vector indexing and hybrid search:

   ```bash
   docker compose up -d qdrant
   ```

3. Ingest a source synchronously:

   ```bash
   uv run pkp ingest-url https://example.com/article
   uv run pkp ingest-pdf /path/to/paper.pdf
   ```

   Both ingestion commands accept `--profile`; `--async` instead creates a SQLite job for the in-process API worker.

4. Serve the local API and review UI:

   ```bash
   uv run pkp serve
   ```

   The server starts the in-process polling worker. It processes queued ingestion, note, proposal, and rebuild jobs. Open `http://127.0.0.1:8000/queue` for pending relationship proposals.

5. Search the corpus:

   ```bash
   uv run pkp search "retrieval systems"
   uv run pkp search --fts "retrieval systems"
   ```

   The first command prefers hybrid search when Qdrant is reachable. The second forces SQLite FTS5.

6. Rebuild the vector index after starting Qdrant:

   ```bash
   uv run pkp rebuild-index --all
   ```

   Without `--all`, the command upserts every matching archived document without deleting collections first. `--doc-type` narrows either form to one document type.

## Rebuilding derived state

Qdrant is derived retrieval state. The source archive stores content-addressed source artifacts, including normalized text and `chunks.jsonl`; SQLite holds the document records used to enumerate the rebuild. `pkp rebuild-index` reads those SQLite records and archived chunks, recreates or checks Qdrant collections, generates BGE-M3 dense and sparse vectors, and upserts the results.

`pkp rebuild-index --all` drops each selected physical collection before reindexing. Without `--all`, the command still processes every matching document but upserts into the existing collections. A successful rebuild therefore requires the archive, SQLite metadata, Qdrant, and the BGE-M3 model path to be available. Archive artifacts are written atomically one file at a time, but archive metadata can later be enriched and rewritten; it should not be treated as entirely immutable.

## External variability

Reproducing the same command does not guarantee identical inputs or outputs in every layer:

- remote webpages may change between URL ingests;
- arXiv and other external APIs may change, fail, or be unavailable;
- local and hosted LLM output can vary;
- model artifacts retrieved from registries can change unless separately pinned outside this repository;
- hardware affects model inference latency.

## What has not been revalidated

The following meaningful runtime checks remain outstanding for this cleanup:

- fresh-clone installation with `uv sync`;
- first-time tokenizer, BGE-M3, and BGE-reranker model loading;
- live Qdrant indexing, hybrid retrieval, and index rebuild;
- live URL and PDF ingestion through Crawl4AI, Trafilatura, and Docling;
- local Ollama and hosted OpenAI/Anthropic explanation calls;
- end-to-end API worker processing, proposal review, and vault connection append.
