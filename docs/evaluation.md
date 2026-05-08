# Validation and Evaluation

PKP currently has unit-level checks for important state transitions, proposal ordering, response parsing, and vault-write boundaries. It also records a small set of operational timings and status fields. Those checks establish that specific mechanisms behave as intended under controlled inputs; they do not measure whether retrieval results or proposed relationships are useful. This document separates those concerns and defines a future evaluation method without presenting it as work already completed.

## What is tested today

### Database and workflow state

The database tests verify that document fields are serialized into the expected insert parameters and committed. They also exercise the atomic job-claim statement at the method boundary: a pending job is returned as `running`, with `started_at` set, and the next claim can find an empty queue. These tests use fake connections, so they validate SQL use and state mapping rather than concurrency between real SQLite connections.

Proposal state has two strong pair-level invariants in the suite. Existing proposals are looked up in both document orders, and rejecting a proposal issues the status update and rejected-pair insertion within one explicit transaction. The suite verifies the transaction sequence and commit, although it does not reopen a database to prove persistence across a process restart. Updating an explanation is also checked to persist both the rationale and relationship type.

There are no direct worker tests. Claiming behavior is covered at the database method boundary, while cancellation resets, startup recovery, handler dispatch, and ordinary failure recording are implementation behavior without dedicated tests.

### Proposal generation

Proposal tests run the generation pipeline with fake storage and retrieval dependencies. They verify that:

- consecutive leading metadata and PKP frontmatter blocks are removed before query construction;
- Qdrant `raw_score` is stored as the first-stage proposal score without inversion;
- running proposal generation twice does not create a second proposal for an existing unordered document pair;
- proposal search explicitly disables the search-result reranker so relationship reranking occurs later against passage pairs;
- passage-pair reranker scores can reorder candidates and remove scored candidates below `reranker_min_score`;
- candidates lacking complete passage evidence remain usable and retain their first-stage score.

The suite therefore checks duplicate proposal prevention, second-stage proposal thresholds, and the distinction between first-stage retrieval scores and reranker scores. It does not directly test the configured `proposal_min_score`, `proposal_top_n`, self-match removal, or a previously rejected pair flowing through the complete proposal generator. Symmetric rejection checks exist in database code and the proposal test fake, but there is no end-to-end test from a real rejection row through later candidate suppression.

### Retrieval and reranking mechanics

The reranker tests replace the BGE model with a mock. They verify normalized pair scoring, the disabled path, and conversion of a model exception into an empty score list. Search reranking tests then prove that reranker scores can reorder first-stage document results, scored results precede unscored results, and disabling reranking returns the original results.

These are useful ordering and fallback invariants. The tests do not execute BGE-M3 embeddings, dense or sparse Qdrant queries, RRF fusion, document aggregation from real chunk hits, SQLite FTS5 ranking, or the outer Qdrant-to-FTS fallback. The current suite validates reranking mechanics, not the retrieval stack as a whole.

### Ingestion helpers and metadata

Focused ingestion tests classify one citation-heavy sample and one semantic sample, covering the heuristic used to exclude citation-dominated chunks from Qdrant. arXiv identifiers are checked across plain IDs, versioned IDs, filenames, and unrelated text. Mocked arXiv responses verify Atom parsing, absence of an entry, and request failure. A Docling title helper is checked for finding an H2 title in extracted Markdown.

These tests do not run URL or PDF ingestion from extraction through archive, SQLite, and indexing. They also do not exercise Crawl4AI-to-Trafilatura selection or real Docling conversion quality.

### LLM parsing and document notes

The rationale tests cover the Ollama request contract, passage truncation, valid structured JSON, malformed JSON, and request failure. They confirm that invalid or unavailable output becomes `None`. OpenAI and Anthropic request construction and response extraction are not tested.

Long-document note helpers are tested for overlapping section splits, bounded combine batches that preserve input order, removal of thinking/preamble text, and trimming repeated planning artifacts. At the pipeline level, a mocked note-generation failure creates a vault scaffold, returns `False`, and writes the visible retry placeholder while retaining the Source section. CLI selection logic recognizes missing, empty, and prior-failure Notes regions and avoids treating malformed section structure as eligible.

### Vault mutation safety

The vault suite establishes several filesystem invariants:

- creating a scaffold never overwrites an existing note;
- a connection append preserves all bytes before and after the managed connection region;
- appending the same rendered connection twice is idempotent;
- missing connection markers raise `VaultWriterError` and preserve the original bytes;
- replacing generated Notes preserves the Connections and Source regions;
- a `## Connections` heading inside generated content is not mistaken for the managed boundary;
- generated filenames use the expected slug and document-hash suffix.

The tests cover missing markers but do not separately exercise every malformed-marker case, such as reversed markers. That behavior is enforced in current source but should not be attributed to the existing suite beyond the tested cases.

## What is not tested today

The suite contains no live integration or end-to-end coverage for:

- BGE-M3 model loading or real dense and learned-sparse inference;
- BGE cross-encoder loading or real scoring quality;
- Qdrant collection creation, indexing, hybrid RRF queries, aliases, rebuilds, or partial collection failures;
- SQLite FTS5 retrieval against a representative corpus;
- Ollama generation with a real local model;
- OpenAI or Anthropic requests with real provider responses;
- Crawl4AI browser extraction, Trafilatura network fallback, or live URL variability;
- Docling conversion across a real PDF corpus;
- the in-process worker's interruption, restart, and exception paths;
- API review flows, including approval followed by vault mutation;
- a fresh-clone setup running from ingestion through review and vault output.

Mocks keep the tests deterministic and avoid external services, but they cannot reveal model-resource failures, service-version incompatibilities, extraction-quality variation, or cross-store partial state.

## Retrieval-quality evaluation status

Repository-wide inspection found no labelled query/relevant-document dataset, qrels, benchmark runner, Recall@K, Precision@K, MRR, nDCG computation, or recorded human relevance judgments. The older architecture document discusses evaluation ideas, but those ideas are not implemented evaluation infrastructure.

> The current test suite validates retrieval mechanics and failure behavior; it does not establish retrieval quality.

Passing unit tests therefore supplies no evidence that dense retrieval, sparse retrieval, RRF, or reranking improves result relevance for PKP's corpus.

## Proposed retrieval evaluation

A useful first evaluation can remain small and offline. Freeze a representative archive snapshot, write a set of information needs in natural language, and have a human judge document relevance for each query. Candidate pooling should combine the top results from every configuration plus a small random sample, then hide system identity and rank during judgment. Graded labels such as irrelevant, useful, and highly useful support both binary and graded metrics. Queries or thresholds used for tuning should be kept separate from the final comparison set.

Run each query over identical documents and chunks with:

1. SQLite FTS5;
2. BGE-M3 dense vectors only;
3. BGE-M3 sparse lexical weights only;
4. BGE-M3 dense and sparse retrieval fused with RRF;
5. dense and sparse RRF followed by the BGE reranker.

PKP cannot expose all five configurations through its current public search function. `Database.search_documents()` supplies the FTS baseline. `search_documents_hybrid(..., rerank=False)` and its default reranked form supply the last two configurations. The lower-level Qdrant search always creates both dense and sparse prefetches and fuses them, so dense-only and sparse-only runs require a small evaluation harness that issues one prefetch branch at a time, or an evaluation-specific lower-level interface. The harness should record ranked document IDs, scores, latency, and configuration without changing normal application behavior.

Use the following metrics at fixed cutoffs such as K = 5 and 10:

| Metric | What it answers for PKP |
| --- | --- |
| Recall@K | How much of the judged-relevant material appears within the first K documents? |
| Precision@K | How much of the limited result list is judged relevant? |
| MRR | How early does the first relevant document appear when finding one useful source is the goal? |
| nDCG@K | Does the ranking place highly useful documents ahead of marginally useful ones? |

Report per-query results and aggregates. The small sample should retain uncertainty, preferably through query-level confidence intervals or paired resampling, rather than turning small differences into broad quality claims. Keep the corpus, judgments, model revisions, chunking configuration, and Qdrant collection version with each run so later comparisons remain interpretable.

## Proposed relationship-proposal evaluation

Relationship proposals need their own judgments because retrieval relevance and durable relationship value differ. A document may answer the same query or discuss the same topic without deserving a lasting connection in the vault.

Build an evaluation set from source documents and pooled candidate documents. For each pair, ask a human to judge: whether a durable connection is useful, whether each stored passage supports that connection, and, when an LLM explanation is shown, whether the rationale is grounded and the relationship type is acceptable. Evaluate proposal ordering before showing model scores or system identity.

The useful measures are:

- **Human acceptance rate:** the share of reviewed proposals approved in normal use. Interpret it alongside thresholds and review behavior; it is not a standalone retrieval metric.
- **Rejection rate:** the share explicitly rejected, kept separate from skips because skip leaves a proposal pending.
- **Precision@K for relationships:** the fraction of the top K proposed pairs judged worthy of a durable connection.
- **Evidence-passage relevance:** human ratings for whether `passage_a` and `passage_b` substantiate the proposed connection, including the rate of missing evidence.
- **Duplicate proposal rate:** repeated unordered document pairs among surfaced proposals; the expected result under the current invariant is zero.
- **Rejected-pair recurrence:** proposals resurfaced after that unordered pair was rejected; the expected result is zero while the same SQLite rejection state is retained.
- **Relationship-type quality:** agreement between a human-accepted type and the optional LLM type, plus rationale groundedness. This applies only when an explanation was generated.

Offline pair judgments measure candidate quality. Live approval and rejection rates measure the combined effect of retrieval, thresholds, evidence, explanations, interface, and the user's current interests; they should not be presented as pure retrieval scores.

## Reranker evaluation

Use a paired ablation on the same queries, candidate depth, corpus, and judgments:

- dense and sparse RRF in its original order;
- the same RRF candidate set reordered by the BGE reranker.

Compare MRR and nDCG@K for ordering, plus Recall@K where the cutoff can cause candidates to enter or leave the visible result set. Record latency and the fraction of candidates lacking a score. The experiment should test whether reranking improves PKP's rankings; current code and tests do not establish that it does.

