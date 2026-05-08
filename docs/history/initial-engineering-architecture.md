# Personal Knowledge Pipeline — Engineering Architecture

> **Historical design document.** This records an early proposed architecture and includes phases, constraints, and components that do not describe the current implementation. See the current [architecture](../architecture.md) and [design decisions](../design-decisions.md).

*Principal Systems Architect Draft. Opinionated. Decisions stated, not deferred.*

---

## 0. Founding Constraints (Non-Negotiable)

Before any design decisions, these constraints shape everything:

1. **Local-first**: No data leaves the machine without explicit opt-in. No required cloud services.
2. **Source preservation is immutable**: Original documents are never modified or deleted by the system.
3. **Markdown vault is the durable truth**: The Obsidian vault is written to *only* after human approval.
4. **Retrieval index is a rebuildable cache**: If the index is deleted, a single command rebuilds it from stored sources.
5. **No autonomous vault corruption**: The system never writes to the vault without an explicit human approval action.
6. **Human gate on connection proposals**: The system proposes. The human decides.

Anything that violates these is a product bug, not a feature tradeoff.

---

## 1. Product Scope: MVP vs. Later

### What MVP Must Deliver

A working MVP has exactly one job: **ingestion → retrieval → proposal → review → vault**. Nothing else.

**MVP scope (ship this or nothing):**

- URL ingestion: paste a URL, get a normalized document
- PDF ingestion: drop a file, get a normalized document
- Source archive: original is stored, immutably
- Chunking, embedding, and indexing of extracted text (BGE-M3 dense + sparse vectors)
- Hybrid retrieval via Qdrant native RRF (sparse + dense, no separate BM25 index needed)
- Connection proposals: when a new document is ingested, retrieve the top-N most related existing documents and propose connections with a one-sentence rationale
- Review queue: a simple local web UI showing pending proposals; approve/reject/edit
- On approval: a Markdown file with YAML frontmatter, wiki-links to approved connections, and source attribution is written to the Obsidian vault
- `rebuild-index` CLI command that reconstructs the entire retrieval layer from the source archive

**MVP explicitly excludes:**

- Graph layer of any kind
- LLM-generated synthesis notes (proposals only; full synthesis is Phase 2)
- Browser extension
- GROBID / academic citation extraction
- Scheduled re-ingestion / freshness checking
- Multi-user
- Any cloud sync
- Mobile

**What people over-engineer in MVP:**

- Knowledge graphs. Do not build a graph in MVP. Vector + BM25 + reranking handles 90% of the connection discovery value at 5% of the complexity.
- A beautiful frontend. The review queue can be a plain HTML table. Ship the pipeline, not the UI.
- Custom embedding models. Use an off-the-shelf model. The gain from fine-tuning is marginal against the cost of doing it.
- An event streaming system. SQLite is your job queue. You are one person, processing dozens of documents per day, not millions per second.
- Semantic chunking. Start with fixed-size + overlap. It works. Optimize later with evidence.

**Phase 2 (after MVP is stable and used daily):**

- Cross-encoder reranking
- LLM-generated connection rationales (replacing simple similarity score + snippet)
- GROBID integration for academic papers
- Contextual retrieval (prepend document context to each chunk before embedding)
- Full LLM synthesis notes (drafts presented for review, never auto-saved)
- Browser extension for one-click URL capture

**Phase 3 (after Phase 2 is validated):**

- Entity extraction → lightweight graph (not GraphRAG; a simple entity co-occurrence graph with human-validated edges)
- Temporal relevance weighting (newer documents scored slightly higher for dynamic topics)
- Export / backup strategy
- Optional Obsidian plugin for inline proposals while writing

---

## 2. System Architecture

### High-Level View

```
┌──────────────────────────────────────────────────────────────┐
│                         User Surface                         │
│   CLI  ·  Local Web UI (review queue)  ·  Obsidian Vault    │
└──────────────────┬───────────────────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────────────────┐
│                     API Layer (FastAPI)                       │
│  POST /ingest/url   POST /ingest/pdf   GET /queue            │
│  POST /queue/{id}/approve   POST /queue/{id}/reject          │
│  POST /index/rebuild        GET /search                      │
└──────────────────┬───────────────────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────────────────┐
│                    Pipeline Orchestrator                      │
│  JobQueue (SQLite)  ·  Worker pool (async Python tasks)     │
└────┬─────────────┬──────────────┬───────────────┬───────────┘
     │             │              │               │
┌────▼────┐  ┌────▼────┐  ┌─────▼─────┐  ┌─────▼──────┐
│Extractor│  │Chunker/ │  │Embedder + │  │Proposal    │
│Service  │  │Normaliz-│  │Indexer    │  │Engine      │
│         │  │er       │  │           │  │            │
└────┬────┘  └────┬────┘  └─────┬─────┘  └─────┬──────┘
     │            │             │               │
┌────▼────────────▼─────────────▼───────────────▼───────────┐
│                       Storage Layer                         │
│                                                             │
│  /archive/        /vault/           metadata.db     Qdrant  │
│  (immutable       (Obsidian,        (SQLite —       (BGE-M3 │
│   originals)       human-gated)     docs, jobs,     dense+  │
│                                     proposals)      sparse) │
└─────────────────────────────────────────────────────────────┘
```

### Data Flow (Single Ingestion)

```
User submits URL
        │
        ▼
[1] Job created in SQLite jobs table (status=pending)
        │
        ▼
[2] Extractor pulls content
    Primary:  Crawl4AI (JS rendering, returns fit_markdown + raw_markdown)
    Fallback: Trafilatura run on Crawl4AI's already-fetched HTML (no second request)
              triggered if fit_markdown word count < 150
    PDF:      Docling
    → Saves original HTML/PDF to /archive/{sha256}/original.*
    → Saves extracted Markdown to /archive/{sha256}/extracted.md
    → Saves metadata JSON to /archive/{sha256}/meta.json
        │
        ▼
[3] Normalizer produces clean Markdown with YAML frontmatter
    Chunks the document (fixed-size, 512 tokens, 64 token overlap)
    → Saves chunks to /archive/{sha256}/chunks.jsonl
        │
        ▼
[4] Embedder runs BGE-M3 on each chunk (produces dense + sparse vectors in one pass)
    → Upserts dense + sparse vectors into Qdrant
    → Records document metadata in SQLite documents table
        │
        ▼
[5] Proposal Engine runs hybrid retrieval via Qdrant native RRF
    Retrieves top-10 candidate documents
    Generates one-sentence rationale per candidate (via LLM or template)
    → Saves proposals to SQLite proposals table (status=pending)
        │
        ▼
[6] User opens review queue UI
    Reviews each proposal
    Approves: vault writer creates/updates Obsidian Markdown file
    Rejects: proposal marked rejected, never touched again
        │
        ▼
[7] Job marked complete in SQLite
```

