# `.knewrall/` — System Directory

Holds Knewrall's own configuration and its ephemeral index.

- `config.json` — the knowledge-base manifest (canonical folder layout, ownership,
  and sub-foldering rules). **Synced.** Safe to read by agents and humans.
- `index.db` — the ephemeral SQLite index: a *materialized view* of the file system.
  **Never synced** (git-ignored) and fully rebuildable at any time with
  `python -m src.cli rebuild-index`. If it is deleted or corrupted, just rebuild it.

The files in `neurons/` and `notes/` are the source of truth; this index is a
disposable cache built from them.
