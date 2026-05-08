# Design Decisions

PKP is designed as a local knowledge workflow: capture sources durably, retrieve plausible relationships, and leave durable connections to human review. These choices keep the system understandable and rebuildable while making the important trade-offs explicit.

### SQLite for local workflow state

**Choice**
One SQLite database stores documents, chunk provenance and FTS content, jobs, proposals, rejected pairs, ingestion metrics, indexing timestamps, and vault paths.

**Why**
The pipeline needs durable state across CLI commands, API requests, and worker restarts without requiring a separate database service. Keeping workflow state and lexical search together also gives proposals and jobs a shared document identity.

**Trade-off**
SQLite fits a local, single-user workflow rather than distributed execution. Individual operations commit independently, so a full ingest or review workflow is not one transaction across SQLite, the archive, Qdrant, and the vault.

### Content-addressed archive with a derived Qdrant index

**Choice**
PKP hashes captured HTML or PDF bytes and stores source artifacts, extracted and normalized Markdown, chunks, and metadata under that SHA-256 directory. Qdrant holds derived dense and sparse chunk vectors and can be rebuilt from archived chunks plus SQLite document records.

**Why**
The archive provides stable source identity, duplicate detection, and provenance. Keeping Qdrant derived means retrieval data can be recreated without making the vector store the only copy of a document.

**Trade-off**
Artifact files are written atomically one at a time, not as one archive-wide transaction. `meta.json` may be enriched later with arXiv metadata, so the archive is content-addressed but not wholly immutable. SQLite, archive, and Qdrant can briefly disagree after an interrupted workflow.

### BGE-M3 hybrid retrieval with RRF

**Choice**
`BGEM3FlagModel` produces dense embeddings and learned sparse lexical weights. Qdrant prefetches both representations and combines their ranked lists with Reciprocal Rank Fusion (RRF); PKP then aggregates chunk hits into document results.

**Why**
Dense retrieval captures semantic similarity while learned sparse weights retain lexical signals. RRF combines rank positions without treating the two score scales as directly comparable.

**Trade-off**
Hybrid retrieval needs local model inference and two Qdrant retrieval branches. Its result scores are ordering signals rather than calibrated relationship confidence, and searches across document types require application-level aggregation.

### FTS5 as a lexical fallback

**Choice**
SQLite FTS5 indexes chunk titles and content, with title-weighted BM25 ranking. PKP uses it when the outer hybrid search path cannot run, and `pkp search --fts` selects it directly.

**Why**
It preserves useful local search and proposal discovery when Qdrant is unavailable.

**Trade-off**
FTS5 is a lexical path with different tokenization, ranking semantics, and chunk coverage from BGE-M3 sparse retrieval. A per-collection Qdrant error can leave partial hybrid results instead of activating the global FTS fallback.

### Reranking after retrieval

**Choice**
The optional local `BAAI/bge-reranker-v2-m3` runs after first-stage retrieval. Search reranks `(query, best passage)` pairs. Proposal generation gathers evidence first, then reranks `(source passage, candidate passage)` pairs.

**Why**
The first stage can retrieve broadly while a cross-encoder focuses on the passages that determine final ordering or relationship strength.

**Trade-off**
Reranking adds model loading, inference time, and passage lookup. When it is disabled or cannot score a candidate, PKP retains usable first-stage ordering rather than discarding the candidate set.

### Bidirectional passage evidence for relationships

**Choice**
For a relationship candidate, PKP retrieves a candidate passage using a source-derived query and a source passage using a candidate-derived query. Both passages are stored with the proposal when available.

**Why**
Relationship review benefits from evidence on both sides of the pair instead of a document-level similarity score alone.

**Trade-off**
Each pair adds two retrieval operations and can have asymmetric or missing evidence. Incomplete evidence skips pairwise reranking but does not automatically remove an otherwise eligible candidate.

### Selective vector indexing

**Choice**
Chunks dominated by citations and identifiers are excluded from Qdrant when their citation-like character ratio exceeds the configured heuristic threshold. They remain in the archive and SQLite FTS5.

**Why**
Reference-heavy material is useful to preserve but is often poor vector-retrieval evidence.

**Trade-off**
The filter is heuristic, so it can exclude useful citation-dense prose. FTS5 and Qdrant intentionally operate over different chunk sets until a rebuild applies the same rule everywhere.

### Local, lazy models and optional LLM enrichment

**Choice**
Embedding and reranking models load on first use. Proposal generation creates a template rationale without an LLM; the review flow can later request structured rationale and relationship type from Ollama, OpenAI, or Anthropic. Generated research notes use Ollama.

**Why**
Core candidate discovery remains available without provider credentials or an LLM service, and commands that do not need inference avoid loading large model objects at import time.

**Trade-off**
First use incurs model initialization cost. Ingestion still needs the configured embedding tokenizer, even when Qdrant is absent. Hosted explanations add provider configuration, while notes are intentionally limited to the Ollama path.

### SQLite-backed, in-process jobs

**Choice**
FastAPI starts one polling worker that atomically claims pending jobs from SQLite. It handles URL and PDF ingest, note generation, proposal generation, and index rebuilds.

**Why**
Long-running work can outlive an HTTP request while its state remains in the same local database as the rest of the workflow.

**Trade-off**
This is a lightweight local queue, not a distributed worker system. Interrupted work is returned to `pending`, while ordinary exceptions are recorded as failed. There is no automatic backoff, retry policy, or dead-letter queue.

### Human-gated relationship connections

**Choice**
PKP automatically retrieves, gathers evidence, reranks, and creates pending proposals. A connection is appended to the vault only after approval. Rejected document pairs are retained and checked symmetrically so they are not immediately proposed again in reverse order.

**Why**
Similarity is a useful suggestion, not an instruction to change a human-maintained knowledge base.

**Trade-off**
Review limits how quickly connections accumulate. Approval commits in SQLite before the filesystem append, so an approved proposal can exist without its vault connection if the later write fails.

### Marker-bounded vault writes

**Choice**
Connections are written only between managed markers, while generated notes replace only the `## Notes` region. Both writes use temporary-file replacement, and identical connection lines are not inserted twice.

**Why**
PKP can update the regions it owns while preserving surrounding Markdown and human edits.

**Trade-off**
The safety boundary depends on the expected headings and markers. Missing or malformed structure causes PKP to refuse the write. Note scaffolds and generated Notes content are separate automated writes; the approval gate applies specifically to relationship connections.

### Extraction fallbacks and best-effort metadata

**Choice**
URL ingestion uses the Crawl4AI Python package first, then Trafilatura for weak or handled failed extraction. PDF ingestion uses Docling and can enrich detected arXiv papers with metadata.

**Why**
The fallback provides a second extraction strategy, and arXiv metadata improves weak PDF titles when an identifier is available.

**Trade-off**
Weak URL extraction can trigger another fetch. Docling has no alternate parser, and arXiv enrichment is intentionally best effort. Extraction uses the Crawl4AI Python package even though CLI and API health checks probe a separate HTTP service.

## Trade-offs worth knowing

- Qdrant is optional and rebuildable, but FTS5 is less semantically rich than hybrid retrieval.
- The worker is local and in-process; failed jobs are recorded rather than automatically retried.
- Proposal approval and vault mutation cross a SQLite/filesystem boundary and are not atomic together.
- Generated research notes require Ollama; OpenAI and Anthropic are used for proposal explanations.
- The Crawl4AI HTTP health check does not represent the Python-package extraction path.
