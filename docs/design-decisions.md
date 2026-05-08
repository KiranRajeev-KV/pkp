# Design Decisions and Failure Semantics

This document records the engineering choices embodied in the current PKP implementation. It focuses on the boundaries each choice creates: what is durable, what can be regenerated, what degrades, and where failures can leave partial state.

### SQLite holds local durable application state

**Decision**
PKP uses one local SQLite database through `aiosqlite` for document records, chunk provenance and FTS content, jobs, proposals, rejected pairs, ingestion metrics, indexing timestamps, and vault paths. Writes use explicit commits, and connections configure a five-second busy timeout.

**Why**
The pipeline needs workflow and review state to survive process restarts while remaining local. Keeping FTS, jobs, and proposal state together also lets those paths share document identifiers and transactions.

**Trade-off**
SQLite is shared by foreground commands, API routes, and the in-process worker. Individual methods often commit independently, so a larger workflow is not one database transaction and is never transactional with archive, Qdrant, or vault writes.

**Failure behavior**
Database initialization failure prevents FastAPI startup. Later database errors propagate to the caller; a worker handler exception is recorded on its job as `failed`.

### Captured sources use a content-addressed archive

**Decision**
PKP hashes raw HTML or PDF bytes and stores `original.*`, `extracted.md`, `normalized.md`, `chunks.jsonl`, and `meta.json` beneath that SHA-256 directory. Every artifact is written through a sibling temporary file and rename. The archive is not wholly immutable: arXiv enrichment can rewrite `meta.json`.

**Why**
The hash gives source identity, duplicate detection, provenance, and a stable location from which vector data can be reconstructed.

**Trade-off**
Atomic replacement protects one artifact at a time, not the entire document directory. Archive and SQLite updates are separate, and later metadata can differ from metadata embedded in previously normalized content.

**Failure behavior**
A mid-ingest exception can leave a partial archive directory or an archive entry without complete database state. Duplicate detection short-circuits normal ingest and only attempts targeted repair when a matching SQLite document exists but lacks `indexed_at`.

### Qdrant is a derived retrieval index

**Decision**
Qdrant stores chunk payloads with named dense and sparse vectors. Physical collections are version-labelled by document type, optional aliases provide stable read/write names, and UUIDv5 maps chunk IDs to stable point IDs. Rebuild reads SQLite document records and archived chunk JSONL.

**Why**
Vector retrieval can be regenerated without making the vector store the only copy of captured text or workflow state.

**Trade-off**
Archive, SQLite, and Qdrant are updated independently. `indexed_at`, collection contents, and archived chunks can temporarily disagree, and rebuild requires both archived chunks and document metadata.

**Failure behavior**
Unavailable Qdrant does not prevent archive and SQLite ingestion. Index setup failure is contained; embedding or upsert failure is retried once, then leaves the document unindexed. A rebuild fails immediately when Qdrant is unavailable.

### BGE-M3 dense and sparse retrieval is fused with RRF

**Decision**
PKP uses `BGEM3FlagModel` to create dense embeddings and learned sparse lexical weights. Qdrant performs separate dense and sparse prefetches and combines them with Reciprocal Rank Fusion. Chunk hits are aggregated into document results.

**Why**
This combines semantic similarity with model-produced lexical matching in one first-stage retrieval path without treating their raw scores as interchangeable.

**Trade-off**
Each query requires local model inference and two Qdrant retrieval branches. RRF output is an ordering signal, not calibrated relationship confidence. Searches across document types query separate collections before application-level aggregation.

**Failure behavior**
Failures surrounding the overall hybrid operation reach FTS fallback. Inside the multi-collection loop, a failed collection is skipped, so the caller can receive partial or empty hybrid results without invoking FTS.

### SQLite FTS5 remains the degraded retrieval path

**Decision**
SQLite indexes chunk title and content with FTS5 using the Porter `unicode61` tokenizer. Document search uses `bm25(chunks_fts, 10.0, 1.0)` and aggregates chunk hits; free-text fallback queries become deduplicated quoted terms joined with `OR`.

