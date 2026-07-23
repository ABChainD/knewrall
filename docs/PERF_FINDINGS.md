# Knewrall performance investigation — embedding-call latency

## Symptom
User reported a single Knewrall query (`recall`/`search-graph --hybrid`) taking
**more than 2 minutes**.

## Method
Measured directly against the live workspace (93 neurons, 265 edges, 245 aliases):

1. Literal-only path (`search_graph(query, hybrid=False)`): **0.01s**. SQLite
   LIKE query over 93 nodes — negligible, not a factor.
2. A single `EmbeddingAdapter.embed_one()` call (provider=openrouter, model=
   `qwen/qwen3-embedding-8b`, the model configured in
   `.knewrall/config.json`): **12.16s** on a clean run.
3. Three back-to-back `embed_one()` calls in the same process:
   `23.74s`, `80.93s`, `80.96s`.

The 80s calls line up exactly with the adapter's retry math: `timeout=30s`,
`max_retries=3`, backoff `1s, 2s, 4s...` between attempts
(`src/knewrall_embeddings.py:81-96,127-160`). An 80s call is one full 30s
timeout, a 1s sleep, a second full 30s timeout, a 2s sleep, then a ~18s
successful third attempt — i.e. the *provider* is intermittently slow/unresponsive
enough to blow through a 30s socket timeout, and the adapter's own retry loop
turns that into 60-90+ seconds before it even reaches the caller.

## Root causes (ranked by impact)

### 1. Provider/model latency + retry amplification (primary cause)
`qwen/qwen3-embedding-8b` via OpenRouter is an 8B-parameter embedding model.
Observed latency is 12s on a good call and 24-81s when a retry is triggered.
Compare to typical embedding APIs (OpenAI `text-embedding-3-small`, Google
`text-embedding-004`), which are usually sub-second. `EmbeddingAdapter`
(`src/knewrall_embeddings.py`) has no fast-fail path — `timeout=30`,
`max_retries=3` are hardcoded defaults with nothing overriding them in
`get_embed_adapter()` (`src/knewrall_middleware.py:102-117`). Worst case for
**one** embed call: `30 + 1 + 30 + 2 + 30 = 93s`.

### 2. Sequential, unbatched per-term embedding in `recall()`
`recall()` (`src/knewrall_middleware.py:400-415`) loops over search terms and
calls `adapter.embed_one(term)` **once per term, sequentially**:
```python
for idx, term in enumerate(terms):
    qvec = adapter.embed_one(term)
    ...
```
`EmbeddingAdapter.embed()` already batches a list of texts into a single HTTP
call (`batch_size=64`), but `recall()` doesn't use it — it does N round trips
instead of 1. Knewrall's own instructions tell agents to *"give it every term
you care about at once (a person, a project, a decision) rather than one call
per term"* — which is exactly the case that multiplies this cost. Three terms
at ~12s "good case" each = 36s; three terms with one retry-triggering term =
36s + 60-80s extra = well over 2 minutes. This matches the reported symptom.

`search_graph(..., hybrid=True)` only embeds one query string, so it's not
exposed to this multiplier — `recall()` with 2+ terms is the likely culprit.

### 3. No query-embedding cache
Every `recall`/`search-graph --hybrid` call re-embeds the query text from
scratch, even for a term searched moments ago in the same session. Stored
neuron/note/code embeddings *are* cached by content hash
(`VectorStore._needs_embed`), but there's no equivalent cache for ephemeral
query vectors.

### 4. Fully synchronous, no partial/early return
The CLI process blocks end-to-end: literal SQLite results are ready in
~10ms but are held until the (possibly 90s+) semantic leg finishes or fails.
There is graceful *degradation* (falls back to literal-only on exception —
`knewrall_middleware.py:273-274`, `414-415`), but no graceful *timeout* — a
slow-but-not-erroring provider call is not treated as a failure, so it isn't
short-circuited.

## Fix plan

