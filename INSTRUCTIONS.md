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

These two habits run automatically for every task, in the background, without
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

---

## 3. Workspace layout

Read `knewrall/.knewrall/config.json` for the authoritative folder map. Key rules:

- **`knewrall/workdesk/`** — raw, unstructured input the user drops for processing.
  **Start here** when asked to "process" or "ingest" something.
- **`knewrall/neurons/ notes/ media/ archive/`** — owned by Knewrall; written **only**
  through the CLI. `neurons/` is sharded by UUID prefix; the others by date.
  `archive/` and `media/` contents are immutable — you may add files, never edit them.
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