**Why**
PKP retains lexical retrieval when Qdrant or vector inference cannot serve the request.

**Trade-off**
FTS uses different tokenization, semantics, and score direction from BGE-M3 sparse retrieval and RRF. Proposal code transforms BM25 rank into a higher-is-better score, so thresholds do not represent the same quantity across paths.

**Failure behavior**
Qdrant unavailability and exceptions escaping hybrid search use FTS. A per-collection Qdrant exception is contained earlier and therefore does not necessarily trigger this path.

### Cross-encoder reranking is a separate second stage

**Decision**
The optional local `BAAI/bge-reranker-v2-m3` model runs after first-stage retrieval. Search reranks `(query, best passage)` pairs. Proposal generation disables search reranking, gathers two passages, then reranks `(source passage, candidate passage)` pairs and applies `reranker_min_score`.

**Why**
Candidate retrieval stays broad while a pairwise model evaluates the text most relevant to ordering search results or judging a proposed relationship.

**Trade-off**
Reranking adds model loading, inference, and passage lookups. Proposal candidates with evidence can be removed by a second threshold, while candidates without complete evidence remain eligible and are appended after reranked candidates.

**Failure behavior**
Disabled scoring, model exceptions, no scoreable passages, or a mismatched score count preserves first-stage ordering. Search also catches unexpected reranking exceptions and returns its Qdrant results.

### Relationship evidence is retrieved in both directions

**Decision**
For each proposal candidate, the source query retrieves the best passage from the candidate, and a query built from the candidate retrieves the best passage from the source. Both are stored on the proposal.

**Why**
The proposal and review UI can carry passage-level evidence from each document rather than only a document-level retrieval score.

**Trade-off**
Each pair adds two hybrid passage searches and query embeddings. The passages may be asymmetric, and incomplete evidence prevents pair reranking without removing an otherwise eligible first-stage candidate.

**Failure behavior**
Each direction is caught independently. A failed lookup stores `None` for that side; the candidate remains eligible but is excluded from the pairwise reranker unless both passages exist.

### Citation-heavy chunks are filtered before vector indexing

**Decision**
PKP measures characters matched by citation and identifier patterns and excludes a chunk from Qdrant when the ratio exceeds 0.50. The original, normalized text, archived chunks, SQLite provenance, and FTS content remain intact.

**Why**
The vector index avoids chunks dominated by references and identifiers while preserving the captured material and lexical search path.

**Trade-off**
The rule is heuristic and can exclude useful citation-dense text. FTS and Qdrant intentionally cover different chunk sets, and existing Qdrant data keeps old citation chunks until rebuilt.

**Failure behavior**
If every chunk is filtered, indexing reports zero vectors and does not set `indexed_at`. Ingest still completes; a rebuild applies the same filter.

### Embedding and reranking models load locally and lazily

**Decision**
The BGE-M3 embedder and BGE reranker instantiate their `FlagEmbedding` models on first use. Normalization separately lazy-loads the configured embedding tokenizer.

**Why**
Commands that do not need model inference avoid constructing the large model objects at import time.

**Trade-off**
First use bears model initialization cost and local resource demand. Normalization is coupled to the configured model tokenizer even when Qdrant is unavailable.

**Failure behavior**
Embedding failure during indexing follows the one-retry path; embedding failure during hybrid query falls back to FTS. Reranker failure retains first-stage results. Tokenizer loading failure during normalization is not caught by an FTS or vector degradation path and aborts ingest.

### Background work uses a SQLite-backed in-process queue

**Decision**
FastAPI starts one polling worker that atomically claims the oldest pending SQLite job with `UPDATE ... RETURNING`. Handlers cover `ingest_url`, `ingest_pdf`, `generate_notes`, `generate_proposals`, and `rebuild_index`.

**Why**
Longer ingestion and enrichment work can outlive an HTTP request while its state remains durable in the same local database.