### Phase 1 — cheap, high-impact, no architecture change
1. **Cut the worst-case blocking time.** Drop per-attempt `timeout` from 30s
   to ~6-8s and `max_retries` from 3 to 1 for query-time embedding calls
   (bulk `embed`/indexing calls used by `embed_neurons`/`embed_code_symbols`
   can keep the current generous values — they're offline/batch, not
   interactive). Worst case for one query call drops from 93s to ~7-15s.
2. **Batch per-term embeddings into one HTTP call.** In `recall()`, replace
   the `for term: embed_one(term)` loop with a single
   `adapter.embed(terms)` call, then zip results back to each term's KNN
   search. Cuts N round trips to 1 regardless of term count.
3. **Treat a timed-out/failed embed call as literal-only degradation**,
   already the pattern used elsewhere in the file — just needs the above two
   changes to make that path trigger fast instead of after 90s.

### Phase 2 — return-early UX (the user's proposed direction)
Given the CLI is invoked as a single blocking subprocess call by the calling
agent (not a long-lived server), true "print now, stream more later" isn't
free, but a bounded approximation is:
4. **Time-boxed semantic leg.** Run the embedding call(s) with a short
   deadline (e.g. 3-5s via a thread + `future.result(timeout=...)`). If the
   deadline passes, return literal results immediately and mark
   `stats.semantic_pending = true` rather than waiting on the slow/degraded
   provider at all — the retry can still happen, just off the critical path.
5. **Optional background top-up.** On timeout, spawn a detached background
   process that finishes the embedding call and writes the semantic hits to
   a small keyed cache file (e.g. `.knewrall/query_cache.db` or a JSON blob
   keyed by term+model hash). The *next* `recall`/`search-graph` call for the
   same/similar term checks that cache first — so a slow provider penalizes
   the first call only, not every retry. This is the closest fit to the
   user's "start ASAP, wait in background" idea without adding a persistent
   daemon.
6. **Query-embedding cache** (short TTL, e.g. process-local LRU or a row in
   `vectors.db`) so identical repeated terms within a session don't re-embed.

### Phase 3 — optional, needs user sign-off
7. **Reconsider the embedding model.** `qwen/qwen3-embedding-8b` (4096-dim)
   is measurably slower than typical small embedding models. Switching to a
   faster model (e.g. `openai/text-embedding-3-small`, 1536-dim) would cut
   both per-call latency and the frequency of timeout-triggered retries, but
   requires re-embedding every neuron/note/code symbol (dimension change
   breaks the existing `vec0` table) — a deliberate, user-approved migration,
   not a silent swap.

## Non-findings (ruled out)
- Local SQLite index (`nodes`/`edges`/`aliases`) is not a bottleneck at this
  graph size (93 nodes) — sub-10ms literal queries, proper indexes already
  exist on `aliases(alias)`, `edges(source_id/target_id/predicate/link_type)`.
- `_shape_neuron_for_recall`'s per-link `indexer.get_node_by_id()` resolution
  (depth=1 related-link summaries) is local-SQLite and negligible at this
  scale; not investigated further as a scaling concern but flagged for future
  reference if the graph grows by orders of magnitude.
- Connection/DNS/TLS overhead per `urllib` request: real (~0.2-0.3s, no
  pooling) but noise against 12s+ model latency. Not worth fixing.

---

## Reviewed by Opus and Kimi K3 (design review, both independent)

Both reviewers verified the root-cause analysis against the actual code and
agreed on the diagnosis. Corrections and additions below are merged from both.

### Additional root-cause finding (Kimi)
`get_vector_store()` (`knewrall_middleware.py:80-96`) calls `_probe_embed_dim()`
→ `detect_dim()` — **another blocking embed call** — the first time
`vectors.db` doesn't exist yet. A cold-start query can pay two sequential
embed calls, not one. One-time cost, but worth noting.

