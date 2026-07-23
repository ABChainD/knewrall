# OpenRouter embedding model benchmark — decision report

Follow-up to `PERF_FINDINGS.md` Phase 3 ("reconsider the embedding model").
This benchmarks the top-10 most-popular embedding models on OpenRouter to
inform that decision.

## Scope selection
Fetched OpenRouter's live model catalog
(`https://openrouter.ai/api/v1/models?output_modalities=embeddings`, 27
models total) and cross-referenced against the site's actual
`order=most-popular` ranking (by 7-day token throughput, read via rendered
page since that ranking isn't exposed on the plain API). Top 10 by real
usage popularity:

| Rank | Model | 7d tokens (site) |
|---|---|---|
| 1 | `openai/text-embedding-3-small` | 176B |
| 2 | `qwen/qwen3-embedding-8b` **(currently configured)** | 142B |
| 3 | `openai/text-embedding-3-large` | 24B |
| 4 | `baai/bge-m3` | 14.7B |
| 5 | `google/gemini-embedding-2` | 14.3B |
| 6 | `mistralai/mistral-embed-2312` | 12.6B |
| 7 | `google/gemini-embedding-001` | 11.2B |
| 8 | `perplexity/pplx-embed-v1-0.6b` | 11.1B |
| 9 | `qwen/qwen3-embedding-4b` | 10.1B |
| 10 | `nvidia/llama-nemotron-embed-vl-1b-v2:free` | 6.61B |

## Method
Standalone script, same `EmbeddingAdapter` class Knewrall uses in
production (`src/knewrall_embeddings.py`), called directly — no changes to
Knewrall itself. Per model: 3 calls, one per test string (short
names/phrases matching real `recall()`/`search-graph` query shape — *not*
long documents), `timeout=20s`, `max_retries=1` (no backoff inflation, so
the numbers reflect raw single-attempt latency rather than
retry-amplified totals). Test strings: `"Roca Bruja"`,
`"Aregon marketing project Q3 planning"`,
`"personal knowledge graph performance tuning"`.

**Caveats:** n=3 per model, single vantage point (this machine, one point
in time), no retry — this is a quick comparative screen, not a
statistically rigorous SLA study. Good enough to separate "clearly fine"
from "clearly problematic," which is all this decision needs.

## Results

| Model | dim | mean | median | min | max | stdev | Verdict |
|---|---|---|---|---|---|---|---|
| `perplexity/pplx-embed-v1-0.6b` | 1024 | 0.42s | 0.42s | 0.40s | 0.45s | 0.02 | Fastest, most stable |
| `nvidia/llama-nemotron-embed-vl-1b-v2:free` | 2048 | 0.72s | 0.72s | 0.70s | 0.74s | 0.02 | Very stable, free |
| `mistralai/mistral-embed-2312` | 1024 | 0.63s | 0.63s | 0.59s | 0.67s | 0.04 | Very stable |
| `google/gemini-embedding-2` | 3072 | 0.57s | 0.60s | 0.52s | 0.60s | 0.05 | Very stable |
| `openai/text-embedding-3-large` | 3072 | 0.72s | 0.71s | 0.68s | 0.78s | 0.05 | Very stable |
| `baai/bge-m3` | 1024 | 0.66s | 0.58s | 0.56s | 0.83s | 0.15 | Stable |
| `openai/text-embedding-3-small` | 1536 | 0.69s | 0.73s | 0.58s | 0.76s | 0.10 | Stable |
| `google/gemini-embedding-001` | 3072 | 0.97s | 0.65s | 0.53s | 1.74s | 0.67 | One 1.74s outlier |
| **`qwen/qwen3-embedding-8b`** (current) | 4096 | **9.28s** | **7.71s** | 4.00s | 16.14s | 6.22 | **Slow and highly variable** |
| `qwen/qwen3-embedding-4b` | — | — | — | — | — | — | **Failed all 3 calls: HTTP 429 "engine_overloaded"** |

`qwen3-4b`'s failure was re-checked with 3 more calls immediately after the
main run — same `engine_overloaded` 429 every time, not a one-off blip.
Currently effectively unusable via OpenRouter regardless of speed.

## Finding
**Every other model in the top 10 is 9-20x faster than the currently
configured `qwen/qwen3-embedding-8b`, and far more stable** (stdev
0.02-0.15s vs. 6.22s). This directly confirms `PERF_FINDINGS.md`'s
diagnosis: the configured model is the actual disease, not just a
contributing factor — it is measurably the slowest and least consistent
option among popular alternatives, not merely "a large model that's
somewhat slower."

## Recommendation
Migrate off `qwen/qwen3-embedding-8b`. Two reasonable picks depending on
priority:

- **`perplexity/pplx-embed-v1-0.6b`** — fastest and most stable measured
  (0.40-0.45s), 1024-dim (smaller vectors, less storage/compute than the
  current 4096-dim). Newer/smaller provider footprint than OpenAI/Google —
  worth a quick check on their embedding quality/MTEB standing before
  committing, since this benchmark only measured speed, not retrieval
  quality.
- **`openai/text-embedding-3-small`** — the most standard/well-trodden
  choice (highest usage share of all embedding models on OpenRouter, 176B
  tokens/week), still consistently sub-second (0.58-0.76s), 1536-dim,
  proven track record for retrieval quality. Lower risk if retrieval
  quality matters more than shaving another ~250ms off an already-fast
  call.

Either requires the Phase-3 migration already flagged: re-embedding every
neuron/note/code symbol, since the vector dimension changes (4096 → 1024
or 1536) and `vec0`'s table dimension is baked in at creation — not a
config toggle. This is a deliberate, user-approved cutover, not a silent
swap.

**Not recommending:** `qwen/qwen3-embedding-4b` (currently overloaded/
erroring), `google/gemini-embedding-001` (one outlier call in a 3-call
sample is a soft flag, not disqualifying, but `gemini-embedding-2` — its
newer sibling — scored cleanly and costs the same, so prefer that one if
staying in the Gemini family).