---

## 3. Service Boundaries

The system runs as a **single process with async background workers**. Do not split into microservices. You are one person. Microservices add deployment complexity that is never worth it for a local tool at this scale.

The correct decomposition is **modules within a monolith**, with clear interface contracts between them.

### Module Contracts

```
ExtractorService
  Input:  SourceRequest { url: str | None, pdf_path: Path | None }
  Output: ExtractedDocument { sha256: str, text: str, metadata: dict,
                               archive_path: Path }
  Side effects: Writes to /archive only.
  Failure modes: Network failure, parser failure, paywall detection.
  Contract: If it returns, the archive contains the original.

NormalizerService
  Input:  ExtractedDocument
  Output: NormalizedDocument { chunks: List[Chunk], frontmatter: dict }
  Side effects: Writes chunks.jsonl to archive.
  No I/O beyond the archive.

IndexerService
  Input:  NormalizedDocument
  Output: None (side effects only)
  Side effects: Upserts BGE-M3 dense + sparse vectors to Qdrant.
                Records document metadata in SQLite.
  Contract: Idempotent. Re-running on same sha256 is safe.

ProposalEngine
  Input:  NormalizedDocument + Qdrant (read-only)
  Output: List[ConnectionProposal] { doc_a, doc_b, rationale, score }
  Side effects: Writes to SQLite proposals table.
  Contract: Never touches /vault. Never touches source documents.

VaultWriter
  Input:  ApprovedProposal
  Output: Path of written vault file
  Side effects: Writes to /vault ONLY.
  Contract: Called only after explicit human approval action.
             Idempotent. Will update existing file if doc already in vault.

ReviewQueueAPI
  Input:  HTTP requests
  Output: JSON
  Side effects: Reads proposals table, calls VaultWriter on approval.
  Contract: The only entry point to VaultWriter.
```

**The vault writer must be the only code path that touches the Obsidian vault.** This is an architectural invariant. Any proposal to "automatically write to the vault for convenience" is a violation of the core trust model.

---

## 4. Storage Design

### Directory Structure

```
~/.pkp/
├── config.toml              # User config (vault path, API keys, model choices)
├── archive/                 # Immutable source archive
│   └── {sha256}/
│       ├── original.html    # or original.pdf
│       ├── extracted.md     # Raw extraction output
│       ├── normalized.md    # Post-normalization Markdown
│       ├── chunks.jsonl     # One chunk per line with offsets
│       └── meta.json        # URL, retrieval date, doc type, title, etc.
└── db/
    └── metadata.db          # Single SQLite file (documents, chunks, proposals, jobs)
# Qdrant runs as a separate local process (Docker or binary)
# Data stored in Qdrant's own data directory (~/.pkp/qdrant/ if local binary)
```

### SQLite Schema

Standard `sqlite3` — no extra dependencies, no network, single file. Connect with `aiosqlite` for async access.

```sql
-- Core document registry
CREATE TABLE documents (
    sha256        TEXT PRIMARY KEY,
    url           TEXT,
    title         TEXT,
    doc_type      TEXT,     -- 'article', 'paper', 'documentation', 'pdf'
    retrieved_at  TEXT NOT NULL,
    indexed_at    TEXT,
    word_count    INTEGER,
    archive_path  TEXT NOT NULL,
    vault_path    TEXT,     -- NULL until written to vault
    tags          TEXT,     -- JSON array
    embedded_with TEXT      -- model name, e.g. 'BAAI/bge-m3'
);

-- Chunk registry (provenance only — content lives in Qdrant payload)
CREATE TABLE chunks (
    chunk_id     TEXT PRIMARY KEY,  -- sha256 + ':' + chunk_index
    doc_sha256   TEXT NOT NULL REFERENCES documents(sha256),
    chunk_index  INTEGER NOT NULL,
    char_start   INTEGER NOT NULL,
    char_end     INTEGER NOT NULL,
    token_count  INTEGER
);

-- Connection proposals (the human review queue)
CREATE TABLE proposals (
    proposal_id  TEXT PRIMARY KEY,
    doc_a_sha256 TEXT NOT NULL REFERENCES documents(sha256),
    doc_b_sha256 TEXT NOT NULL REFERENCES documents(sha256),
    score        REAL NOT NULL,
    rationale    TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    created_at   TEXT NOT NULL,
    reviewed_at  TEXT,
    link_type    TEXT,     -- 'related'|'contradicts'|'extends'|'prerequisite'
    human_edited INTEGER DEFAULT 0  -- 1 if user modified the rationale
);

-- Rejected pairs (never repropose these)
CREATE TABLE rejected_pairs (
    doc_a_sha256 TEXT NOT NULL,
    doc_b_sha256 TEXT NOT NULL,
    rejected_at  TEXT NOT NULL,
    PRIMARY KEY (doc_a_sha256, doc_b_sha256)
);

-- Job queue
CREATE TABLE jobs (
    job_id       TEXT PRIMARY KEY,
    job_type     TEXT NOT NULL,   -- 'ingest_url'|'ingest_pdf'|'rebuild_index'
    payload      TEXT NOT NULL,   -- JSON
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    completed_at TEXT,
    error        TEXT
);

CREATE INDEX idx_proposals_status ON proposals(status);
CREATE INDEX idx_proposals_doc_a  ON proposals(doc_a_sha256);
CREATE INDEX idx_jobs_status      ON jobs(status, created_at);
```

No FTS5 table. Lexical search is handled entirely by Qdrant's BGE-M3 sparse vectors, which replaces BM25 with a learned sparse representation. One index, one query path.

