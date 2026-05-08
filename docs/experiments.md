# Experiments and Evolution

PKP began as a place to explore retrieval, RAG, local AI, and knowledge-management techniques. As those experiments accumulated, it became an integrated local-first pipeline for ingesting documents, retrieving related material, proposing cross-document relationships, and letting a human curate what becomes durable knowledge. Current source establishes what exists; Git history establishes what changed.

## Retrieval and ranking

| Experiment / decision | What I was exploring | Where it ended up |
| --- | --- | --- |
| SQLite FTS5 with title-weighted BM25 | Local lexical retrieval with titles weighted above body matches. | **Current and retained as fallback.** Document search aggregates chunk matches and uses `bm25(chunks_fts, 10.0, 1.0)`. |
| BGE-M3 dense and sparse representations | Using one local model for semantic vectors and learned lexical weights. | **Current.** `BGEM3FlagModel` produces both forms; Qdrant stores named dense and sparse vectors. |
| Dense + sparse Qdrant retrieval with Reciprocal Rank Fusion | Combining semantic and lexical candidate lists. | **Current, with optional Qdrant.** Dense and sparse prefetches are fused with RRF; unavailability or an outer hybrid-search failure falls back to FTS5, while a per-collection failure can leave partial results. |
| First-stage retrieval followed by a BGE cross-encoder | Separating broad retrieval from pairwise relevance scoring. | **Current and optional.** `BAAI/bge-reranker-v2-m3` reranks query/passage pairs. Proposal search skips this stage, gathers paired evidence, then reranks candidates; failures preserve Qdrant order. |
| Retrieval and reranker thresholds | Controlling which relationships become review work. | **Current.** `proposal_min_score`, `reranker_min_score`, and `proposal_top_n` are operational cutoffs, not calibrated confidence scores. |
| Passage-grounded relationship evidence | Checking relationships against relevant chunks. | **Current.** PKP retrieves evidence in both document directions and stores both passages with the proposal. |
| Citation-heavy chunk filtering | Keeping references and identifier-dense chunks out of vector results. | **Current.** A matched-character threshold excludes them during ingest and rebuild; archive and FTS data remain. |
| Tokenizer alignment | Aligning chunk boundaries and sparse token IDs with BGE-M3. | **Current after replacement.** The first normalizer used `tiktoken`/`cl100k_base`; `df2f6cb` switched to the configured model's tokenizer. |
| Stable points and replaceable collections | Making upserts stable and collections indirect. | **Current.** Chunk IDs map to UUIDv5 points; `*_v1` collections can use `*_current` aliases and be rebuilt from archived chunks. |

## Ingestion and document processing

| Experiment / decision | What I was exploring | Where it ended up |
| --- | --- | --- |
| Trafilatura URL extraction | Converting fetched HTML, links, tables, and metadata to Markdown. | **Current as fallback.** It can fetch directly or reprocess Crawl4AI HTML. |
| Crawl4AI before Trafilatura | Adding browser-rendered extraction. | **Current, with fallback.** Short output is compared with Trafilatura over the same HTML; extraction failure triggers a Trafilatura fetch. |
| Docling PDF extraction | Converting PDFs to Markdown while preserving a content hash and basic page metadata. | **Current.** Title selection checks opening Markdown headings, Docling's document name, then a cleaned filename. Insufficient output is rejected. |
| Normalization and overlapping token chunks | Producing bounded units with provenance offsets. | **Current.** Token size and overlap produce deterministic `sha256:index` IDs and archived JSONL. |
| arXiv detection and enrichment | Recovering paper metadata from IDs in filenames, titles, or opening text. | **Current and best-effort.** PDF ingest can update SQLite/FTS titles and archive metadata; failure does not abort ingest. |
| Poor extraction handling | Rejecting empty pages, scanned PDFs, and weak browser output. | **Current.** Extractors enforce minimum content and may replace low-content Crawl4AI output with longer Trafilatura output. |

## Storage and execution

| Experiment / decision | What I was exploring | Where it ended up |
| --- | --- | --- |
| Local libsql/Turso service scaffold | Running libsql beside PKP with CLI/API checks. | **Replaced early.** The container and checks were removed. Metadata already used `aiosqlite`, so stored records were not migrated. |
| Local SQLite via `aiosqlite` | Keeping document metadata, chunk provenance, FTS, proposals, rejected pairs, jobs, and timings in one local database. | **Current.** Async connections use a busy timeout, explicit commits, and atomic job/proposal state transitions. |
| Content-addressed file archive | Keeping artifacts independent of the vector database. | **Current.** SHA-256 directories hold originals, Markdown, chunks, and metadata; writes use temporary siblings and rename. |
| Qdrant as derived state | Treating vector search as an acceleration layer rather than the only copy of content. | **Current and optional.** Archived `chunks.jsonl` plus SQLite document records drive non-destructive reindexing and full rebuild. |
| SQLite-backed jobs and an in-process worker | Moving long work off request/CLI paths without another queue service. | **Current.** The lifespan worker polls and atomically claims jobs; startup and cancellation return interrupted work to pending. |
| FastAPI surface | Exposing the local pipeline through HTTP and a review UI. | **Current.** Routes share SQLite and start the polling worker in process. |
| Ingestion timing and CLI profiling | Separating extraction, normalization, archive, and total time. | **Current.** Timings can be printed and are persisted in `ingestion_metrics`; no benchmark conclusions are recorded. |
| Lazy model loading and bounded failure | Deferring expensive initialization and containing optional failures. | **Current.** Embedding and reranker wrappers load their models lazily, vector upserts retry once, and optional-component failures do not erase archived sources. |

