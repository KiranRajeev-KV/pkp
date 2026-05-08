# PKP Reference

This document covers PKP's CLI, configuration, API, storage, and maintenance commands. For data flow and system boundaries, see [Architecture](architecture.md); for rationale and trade-offs, see [Design Decisions](design-decisions.md). [Setup and reproducibility](reproducibility.md), [Evaluation](evaluation.md), and [Experiments and evolution](experiments.md) cover those topics in more depth.

## CLI

Run commands through the installed `pkp` entry point, for example `uv run pkp <command>` in a source checkout.

| Command | Important options and behavior |
| --- | --- |
| `init` | Creates or updates configuration and local directories, initializes SQLite, then checks hard-coded local Qdrant and Crawl4AI HTTP health endpoints. `--data-dir PATH` changes `data_dir`; `--vault-path PATH` changes the vault location. Inspect generated TOML when using a custom data directory because derived database and archive paths are loaded before that option is applied. |
| `status` | Prints the version, configured paths, and archive document count. |
| `ingest-url URL` | Runs URL extraction and ingestion inline. `--profile` prints the available ingestion timing fields. `--async` creates an `ingest_url` job instead. |
| `ingest-pdf PDF_PATH` | Runs PDF extraction and ingestion inline; the CLI requires the local path to exist. `--profile` prints timing fields. `--async` creates an `ingest_pdf` job instead. |
| `search QUERY` | Searches Qdrant when available, otherwise SQLite FTS5. `--fts` forces FTS5; `--limit` or `-n` sets the result limit; `--json` emits JSON. |
| `serve` | Starts the FastAPI application and its in-process polling worker. `--host` defaults to `127.0.0.1`; `--port` defaults to `8000`. |
| `generate-notes SHA256` | Runs Ollama-backed note generation for one stored document immediately. |
| `generate-notes --all` | Enqueues `generate_notes` jobs for documents with missing, empty, or prior-failure Notes content. It requires a running worker to process the queued jobs. |
| `enrich-titles` | Best-effort arXiv enrichment for archived PDF titles. |
| `backfill-vault` | Creates note scaffolds for database documents whose `vault_path` is unset. |
| `rebuild-index` | Re-upserts archived chunks into the derived Qdrant index. `--all` additionally drops selected physical collections first; `--doc-type TYPE` narrows the work. |

### Sync and async ingestion

Without `--async`, URL and PDF commands run extraction, normalization, archive/SQLite persistence, and any available Qdrant indexing in the CLI process. When a new document is created, they also insert `generate_notes` and `generate_proposals` job rows. Those follow-up rows are only processed while the API worker is running.

With `--async`, the CLI inserts only the ingestion job. Start `pkp serve` for the in-process worker to claim it. The API ingestion endpoints also always enqueue rather than ingest inline.

## Configuration

PKP reads `~/.pkp/config.toml` by default. Settings live under `[pkp]`; most settings are TOML values rather than environment variables. Paths shown as defaults are derived when the corresponding field is unset.

### Storage paths

| Field | Default | Controls |
| --- | --- | --- |
| `data_dir` | `~/.pkp` | Base location for PKP state and the default config file. |
| `vault_path` | `~/.pkp/vault` | Obsidian vault directory used for scaffolds, generated notes, and approved connections. |
| `db_path` | `~/.pkp/db/metadata.db` | SQLite metadata, FTS5, jobs, proposals, rejections, and timing metrics. |
| `archive_path` | `~/.pkp/archive` | Content-addressed document archive. |

### Embeddings and retrieval

| Field | Default | Controls |
| --- | --- | --- |
| `embedding_model` | `BAAI/bge-m3` | Model identifier for the normalizer tokenizer and BGE-M3 embeddings. |
| `embedding_dimension` | `1024` | Expected dense-vector dimension used when creating Qdrant collections. |
| `embed_batch_size` | `32` | Chunk-embedding batch size. |
| `qdrant_url` | `http://localhost:6333` | Qdrant client endpoint. |
| `chunk_size_tokens` | `512` | Target token count per normalized chunk. |
| `chunk_overlap_tokens` | `64` | Token overlap between adjacent chunks. |

