# Knewrall — Agent Operating Instructions

> **This file is the canonical, harness-agnostic instruction set for Knewrall.**
> Root files like `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, and `.clinerules` are thin
> pointers to this document. If you are an AI agent working in this workspace, the
> rules below are **always in effect** — you do not need the user to ask.

Knewrall is a local-first, file-backed **personal knowledge graph**. Its units are
**Neurons** (nodes) connected by typed **Links** (edges), organized under the 6
pillars **Who / What / Where / When / Why / How**. Files are the source of truth; a
disposable SQLite index makes them searchable.

You interact with it **only** through the Knewrall CLI (the middleware). Never write
to neuron/note/index files directly — the middleware enforces schema validation and
deterministic formatting so the graph stays conflict-free under Git/Dropbox sync.

---

## 1. Always-on behavior (no user request needed)

These three habits run automatically for every task, in the background, without
announcing them unless they affect your answer:

1. **Ground before you act.** At the start of a task — and whenever you hit a name,
   person, project, place, decision, or preference you might already know — recall
   the knowledge base first and use what you find to inform your response:

   ```
   python knewrall/bin/knewrall.py recall "the name or term" ["another term" ...]
   ```

   `recall` finds, loads, and shapes the matching Neurons' full content in one call
   — no follow-up file read needed. Give it every term you care about at once
   (a person, a project, a decision) rather than one call per term; if the first
   response surfaces a related name worth expanding, call `recall` again on that
   name. Reach for the lighter `search-graph "<term>"` only when you need to check
   whether something already exists (e.g. before `propose-node`), not its content.

2. **Save durable knowledge as you learn it.** Whenever the user reveals a fact worth
   remembering — people, organizations, projects, places, events, decisions, stable
   preferences, how things are done — capture it as a Neuron (and link it to related
   Neurons). Do this proactively. Skip throwaway/conversational details and anything
   already in the graph (check first with `search-graph`).

3. **Fold before you flood.** Before running a command or reading a file you expect
   to produce more than ~150 lines — test runs, builds, `git log`, `docker logs`,
   whole-file reads of large sources, bulk `grep` — run it through `fold-run` instead:

   ```
   python knewrall/bin/knewrall.py fold-run --label "<why you ran it>" -- pytest -q tests/
   ```

   You get the head, the tail, and a digest inline, plus a retrieval key. If you
   later need a part you didn't get, `unfold <key> --grep "<pattern>"`. Never fold
   Knewrall's own `recall` output — it is already budgeted. See §2's short-term
   memory table for the full command surface (`fold`, `unfold`, `folds`, ...).

If a fact is ambiguous or sensitive, ask once before saving. Otherwise just save it.

> **Path note:** the commands above assume Knewrall is installed as a `knewrall/`
> subfolder of the workspace. If it lives elsewhere, adjust the path to
> `bin/knewrall.py`, or set `KNEWRALL_ROOT` to the Knewrall directory. The launcher
> works from any working directory.

---

## 2. The CLI (your only tools)

Run everything through the launcher; it is path-independent.

### Core knowledge graph commands

| Command | Purpose |
|---|---|
| `recall "<term>" ["<term>" ...] [--depth 0\|1] [--format toon\|json]` | **Consolidated retrieval** — the default way to ground: finds, loads, and shapes matching Neurons' full content (descriptions/properties/links, with link targets resolved to names) in one call, plus a capped summary of their immediate links at `--depth 1` (default). TOON-encoded by default (fewer tokens than JSON). Output is deliberately bounded — call again on a related name to expand it, rather than expecting one call to return the whole neighborhood. |
| `search-graph "<query>"` | Find existing Neurons by canonical name, alias, or tag — **pointers only** (id/type/name), not content. **Use before creating anything** to avoid duplicates. Add `--hybrid` for semantic (vector) search. |
| `propose-node --json '<payload>'` | Validate and create a new Neuron from inline JSON (also accepts a file path, or `-`/stdin). The system generates the UUID; do not supply one. |
| `propose-link <source_id> <target_id> <predicate>` | Create a typed link between two existing Neurons (e.g. `employed_by`, `located_in`). |
| `update-node <id> --json '<updates>'` | Correct a stale fact on an existing Neuron (also accepts a file path or stdin). Schema-validated and re-indexed. Given `properties` keys are **replaced** by default; pass `--append` to keep prior values as history instead. Never touches `links` (use `propose-link`) or `system`. |
| `update-note-links <note_path> <name> [<name> ...]` | Append clickable markdown links from a note in `notes/` to the named Neurons. |
| `search "<query>"` / `stats` | Inspect the index. |
| `refresh-index` | **Routine.** Incrementally sync the index to whatever changed on disk (content-hash based, not a full rescan) — safe and cheap to run every session. |
| `rebuild-index --full` | Hard reset: drop and rebuild the whole index from files. Only needed after a structural/schema change or index corruption — `refresh-index` is the everyday command. |

To create a node, write the payload to a temp `.json` file, then call
`propose-node` on it. A warning about a "potential collision" means a similar Neuron
may already exist — investigate and, if genuinely distinct, set the `disambiguation`
field rather than creating a near-duplicate.

### Code repository commands (when the workspace contains code repos)

Repos live under `knewrall/projects/` (read-only; Knewrall never modifies them).
After placing a repo there, index it once. From then on, you can query its symbols
and link neurons to code.

| Command | Purpose |
|---|---|
| `index-code [path] [--full]` | Build/refresh the code symbol graph. Omit `path` for all repos in `projects/`. `--full` drops and rebuilds. |
| `code-search "<query>"` | Full-text search over function/class/method names, signatures, and docstrings. |
| `code-defs <name> [--repo <repo>]` | Look up all definitions of a symbol by name. |
| `code-callers <symbol_id>` | Find what calls a given symbol. |
| `code-callees <symbol_id>` | Find what a symbol calls. |
| `code-imports <rel_path> [--repo <repo>]` | List import edges from a source file. |
| `code-stats` | Symbol/edge/file counts. |
| `link-code <neuron_id> <symbol_id> <repo> [--kind function]` | Attach a code symbol reference to an existing Neuron. |

**symbol_id format:** `<repo_relative_path>::<qualified_name>`
e.g. `src/indexer.py::KnewrallIndexer.rebuild_index`

When a codebase is in the workspace, capture dev-evolution concepts as Neurons:
- Key modules/subsystems → **What** Neurons (link via `link-code`)
- Significant changes/releases → **When** Neurons
- Rationale for a change → **Why** reified edges
- System workflows → **How** reified edges

### Semantic search / embeddings

| Command | Purpose |
|---|---|
| `embed [--neurons] [--code] [--repo <repo>]` | Generate / refresh vector embeddings for neurons and/or code symbols. Reads the API key straight from the environment — set a dedicated `OPENROUTER_API_KEY_EMBEDS` (checked first, kept separate from any shared key other tools use for that provider) or the generic `KNEWRALL_EMBED_API_KEY` in the workspace `.env`; the engine loads it itself, independent of the agent harness. |
| `embed --reconcile <conflict_db_path>` | Recover embeddings that exist only in a Syncthing sync-conflict copy of `vectors.db` and are missing locally. Never overwrites a local embedding. |
| `search-graph "<query>" --hybrid` / `recall "<term>"` (hybrid on by default) | Blend literal + semantic (vector KNN) results via Reciprocal Rank Fusion. |

Embeddings are cached by content hash — re-run `embed` only when content changes.
`rebuild-index`/`refresh-index` do **not** wipe embeddings; they survive index refreshes.
`vectors.db` is synced across machines on purpose (embeddings are paid API calls); if
Syncthing ever reports a conflict on it, run `embed --reconcile` on the conflict file.

### Short-term memory / context folding

The **Engram Layer** — fold verbose output out of your live context, retrieve it
by key only if you actually need it. Knewrall is a subprocess, not a proxy: it
cannot delete tokens already in your context window, but it can stop content
from entering it in the first place, which is strictly better anyway.

| Command | Purpose |
|---|---|
| `fold-run [--label "<why>"] [--kind <k>] [--keep-head N] [--keep-tail N] [--quiet] -- <command…>` | **The primary context saver.** Runs `<command>`, stores its full combined stdout+stderr as an engram, prints head + tail + a type-aware digest + a retrieval marker. The raw output never enters your context. Exit code is passed through unchanged. |
| `fold [--label "<why>"] [--kind <k>] [--file <path>] [--quiet]` | Fold content you already have (stdin) or a file you're about to read. Returns a marker with the retrieval key. No-ops (passthrough, no engram written) below the size floor (~2KB/40 lines) — folding something small is a net loss. Refuses (passthrough + warning) on instruction files (`INSTRUCTIONS.md`, `CLAUDE.md`, `AGENTS.md`, schemas, ...) and on Knewrall's own output. |
| `unfold <key> [--grep <pat> [--context N]] [--lines A-B] [--head N] [--tail N] [--max-chars N] [--meta]` | Read folded content back. **Prefer `--grep`/`--lines`** — a bare `unfold` is capped at 40,000 chars. Also exposed read-only over MCP as `knewrall_unfold` for harnesses like OpenCode that have a tool-injection position but no Claude-Code-style hooks. An unresolved key means the engram expired/was discarded, or never existed on this machine — engrams are per-machine and never synced; `consolidate` first if content needs to cross machines. |
| `folds [--session <id>] [--kind <k>] [--grep <t>] [--limit 20] [--all]` | List this session's engrams (metadata only, TOON) with a token-savings total. |
| `fold-scan "<terms>"…` | Budgeted relevance check: which folded content might matter to these terms. Emits ≤ 4 markers, ≤ 2,000 chars. Runs automatically once per turn via a `UserPromptSubmit` hook; callable by hand too. |
| `consolidate <key> (--json '<payload>'\|<file>\|- \| --suggest \| --archive-only) [--archive] [--link <id> <predicate>]` | Promote an engram into the durable graph — wraps `propose-node`/`propose-link` (no parallel node-creation path). `--archive` also copies the raw blob into `archive/`. `--suggest` drafts a payload without writing. `--json` accepts inline, a file path, or stdin, matching `propose-node`. **Never put a bare engram key in a `remote add --task` string** — `consolidate` first if content needs to cross machines. |
| `fold-gc [--session <id>] [--all] [--older-than <dur>] [--keep-consolidated] [--purge-consolidated] [--dry-run]` | Discard engrams. TTL sweeps also run opportunistically on every `fold`/`unfold`/`folds`. Consolidated/archived engrams are preserved indefinitely unless `--purge-consolidated`. |
| `fold-stats` | Per-kind fold/unfold counts, unfold rate, adaptive digest settings, disk usage. |

Engrams live in `knewrall/engrams/`, are never indexed (invisible to
`refresh-index`/`rebuild-index` by construction), never synced across machines, and
TTL out automatically (default 72h). Nothing here is durable — promote anything
worth keeping into a Neuron. **Never put a bare engram key in a prompt or task
string handed off to a different machine or process** — the receiving side can't
resolve it; `consolidate` the content into a Neuron first if it needs to cross
machines.

---

## 3. Workspace layout

Read `knewrall/.knewrall/config.json` for the authoritative folder map. Key rules:

- **`knewrall/workdesk/`** — raw, unstructured input the user drops for processing.
  **Start here** when asked to "process" or "ingest" something.
- **`knewrall/neurons/ notes/ media/ archive/`** — owned by Knewrall; written **only**
  through the CLI. `neurons/` is sharded by UUID prefix; the others by date.
  `archive/` and `media/` contents are immutable — you may add files, never edit them.
- **`knewrall/engrams/`** — ephemeral short-term memory (see §2's folding table).
  Sharded by session start date, never indexed, never synced, TTL'd. Written only
  through `fold`/`fold-run`; discardable at any time with no loss of durable
  knowledge.
- The **surrounding workspace** (everything outside `knewrall/`) is the user's own
  project. You may read it as a source of knowledge, but Knewrall automation must
  never modify it as a side effect.

---

## 4. Node schema (6 pillars)

Every Neuron follows `knewrall/schemas/master.schema.json`:

- **`header`** — `type` (Who/What/Where/When/Why/How), `canonical_name`, `aliases`,
  optional `full_legal_name`, `disambiguation`.
- **`descriptions`** — optional `physical` / `psychological` / `conceptual`.
- **`properties`** — key/value attributes (may carry `when`/`where`/`certainty`).
- **`links`** — typed relationships (usually added via `propose-link`).
- **`tags`** — global categorization tags.

The pillars: **Who** (people, orgs, agents), **What** (objects, concepts, projects),
**Where** (locations, platforms), **When** (times, eras), **Why** (motivations,
causes), **How** (methods, processes).

### 4.1 Provenance & reified edges (assertions)

Links and property values can carry an optional **`assertion`** block that records
*where a claim came from and when it entered the KB* — distinct from `certainty`
(epistemic stance) and from validity-time. All fields are optional and additive;
existing neurons validate unchanged.

- **`assertion.sources`** — array of **prefixed provenance strings**, whitelisted
  prefixes `neuron:` `note:` `code:` `url:` `conv:` `text:` (e.g.
  `note:notes/2026/08/x.md`, `code:<repo>::<qualified_name>`, `text:<free text>`).
  Never dereferenced at write time; unresolvable `neuron:`/`code:` refs **warn, not
  block**. The CLI sorts `sources` deterministically on every write.
- **`assertion.recorded_at`** — transaction time, **enforced RFC 3339 date-time**
  (e.g. `2026-08-08T10:00:00Z`); a bare/date-only string is rejected.
- **`assertion.recorded_by`** — the **user's** identity that recorded the claim
  (not an agent's). Optional today; reserved for multi-user support.
- **`assertion.note`** — free-form annotation on this specific claim.

Links additionally accept:
- **`valid_from` / `valid_until`** — inline validity window, enforced RFC 3339
  date-time (the lightweight alternative to a When-node link).
- **`via_node_id`** — UUID back-pointer to the Why/How neuron that reifies this edge
  (warn-not-block if dangling).

**Reserved link predicates** — `supersedes` · `contradicts` · `corroborates` — model
contradiction/supersession between claims. Rule (validator-enforced on both
`propose-link` **and** inline links in `propose-node`): `direction` must be
`outbound` and `certainty` must be present. `corroborates` adds weight/credibility
to recorded knowledge. `recall` surfaces a node's own (outbound) reserved links under
a `supersession` key, and the **reverse** view — claims that another neuron
supersedes/contradicts/corroborates — under `reverse_claims` (each with a `relation`
of `superseded_by` / `contradicted_by` / `corroborated_by`), so a superseded claim
shows what replaced it.

CLI: `propose-link` takes `--source-ref` (repeatable), `--recorded-at`,
`--recorded-by`, `--note`, `--valid-from`, `--valid-until`, `--via-node-id`.
Date fields (`recorded_at`, `valid_from`, `valid_until`) are enforced as real RFC
3339 date-times on **every** write path (not just the schema). `recall` elides
`assertion` blocks by default; pass `--include-assertions` to include them.

---

## 5. Extraction workflow

When processing input (e.g. a note in `workdesk/` or `notes/`):

1. **Analyze** — identify the entities, events, and concepts.
2. **Search** — `search-graph` each one to find its UUID if it already exists.
3. **Propose nodes** — `propose-node` for entities not yet in the graph.
4. **Link** — `propose-link` to connect the entities as the source describes.
5. **Backlink** — `update-note-links` to append clickable links from the source note
   to the Neurons it mentions.

Goal: a dense, accurate graph where every Neuron is a connected piece of a larger
whole.