**Trade-off**
Execution is tied to the API process and polling interval. The source has no external broker, distributed worker coordination, backoff policy, or dead-letter path. Handler return values are not interpreted as success or failure.

**Failure behavior**
Cancellation shields a reset of the active job to `pending`; startup resets leftover `running` rows. An ordinary exception records `failed`, completion time, and error text without automatic resubmission. A handler that returns `False`, such as failed note generation, is still marked `done`.

### LLM explanations are optional and downstream of retrieval

**Decision**
Proposal generation stores a template rationale and default `related` type without calling an LLM. The review UI can request a structured rationale and relationship type from Ollama, OpenAI Responses, or Anthropic Messages.

**Why**
Candidate discovery and the review queue remain usable without a running LLM or hosted credentials.

**Trade-off**
The baseline rationale contains little beyond title and score. Generated explanations add provider configuration and response parsing, and the endpoint shape determines provider selection.

**Failure behavior**
Missing credentials, request errors, missing text, invalid JSON, or an invalid relationship type return no explanation and leave the proposal unchanged. The queue error currently tells the user to check Ollama even when another provider is configured.

### Human approval gates durable relationship links

**Decision**
PKP automatically discovers candidates, gathers evidence, reranks, and creates pending proposals. Only approval changes a proposal to `approved` and attempts to append its relationship link to the source document's vault note.

**Why**
Retrieved similarity remains a suggestion until a person decides it belongs in durable knowledge.

**Trade-off**
Connections accumulate only as quickly as proposals are reviewed. Approval status and filesystem mutation are separate operations.

**Failure behavior**
SQLite commits `approved` first and the vault append follows. If the note, markers, or write fails, the response reports a warning but the proposal remains approved without a corresponding connection. Scaffold creation and generated `## Notes` content do not require relationship approval.

### Vault mutation is bounded by managed regions

**Decision**
Connections are added only between explicit connection markers. Generated notes replace only the region from `## Notes` to the `## Connections` heading associated with the managed marker. Both use temporary-file replacement, and identical connection lines are not inserted twice.

**Why**
PKP can update its own regions while retaining surrounding Markdown and human edits.

**Trade-off**
Safe mutation depends on exact structural anchors. Moving or deleting headings and markers makes the note unwritable by PKP, and connection idempotence compares the complete rendered line.

**Failure behavior**
A missing note, missing marker, reversed markers, or missing Notes/Connections boundary raises `VaultWriterError` before replacement. Route handlers log connection failures after approval; note generation returns `False` when it cannot create or update the scaffold.

### Rejected document pairs are remembered persistently

**Decision**
Rejecting a pending proposal updates its status and inserts its document pair into `rejected_pairs` in one SQLite transaction. Candidate generation checks both pair orientations, as it also does for existing proposals.

**Why**
A relationship rejected during review is not repeatedly presented when either document later drives proposal generation.

**Trade-off**
The stored pair is not canonicalized, although lookups are symmetric. Rejection memory persists with the database and has no source-defined expiry.

**Failure behavior**
If the proposal is no longer pending, the transaction rolls back and no pair is recorded. Any exception during the status/pair transaction also rolls back both changes.

### arXiv metadata enrichment is best-effort

**Decision**
PDF ingest detects an arXiv ID in the title, filename, or opening text, then requests title, authors, and abstract from the arXiv Atom API. On success it updates the SQLite/FTS title and rewrites `meta.json` atomically.

**Why**
The remote metadata can replace weak PDF-derived titles and attach paper metadata when an identifiable arXiv source exists.

**Trade-off**
Ingest gains a remote lookup and the content-addressed archive gains mutable metadata. SQLite and archive mutations are not one transaction, so a late write error can leave their metadata out of step.

**Failure behavior**
Request, parse, missing-entry, and missing-title failures log and preserve the extracted title. Unexpected enrichment errors are caught by PDF ingest, which continues.

### Crawl4AI extraction falls back to Trafilatura