### Extraction

| Field | Default | Controls |
| --- | --- | --- |
| `user_agent` | `PKP/0.1.0 (https://github.com/KiranRajeev-KV/pkp)` | HTTP user agent for URL extraction. |
| `crawl4ai_timeout` | `30.0` | Crawl4AI extractor timeout in seconds. |
| `crawl4ai_browser_type` | `chromium` | Browser type passed to Crawl4AI. |
| `crawl4ai_headless` | `true` | Whether Crawl4AI launches headlessly. |
| `fallback_word_count_threshold` | `150` | Minimum Crawl4AI word count before PKP accepts its output without comparing Trafilatura output. |

### Proposals and reranking

| Field | Default | Controls |
| --- | --- | --- |
| `proposal_top_n` | `10` | Maximum pending proposals inserted for one source document after filtering and ranking. |
| `proposal_min_score` | `0.15` | Minimum first-stage candidate score before passage evidence and reranking. |
| `reranker` | `local` | Enables the local BGE reranker only when set to `local`; another value disables it. |
| `reranker_min_score` | `0.01` | Minimum score for proposal candidates that received a local reranker score. |

### LLM

| Field | Default | Controls |
| --- | --- | --- |
| `llm_endpoint` | `http://localhost:11434` | Endpoint or provider selection used for optional proposal explanations and document-note generation. |
| `llm_model` | `qwen3:4b` | Provider-specific model name. |

### Vault behavior

| Field | Default | Controls |
| --- | --- | --- |
| `auto_vault_on_ingest` | `true` | Creates a vault note scaffold during successful ingestion when a vault path is configured. |

## Environment variables

PKP loads `~/.pkp/.env` when it loads the default configuration. The only environment variables it reads are:

| Variable | Used for |
| --- | --- |
| `OPENAI_API_KEY` | Hosted OpenAI proposal explanations. |
| `ANTHROPIC_API_KEY` | Hosted Anthropic proposal explanations. |

Use `.env.example` as the local template. Local Ollama explanations and notes do not require either key.

## Ingestion

### URLs

PKP uses the Crawl4AI Python extractor first. If its output is below `fallback_word_count_threshold`, PKP tries Trafilatura against the same HTML and uses it only when longer. When Crawl4AI is unavailable, unsuccessful, or raises its handled extraction error, Trafilatura fetches the URL directly. Unexpected Crawl4AI exception types propagate.

### PDFs

PDF ingestion uses Docling to produce Markdown. PKP can detect an arXiv identifier from the PDF title, filename, or opening text and then attempt metadata enrichment. That lookup is best effort; failure preserves the extracted title and does not abort ingestion.

### Common persistence path

Both paths clean and normalize Markdown, add frontmatter, and split text with the configured embedding tokenizer. PKP stores original input, extracted Markdown, normalized Markdown, chunks, and metadata in the content-addressed archive; it stores document and chunk state plus FTS5 content in SQLite. It records ingestion timing fields, may create a note scaffold when `auto_vault_on_ingest` is enabled, and attempts Qdrant indexing when Qdrant is reachable. Citation-heavy chunks are excluded from Qdrant indexing but remain in the archive and SQLite FTS5.

Newly created documents cause the CLI or worker to enqueue `generate_notes` and `generate_proposals` jobs. A duplicate archive source skips normal ingestion and may attempt an indexing repair when its matching SQLite document is unindexed.

## Search and retrieval

The ordinary search path checks Qdrant first. BGE-M3 creates a dense query embedding and sparse lexical weights. Qdrant performs dense and sparse prefetches, fuses them with Reciprocal Rank Fusion, and PKP aggregates chunk hits into document results. When enabled, the local BGE reranker then scores the query against each result's best passage and reorders scored results.

If Qdrant is unavailable or hybrid retrieval raises at the outer search boundary, PKP searches SQLite FTS5. FTS5 uses title weighting and BM25-style ranking; it is a lexical fallback with different tokenization and score semantics from BGE-M3 sparse retrieval. `pkp search --fts` selects it explicitly.