### Real hazard identified for Phase 2 (both reviewers independently flagged this)
`VectorStore` holds one `sqlite3.Connection` opened with default
`check_same_thread=True` (`knewrall_vectors.py:110`). If a background
thread/process ever calls `knn_search()`/`upsert_embedding()` on the shared
singleton, it raises `ProgrammingError`. **Any time-boxed-thread
implementation must confine the backgrounded thread to the HTTP embed call
only — KNN search stays on the main thread**, using the vector the thread
returns (or none, if it timed out).

### Revised, final plan

**Phase 1 (ship first) — now includes the query cache:**
1. Batch `recall()`'s per-term `embed_one()` loop into one
   `adapter.embed(terms)` call (`knewrall_middleware.py:407-413`).
2. Add per-call `timeout`/`max_retries` **override params** on
   `EmbeddingAdapter.embed()`/`_request()` (default to the instance's
   values) rather than forking a second adapter class/config. `recall()` and
   `search_graph(hybrid=True)` pass short values (e.g. 6-8s, 1 retry);
   `embed_neurons`/`embed_code_symbols` (bulk/offline) pass nothing and keep
   today's generous 30s/3. **Apply the same override to the Google AI path**
   (`knewrall_embeddings.py:179-221`), which duplicates the retry loop
   separately from the OpenAI-compatible path — easy to fix one and miss the
   other.
3. **Query-embedding cache**, moved up from Phase 2: persist successful query
   vectors keyed by `(term, model)` — a small table in `vectors.db` is fine,
   no need for the elaborate background-process cache originally sketched.
   Deterministically kills repeated-query cost with no threading involved.
4. Note `adapter_from_config()` (`knewrall_embeddings.py:266-282`) is dead
   code that already ignores timeout — don't route the new override through
   it by accident.
5. **Proactive query-term pre-seeding (extends item 3).** `recall()`/
   `search_graph()` terms are overwhelmingly names of things already in the
   graph (grounding on a known person/project/place), not arbitrary novel
   text. So beyond the *reactive* cache (item 3, populated only after a term
   is searched once live), also *proactively* embed every existing
   `canonical_name` + alias + tag string as a new `kind="query_term"` row in
   `vectors.db`, refreshed alongside `embed --neurons`/`refresh-index` (off
   the interactive path, so it can safely keep the generous 30s/3-retry
   settings). This turns the *first* search for an already-known entity name
   into a cache hit too, not just repeats. Only covers exact/near-exact
   name/alias/tag matches — a genuinely novel or paraphrased query still
   needs a live embed call, which items 1-2's short-timeout/graceful-degrade
   path already covers.

**Phase 2 (conditional — measure after Phase 1 before building):**
5. Time-boxed semantic leg via a **daemon thread** (dies with the process,
   so a leaked `urlopen` on timeout is safe) that does **only** the HTTP
   embed call; the main thread calls `future.result(timeout=...)` and, on
   timeout, returns literal results immediately with
   `stats.semantic_pending = true`. Skip this if the Phase-1 short timeout
   already makes literal-only fallback rare enough in practice — a 12s
   *clean-case* embed latency means an aggressive interactive timeout will
   degrade a large fraction of queries to literal-only, so validate that
   trade-off is acceptable before building the extra machinery.
6. **Cut the originally-proposed detached-background-process + cache-file
   top-up** (old item 5). Both reviewers independently called this
   over-engineered for a one-shot CLI with no daemon: process cold-start
   cost, orphan/cleanup concerns, and cache-write races for a benefit that
   only helps a *second* identical call — which Phase 1's query cache
   already covers once the first call succeeds.

**Phase 3 (unchanged, user-gated):** reconsider the embedding model. Both
reviewers agree this is the actual disease, not the symptom — Phases 1-2 are
mitigation. A faster/smaller embedding model (e.g. `text-embedding-3-small`,
sub-second, 1536-dim) would shrink or eliminate the need for Phase 2
entirely, at the cost of a deliberate re-embedding migration (dimension
change breaks the existing `vec0` table). Worth surfacing as a decision point
*before* investing in Phase 2, not after.
