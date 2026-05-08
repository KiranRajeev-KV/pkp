# Evaluation

PKP separates deterministic pipeline testing from retrieval and relationship-quality evaluation. The existing tests focus on mechanics and state boundaries; useful retrieval quality needs a labelled corpus and human judgments.

## Test coverage

The test suite covers proposal generation and pair deduplication, reranker ordering and fallback mechanics, database and job-state transitions, LLM response parsing, long-document note helpers, and vault marker/idempotency invariants. It also covers citation-heavy chunk classification and arXiv identifier and metadata helpers.

The strongest invariants are practical ones: an existing document pair is not proposed twice, rejected pairs are remembered, reranking can reorder first-stage candidates while a disabled or failed reranker keeps usable candidates, and vault writes preserve content outside their managed regions. Duplicate connection appends are idempotent, and missing markers refuse a write rather than rewriting an ambiguous note.

## Retrieval evaluation

PKP does not yet include a labelled retrieval benchmark.

A useful offline evaluation would freeze a representative archive snapshot, define information needs, and collect human document-relevance judgments. Pooling results from each system before judgment helps avoid evaluating only one retrieval method's candidate set. Keep queries used for tuning separate from the final comparison set.

Compare the same corpus and queries with:

1. SQLite FTS5.
2. BGE-M3 dense-only retrieval.
3. BGE-M3 sparse-only retrieval.
4. Dense and sparse retrieval fused with RRF.
5. RRF followed by the BGE reranker.

FTS5 and the two hybrid variants are available through existing paths. Dense-only and sparse-only runs need a small evaluation harness because normal Qdrant search prefetches and fuses both representations.

| Metric | Question it answers |
| --- | --- |
| Recall@K | How much judged-relevant material appears in the first K documents? |
| Precision@K | How much of a short result list is relevant? |
| MRR | How early does the first useful document appear? |
| nDCG@K | Does the ranking place highly useful documents ahead of marginally useful ones? |

Record the archive snapshot, chunking settings, model revisions, collection version, ranked document IDs, and per-query results with each run. This makes later comparisons interpretable without turning small samples into broad quality claims.

## Relationship proposal evaluation

Retrieval relevance and a useful durable relationship are different targets. Two documents can be related to the same query without deserving a connection in the vault.

Build a set of source documents and pooled candidate pairs, then ask a human to judge whether the connection is useful and whether each evidence passage supports it. When an LLM explanation is present, judge the relationship type and whether the rationale is grounded in the stored passages.

Useful measures include:

- relationship Precision@K;
- human acceptance and rejection rates;
- evidence-passage relevance and missing-evidence rate;
- duplicate proposal rate;
- rejected-pair recurrence;
- relationship-type and rationale quality for explained proposals.

Live review rates describe the combined workflow, including thresholds, evidence, explanations, and user interests. They should not be presented as pure retrieval metrics.

## Reranker ablation

Compare hybrid RRF in its original order with the same candidate set after BGE reranking. Measure MRR and nDCG@K for ordering, Recall@K where the visible cutoff changes, reranking latency, and the share of candidates without a score.

Run the experiment separately for both tasks. Search reranking scores `(query, passage)`. Proposal reranking scores `(passage A, passage B)` after bidirectional evidence retrieval and uses a proposal-specific threshold. Improvement in one task does not imply improvement in the other.

## Robustness

Robustness tests should exercise the main degradation boundaries:

- Qdrant unavailable, leading to FTS5 candidate retrieval;
- reranker unavailable, preserving first-stage ordering;
- worker interruption, returning the active job to `pending`;
- malformed vault markers, refusing unsafe mutation;
- a vault write failure after approval, leaving a detectable partial state.

It is also useful to test an individual Qdrant collection failure, because other collections can still return partial hybrid results without switching to global FTS fallback.

## Performance

Useful measurements include extraction, normalization, archive and SQLite persistence, embedding/indexing, retrieval, evidence lookup, reranking, proposal generation, and LLM calls. Measure cold model loads separately from warm inference.

`IngestionMetric`, CLI `--profile`, and worker logs provide basic timing signals. `total_time_ms` currently ends before Qdrant indexing, so it should not be treated as full end-to-end ingestion latency. More complete performance analysis needs dedicated instrumentation and a fixed corpus.