Run two separate ablations. Search reranking scores `(query, top passage from result document)`. Proposal reranking scores `(source passage, candidate passage)` after bidirectional evidence retrieval and applies a proposal-specific threshold. A result for one pairing task does not demonstrate quality for the other.

## Robustness evaluation

Fault injection should confirm the behavior already defined by current code:

| Injected condition | What to verify |
| --- | --- |
| Qdrant unavailable | Search and proposal candidate retrieval use SQLite FTS5; ingestion preserves archive/SQLite state and remains unindexed. |
| One Qdrant collection query fails | Other collections can still return partial results, and the contained exception does not necessarily invoke global FTS fallback. |
| Reranker unavailable or returns unusable scores | Search and proposals retain usable first-stage ordering/candidates. |
| One or both passage lookups fail | The failure is contained per direction; the candidate remains eligible, missing evidence is stored as `None`, and incomplete pairs are not reranked. |
| LLM explanation is unavailable or malformed | No explanation update occurs; the existing template rationale and type remain. |
| Document-note generation fails | `generate_notes` returns `False` and attempts to write a retry placeholder; a worker invocation still reaches `done` because handler return values are not interpreted. |
| Worker is cancelled during a job | The active job is reset to `pending`, and cancellation propagates. |
| An ordinary worker job raises | The job becomes `failed` with completion time and error text; no automatic job retry is created. |
| Vault markers are missing or malformed | Mutation raises before replacement and preserves the existing file. |
| Vault append fails after approval | The proposal remains `approved` in SQLite while the response reports the vault warning; no connection is assumed to exist. |

Each scenario should inspect the relevant persisted row and filesystem bytes as well as the return value or log. That is necessary to detect partial success across SQLite, Qdrant, archive, and vault boundaries.

## Latency and resource evaluation

PKP currently records `extraction_time_ms`, `normalization_time_ms`, `archive_time_ms`, and `total_time_ms` in `ingestion_metrics`. Synchronous CLI ingestion can print those same values with `--profile`. Worker logs add total handler duration for claimed jobs, but that duration is not persisted as a separate latency metric.

The existing ingestion names have narrower and less intuitive boundaries than a complete pipeline profile. `total_time_ms` is captured before Qdrant indexing begins, so it excludes embedding and vector upsert time. `archive_time_ms` starts before archive writes and ends after document insertion, optional arXiv enrichment, optional vault scaffold creation, and per-chunk SQLite/FTS insertion; it is not isolated filesystem-write latency. Extraction and normalization have their own timers.

A future measurement run should separately capture extraction, normalization, archive writes, SQLite persistence, embedding/indexing, retrieval, passage lookup, reranking, proposal generation, and optional LLM explanation latency. Record model-load cold starts separately from warm inference, along with peak memory and corpus/chunk counts. These measurements require new instrumentation except where the existing timers or worker duration logs already cover the boundary. No historical latency or resource results are stored in the repository.

## Operational visibility

Current operational visibility consists of standard Python module logging across extraction, ingestion, indexing, proposal generation, LLM calls, note generation, worker execution, API review paths, and vault mutation. Worker logs include claim, dispatch, completion, failure, cancellation, startup reset, and elapsed handler duration. SQLite persists job status, timestamps, and error text, and it persists the ingestion timing fields described above. CLI `--profile` displays ingestion timing for synchronous URL and PDF commands.

This is targeted logging and timing rather than full tracing or observability infrastructure. Current source provides no OpenTelemetry integration, trace/span correlation, retrieval-quality telemetry, token or cost accounting, dashboards, or agent trajectory tracing.

## Known limitations

- There is no formal retrieval relevance benchmark or stored human-judgment dataset.
- Local BGE-M3 embedding and BGE reranking models are heavyweight and introduce cold-start, memory, and inference costs that are not currently measured.
- SQLite FTS fallback preserves lexical search but loses BGE-M3 semantic retrieval and uses different scoring semantics.
- A per-collection Qdrant error can yield partial or empty hybrid results without necessarily activating FTS fallback.
- The in-process polling worker is not a distributed queue. Ordinary failed jobs have no automatic retry, backoff, or dead-letter path.
- A failed `generate_notes` handler can return `False` while its worker job is recorded as `done`.
- Proposal approval and vault append are separate operations; an approved proposal can lack its corresponding vault connection.
- Extraction uses the Crawl4AI Python package, while CLI and API health checks probe a Crawl4AI HTTP endpoint that the extraction path does not use.
- Generated document notes support Ollama only, even though proposal explanations also support OpenAI and Anthropic.
- arXiv enrichment commits a SQLite/FTS title update before rewriting archive metadata, so a later metadata-write failure can leave the two views temporarily inconsistent.
- Source freshness is not scheduled: current code has no periodic re-ingestion or refresh mechanism.
- Real external services, provider responses, model inference, and fresh-clone end-to-end behavior are not covered by the current tests.
- Existing ingestion `total_time_ms` excludes vector indexing, so it cannot be used as complete ingestion latency.
