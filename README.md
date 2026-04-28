# PKP (Personal Knowledge Pipeline)

PKP is a local-first pipeline that ingests URLs and PDFs, indexes them for retrieval, proposes cross-document connections, and writes only human-approved connections into an Obsidian vault.

PKP exists to solve a boring problem: you save a lot of things, but the useful connections between them only show up months later when you do not remember what you saved. PKP's design goal is to preserve sources immutably, keep all processing local by default, and treat your vault as a human-owned artifact. The system proposes links; you decide what becomes durable by approving proposals in a local review UI.

## What It Does

Concrete walkthrough (URL ingestion):

1. You run `pkp init` once. PKP creates `~/.pkp/` (config, archive, SQLite DB, vault directory).
2. You ingest a URL: PKP extracts the page to Markdown, chunks it, stores the original HTML + extracted/normalized text in an immutable archive, and inserts metadata/chunks into SQLite. If Qdrant is available, it also embeds chunks (BGE-M3) and upserts vectors.
3. PKP generates connection proposals for the new document against existing documents.
4. You open the review queue (`/queue`) in a browser, read evidence passages, optionally request an LLM explanation, then approve/reject/skip.
5. On approval, PKP appends exactly one Markdown bullet under a marker-bounded "Connections" block inside the source document's vault note. Nothing outside the markers is edited.

Example: what appears in your vault after approving a connection (real note from `~/.pkp/vault/`):

```markdown
---
title: "Chunking Strategies to Improve LLM RAG Pipeline Performance | Weaviate"
source: "https://weaviate.io/blog/chunking-strategies-for-rag"
retrieved: "2026-04-26"
doc_type: "article"
sha256: "d83cebdbfa5ea3ea9a1cc666d3a04f97cab40c2011590ea5eca964ec83097010"
pkp_version: "1"
---

# Chunking Strategies to Improve LLM RAG Pipeline Performance | Weaviate

## Notes


## Connections
<!-- pkp:connections:start -->
- [[chunking-strategies-for-llm-applications-pinecone-e8687591]] - *related*: Related to "Chunking Strategies for LLM Applications | Pinecone" (score: 0.35).
<!-- pkp:connections:end -->

## Source

- Archive: /home/kr/.pkp/archive/d83cebdbfa5ea3ea9a1cc666d3a04f97cab40c2011590ea5eca964ec83097010
```

One real local run (April 26, 2026) produced:

- 15 documents in the archive/DB (10 `article`, 5 `pdf`)
- 516 chunks in SQLite
- 54 pending proposals
- 15 vault notes

## Architecture Overview

Pipeline (what happens on ingestion):

```
ingest (url/pdf)
  -> extract (Crawl4AI -> Trafilatura fallback for URLs; Docling for PDFs)
  -> normalize (clean markdown + frontmatter + token chunking)
  -> archive (immutable original + extracted + normalized + chunks + meta.json)
  -> embed/index (BGE-M3 dense+sparse -> Qdrant; SQLite rows always)
  -> propose (retrieve candidates via Qdrant hybrid; fallback to SQLite FTS)
  -> rerank (optional local cross-encoder reranker on passage pairs)
  -> explain (on-demand via Ollama/OpenAI/Anthropic; updates proposal rationale/type)
  -> review (local web UI: approve/reject/skip; batch mode available)
  -> vault (append approved connections inside marker block)
```

Major components:

**CLI (`pkp/cli.py`)** is the user-facing entry point for init/ingest/search/maintenance and for starting the API server. It is also where "sync vs async" ingestion is decided.

**API (`pkp/api/app.py`, `pkp/api/routes/*`)** is a FastAPI app that exposes ingestion/job/proposal endpoints and serves the review queue UI. It is the main integration point for anything that wants to drive PKP programmatically.

**Worker (`pkp/api/worker.py`)** is an in-process async poller that claims rows from the SQLite `jobs` table and executes ingestion/proposal generation. It exists so URL/PDF ingestion can be queued (`--async` or API enqueue) without requiring an external queue system.

**Extraction (`pkp/pipeline/extractor.py`)** is responsible for turning a URL/PDF into Markdown plus basic metadata. It tries Crawl4AI first for URLs (JS-capable) with a Trafilatura fallback; PDFs are converted to Markdown with Docling.

