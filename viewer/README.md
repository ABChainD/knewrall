# Knewrall Viewer

A local, self-contained 3D exploration tool for the Knewrall knowledge graph.
Fly through nodes colored by pillar (Who/What/Where/When/Why/How), hover for a
preview, click to expand the full neuron, follow any linked entity, and search
— literal or semantic — without leaving the graph.

This tool is a sibling of the Knewrall engine, not part of it: it lives outside
`neurons/notes/media/archive` (the folders `.knewrall/config.json` marks as
Knewrall-owned) and only ever reads from them. Nothing here writes to a neuron,
a note, `index.db`, or `vectors.db`.

## Architecture

- **Backend** (`backend/`, Python + FastAPI): the only process that touches
  data. Imports the existing Knewrall engine (`search_graph`, `load_node`,
  `KnewrallIndexer`) directly rather than reimplementing search/embedding/KNN.
  Opens its own bulk-read connections to `index.db`/`vectors.db` with SQLite's
  `mode=ro` — never writes.
- **Frontend** (`frontend/`, TypeScript + Vite + three.js via `3d-force-graph`):
  built to static assets; served by the same backend process in normal use.
  Node.js/Vite are a **build-time** dependency only — nothing Node-related runs
  at viewer runtime.

See the file headers in `backend/engine.py` and `backend/app.py` for the
read-only guarantees in more detail.

## Quick start

From the `knewrall/` folder root, run the launcher for your platform. It
handles first-time setup (venv, `pip install`, `npm install`, `npm run
build`) automatically and idempotently — safe to run every time, it skips
anything already present — then starts the server and opens it in your
browser. If a server is already running, it just opens the browser instead
of starting a duplicate.

```bash
# Windows
knewrall\launch-viewer.bat

# Linux / macOS / Git Bash
./knewrall/launch-viewer.sh
```

Other options: `--rebuild` (force a fresh frontend build — use after editing
`frontend/src/**`), `--stop` (stop a server started by this script, from any
terminal). Also available as the `/knewrall-viewer` agent skill.

The sections below are for developing the viewer itself (manual setup,
live-reload dev loop); skip them if you just want to use the tool.

## Setup (one-time, needs network for the package installs)

The backend gets its **own virtual environment** (`knewrall/viewer/.venv`),
kept separate from whatever Python environment the rest of Knewrall/Teamwork
use. Installing FastAPI/uvicorn into a shared global environment can drag in
an incompatible `starlette`/`uvicorn` pin and break other tools that depend on
a newer one (e.g. anything using `sse-starlette`, like the MCP server) — the
venv avoids that entirely.

```bash
cd knewrall/viewer
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt -r ../requirements.txt

cd frontend
npm install
```

## Dev loop (live-reload, two processes)

```bash
# Terminal 1 — API on 127.0.0.1:8798
cd knewrall/viewer
./.venv/Scripts/python.exe -m backend.app

# Terminal 2 — Vite dev server on :5173, proxies /api -> :8798
cd knewrall/viewer/frontend
npm run dev
```

Open http://localhost:5173.

## Local "production" (single process, fully offline)

```bash
cd knewrall/viewer/frontend && npm run build
cd .. && ./.venv/Scripts/python.exe scripts/run.py
```

Open http://127.0.0.1:8798.

## Offline guarantee

After the one-time `pip`/`npm install`, nothing here contacts an external
service **except** embedding the query string during a *semantic* search
(OpenRouter, only when `OPENROUTER_API_KEY_EMBEDS` / `KNEWRALL_EMBED_API_KEY`
is set and the semantic toggle is on). That call has a 4s timeout and falls
back to literal search automatically. Browsing the graph, hovering, expanding
a node, following any link, and literal search are 100% offline. Fonts and all
JS/CSS are bundled locally — no CDNs.

## Configuration

- `KNEWRALL_VIEWER_PORT` — port to bind (default `8798`). Always binds
  `127.0.0.1` only, never `0.0.0.0`.
- `KNEWRALL_ROOT` — same variable the rest of Knewrall uses; auto-detected
  from this file's location if unset.
