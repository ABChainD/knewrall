# Assistant Reader Layer (ARL)

Query-conditioned answering over text your agent is already holding. Enabled by
default — it auto-selects whichever configured model actually resolves on your
machine and never spends a call you didn't ask for. See
[What it is](../README.md#what-it-is) in the README for where this fits in
Knewrall's broader job as an agent context management layer — this doc is the
deep dive on the mechanism itself.

## What it does

The Engram Layer computes one fixed digest at fold time, before anyone knows what
will actually be asked about that content — correct and cheap for keeping context
compact, but structurally unable to answer a specific question later. ARL adds an
**optional, explicitly-invoked** enrichment pass on top of three existing entry
points — `unfold`, `recall`, and `fold`/`fold-run` — plus a standalone `reader-ask`
command: pass `--ask "<question>"` and the underlying text (chunked if needed) is
routed to a locally-resolved model that answers *that question against that text*.

The deterministic output of whichever command you called is **always returned
unchanged alongside** the answer. The assistant's response is additive and
attributed — a labeled, machine-derived claim — never a substitute for the real
data.

```bash
python knewrall/bin/knewrall.py recall "Grupo Roca" --ask "what's the current Odoo version and is it in production?"
python knewrall/bin/knewrall.py unfold <engram_key> --ask "did the deploy step actually fail, or just warn?"
python knewrall/bin/knewrall.py reader-ask --file notes/2026/08/meeting.md --ask "what did we decide about pricing?"
```

## Why it exists

Loosely inspired by Recursive Language Models (Zhang/Khattab, arXiv 2512.24601):
keep a large context in a REPL, let the model write code to select the relevant
slice, then call an LLM recursively on just that slice — selection is
**query-conditioned**, decided at question time, not fold time. Knewrall's Engram
Layer is structurally the opposite of that by design (a fixed digest is what makes
folding cheap and deterministic), so ARL borrows only the piece that doesn't
conflict: *return an answer, not a window* — applied only when a question is
actually asked, since Knewrall has no vantage point from which to know that
question in advance.

It's the same "agent context management layer" job as the rest of Knewrall
(rich, relevant, compact, efficient) approached from the read side instead of the
write side: folding keeps noise out of context at write time; ARL lets an agent
get a targeted answer instead of a whole digest at read time, without spending the
tokens to read the underlying text itself.

## Design

### Two modules, cleanly split

- **`knewrall_reader_router.py`** — config-driven provider resolution and
  transport. Two co-equal transport kinds, **`cli`** (spawns a local harness
  binary, e.g. `opencode`) and **`http`** (any OpenAI-compatible
  `/chat/completions` endpoint). Binaries are resolved via `shutil.which` (never
  a bare command string — that raises `WinError 2` on Windows) and detection
  results are cached (`assistant.detect_cache_seconds`) so repeated calls don't
  re-probe. Adding a harness or endpoint is a config edit, not a code change.
- **`knewrall_reader.py`** — the actual `ask()`/`stats()`/`cache_gc()` surface:
  chunking, prompting, response parsing, and the per-machine SQLite answer cache.

### Fail-loud, never silent

An assistant failure must never look like a successful read. The CLI transport's
mandatory check is `returncode == 0 AND non-empty stdout` — `rc=0` with empty
stdout is a real, previously-observed failure shape for local harness subprocesses,
not a hypothetical edge case, so it's treated as an error rather than an empty
answer. Prompts are sent via **stdin as bytes** (not `argv` — real CLI prompts
blow past an ~8-9k char argv ceiling — and not `text=True`, which breaks on
non-UTF-8 default codepages like `cp1252`).

### Chunking, budgets, and caching

Long text is split into overlapping chunks (`chunk_chars`, `chunk_overlap_chars`,
capped at `max_chunks`) and each chunk is asked the question independently. The
whole call is bounded by `total_deadline_seconds`; hitting it returns whatever
answer(s) were gathered so far with `partial: true` rather than blocking
indefinitely or discarding partial progress.

Answers are cached in a per-machine SQLite database (`assistant.cache_db_path`,
default `.knewrall/reader_cache.db`), keyed on
`(content_hash, question_hash, provider, model)` so the same question against the
same content never pays for a second model call, with a configurable TTL
(`cache_ttl_hours`). `reader-stats` reports invocation/cache counts;
`reader-cache-gc` sweeps expired rows.

### Passive hints, never auto-triggers

ARL never fires on its own. Two config-gated hints exist, both purely
informational:

- **`assistant.min_chars` / `hint_on_threshold`** — when a `fold`/`unfold` result's
  text is at or above `min_chars`, the response gets an extra `assistant_hint`
  field suggesting `--ask` — the caller still has to opt in.
- **`assistant.recall_min_chars` / `recall_max_full`** — the "widened recall"
  path: when a `recall()` result's text is large enough and under the
  `recall_max_full` node-count cap, `--ask` can be answered against the fuller
  text instead of just the trimmed default view.

### Optional PDF extraction

`pypdf` is an optional dependency, imported under `try/except ImportError`. When
present, `read_file()` extracts a PDF's text layer directly; when absent, PDF
handling degrades to "pipe the extracted text in yourself" — no hard dependency,
no broken install for anyone who doesn't need it.

## Usage

| Command | Purpose |
|---|---|
| `recall "<term>" --ask "<question>"` | Answer a question against the recalled neuron content (widens to fuller text when it qualifies — see above). |
| `unfold <key> --ask "<question>"` | Answer a question against a folded engram's full content. |
| `fold [...] --ask "<question>"` / `fold-run [...] -- <cmd> --ask "<question>"` | Answer a question against content being folded right now. |
| `reader-ask --file <path> --ask "<question>"` (or pipe via stdin, omit `--file`) | Standalone: answer a question against any text file, independent of the fold/recall/unfold pipeline. |
| `reader-probe [--refresh]` | Show which configured providers actually resolve on this machine. |
| `reader-stats` | ARL invocation and cache statistics. |
| `reader-cache-gc [--older-than <dur>] [--dry-run]` | Remove expired cache rows. |

Shared flags on every `--ask`-capable command: `--provider <name>` (override the
configured default), `--no-assistant` (force the deterministic path even if
`--ask` is present — useful for scripting), `--ask-strict` (fail instead of
degrading silently if no provider resolves).

## Hard constraints

- **Zero coupling to anything outside this repo.** Nothing under `knewrall/src/`
  may import, read, shell out to, or require any other tool or path that merely
  happens to live alongside Knewrall in a given workspace. A fresh clone of this
  repo alone, with no surrounding workspace, yields a working ARL; everything
  environment-specific lives in `.knewrall/config.json`, never in code.
- **Enabled by default, but never fires unprompted.** `assistant.enabled` ships
  `true`, and the shipped `fallback_order` auto-selects whichever configured
  provider actually resolves on your machine — but the mechanism only ever
  activates on an explicit `--ask "<question>"`. No call is ever made on your
  behalf without one, so "enabled" costs nothing until you actually ask it
  something. Set `assistant.enabled: false` to turn the whole layer off.
- **Additive, never a substitute.** The deterministic output of `unfold`/
  `recall`/`fold`/`fold-run` is always returned unchanged; the assistant's answer
  is a separate, clearly attributed field alongside it.

## Configuration reference

All keys live under `assistant` in `.knewrall/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch. Set `false` to turn the whole layer off; everything else is inert while it is. |
| `provider` / `fallback_order` | `"claude"` / `["claude", "opencode", "openrouter", "ollama"]` | Preferred provider, then a fallback chain tried in order until one actually resolves on this machine (`resolve()` picks the first available; see [Design](#design)). Ships with a cheap model per provider: Claude Code's Haiku, OpenCode's DeepSeek V4 Flash, and whatever you've configured for OpenRouter/Ollama. |
| `providers.<name>` | — | Per-provider transport config (`cli` or `http`), model, timeouts, and (for `http`) `key_env` — an ordered list of environment variables checked for an API key. |
| `detect_cache_seconds` | `3600` | How long a provider-availability probe is trusted before re-checking. |
| `min_chars` / `hint_on_threshold` | `20000` / `true` | Passive `--ask` hint threshold on fold/unfold results. |
| `recall_min_chars` / `recall_max_full` | `12000` / `40` | Widened-recall thresholds (see above). |
| `chunk_chars` / `chunk_overlap_chars` / `max_chunks` | `48000` / `800` / `4` | Chunking parameters for long text. |
| `total_deadline_seconds` | `90` | Hard wall-clock budget for a whole `ask()` call; overrun returns `partial: true`. |
| `cache_db_path` / `cache_ttl_hours` | `.knewrall/reader_cache.db` / `720` | Per-machine answer cache location and TTL. |

Wall-clock performance of any one model is treated as a **deployment property to
tune**, not an architectural constraint — if a configured model is too slow,
change `assistant.model` for that provider; the design's job is only to keep the
cost bounded and escapable (`total_deadline_seconds`, `partial: true`) regardless
of which model is configured.

## Status

Implemented 2026-08-14: harness router, standalone reader + cache, `unfold`/
`recall` integration, `fold`/`fold-run` integration, and optional PDF extraction
are all in place and covered by the test suite. Ships `assistant.enabled: true`,
with `install.py` reporting which configured provider actually resolved on your
machine (or, if none did, explaining why and offering to disable it rather than
leaving every `--ask` call failing loud).

Background enrichment (a previously-considered phase that would run ARL
*unprompted*, without an explicit `--ask`) was **deliberately deferred** — that's
a different question from whether the layer itself ships on. The "no silent
background spend" principle is about never calling a model the caller didn't
ask for, which `enabled: true` doesn't change: nothing here fires without an
explicit `--ask` either way. Set `assistant.enabled: false` to opt back out of
the whole layer at any time.