### Qdrant Collection Setup

BGE-M3 produces dense (1024-dim) and sparse vectors in a single inference pass. Both go into one Qdrant collection with named vector slots. Set this up once — changing vector config later requires recreating the collection.

```python
from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, SparseVectorParams, Distance
)

client = QdrantClient(url="http://localhost:6333")

client.create_collection(
    collection_name="chunks",
    vectors_config={
        "dense": VectorParams(
            size=1024,           # BGE-M3 dense output — do not change after init
            distance=Distance.COSINE,
        )
    },
    sparse_vectors_config={
        "sparse": SparseVectorParams()  # BGE-M3 sparse/lexical output
    }
)
```

Each Qdrant point stores the chunk content and metadata as payload (for display in proposals and for metadata filtering):

```python
from qdrant_client.models import PointStruct, SparseVector

client.upsert(
    collection_name="chunks",
    points=[
        PointStruct(
            id=chunk_uuid,        # deterministic UUID from chunk_id string
            vector={
                "dense": dense_vector,           # List[float], len=1024
                "sparse": SparseVector(
                    indices=sparse_indices,       # from BGE-M3 sparse output
                    values=sparse_values,
                )
            },
            payload={
                "chunk_id":    chunk_id,          # sha256:chunk_index
                "doc_sha256":  doc_sha256,
                "doc_type":    doc_type,
                "title":       title,
                "url":         url,
                "retrieved_at": retrieved_at,
                "content":     chunk_text,        # for display in review UI
            }
        )
    ]
)
```

### Why This Schema

- `documents` in SQLite is the registry. Every query needing document-level metadata goes here. The `embedded_with` field is critical — if the model changes, `rebuild-index` detects the mismatch and re-embeds everything. Mixing BGE-M3 embeddings with another model's embeddings in the same Qdrant collection produces garbage retrieval.
- `chunks` in SQLite records provenance (offsets) only. Chunk content lives in Qdrant payload. No content duplication.
- `proposals` is the human review queue. Status transitions are the only mutations. Approved proposals drive VaultWriter.
- `rejected_pairs` prevents the system from reproposing connections the user already rejected.
- `jobs` is the async work queue. SQLite with a polling worker is entirely sufficient for a single-user local tool. No Redis. No RabbitMQ.
- Qdrant sparse vectors replace FTS5/BM25 entirely. BGE-M3's sparse output is a learned sparse representation (similar to SPLADE) that outperforms BM25 on semantic recall while preserving lexical precision. One index, one query path.

### What NOT to Build in Storage

- **Do not build a graph store.** No Neo4j, no networkx persistence, nothing. If Phase 3 needs a graph, derive it from the proposals table (every approved proposal is an edge) at query time. The proposals table is already a graph.
- **Do not build a BM25/FTS index.** Qdrant sparse vectors from BGE-M3 handle lexical search. Adding a separate FTS layer duplicates functionality and adds maintenance surface.
- **Do not build a separate search history or analytics layer in MVP.** Log to a plain file if you need it. Don't build an analytics schema before you know what you need to measure.

---

## 5. Retrieval Architecture

### The Hybrid Stack

BGE-M3 produces both dense and sparse vectors in one inference pass. Qdrant handles hybrid fusion natively — no custom RRF implementation required.

```
Query text → BGE-M3 → dense vector (1024d) + sparse vector
                              │                    │
                              ▼                    ▼
                    Qdrant dense search   Qdrant sparse search
                    (cosine, limit=50)    (limit=50)
                              │                    │
                              └──────────┬─────────┘
                                         ▼
                              Qdrant native RRF fusion
                                         │
                                         ▼
                                  Top-20 candidates
                                         │
                                   (Phase 2 only)
                                         ▼
                              BGE-reranker-v2-m3 (local)
                                         │
                                         ▼
                                  Top-10 final candidates
```

```python
from qdrant_client.models import Prefetch, FusionQuery, Fusion

# Embed the query with BGE-M3
dense_vec, sparse_vec = embed_bge_m3(query_text)

results = client.query_points(
    collection_name="chunks",
    prefetch=[
        Prefetch(query=sparse_vec, using="sparse", limit=50),
        Prefetch(query=dense_vec,  using="dense",  limit=50),
    ],
    query=FusionQuery(fusion=Fusion.RRF),
    limit=20,
    with_payload=True,   # returns chunk content for display
)
```

Qdrant's RRF implementation is equivalent to the manual implementation in the original design. No custom fusion code needed.

### Retrieval at Ingestion Time (Proposal Engine Input)

When a new document is ingested, the Proposal Engine needs to find related documents. The query is constructed from the document itself — not from a user query string. Two approaches:

1. **Title + abstract query**: If the document has a clear title and summary section, use that as the query. Fast and often sufficient.
2. **Chunk-level retrieval + aggregation**: Retrieve top-5 candidates for each of the document's N chunks, then aggregate by document (count how many chunks triggered each candidate document). Rank candidate documents by number of triggering chunks. More accurate for longer documents, more expensive.

Start with approach 1 (title + first paragraph). Move to approach 2 if proposal quality is poor.

### Metadata Filtering

Qdrant supports payload filtering combined with vector search in a single query. Filter before or alongside the vector search — no separate query needed.

```python
from qdrant_client.models import Filter, FieldCondition, MatchValue, Range

results = client.query_points(
    collection_name="chunks",
    prefetch=[
        Prefetch(query=sparse_vec, using="sparse", limit=50),
        Prefetch(query=dense_vec,  using="dense",  limit=50),
    ],
    query=FusionQuery(fusion=Fusion.RRF),
    query_filter=Filter(must=[
        FieldCondition(key="doc_type", match=MatchValue(value="paper")),
    ]),
    limit=20,
)
```

Useful filters for PKM: `doc_type` (restrict to papers, articles, docs), `retrieved_at` range (only recent saves), tag matching via payload field. Tag filtering requires storing tags as a list field in Qdrant payload and using `MatchAny`.

### Index Rebuild

```bash
pkp index rebuild
```