**Normalization (`pkp/pipeline/normalizer.py`)** cleans extracted Markdown and chunks it by token count using the embedding model's tokenizer. It exists to produce stable chunk boundaries for both embedding and FTS indexing.

**Indexing (`pkp/embedder.py`, `pkp/storage/qdrant.py`)** embeds chunks with BGE-M3 (dense + sparse) and upserts them into Qdrant when available. It exists to make hybrid retrieval fast and rebuildable, without treating the vector DB as the source of truth.

**Proposals (`pkp/pipeline/proposals.py`)** queries for related documents (Qdrant hybrid if available; SQLite FTS fallback otherwise), optionally reranks candidates, and inserts pending proposals into SQLite. It exists to keep proposal generation deterministic and reviewable.

**Vault writer (`pkp/vault/writer.py`)** creates per-document note scaffolds and appends approved connections strictly between marker comments. It exists to prevent accidental edits to user-owned vault content.

Storage layers (what each contains):

1. **Config (`~/.pkp/config.toml`)**: user configuration (paths, service endpoints, model names, thresholds).
2. **Archive (`~/.pkp/archive/<sha256>/`)**: immutable originals + derived artifacts (`original.*`, `extracted.md`, `normalized.md`, `chunks.jsonl`, `meta.json`).
3. **SQLite (`~/.pkp/db/metadata.db`)**: durable metadata, job queue, proposals, and an FTS5 index over chunk content.
4. **Qdrant (external process)**: rebuildable retrieval cache storing vectors + chunk payloads, queried with hybrid RRF.
5. **Vault (Obsidian directory)**: human-facing Markdown notes; PKP only edits a marker-bounded "Connections" region.

Trust invariants (what PKP tries to guarantee):

- **Source preservation**: the archive is append-only and written atomically (temp file + rename). Existing archived files are never overwritten.
- **Failure safety**: if extraction fails, PKP raises an error before any archive write. If a crash happens mid-archive-write, files are still individually atomic; you may end up with an incomplete document directory, but PKP will not corrupt an existing one.
- **Vault safety**: PKP never edits outside the connection marker block (`<!-- pkp:connections:start --> ... <!-- pkp:connections:end -->`). If markers are missing or malformed, the write is refused.
- **Human gate for connections**: connections are only appended when you approve a proposal (via `/queue` or the proposal approval API).
- **Rebuildability**: Qdrant is treated as a cache. `pkp rebuild-index` re-embeds archived chunks and repopulates Qdrant.

Important nuance: by default `auto_vault_on_ingest = true` creates an empty note scaffold at ingest time (no connections). Set it to `false` if you want *all* vault writes to happen only after explicit approvals.

## Prerequisites

Python:

- Python **3.12+** (`requires-python = ">=3.12"` in `pyproject.toml`).

Qdrant (recommended; required for vector retrieval + `rebuild-index`):

```bash
docker compose up -d qdrant
```

Ollama (optional; required only for the "Explain connection" button if you use Ollama):

```bash
ollama serve
ollama pull qwen3:4b
```

Installation note: PKP assumes Ollama is installed and listening on `llm_endpoint` (default `http://localhost:11434`). Install Ollama from its official installer for your OS, then run `ollama serve`.

Embedding + reranking models (local, via `FlagEmbedding`, not Ollama):

- Embeddings: `BAAI/bge-m3` (dense + sparse).
- Reranking: `BAAI/bge-reranker-v2-m3` (cross-encoder).

These are downloaded on first use (Hugging Face). Plan for a slow first run and significant disk usage.

Crawl4AI:

```bash
docker compose up -d crawl4ai
```

Note: URL extraction uses the Python `crawl4ai` package directly. The container is currently only used by PKP's service health check (`pkp init` / `/health`), which expects `http://localhost:11235/health`.

Hardware:

- For practical throughput with BGE-M3 embeddings and the local reranker, expect an NVIDIA GPU roughly in the RTX 4050 class (4GB+ VRAM). CPU-only can work but is usually slow and may run out of memory when loading models.

Optional vs required:

- Required for basic ingestion + archive + SQLite FTS search: Python + local dependencies (no Qdrant required).
- Required for vector retrieval and higher-quality proposals/search: Qdrant.
- Required for on-demand rationales in the UI: an LLM endpoint (Ollama by default; OpenAI/Anthropic supported via config and env vars).

## Installation

Important naming note: `pip install pkp` installs an unrelated PyPI project (a KeePass CLI). This repo is not published to PyPI under a unique name.

