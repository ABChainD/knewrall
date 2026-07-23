---
name: knewrall
description: Search, create, and link Neurons in the Knewrall personal knowledge graph. Use whenever grounding an answer in remembered facts about people/projects/places/decisions, or when capturing durable new knowledge the user shares.
---

# Knewrall knowledge graph

This workspace has a Knewrall knowledge base at `knewrall/`. Operate on it **only**
through the path-independent launcher (never edit neuron/note files directly):

```
python knewrall/bin/knewrall.py <command>
```

## Common operations

- **Ground / recall:** `recall "<term>" ["<term>" ...]` — the default way to ground.
  One call finds, loads, and shapes matching Neurons' full content (no follow-up file
  read needed), plus a capped summary of their immediate links. Call again on a
  related name to expand it. TOON-encoded by default; `--format json` if needed.
- **Dedupe check:** `search-graph "<term>"` — pointers only (id/type/name), not
  content. Use before creating a node, or when you just need to confirm something
  exists. Add `--hybrid` for semantic vector search.
- **Create a Neuron:** `propose-node --json '<payload>'` (inline JSON). Also accepts a
  file path or `-`/stdin. The system generates the UUID; do not supply one.
- **Link two Neurons:** `propose-link <source_id> <target_id> <predicate>`.
- **Correct a stale fact:** `update-node <id> --json '<updates>'`. Replaces the given
  `properties` keys by default; add `--append` to keep prior values as history instead.
  Never touches `links` or `system`.
- **Backlink a note:** `update-note-links <note_path> <name> [<name> ...]`.
- **Inspect:** `search "<query>"`, `stats`.
- **Refresh cache (routine):** `refresh-index` — incremental, content-hash based, cheap
  enough to run every session. **Hard reset:** `rebuild-index --full`.

## Code repository operations

When the workspace contains code repos (under `knewrall/projects/`):

- **Index a repo:** `index-code [path] [--full]` — parse and build the code graph.
- **Search symbols:** `code-search "<query>"` — FTS over functions/classes/methods.
- **Look up a symbol:** `code-defs <name> [--repo <repo>]`.
- **Call graph:** `code-callers <symbol_id>` / `code-callees <symbol_id>`.
- **Imports:** `code-imports <rel_path>`.
- **Stats:** `code-stats`.
- **Link to neuron:** `link-code <neuron_id> <symbol_id> <repo> [--kind function]`.

`symbol_id` format: `<repo_relative_path>::<qualified_name>`

Capture dev-evolution concepts as Neurons: key modules → **What**, changes →
**When**, rationale → **Why**, workflows → **How**.

## Embeddings

- **Generate / refresh:** `embed [--neurons] [--code] [--repo <repo>]` — key comes
  straight from the environment (a dedicated `OPENROUTER_API_KEY_EMBEDS`, or generic
  `KNEWRALL_EMBED_API_KEY`, in the workspace `.env`); the engine talks to the provider
  itself, no harness-specific setup needed.
- Content-hash cached — only re-embeds changed text.
- Vectors survive `rebuild-index` / `refresh-index`.
- **Recover from a sync conflict:** `embed --reconcile <conflict_db_path>`.

## Rules

Neurons use the 6 pillars — **Who / What / Where / When / Why / How** — and follow
`knewrall/schemas/master.schema.json`. Put raw input to process in `knewrall/workdesk/`.
A "potential collision" warning means a similar Neuron may exist: investigate and set
the `disambiguation` field rather than creating a near-duplicate.
`projects/` is read-only — Knewrall automation never modifies it.

For the full operating contract, read `knewrall/INSTRUCTIONS.md`.