This command:
1. Deletes and recreates the Qdrant `chunks` collection
2. Iterates over every `sha256` directory in `/archive`
3. Re-chunks and re-embeds from `normalized.md` using BGE-M3
4. Re-upserts dense + sparse vectors to Qdrant
5. Updates `indexed_at` and `embedded_with` in SQLite

The rebuild must be idempotent and interruptible. Store progress as a `rebuild_index` job row in SQLite. Allow resume from last successful `sha256` by checking which documents already exist in the Qdrant collection before upserting.

**Target rebuild performance**: For a personal vault of 5,000 documents, rebuild should complete in under 30 minutes on an RTX 4050. Batch embedding calls — send 32–64 chunks per BGE-M3 inference call, not one at a time.

---

## 6. Connection Proposal Engine

### What the Proposal Engine Does

When a new document is fully indexed, the Proposal Engine:

1. Constructs a query from the document (title + leading text)
2. Runs hybrid retrieval against the existing index (excluding the new document itself)
3. Takes the top-10 candidate documents (not chunks — collapse chunk results to document level)
4. For each candidate pair `(new_doc, candidate)`, generates a proposal with:
   - A numerical score (the RRF score, normalized to 0–1)
   - A rationale (see below)
   - A proposed link type (`related`, `extends`, `contradicts`, `prerequisite`)
5. Writes proposals to the `proposals` table with `status='pending'`

### Rationale Generation: Two Tiers

**Tier 1 (MVP): Template-based rationale**

No LLM required. Use the highest-scoring matching chunk from each document to construct a rationale:

```python
def template_rationale(new_doc_title, candidate_doc_title,
                        top_chunk_new, top_chunk_candidate, score):
    return (
        f"'{new_doc_title}' and '{candidate_doc_title}' discuss related content "
        f"(similarity score: {score:.2f}). "
        f"Relevant passage from source: \"{top_chunk_new[:120]}...\" "
        f"Relevant passage from candidate: \"{top_chunk_candidate[:120]}...\""
    )
```

This is honest: it shows the user *why* the system thinks these are related, not a hallucinated summary.

**Tier 2 (Phase 2): LLM-generated rationale**

Pass both documents' leading sections to a local LLM with this prompt:

```
You are helping a researcher identify meaningful connections between documents.

Document A: {title_a}
{leading_text_a}

Document B: {title_b}
{leading_text_b}

In one sentence, state what conceptual relationship, if any, exists between
these documents from a researcher's perspective.
If no meaningful relationship exists, say "No significant connection found."
Do not invent connections. Be specific about what the connection is.

Also classify the connection type: related | extends | contradicts | prerequisite | none
```

**Critical constraint**: The LLM rationale is advisory. The system must display the source passages alongside it so the user can verify whether the LLM's rationale is grounded in actual content.

### Proposal Scoring

The `score` field is a composite:

```python
score = (
    0.6 * rrf_score_normalized +     # retrieval signal
    0.2 * tag_overlap_score +         # if both docs share tags
    0.2 * temporal_proximity_score    # 1.0 if same week, decays over months
)
```

This is tunable. The point is: score is a ranking signal for surfacing proposals, not a confidence that the connection is meaningful. Never present the score as "X% confidence" to users. That is a precision lie.

### What NOT to Build in the Proposal Engine

- **Do not auto-generate bidirectional links.** When doc A proposes a link to doc B, that creates one proposal. If the user approves it, the vault writer creates the link in doc A's note. Doc B's note is only updated if the user explicitly requests it or if a separate proposal for `(B → A)` is approved. This prevents the vault from accumulating links the user never saw.

- **Do not score more than 15 proposals per ingestion.** Show the top 10. More proposals means lower signal-to-noise. The user's review time is the bottleneck; don't flood the queue.

- **Do not re-run proposals on old documents when new ones are added.** This creates an exploding queue. Proposals are one-directional and generated at ingestion time of the *new* document. If the user wants to re-analyze an old document, that's an explicit `pkp reanalyze {sha256}` command.

---

## 7. Human Review Workflow

### Review Queue UI

A minimal local web UI served by the FastAPI backend. No framework overkill — plain HTML + HTMX is sufficient and keeps the binary small.

```
┌─────────────────────────────────────────────────────────────┐
│  PKP Review Queue   [15 pending]   [Sort: newest first ▼]  │
├─────────────────────────────────────────────────────────────┤
│  NEW DOCUMENT                                               │
│  "Contextual Retrieval — Anthropic, 2024"                   │
│  Saved: 2 minutes ago  ·  Type: article  ·  Words: 2,847   │
├─────────────────────────────────────────────────────────────┤
│  PROPOSED CONNECTION  [Score: 0.87]  [Type: extends ▼]     │
│                                                             │
│  ← "Retrieval-Augmented Generation for NLP Tasks"          │
│     (saved 2024-03-12)                                      │
│                                                             │
│  Rationale: "Contextual Retrieval extends the RAG          │
│  approach by prepending document-level context to chunks    │
│  before embedding, addressing a failure mode described     │
│  in the RAG paper where decontextualized chunks lose       │
│  meaning."                                                  │
│                                                             │
│  Passage from new doc: "...prepending a chunk-specific     │
│  context to each chunk before embedding..."                 │
│  Passage from candidate: "...chunks retrieved without      │
│  surrounding context often fail to capture..."             │
│                                                             │
│  [✓ Approve]  [✗ Reject]  [✎ Edit rationale]  [→ Skip]   │
└─────────────────────────────────────────────────────────────┘
```

### Review Actions

**Approve**:
- Marks proposal as `approved`
- Calls VaultWriter with the proposal
- VaultWriter creates or updates the vault Markdown file for the new document
- If the candidate document already has a vault file, optionally append a backlink (offer this as a checkbox, not automatic)

**Reject**:
- Marks proposal as `rejected`
- The pair (doc_a_sha256, doc_b_sha256) is recorded in a `rejected_pairs` table
- Future ingestion will not repropose the same pair
- No vault modification

**Edit rationale**:
- Opens an inline text editor for the rationale
- Saves the user's version, then offers Approve with the edited text
- User-edited rationales are flagged as `human_edited = true` in the database

**Skip**:
- Leaves as pending. Surfaces again later.
- Use sparingly — a large backlog of skipped proposals is a product failure signal