Per-document-type Qdrant queries can fail independently during multi-collection search, leaving partial results without necessarily activating FTS5. See [Design Decisions](design-decisions.md) for the detailed degradation behavior.

## Background jobs

Jobs live in SQLite and are processed by one in-process polling worker started with the FastAPI application. Current job types are:

- `ingest_url`
- `ingest_pdf`
- `generate_notes`
- `generate_proposals`
- `rebuild_index`

The worker atomically claims the oldest `pending` job and marks it `running`. Normal completion records `done`; an ordinary handler exception records `failed` with error text and completion time. At worker startup, stale `running` jobs are returned to `pending`. Cancellation while a job is active also resets that job to `pending` before propagating cancellation.

Failed jobs are recorded as failed; the worker does not implement automatic retry, backoff, a dead-letter queue, or distributed execution. A note-generation handler can return `False` while the worker records the job as `done`, because handler return values are not converted into job failure.

## Relationship proposals

For a source document, PKP builds a query from its title and the opening normalized body. It retrieves a broad candidate set through hybrid search or FTS5, then removes the source document itself, candidates below `proposal_min_score`, existing unordered proposal pairs, and previously rejected unordered pairs.

When Qdrant is available, PKP gathers evidence in both directions: the source query finds a passage in the candidate, and a candidate query finds a passage in the source. The proposal stores `passage_a` and `passage_b` when available. The local reranker scores complete passage pairs and applies `reranker_min_score`; candidates without complete evidence remain eligible in their first-stage order.

Each inserted proposal stores its ID, source/candidate document hashes, score, status, creation and review times, rationale, relationship type, and optional passage evidence. Initial proposals use a template rationale and `related` type. The LLM explanation action is optional and updates only rationale and relationship type; it does not create the initial candidate set.

Rejecting a pending proposal records the pair in `rejected_pairs` in the same SQLite transaction. Pair lookups are symmetric, so reversed document ordering is treated as the same relationship for existing proposals and rejections.

## Review queue

The HTML queue is available at `/queue` while the server runs.

Single-item review shows the current pending proposal, its score and rationale, and available source/related passages. It supports:

- **Approve:** selects one of `related`, `extends`, `contradicts`, or `prerequisite`, marks the proposal approved, then attempts the vault connection append.
- **Reject:** marks the proposal rejected and records its document pair to prevent later reproposal.
- **Skip:** renders another pending item without changing the skipped proposal, so it remains pending.
- **Explain connection:** requests an on-demand structured explanation from the configured LLM provider and persists the returned rationale and type when valid.

Single-mode keyboard shortcuts are `a` for approve, `r` for reject, and `s` for skip. They are ignored when focus is in an editable control, link, or button.

Batch mode loads up to 100 pending proposals, filters by document title, supports selecting all visible rows, and provides bulk approve/reject. The `j` and `k` shortcuts move batch focus between visible rows; they do not approve or reject. Bulk approval has the same approve-then-vault-append ordering as single review.

## API

`/queue` routes render HTML fragments for the local UI; the remaining API routes return JSON unless noted.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Service name and version. |
| `GET` | `/health` | Checks hard-coded local Qdrant and Crawl4AI HTTP endpoints. |
| `GET` | `/search` | Searches documents with required `q` and optional `limit`. |
| `POST` | `/ingest/url` | Enqueues a URL ingestion job from `{"url": "..."}`. |
| `POST` | `/ingest/pdf` | Enqueues a PDF ingestion job from `{"path": "..."}` when that path exists on the server filesystem. |
| `GET` | `/jobs/{job_id}` | Returns a job's payload, state, timestamps, and error. |
| `GET` | `/proposals` | Lists proposals; accepts `status` and `limit` from 1 through 100. |
| `GET` | `/proposals/{proposal_id}` | Returns one proposal with document display metadata. |
| `POST` | `/proposals/{proposal_id}/approve` | Approves a pending proposal; request body may provide `link_type`. It then attempts the vault append and can return a vault warning. |
| `POST` | `/proposals/{proposal_id}/reject` | Rejects a pending proposal and records the pair. |
| `GET` | `/queue` | Renders the review queue page. |
| `GET` | `/queue/pending-count` | Renders the pending-count badge fragment. |
| `GET` | `/queue/batch` | Renders the batch-review fragment. |
| `GET` | `/queue/next` | Renders the next pending card; optional `skip_id` moves past one proposal without changing it. |
| `POST` | `/queue/{proposal_id}/explain` | Generates an explanation and returns an updated HTML card. |
| `POST` | `/queue/{proposal_id}/approve` | Approves from submitted form data and returns the next HTML card. |
| `POST` | `/queue/{proposal_id}/reject` | Rejects from the UI and returns the next HTML card. |
| `POST` | `/queue/bulk/approve` | Approves form-selected pending proposal IDs and returns updated batch HTML. |
| `POST` | `/queue/bulk/reject` | Rejects form-selected pending proposal IDs and returns updated batch HTML. |

