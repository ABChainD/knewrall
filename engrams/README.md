# engrams/

This is the **Engram Layer** — Knewrall's short-term memory / context-folding
subsystem. Everything else in this folder is disposable.

- `<YYYY>/<MM-DD>/<session_short>/` — one directory per agent session,
  sharded by the session's **start date** (never today's date — a
  midnight-spanning session doesn't split).
- Inside each session directory: one `session.json` manifest plus one
  write-once `<key>.engram` file per folded record (a single-line JSON
  header followed by the raw payload, byte-for-byte).

Nothing here is indexed, checked into git (besides this file — see
`../.gitignore`), or synced across machines (see the workspace `.stignore`).
Content here TTLs out automatically (default 72h) and can be discarded at any
time with no loss of durable knowledge — anything worth keeping should be
promoted to a Neuron via `knewrall consolidate <key>`.

See `INSTRUCTIONS.md` and
`_projects/knewrall-dev/plans/short-term-memory-layer-plan.md` for the full
design.