## LLM and local-model experiments

| Experiment / decision | What I was exploring | Where it ended up |
| --- | --- | --- |
| Template proposal rationale | Giving every retrieved pair a readable initial explanation without requiring generation. | **Current baseline.** Proposal creation stores a deterministic score-based sentence and `related` link type. |
| On-demand generated explanations | Explaining an already-retrieved pair and suggesting a link type. | **Current and optional.** Ollama, OpenAI Responses, and Anthropic Messages return validated rationale/link-type JSON. Candidate retrieval does not use the LLM. |
| Local generated research notes | Turning archived normalized text into editable Markdown inside the vault. | **Current and optional.** Note generation is implemented only for Ollama and records the model/date in the note. Missing or failed generation leaves a visible retry marker. |
| Long-document extraction and synthesis | Covering text beyond one prompt. | **Current from the first notes implementation.** Overlapping sections are summarized and combined iteratively in context-bounded batches. |
| Context and output controls for local models | Bounding generation and cleaning local-model artifacts. | **Current.** PKP estimates Ollama budgets, strips thinking/planning text, and validates Markdown output. |
| Prompt-injection boundaries | Treating document text as data despite apparent instructions. | **Current for note prompts.** Fenced source text is labeled untrusted and prompts prohibit obeying it or inventing claims. |

## Human-in-the-loop knowledge curation

| Experiment / decision | What I was exploring | Where it ended up |
| --- | --- | --- |
| Relationship proposals instead of automatic links | Surfacing connections while reserving durable decisions for a person. | **Current.** Proposals carry scores, evidence, rationale, status, and link type; existing pairs are suppressed in either direction. |
| Persistent rejection memory | Preventing a rejected relationship from being proposed again later. | **Current.** Rejection updates proposal state and records the pair transactionally; lookup treats both pair orders as equivalent. |
| Local review queue | Reviewing one suggestion or a selected batch. | **Current.** It supports skip, single/bulk approve or reject, pending counts, and optional explanations. Skip leaves status pending. |
| Obsidian vault scaffolds | Turning ingested documents into ordinary Markdown files with source metadata and editable notes. | **Current and configurable.** Notes can be created during ingest or backfilled, and their stored paths are tracked in SQLite. |
| Marker-bounded, idempotent writes | Maintaining generated regions without rewriting other content. | **Current.** Connections stay between markers, duplicate lines are ignored, and note generation replaces only the Notes region. |

## Approaches I replaced or simplified

### Turso service scaffold -> local SQLite only

The early repository included a Turso/libsql container and `libsql_client` health checks, while `pkp/storage/db.py` already used `aiosqlite`. Commit `03d3988` removed the service and checks. Current state and FTS live in local SQLite.

### FTS-only search -> hybrid retrieval with FTS fallback

Commit `4f8b439` established title-weighted FTS5. Commit `df2f6cb` added BGE-M3 dense/sparse vectors, Qdrant, and RRF, then made CLI/API search prefer them. FTS remains an explicit CLI option and the fallback for Qdrant unavailability or failures that reach the outer hybrid-search boundary.

### `tiktoken` chunks -> BGE tokenizer chunks

The initial normalizer used `cl100k_base`. Commit `df2f6cb` switched normalization to the configured embedding tokenizer, aligning chunk sizing with sparse-vector token handling.

### Positional Qdrant IDs and direct collections -> stable IDs and aliases

The first Qdrant upsert used batch positions as IDs and addressed physical collections directly. Commit `7e53263` introduced chunk-derived UUIDv5 IDs and alias-aware access to versioned collections. Archive-backed rebuild remained the recovery path.

### Document scores -> passage-grounded, reranked proposals

Commit `0c8671f` created score-based proposals with templates and pair checks. Commit `985a1eb` added two-direction passage evidence. Commits `7a2296a`, `c969986`, and `336b04c` added passage-based BGE reranking. Earlier ordering remains the fallback.

### Template explanation -> optional model explanation

Proposal generation still starts with a template. Commits `5307631` and `730ea70` added a review action that sends stored passages to Ollama, OpenAI, or Anthropic and validates structured output, without changing candidate retrieval.

### CLI-owned ingest -> shared async pipeline and durable local jobs

Early CLI commands owned extraction, persistence, and indexing. Commit `4f5b969` added SQLite jobs and interruption recovery; `3ff9b86` moved ingest/rebuild into shared async functions. Direct and queued execution remain available.

### Source archive -> reviewed vault connections

The initial system captured sources but had no connection-writing path. Commits `0c8671f`, `c6ebcde`, `8e0efc1`, and `4f42dfc` connected proposals, review, vault scaffolds, and approval-triggered writes. Durable connections therefore reflect review rather than retrieval alone.

## What PKP ultimately became

PKP did not converge into a conventional “chat with your documents” RAG application. Its current path is retrieval-driven knowledge discovery: ingest and preserve sources, retrieve related documents, gather passage evidence in both directions, rerank candidate relationships, surface proposals, and require human review before links become durable in the vault. LLMs can explain proposals and draft research notes, but they remain optional additions around retrieval and curation rather than the mechanism that chooses candidate relationships.

Earlier planning and landscape material is preserved in the [initial engineering architecture](history/initial-engineering-architecture.md) and [research report](history/research-report.md). Both predate the current implementation.