Preferred (uv, from source checkout):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/KiranRajeev-KV/pkp.git
cd pkp
uv sync --group dev
```

If `uv` fails because it cannot write to its cache directory, retry with a writable cache path:

```bash
env UV_CACHE_DIR=/tmp/uv-cache uv sync --group dev
```

Pip fallback (editable install):

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Initialize PKP (creates `~/.pkp/`, writes `config.toml`, initializes SQLite, runs a quick service check):

```bash
uv run pkp init
```

First health check:

```bash
uv run pkp status
```

## Configuration

Config file location:

- Default: `~/.pkp/config.toml` (created by `pkp init`).
- Optional: `~/.pkp/.env` is loaded automatically (useful for API keys).

Most important fields (defaults shown from `PKPConfig` / `~/.pkp/config.toml`):

| Field | Default | What it controls |
| --- | --- | --- |
| `data_dir` | `~/.pkp` | Base directory for all PKP state |
| `vault_path` | `~/.pkp/vault` | Where PKP writes Obsidian notes |
| `qdrant_url` | `http://localhost:6333` | Qdrant endpoint for vector search |
| `embedding_model` | `BAAI/bge-m3` | FlagEmbedding model used for dense+sparse embeddings |
| `embedding_dimension` | `1024` | Qdrant dense vector size; must match the embedding model |
| `llm_endpoint` | `http://localhost:11434` | LLM endpoint for explanations (Ollama/OpenAI/Anthropic) |
| `llm_model` | `qwen3:4b` | LLM model name (provider-specific) |
| `proposal_top_n` | `10` | How many proposals to keep per ingested document |
| `proposal_min_score` | `0.15` | Candidate threshold before reranking/insertion |
| `reranker` | `local` | `local` enables cross-encoder reranking; `none` disables |
| `reranker_min_score` | `0.01` | Drop reranked candidates below this score |
| `auto_vault_on_ingest` | `true` | Create an empty vault note scaffold during ingestion |

Fields to change first:

1. `vault_path` (point at your real Obsidian vault).
2. `llm_endpoint` and `llm_model` (only if you want "Explain connection").
3. `qdrant_url` (if you run Qdrant elsewhere).

Example `config.toml` snippet:

```toml
[pkp]
vault_path = "/path/to/your/Obsidian/Vault"
llm_endpoint = "http://localhost:11434"
llm_model = "qwen3:4b"
auto_vault_on_ingest = true
```

LLM provider notes:

- `llm_endpoint` can be a full URL (Ollama by default) or a legacy provider string (`ollama`, `openai`, `anthropic`).
- OpenAI requires `OPENAI_API_KEY` in your environment (or `~/.pkp/.env`).
- Anthropic requires `ANTHROPIC_API_KEY` in your environment (or `~/.pkp/.env`).

## Usage

### Ingestion

Sync URL ingestion (runs extraction + indexing inline; queues proposal generation if it created a new doc):

```bash
uv run pkp ingest-url "https://docs.weaviate.io/weaviate/concepts/search/hybrid-search"
```

Async URL ingestion (inserts a job row; a running server worker must pick it up):

```bash
uv run pkp ingest-url --async "https://docs.weaviate.io/weaviate/concepts/search/hybrid-search"
```

Sync PDF ingestion:

```bash
uv run pkp ingest-pdf ./data/1706.03762v7.pdf
```

Async PDF ingestion:

```bash
uv run pkp ingest-pdf --async ./data/1706.03762v7.pdf
```

Run the server (starts the API and an in-process background worker via FastAPI lifespan):

```bash
uv run pkp serve
```

Status:

```bash
uv run pkp status
```

### Review queue

Open the review queue in your browser:

```bash
python -m webbrowser "http://127.0.0.1:8000/queue"
```

What a proposal card shows (from `pkp/api/templates/queue/_proposal_card.html`):

- Document A title + Document B title.
- Score (displayed as a decimal and a progress bar).
- Rationale (template by default; can be replaced by an LLM explanation).
- Optional passages (`passage_a`, `passage_b`) with "show full passage" expanders.

Explain button (what it does):

- Sends `POST /queue/{proposal_id}/explain`.
- Calls `pkp.llm.client.generate_rationale(...)` using:
  - `llm_endpoint` and `llm_model` from config
  - a prompt with both titles and passages truncated to 400 chars
  - strict JSON-only output parsing (`{"rationale": ..., "link_type": ...}`)