**Batch actions**:
- "Approve all with score > 0.85" — risky but useful for high-confidence batches. Only available when the user has explicitly set a trust threshold in config.
- "Reject all" for a document — useful when an entire ingestion's proposals are wrong

### The Trust Model

Every approved proposal creates a vault entry. Every vault entry is immutable from the system's perspective — the user owns it. If the system later generates a contradicting proposal about the same documents, it creates a new proposal for review. It does not overwrite the existing vault entry.

**Under no circumstances should the system modify a vault file that has been approved and written.** The vault is the user's notebook. The system is a pencil.

---

## 8. Obsidian Integration Strategy

### File Writing

The VaultWriter creates one Markdown file per ingested document (after at least one proposal is approved, or when the user explicitly requests it). File structure:

```markdown
---
title: "Contextual Retrieval — Anthropic"
source: "https://www.anthropic.com/research/contextual-retrieval"
retrieved: 2025-04-18
doc_type: article
tags: [retrieval, rag, embeddings, anthropic]
sha256: "abc123def456..."
pkp_version: 1
---

# Contextual Retrieval — Anthropic

## Summary
*[Only if user explicitly requests LLM summary at approval time — not auto-generated]*

## Connections
- [[Retrieval-Augmented Generation for NLP Tasks]] — *extends*: Contextual Retrieval addresses the decontextualized chunk problem described in the RAG paper by prepending document-level context before embedding.
- [[BM25 vs Dense Retrieval Comparison]] — *related*: Both analyze hybrid retrieval trade-offs; this paper provides complementary empirical evidence.

## Source
Original document archived at: `~/.pkp/archive/abc123def456/`

```

### What Gets Written

- YAML frontmatter with source URL, retrieval date, `sha256`, and tags
- A `## Connections` section with wiki-links and approved rationales
- A `## Source` section pointing to the archive path
- **Nothing else is auto-generated**

The user writes their own notes. They paste in highlights. They develop their own interpretation. The system only provides the scaffolding and the connections — not the thinking.

### File Naming

```python
def vault_filename(title: str, sha256: str) -> str:
    # Sanitize title to filesystem-safe slug
    slug = re.sub(r'[^\w\s-]', '', title.lower())
    slug = re.sub(r'[\s_]+', '-', slug).strip('-')[:60]
    # Include first 8 chars of sha256 to prevent collisions
    return f"{slug}-{sha256[:8]}.md"
```

### Vault Write Rules

1. **Never overwrite the user's content.** If a vault file already exists for a document (the user wrote their own notes), the system appends to the `## Connections` section only — it does not touch anything above or below it.
2. **Mark the Connections section boundaries:**
   ```markdown
   <!-- pkp:connections:start -->
   - [[Some Note]] — rationale
   <!-- pkp:connections:end -->
   ```
   VaultWriter only touches content between these markers. Everything outside is user-owned.
3. **If markers are not found:** Create a new vault file. Do not modify an existing file without markers.

### Obsidian Plugin (Phase 3 only)

Not MVP. Phase 3 could add an Obsidian plugin that:
- Shows a sidebar panel with pending proposals for the currently open note
- Allows approve/reject without leaving Obsidian
- Triggers a reanalysis of the current note against the full vault

This requires building an Obsidian plugin (TypeScript), which is a separate codebase. Do not conflate it with the core pipeline.

### What NOT to Do in Vault Integration

- **Do not use Obsidian's internal SQLite database.** Obsidian reads Markdown files from disk. Write files to disk. You do not need to understand Obsidian internals.
- **Do not use the Obsidian API.** The API requires the plugin architecture. Filesystem writes are sufficient and more durable.
- **Do not sync the archive to the vault.** The archive is for machines. The vault is for humans. They are different directories with different formats and different audiences.

---

## 9. Failure Modes and Trust Boundaries

### Failure Mode Taxonomy

**Class A: Silent data loss** — The system appears to succeed but loses information.

| Scenario | Prevention |
|---|---|
| PDF extraction returns empty text (scanned PDF, no OCR) | Detect zero-word extraction. Halt job. Write error to `meta.json`. Alert user. |
| JS-rendered page returns login redirect | Check word count post-extraction. If < 100 words for a URL that's not a short page, flag as extraction failure. |
| Duplicate URL submitted | Detect by URL normalization + SHA256 check. Return existing document instead of re-ingesting. |
| Archive write fails mid-job | Use atomic writes (write to temp file, rename). If rename fails, job fails. Archive is never partially written. |

**Class B: Vault corruption** — The system writes incorrect or corrupted content to the vault.

| Scenario | Prevention |
|---|---|
| VaultWriter called outside review flow | VaultWriter is only accessible through `ReviewQueueAPI.approve()`. No other code path instantiates it. |
| Concurrent writes to the same vault file | Write lock per vault file path. Queue concurrent writes. |
| Rationale contains hallucinated claim | Rationale is user-reviewed before vault write. Source passages displayed alongside. |
| File encoding corruption | Always write UTF-8. Validate round-trip before atomic rename. |

**Class C: Index drift** — The retrieval index diverges from the archive.

| Scenario | Prevention |
|---|---|
| Embedding model changed | Store `embedded_with` on every document row in SQLite. On rebuild, detect mismatch and re-embed everything. |
| Partial index failure during ingestion | Log failed chunk IDs in SQLite. `rebuild` command re-indexes only failed chunks if given `--repair` flag. |
| Qdrant collection corruption | Qdrant is a cache. Delete collection and rebuild from `chunks.jsonl` in the archive. Source is always the archive. |

**Class D: Trust boundary violations** — The system behaves in ways that undermine user trust.

| Scenario | Prevention |
|---|---|
| Auto-approved proposals (user sets threshold too low) | Batch approval only available via explicit CLI flag. UI default is always individual review. |
| Proposal for pair the user already rejected | Rejected pairs table. Never repropose unless user explicitly clears rejections. |
| LLM hallucinates a rationale | Rationale is advisory and shown alongside source passages. User can see grounding. |
| System modifies vault content outside markers | VaultWriter strict mode: abort if markers not found in existing file. Never touch content outside markers. |

### Trust Boundaries as Code

The trust model should be enforced structurally, not by convention:

```python
# VaultWriter should be a class with no public constructor
# Only ReviewQueueAPI can create it via a factory method
class VaultWriter:
    def __init__(self, _token: ReviewApprovalToken):
        # ReviewApprovalToken is only created by ReviewQueueAPI.approve()
        # Any code trying to instantiate VaultWriter directly will fail at type check
        self._token = _token

    @classmethod
    def _create(cls, token: ReviewApprovalToken) -> 'VaultWriter':
        return cls(token)
```

```python
# ReviewQueueAPI is the only code path that creates VaultWriter
class ReviewQueueAPI:
    async def approve(self, proposal_id: str) -> VaultPath:
        proposal = await self._db.get_proposal(proposal_id)
        if proposal.status != 'pending':
            raise ProposalAlreadyActedOn(proposal_id)
        token = ReviewApprovalToken(proposal_id=proposal_id, approved_at=now())
        writer = VaultWriter._create(token)
        path = writer.write(proposal)
        await self._db.mark_approved(proposal_id, path)
        return path
```

This is not over-engineering — it is the mechanical enforcement of the core product invariant.

---

## 10. Evaluation Strategy

### The Core Evaluation Problem

You cannot evaluate a personal knowledge pipeline with standard IR benchmarks. NDCG and MAP measure retrieval quality against labeled relevant documents. You have no labeled dataset. The goal is not retrieval quality in the abstract — it is "did this surface a connection that helped me think?"

This means evaluation is partly qualitative and partly behavioral.

### Tier 1: Retrieval Sanity Checks (Automated)

**Smoke tests you can run continuously:**

```python
# Test 1: Sparse retrieval finds exact-match content
# Ingest a document with a unique rare phrase
# Query for that phrase via Qdrant sparse → must be in top-3

# Test 2: Dense retrieval finds paraphrases
# Ingest "Attention mechanisms compute weighted sums over values"
# Query "how transformers weight their inputs" → must retrieve the document

# Test 3: Hybrid beats either alone on ambiguous queries
# Construct query pairs where sparse fails (needs semantics)
# and where dense fails (needs exact terms) → hybrid RRF should win both

# Test 4: Index rebuild is idempotent
# Index N documents, record top-5 results for K queries
# Rebuild Qdrant collection, re-run same queries → results must be identical
```

### Tier 2: Proposal Quality Audit (Manual, Periodic)

Every two weeks, sample 20 approved proposals and 20 rejected proposals and ask:

- For approved: "Is this connection something I would have wanted to know? Does the rationale accurately describe the relationship?"
- For rejected: "Did I correctly reject this? With more information, would I have approved it?"