## Vault behavior

PKP scaffolds one Markdown note per document when requested. The scaffold includes YAML frontmatter, a `## Notes` section, a `## Connections` section containing these managed markers, and a `## Source` section:

```text
<!-- pkp:connections:start -->
<!-- pkp:connections:end -->
```

An approved relationship appends one rendered Obsidian wikilink bullet only between those markers in the source document's note. The writer uses a temporary sibling file followed by replacement, refuses a missing note, missing markers, or invalid marker order, and does not add an identical connection line twice.

Relationship insertion is human-gated. Vault scaffolds created by `auto_vault_on_ingest` and generated content written into `## Notes` have separate semantics and do not require relationship approval. Generated-note writes replace the Notes region bounded by the managed Connections structure while preserving Connections and Source content.

Proposal approval and the filesystem append are separate operations. An approved proposal can remain approved if the later vault append fails; the UI/API reports a warning in that case.

## Storage

| Layer | Contents and role |
| --- | --- |
| Archive | Content-addressed original source plus extracted and normalized Markdown, chunks, and metadata. Artifact writes are atomic one file at a time; `meta.json` can later be enriched. |
| SQLite | Durable document/chunk state, FTS5 content, jobs, proposals, rejected pairs, and ingestion metrics. |
| Qdrant | Derived dense+sparse chunk retrieval index, rebuilt from SQLite document records and archived chunks. |
| Obsidian vault | Human-facing notes and curated connections. It is not the source of PKP job or proposal state. |

For state ownership and rebuild boundaries, see [Architecture](architecture.md).

## Maintenance

- **Reindex without collection deletion:** `pkp rebuild-index` reads SQLite document records and archived `chunks.jsonl`, then attempts to upsert every matching document with archived chunks. It requires Qdrant and does not skip records based on `indexed_at`. Re-ingesting an already archived source can also attempt targeted indexing repair when the matching document has no `indexed_at` timestamp.
- **Full rebuild:** `pkp rebuild-index --all` drops matching physical Qdrant collections, recreates them, and indexes all matching document records from archived chunks. Citation-heavy chunks are filtered during rebuild as during normal indexing.
- **Vault backfill:** `pkp backfill-vault` only considers documents whose SQLite `vault_path` is null. It does not repair a stored path that now points to a missing file.
- **Title enrichment:** `pkp enrich-titles` scans archived PDF records for arXiv identifiers, fetches metadata best effort, then updates SQLite title/FTS content and archive metadata when available. It does not automatically rebuild Qdrant or regenerate proposals afterward.
- **Note retry:** `pkp generate-notes SHA256` retries one document immediately. `pkp generate-notes --all` queues work for documents with missing, empty, or failure-placeholder Notes content.

## Known operational quirks

- CLI and API health checks use hard-coded local service URLs rather than `qdrant_url`. They also probe a Crawl4AI HTTP service on port 11235, while URL extraction uses the Crawl4AI Python package instead.
- Qdrant is optional for degraded SQLite FTS5 search and ingestion persistence, but vector indexing, hybrid retrieval, and passage evidence require it.
- Generated research notes currently require Ollama. OpenAI and Anthropic support proposal explanations only.