- Persists the returned `rationale` and `link_type` back into SQLite (`proposals` table).

Approve / Reject / Skip:

- Approve appends one bullet under the connection markers in doc A's note and marks the proposal `approved`.
- Reject marks the proposal `rejected` and records the document pair in `rejected_pairs` to avoid re-proposing.
- Skip loads the next pending proposal; the skipped proposal remains pending.

Batch mode:

- Switch to "Batch" in the UI to view a table of pending proposals.
- Filter by title, select rows, then "Approve All" or "Reject All".

Keyboard shortcuts (from `pkp/api/static/queue.js`):

- Single mode: `a` approve, `r` reject, `s` skip.
- Batch mode: `j`/`k` move focus between visible rows.

### Search

CLI search (hybrid via Qdrant when available; otherwise SQLite FTS5):

```bash
uv run pkp search "hybrid retrieval" -n 10
```

Force SQLite FTS fallback:

```bash
uv run pkp search --fts "hybrid retrieval" -n 10
```

API search:

```bash
curl "http://127.0.0.1:8000/search?q=hybrid%20retrieval&limit=10"
```

### Maintenance

Rebuild the Qdrant index from the archive (requires Qdrant):

```bash
uv run pkp rebuild-index
```

Drop all collections and rebuild everything:

```bash
uv run pkp rebuild-index --all
```

Backfill vault notes for documents that have `vault_path` unset in SQLite:

```bash
uv run pkp backfill-vault
```

Enrich PDF titles using arXiv metadata (best-effort; only runs when an arXiv ID can be detected):

```bash
uv run pkp enrich-titles
```

## API Reference

When the server is running, the OpenAPI UI is available at:

```bash
python -m webbrowser "http://127.0.0.1:8000/docs"
```

Endpoints:

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Basic service identity (`name`, `version`) |
| `GET` | `/health` | Service health summary (Qdrant + Crawl4AI check) |
| `GET` | `/search` | Search documents (`q`, optional `limit`) |
| `POST` | `/ingest/url` | Enqueue a URL ingestion job (`{"url": "..."}`) |
| `POST` | `/ingest/pdf` | Enqueue a PDF ingestion job (`{"path": "/abs/path.pdf"}`) |
| `GET` | `/jobs/{job_id}` | Job status + payload |
| `GET` | `/proposals` | List proposals by status (`status`, `limit`) |
| `GET` | `/proposals/{proposal_id}` | Fetch one proposal |
| `POST` | `/proposals/{proposal_id}/approve` | Approve proposal (optional `{"link_type": "related|extends|contradicts|prerequisite"}`) |
| `POST` | `/proposals/{proposal_id}/reject` | Reject proposal |
| `GET` | `/queue` | Review queue HTML UI |
| `GET` | `/queue/next` | Next pending proposal card (optional `skip_id`) |
| `POST` | `/queue/{proposal_id}/explain` | Generate and persist an explanation (LLM) |
| `POST` | `/queue/{proposal_id}/approve` | Approve from UI (form `link_type`) |
| `POST` | `/queue/{proposal_id}/reject` | Reject from UI |
| `GET` | `/queue/batch` | Batch review HTML fragment |
| `POST` | `/queue/bulk/approve` | Bulk approve selected proposals (form `proposal_ids`) |
| `POST` | `/queue/bulk/reject` | Bulk reject selected proposals (form `proposal_ids`) |
| `GET` | `/queue/pending-count` | Pending count badge fragment |

## How Proposals Work

Proposal generation for a newly ingested document (`pkp/pipeline/proposals.py`):

1. **Candidate retrieval**:
   - If Qdrant is available: `search_documents_hybrid(..., rerank=False)` queries Qdrant with dense+sparse RRF fusion.
   - Otherwise: constructs an FTS query and uses SQLite FTS5.
2. **Passage evidence** (Qdrant only):
   - For each candidate, fetches one "best" passage from the candidate given the source query and one "best" passage from the source given the candidate query.
3. **Reranking** (optional):
   - If `reranker = "local"`, it runs `BAAI/bge-reranker-v2-m3` over passage pairs and replaces the proposal score with the reranker score.
4. **Insertion**:
   - Inserts up to `proposal_top_n` proposals as `pending` into SQLite with `passage_a`, `passage_b`, and a template rationale.

