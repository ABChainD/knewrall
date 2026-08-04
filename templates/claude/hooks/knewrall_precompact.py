#!/usr/bin/env python3
"""
Knewrall PreCompact hook — transcript ingestion (Engram Layer Phase 3b).

Fires on every auto/manual compaction, before Claude Code discards
transcript content. Ingests the transcript's tool results into engrams so
compaction becomes REVERSIBLE (the buy here is reversibility, not savings —
every one of these tokens has already been paid for) instead of lossy.

Discipline: best-effort, hard timeout, bare `except: pass` at the top level —
same as knewrall_session_start.py's _refresh_index_best_effort() and
knewrall_fold_turn.py. A hook failure must never block compaction. An
unrecognized transcript record shape (e.g. after a Claude Code version bump
changes the .jsonl format) degrades to NO ingestion, silently — visible as a
zero counter in `fold-stats`, never a broken hook.

Emits no stdout on purpose — PreCompact has nothing analogous to
additionalContext worth injecting here; this is pure background housekeeping.
"""

import json
import os
import sys
from pathlib import Path


def run() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        payload = {}

    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        return

    # This hook is installed at <workspace>/.claude/hooks/knewrall_precompact.py
    # by install.py, always alongside a <workspace>/knewrall/ subfolder.
    workspace_root = Path(__file__).resolve().parents[2]
    knewrall_dir = workspace_root / "knewrall"
    if not (knewrall_dir / "src").is_dir():
        return

    if str(knewrall_dir) not in sys.path:
        sys.path.insert(0, str(knewrall_dir))
    os.environ.setdefault("KNEWRALL_ROOT", str(knewrall_dir))

    from src.knewrall_middleware import precompact_ingest
    precompact_ingest(transcript_path, session_id=payload.get("session_id"), root=knewrall_dir)


def main() -> None:
    try:
        run()
    except Exception:
        pass  # a hook failure must never block compaction


if __name__ == "__main__":
    main()