**Decision**
URL extraction tries the Crawl4AI Python library first. Below the configured word threshold, Trafilatura processes the already-fetched HTML and replaces Crawl4AI output only if longer. If that is insufficient, Crawl4AI is unavailable, reports failure, or raises `ExtractionError`, Trafilatura performs its own network fetch.

**Why**
Crawl4AI supplies browser-rendered content while Trafilatura supplies a second extraction strategy and a path that does not depend on Crawl4AI.

**Trade-off**
The browser path adds runtime cost, and weak output may cause a second network request. Extraction code uses the Python package even though CLI and API health checks probe a separate Crawl4AI HTTP service.

**Failure behavior**
Trafilatura network or parser errors propagate as extraction failures. Unexpected Crawl4AI exceptions outside the `ExtractionError` family also propagate instead of reaching direct fetch.

## Failure Matrix

| Component / failure | Current behavior | Degraded path / recovery |
| --- | --- | --- |
| Crawl4AI failure | Unsuccessful results, missing package, and `ExtractionError` fall through. Other exception types propagate. | Trafilatura fetches the URL directly for handled failures. |
| Weak Crawl4AI extraction | Trafilatura is tried on the same HTML and wins only when its text is longer. | Otherwise Trafilatura performs a fresh fetch. |
| Qdrant unavailable | Vector indexing is skipped; rebuild raises. | Document search and proposal candidates use SQLite FTS5; proposal passage evidence is absent. |
| Per-collection Qdrant query failure | The failed document-type collection is skipped. | Results may be partial or empty; global FTS fallback is not necessarily invoked. |
| Vector indexing failure | Setup failure is contained; embed/upsert gets one retry, then ingest completes without `indexed_at`. | Re-ingesting the same source can attempt repair; full rebuild reads archived chunks. |
| Reranker failure | Empty or mismatched scores preserve the incoming candidates. | Search keeps Qdrant order; proposals keep eligible first-stage candidates. |
| Passage-evidence retrieval failure | Each direction logs independently and stores `None`. | Candidate remains eligible but is not pair-reranked without both passages. |
| Optional proposal explanation failure | The explanation call returns no usable result and the proposal stays unchanged. | The template rationale and existing relationship type remain. |
| Document-note generation failure | It returns `False` and writes a retry placeholder when the note can be updated. | Manual retry or another queued job is required; the current worker still marks this invocation `done`. |
| Worker cancellation or interruption | The active job is reset to `pending`; startup resets stale `running` rows. | The polling worker can claim it again. |
| Ordinary worker job exception | Job state becomes `failed` with completion time and error text. | No source-defined automatic resubmission exists. |
| Missing or malformed vault markers | The writer raises before replacing the file. | Markers or section structure must be restored before retry. |
| Vault write after approval fails | The warning is returned after SQLite has committed `approved`. | The proposal remains approved; no automatic reconciliation is implemented. |
| arXiv enrichment failure | The failure is logged and PDF ingest continues with the extracted title. | No metadata retry is scheduled by ingest. |

## Human-in-the-loop Boundary

Ingestion, retrieval, proposal generation, evidence gathering, and reranking are automatic. Note generation is an optional background path. Explanation generation is also optional and, once requested, runs without making an approval decision.

The human-gated operation is converting a pending relationship into a durable vault connection. The LLM does not choose approval, create first-stage candidates, or directly write a relationship link. Approval is performed by the proposal API or review queue, which updates SQLite and only then calls the vault writer.

Other vault writes have different semantics. `auto_vault_on_ingest` may create a scaffold, and `generate_notes` may create a missing scaffold and replace the `## Notes` region without relationship approval.

## Operational Visibility

PKP uses module logging for extraction, indexing, proposal, LLM, vault, and error paths. The worker logs claim, dispatch, completion, failure, duration, interruption, and startup reset events. SQLite persists job status and error text plus ingestion timing fields for extraction, normalization, archive, and total duration; CLI profiling can print those ingestion timings. This is targeted logging and timing, not full tracing or observability infrastructure.