What BGE-M3 does here:

- Generates a dense embedding and a sparse "lexical weights" vector per chunk (hybrid retrieval).
- PKP converts the sparse weights into Qdrant sparse vector indices/values and stores both dense and sparse vectors under the chunk point.

What the reranker does:

- Cross-encoder scoring over `(passage_a, passage_b)` pairs to reduce false-positive "nearest neighbors".

How the LLM rationale works:

- It is on-demand (triggered by the UI "Explain connection" button).
- It is passage-grounded: the prompt includes both passages (truncated) and asks for strict JSON output.
- It updates the proposal row in SQLite; it does not write to the vault by itself.

Why the human review gate exists:

- Automatic link insertion has a high false-positive rate at useful recall.
- PKP treats the vault as a curated artifact; review is the mechanism that prevents slow quality decay.

## Vault Integration

What a vault note contains (see `pkp/vault/writer.py`):

- YAML frontmatter with:
  - `title`, `source`, `retrieved`, `doc_type`, `sha256`, `pkp_version`
- A `## Notes` section (intentionally empty; user-owned).
- A `## Connections` section with PKP markers:
  - `<!-- pkp:connections:start -->`
  - `<!-- pkp:connections:end -->`
- A `## Source` section with an archive path reference.

Marker system and why it exists:

- PKP only mutates text between the two marker comments. This makes vault edits auditable and reduces the chance of clobbering user content.

Guarantee about user content:

- Everything outside the marker-bounded block is guaranteed to be byte-for-byte preserved on connection appends (or the write fails).

Backfilling:

- `pkp backfill-vault` creates missing note scaffolds for any documents with `vault_path IS NULL` in SQLite.

## Storage Design

The five layers:

1. **Archive (immutable)**: long-term source truth; can be backed up independently; contains everything needed to rebuild embeddings.
2. **SQLite (metadata + jobs + proposals + FTS)**: durable state and the only job queue.
3. **Qdrant (vectors)**: retrieval cache; deletable and rebuildable.
4. **Vault (human-gated)**: curated output; PKP only appends approved connections inside markers.
5. **Config**: single file describing where things live and what models/endpoints to use.

Rebuild guarantee:

- Deleting Qdrant state and running `pkp rebuild-index --all` repopulates the full vector index from archived chunks.

## Known Limitations

- Non-arXiv PDFs rely on Docling's title extraction; arXiv enrichment only happens when an arXiv ID is detected in the PDF title/filename/early content.
- Vault filenames are stable (title slug + sha prefix) and do not get renamed when titles are corrected later.
- Local reranking adds noticeable latency to proposal generation (seconds per document, depending on hardware).
- Some archived normalized documents include two frontmatter blocks (extractor metadata + PKP-injected frontmatter), which can leak into chunk payloads.
- If Qdrant is unavailable, PKP falls back to SQLite FTS5 for search/proposals (lower quality).
- No browser extension; URLs must be submitted manually.
- No scheduled re-ingestion / freshness checking.
- Crawl4AI container health checks are currently stricter than what ingestion actually uses (Python package vs HTTP service mismatch).

## Development

Running tests:

```bash
uv run pytest -x -q
```

Test coverage reality (37 tests):

- Covered: SQLite behaviors, citation chunk filter, proposal scoring/reranking logic, LLM client parsing, vault marker safety, Qdrant reranking ordering.
- Not covered intentionally: real model downloads/loads, real network calls, real Qdrant/Ollama containers.

Project structure (top-level):

```text
pkp/                Python package
  api/              FastAPI app, routes, worker loop, and review UI templates/static JS
  pipeline/         Extraction, normalization, ingestion workflow, proposal generation
  storage/          Archive manager, SQLite layer, Qdrant integration, data models
  vault/            Obsidian vault note writer (marker-safe edits)
docs/               Architecture and research notes that describe intended system behavior
data/               Sample inputs (PDFs, urls.txt) used during local testing
tests/              Pytest suite
docker-compose.yml  Local Qdrant + Crawl4AI containers
```

## Roadmap

Planned (not built):

- Semantic Scholar citation links (beyond best-effort arXiv title enrichment).
- Browser extension / capture flow.
- YouTube transcript ingestion.
- Scheduled re-ingestion / freshness checking.

Explicitly out of scope:

- Multi-user deployments.
- Cloud sync.
- Mobile clients.
