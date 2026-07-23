# Vector search (hybrid semantic + literal retrieval)

How Knewrall's semantic layer works and why it's shaped the way it is. See
[Configuring the embeddings model](../README.md#configuring-the-embeddings-model)
in the README for how to set one up — this doc has no opinion on which model you
should pick beyond the comparison data below.

## What it does

`search-graph --hybrid` and `recall()` (hybrid on by default) blend two
retrieval paths:

1. **Literal** — a SQLite `LIKE` match over `canonical_name`, aliases, and
   tags. Instant (sub-10ms even at moderate graph sizes), but only finds
   exact/substring matches.
2. **Semantic** — a k-nearest-neighbour search (via `sqlite-vec`'s `vec0`
   virtual table) over embedding vectors of neuron/note/code-symbol content,
   so a query can match on *meaning* rather than literal substring.

Results from both are merged with Reciprocal Rank Fusion (RRF), so a genuine
literal hit and a genuine semantic hit both get a fair shot at ranking near
the top rather than one path silently dominating.

Everything lives in `.knewrall/vectors.db`: one `embedding_meta` table
(kind, ref_id, content hash, model, dim) plus one `vec0` virtual table
per configured dimension. It's marked `ephemeral`/`rebuildable` in
`.knewrall/config.json` — safe to delete and regenerate via `embed`.

## The three embedding "kinds"

| `kind` | What's embedded | Populated by |
|---|---|---|
| `neuron` | Flattened neuron body (name, aliases, descriptions, properties) | `embed --neurons` |
| `code_symbol` | Function/class/method signature + docstring | `embed --code` |
| `query_term` | Every existing `canonical_name` + alias + tag string | `embed --terms` |

`embed` with no flags runs all three. The first two are the searchable
*corpus*. `query_term` is different: it isn't searched against — it's a
**cache of query-side embeddings**, explained next.

## The query-embedding problem, and how it's solved

Embedding a neuron once, ahead of time, is cheap regardless of latency — it
happens off the interactive path. But `recall()`/`search-graph --hybrid`
must embed the **query text** live, every time, because in general you can't
precompute the embedding of text you haven't seen yet. That live call sits
directly in the response-time critical path.

This used to be a serious problem: with the model this project shipped with
initially, a single query embed call measured 12s on a clean run and
24-81s across repeated calls in the same process (the 80s calls matching
the adapter's own 30s-timeout × 3-retry math). `recall()` additionally
embedded each search term **sequentially, one HTTP call per term** — and
Knewrall's own agent instructions say to pass multiple terms to `recall()`
at once, which directly multiplied the cost. See `PERF_FINDINGS.md` and
`EMBEDDING_MODEL_BENCHMARK.md` for the full investigation and the 10-model
benchmark that led to the fixes below.

Four things now bound and shrink that cost:

### 1. Batching
`recall()` collects every cache-miss term and embeds them in **one** HTTP
call (`adapter.embed(terms)`), not one call per term. `search_graph()`'s
single query term was already one call.

### 2. Interactive-path timeout/retries
`EmbeddingAdapter.embed()`/`embed_one()` accept per-call `timeout`/
`max_retries` overrides (`src/knewrall_embeddings.py`). Interactive callers
(`recall`, `search_graph(hybrid=True)`) pass a short pair — 8s timeout,
1 attempt (`_INTERACTIVE_EMBED_TIMEOUT`/`_INTERACTIVE_EMBED_MAX_RETRIES` in
`knewrall_middleware.py`). Bulk/offline callers (`embed_neurons`,
`embed_code_symbols`, `embed_query_terms`) omit the override and keep the
adapter's generous instance defaults (30s × 3 retries) — they're not on
anyone's interactive clock.

### 3. Time-boxed deadline
The batched interactive embed call runs on a **daemon thread** with a hard
9s wall-clock deadline (`_embed_batch_with_deadline`). If the deadline
passes, `recall()`/`search_graph()` return literal-only results immediately
(`recall()` sets `stats.semantic_pending = true`) rather than blocking on a
slow/unresponsive provider. The daemon thread — deliberately *not* a
`concurrent.futures.ThreadPoolExecutor`, whose worker threads are joined at
interpreter exit — is abandoned and dies with the process; its eventual
result (if any) is simply discarded. That thread does **only** the HTTP
call — never `VectorStore`/KNN, whose `sqlite3` connection is opened with
the default `check_same_thread=True` and isn't safe to touch off the main
thread.

### 4. Query-term cache — reactive and proactive
Every successful live embed result is cached (`vectors.db`, `kind=
"query_term"`, keyed by the normalized term) so a repeated search never
re-embeds. Beyond that reactive cache, `recall()`/`search_graph()` terms are
overwhelmingly names of things already in the graph — you're grounding on
a known person/project/place, not typing arbitrary prose. So
`embed --terms` **proactively** embeds every existing `canonical_name` +
alias + tag as a `query_term` cache entry ahead of time, off the interactive
path. That turns the *first* search for an already-known entity into a
cache hit too, not just repeats.

This only helps when the query text matches an existing name/alias/tag
(exact, case-insensitive). A genuinely novel or paraphrased query still
needs a live embed call — items 1-3 above are what keep that call fast and
bounded.

## Embedding model comparison

As part of the query-latency investigation above, 10 of the most popular
embedding models on OpenRouter (ranked by actual 7-day usage share, not just
API list order) were benchmarked head-to-head. Method: same
`EmbeddingAdapter` class Knewrall uses in production, 3 calls per model
against short query-shaped strings (names/short phrases, not documents),
20s timeout, no retries (raw single-attempt latency).

| Model | dim | mean | median | stdev | Notes |
|---|---|---|---|---|---|
| `perplexity/pplx-embed-v1-0.6b` | 1024 | 0.42s | 0.42s | 0.02 | Fastest and most stable measured |
| `nvidia/llama-nemotron-embed-vl-1b-v2:free` | 2048 | 0.72s | 0.72s | 0.02 | Very stable, free |
| `mistralai/mistral-embed-2312` | 1024 | 0.63s | 0.63s | 0.04 | Very stable |
| `google/gemini-embedding-2` | 3072 | 0.57s | 0.60s | 0.05 | Very stable |
| `openai/text-embedding-3-large` | 3072 | 0.72s | 0.71s | 0.05 | Very stable |
| `baai/bge-m3` | 1024 | 0.66s | 0.58s | 0.15 | Stable |
| `openai/text-embedding-3-small` | 1536 | 0.69s | 0.73s | 0.10 | Highest usage share of all 27; best proven quality track record |
| `google/gemini-embedding-001` | 3072 | 0.97s | 0.65s | 0.67 | One 1.74s outlier in 3 calls |
| `qwen/qwen3-embedding-8b` | 4096 | **9.28s** | 7.71s | **6.22** | Slow and highly variable — root cause of the original latency problem investigated above |
| `qwen/qwen3-embedding-4b` | — | — | — | — | Failed all 6 calls across 2 runs: HTTP 429 "engine_overloaded" |

None of this is a recommendation baked into the release — see
[Configuring the embeddings model](../README.md#configuring-the-embeddings-model)
for how to pick and set one for your own install. Full methodology and
caveats (n=3, single vantage point — a quick comparative screen, not an SLA
study) are in `EMBEDDING_MODEL_BENCHMARK.md`.

### Changing the model
The vector dimension is baked into `vec0`'s table schema at creation — it
is **not** a live config toggle. To switch models:

1. Update `.knewrall/config.json`'s `embeddings.model`/`embeddings.dim`.
2. Delete (or rename) `.knewrall/vectors.db` — it's `ephemeral`/
   `rebuildable` by design, so this is safe; a fresh one gets created at
   the new dimension on next use.
3. Re-run `embed` (no flags → re-embeds neurons, code symbols, and
   query terms in one go).

Note: `embedding_meta`'s content-hash guard (used to skip re-embedding
unchanged text) hashes the text being embedded, not the model — it doesn't
know the model changed. That's why step 2 (a full wipe) is required rather
than an incremental re-embed; a partial/incremental model swap would
silently leave stale vectors from the old embedding space mixed in with the
new ones, which is worse than a full rebuild since nothing would flag the
mismatch.