Track:
- Approval rate (target: 30–60% of proposals approved; below 30% means retrieval quality is poor; above 60% means score threshold is too high and low-quality proposals aren't being filtered)
- Edit-before-approve rate (how often users modify the rationale; high rate means LLM rationales are poor)
- Time-to-review per proposal (high time means UI friction, not just review difficulty)

### Tier 3: Vault Quality Audit (Manual, Quarterly)

Periodically review vault files and ask:

- Are the `## Connections` sections correct? (have links rotted? are they still meaningful?)
- Are there connections you wish the system had proposed that it did not?
- Are there connections in the vault you regret approving?

These feed back into:
- Score threshold adjustment
- Retrieval parameter tuning
- Chunking strategy changes

### Tier 4: Benchmark on Synthetic Data (If You Want to Test Rigorously)

Build a small synthetic test corpus:

- 50 documents across 5 clearly distinct topics
- 5 document pairs with known "obvious" connections (same topic, different sources)
- 5 document pairs with known "subtle" connections (different vocabulary, same concept)
- 5 document pairs with no meaningful connection

Run the proposal engine. Measure:
- **Recall@10**: How many known-connected pairs appear in the top-10 proposals?
- **Precision@10**: What fraction of top-10 proposals are from the known-connected set?
- **False positive rate**: How many obviously-unconnected pairs appear in top-10?

This is not a substitute for real-world use, but it gives you a regression test when you change retrieval architecture.

### What NOT to Measure

- **Raw embedding similarity scores.** They are not calibrated and not comparable across models.
- **Query latency as a primary metric.** For a personal tool processing dozens of documents per day, query latency under 2 seconds is sufficient. Don't optimize for millisecond latency.
- **Vault note count.** More notes is not better. Quality of notes and connections is the signal.

---

## 11. Recommended Tech Stack

### Parser Stack

**For web content:**

| Option | Pros | Cons |
|---|---|---|
| Crawl4AI (primary) | Local, JS rendering via Playwright, returns `fit_markdown` + `raw_markdown`, BM25 content filtering built-in, 50k+ GitHub stars, self-hostable via Docker | Requires Chromium binary, heavier than pure-HTTP tools |
| Trafilatura (fallback) | Fully local, fast, best mean extraction quality on static HTML, no second network request when fed Crawl4AI's HTML | Fails on JS-rendered pages without a browser |
| Jina Reader (`r.jina.ai`) | Zero setup, handles many sites, free tier | External API, 7.9s avg latency, sends content to Jina servers |

**Recommendation**: **Crawl4AI as primary, Trafilatura as fallback on the same fetched HTML.** Trigger the fallback when Crawl4AI's `fit_markdown` word count is below 150 — pass Crawl4AI's already-fetched `result.html` directly to Trafilatura, no second network request. Use Jina Reader only as a last-resort cloud fallback for edge cases.

```python
result = await crawl4ai_crawler.arun(url=url, config=crawl_config)

if word_count(result.markdown.fit_markdown) < 150:
    # Re-process the HTML Crawl4AI already fetched — no second request
    fallback_text = trafilatura.extract(result.html, include_comments=False)
    if fallback_text and word_count(fallback_text) > word_count(result.markdown.fit_markdown):
        extracted = fallback_text
    else:
        flag_as_extraction_failure(url)  # queue for manual review
else:
    extracted = result.markdown.fit_markdown  # use fit_markdown for clean output
    raw = result.markdown.raw_markdown        # store raw_markdown in archive for reprocessing
```

Use `fit_markdown` for LLM calls (lower token cost). Store `raw_markdown` in the archive alongside `normalized.md` for reprocessing if heuristics improve.

**For PDFs:**

| Option | Pros | Cons |
|---|---|---|
| Docling (IBM Research) | Best-in-class layout detection, table extraction, reading order, Markdown + JSON export, fully local | Slower than naive methods (~2–5s per page), heavier dependency |
| PyMuPDF (fitz) | Fast, lightweight, good text extraction | Destroys multi-column layout, poor table handling |
| pdfminer.six | Widely used, mature | Column layout failure, poor for academic papers |
| LlamaParse | Strong visual parsing, classification | External API, not local-first |

**Recommendation**: Docling for all PDFs. The quality delta for academic papers and complex documents is significant enough to justify the slower speed. Add PyMuPDF as a fast fallback for simple single-column PDFs where Docling is overkill.

**For academic papers specifically:**

| Option | Pros | Cons |
|---|---|---|
| GROBID | Purpose-built for academic papers, extracts citations, authors, sections with high precision, runs as local Docker service | Docker dependency, REST API setup, Java runtime |
| Docling | Also handles academic papers, no separate service needed | Less precise on citation extraction than GROBID |

**Recommendation**: GROBID is Phase 2, not MVP. When added, run it as a Docker container on port 8070. Call it after Docling — use Docling for body text and GROBID for structured bibliographic data.

---

### Vector Database

| Option | Pros | Cons |
|---|---|---|
| Qdrant | Production-grade, named vectors, native sparse vector support, native RRF hybrid fusion, excellent payload filtering, good Python client | Requires separate process (Docker or local binary) |
| LanceDB | Embedded (no server), local files, Python native | Sparse vector support less ergonomic, no native RRF |
| Chroma | Simple API, embedded mode available | No native sparse vector support, performance issues at scale |
| pgvector (PostgreSQL extension) | SQL-native, good for existing Postgres users | No native sparse vectors, requires PostgreSQL |

**Recommendation**: **Qdrant**. The deciding factor is BGE-M3 sparse vector support — Qdrant's named vector architecture handles dense + sparse in one collection with native RRF fusion. This eliminates the need for a separate BM25/FTS index entirely. Run Qdrant as a local binary or Docker container with data stored in `~/.pkp/qdrant/`. The infrastructure cost (one extra process) is justified by the retrieval architecture it enables.

---

### Metadata Database

| Option | Pros | Cons |
|---|---|---|
| SQLite | Zero setup, embedded, ACID, JSON functions, widely understood, single file to back up | Single-writer (fine for this use case), single-device |
| Turso (libSQL) | SQLite-compatible, remote access, edge replication | Cloud dependency, FTS5 support incomplete, adds network to hot path |
| PostgreSQL | Production-grade, concurrent, full-featured | Requires server process, overkill for single user |

**Recommendation**: **SQLite**. Zero infrastructure. Single file in `~/.pkp/db/metadata.db`. Backs up with `cp`. The single-writer limitation is not a problem for a local personal tool. Do not introduce a network dependency into the metadata hot path — every job queue poll and proposal write goes through this database hundreds of times per day.

---

### Queueing / Event System

| Option | Pros | Cons |
|---|---|---|
| SQLite jobs table (polling) | Zero additional infrastructure, survives restarts, consistent with metadata layer, simple to understand | Polling overhead (negligible at this scale) |
| Redis + arq/rq | Well-understood, good for concurrent workers | Requires Redis server, additional infrastructure |
| Celery + broker | Production-grade, feature-rich | Massive complexity overhead for this use case |
| asyncio queue (in-memory) | Zero overhead | Lost on process restart |

**Recommendation**: **SQLite jobs table with a polling worker**. Poll interval: 1 second. Entirely sufficient for a personal tool where ingestion throughput is at most a few documents per minute. Survives restarts. No additional infrastructure. Any engineer telling you this needs Kafka is wrong.

```python
async def worker_loop(db: aiosqlite.Connection, interval_seconds: float = 1.0):
    while True:
        # Claim next job atomically
        async with db.execute(
            "UPDATE jobs SET status='running', started_at=? "
            "WHERE job_id = (SELECT job_id FROM jobs WHERE status='pending' "
            "ORDER BY created_at LIMIT 1) RETURNING *",
            [now()]
        ) as cursor:
            job = await cursor.fetchone()
        if job:
            await process_job(job)
        else:
            await asyncio.sleep(interval_seconds)
```

---

### Embedding Model Strategy

| Option | Pros | Cons |
|---|---|---|
| BGE-M3 (via sentence-transformers) | Dense + sparse in one pass, 1024-dim, multilingual, pairs perfectly with Qdrant named vectors, strong quality | ~570MB model, slower than smaller models without GPU |
| nomic-embed-text (via Ollama) | Lightweight, fast, good English quality | Dense only — requires separate BM25; no sparse output |
| text-embedding-3-small (OpenAI API) | Strong quality, cheap | External API, dense only |
| all-MiniLM-L6-v2 | Tiny, fast | Dense only, lower quality |

**Recommendation**: **BGE-M3 via sentence-transformers**. It is the only local model that produces both dense and sparse vectors in a single inference pass, which is exactly what the Qdrant hybrid architecture requires. On an RTX 4050, BGE-M3 runs comfortably fast for a personal ingestion pipeline.

```python
from FlagEmbedding import BGEM3FlagModel

model = BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)

def embed_chunks(texts: list[str]) -> list[dict]:
    output = model.encode(
        texts,
        batch_size=32,
        max_length=512,
        return_dense=True,
        return_sparse=True,
        return_colbert_vecs=False,  # not needed for this architecture
    )
    return [
        {
            "dense": output["dense_vecs"][i].tolist(),
            "sparse_indices": list(output["lexical_weights"][i].keys()),
            "sparse_values":  list(output["lexical_weights"][i].values()),
        }
        for i in range(len(texts))
    ]
```

**Critical**: Store `embedded_with = 'BAAI/bge-m3'` on every document row in SQLite. If you ever change models, `pkp index rebuild` detects the mismatch via this field and re-embeds everything. Never mix embeddings from different models in the same Qdrant collection.

---

### Reranking Strategy

| Option | Pros | Cons |
|---|---|---|
| cross-encoder/ms-marco-MiniLM-L-6-v2 (local) | Tiny (~80MB), fast (CPU-viable), standard benchmark performer | Less accurate than larger cross-encoders |
| BGE-reranker-v2-m3 (local) | Stronger quality, multilingual | ~1GB, slower |
| Cohere Rerank API | Very high quality, easy API | External, paid |
| mxbai-rerank-base-v1 (local) | Good quality, local | Less community testing |

**Recommendation**: **BGE-reranker-v2-m3 as Phase 2 default**. Skip reranking in MVP — Qdrant's native RRF is adequate for the proposal engine's top-10 use case. In Phase 2, add BGE-reranker-v2-m3. It pairs naturally with BGE-M3 (same model family, same vocabulary). On an RTX 4050 it runs in milliseconds per candidate set. Make it optional in config: `reranker: none | local | cohere`.

---

### Local LLM Strategy

| Option | Pros | Cons |
|---|---|---|
| Ollama | Best DX for local models, easy model management, REST API, GPU acceleration | Separate process, limited batching |
| llama.cpp (direct) | More control, lower overhead | More setup, fewer convenience features |
| vLLM | High throughput, excellent batching | Heavy, designed for server workloads |
| Transformers + bitsandbytes | Flexible, direct HuggingFace access | More setup, slower than optimized runtimes |

**Recommendation**: **Ollama**. It is the right tool for local LLM serving on consumer hardware. For your RTX 4050 (6GB VRAM), realistic model choices:

- **Proposal rationale generation**: mistral-nemo:12b (q4_K_M quantization, ~7GB — fits in your VRAM). Alternatively phi3.5:3.8b if latency is a concern.
- **Note synthesis (Phase 2)**: Same model. Synthesis tasks are quality-sensitive; don't use a smaller model to save time.
- **Fallback**: OpenAI `gpt-4o-mini` for users with API keys. Substantially better quality for rationale generation, cheap enough for personal use.

Config approach: `llm_provider: ollama | openai | anthropic`, `llm_model: mistral-nemo`. The pipeline calls a thin LLM client abstraction that routes to the configured provider. Never hard-code model calls.

---

### API / Backend Stack

| Option | Pros | Cons |
|---|---|---|
| FastAPI (Python) | Async, fast, excellent for ML ecosystem, Pydantic validation, auto-docs | Python GIL for CPU-bound work (use ProcessPoolExecutor for extraction) |
| Flask | Simpler | Sync-by-default, worse for async pipelines |
| Go (net/http or Fiber) | Excellent concurrency, small binary, no GIL | Python ML library interop requires subprocess or IPC |
| Node.js (Fastify) | Fast, good for I/O-heavy pipelines | ML library ecosystem not native |

**Recommendation**: **FastAPI**. The parsing and embedding stack is Python-dominated. Fighting the ecosystem with a different language for the backend adds IPC complexity with no benefit. Use `asyncio` for I/O concurrency and `ProcessPoolExecutor` for CPU-bound extraction tasks. The GIL is not a problem at this scale.

Project structure:
```
pkp/
├── api/
│   ├── routes/
│   │   ├── ingest.py
│   │   ├── queue.py
│   │   └── search.py
│   └── app.py
├── pipeline/
│   ├── extractor.py        # Crawl4AI primary + Trafilatura fallback
│   ├── normalizer.py       # Chunking, frontmatter
│   ├── indexer.py          # BGE-M3 embed + Qdrant upsert
│   └── proposals.py        # Proposal engine (Qdrant hybrid query)
├── storage/
│   ├── archive.py          # Immutable source archive (filesystem)
│   ├── db.py               # SQLite access layer (aiosqlite)
│   └── vectors.py          # Qdrant client wrapper
├── vault/
│   └── writer.py           # VaultWriter (the trusted module)
├── llm/
│   └── client.py           # Provider-agnostic LLM client (Ollama / OpenAI)
└── cli.py                  # Click CLI
```

---

### Deployment Model

| Option | Pros | Cons |
|---|---|---|
| pip-installable package + `pkp serve` | Standard Python distribution, no Docker required | Users must manage Python environment |
| Docker Compose | Reproducible environment, isolation | Docker required, heavier for a personal tool |
| Electron app (with embedded Python) | Better user experience, no terminal required | Complex packaging, large binary |
| System daemon (systemd/launchd) | Runs in background, starts on boot | More complex install script |

**Recommendation**: **pip-installable package with a `pkp serve` command**, plus an optional `install-service` subcommand that writes a systemd/launchd unit file for background operation. This respects Python norms, works without Docker, and gives power users a way to run it as a daemon.

```bash
pip install pkp
pkp init              # Creates ~/.pkp/, prompts for vault path and config
pkp serve             # Starts the FastAPI server + background worker
pkp ingest url https://...
pkp ingest pdf /path/to/file.pdf
pkp index rebuild
pkp queue             # Opens review UI in browser
```

Wrap in a `uv` or `pipx`-compatible setup for cleaner isolation. Document a `uv tool install pkp` path.

---

## Summary: What To Build, In Order

**Week 1–2**: Archive structure, SQLite schema, Crawl4AI + Trafilatura extraction, Docling for PDFs, fixed-size chunking, normalized Markdown output. No embeddings yet. Just get clean documents into `~/.pkp/archive/` with metadata in `metadata.db`.

**Week 3–4**: BGE-M3 integration, Qdrant collection setup (dense + sparse named vectors), ingestion-time embedding and upsert, Qdrant hybrid query (RRF). CLI `pkp search "query"` that returns results. No proposals yet. Validate retrieval quality manually against a test corpus.

**Week 5–6**: Proposal engine — Qdrant hybrid query at ingestion time, template-based rationales, SQLite proposals table, job queue worker. `pkp ingest url` end-to-end without the UI.

**Week 7–8**: FastAPI review queue. Minimal HTML UI. VaultWriter with markers. End-to-end flow: URL → archive → Qdrant → proposals → review → vault. This is the MVP.

**After MVP is in daily use**: measure approval rate, time-to-review, vault quality. Let real usage tell you what to optimize. Don't guess.

The system that you can use every day and trust completely is worth more than a system with impressive architecture that you don't trust.
