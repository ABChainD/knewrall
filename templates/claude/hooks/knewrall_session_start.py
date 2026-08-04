#!/usr/bin/env python3
"""
Knewrall SessionStart hook for Claude Code.

Injects the Knewrall always-on behavior into the session context so the agent
grounds from the knowledge base and saves durable facts without being asked.
Also opportunistically runs `refresh-index` — content-hash based, so on a
freshly Syncthing-synced workspace it picks up real changes cheaply, and is a
near-instant no-op otherwise. That subprocess call is best-effort with a hard
timeout: any failure here must never block session start or the context
injection below, which is this hook's actual job.

Also registers this Claude Code session's `session_id` (carried on stdin in
the hook payload) as the Engram Layer's session identity — the "real,
automatic path" from short-term-memory-layer-plan.md §7.1: once registered,
`fold`/`unfold`/`fold-scan` resolve the SAME session via
`.knewrall/session_current` without the agent ever having to pass --session.
Best-effort and non-blocking, same discipline as refresh-index above.
"""

import json
import subprocess
import sys
from pathlib import Path


def _read_hook_payload() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError, OSError):
        return {}


def _refresh_index_best_effort() -> None:
    # This hook is installed at <workspace>/.claude/hooks/knewrall_session_start.py
    # by install.py, always alongside a <workspace>/knewrall/ subfolder.
    workspace_root = Path(__file__).resolve().parents[2]
    launcher = workspace_root / "knewrall" / "bin" / "knewrall.py"
    if not launcher.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(launcher), "refresh-index"],
            capture_output=True, timeout=15, check=False,
        )
    except Exception:
        pass


def _register_engram_session_best_effort(payload: dict) -> None:
    session_id = payload.get("session_id")
    if not session_id:
        return
    workspace_root = Path(__file__).resolve().parents[2]
    knewrall_dir = workspace_root / "knewrall"
    if not (knewrall_dir / "src").is_dir():
        return
    try:
        if str(knewrall_dir) not in sys.path:
            sys.path.insert(0, str(knewrall_dir))
        # A lightweight, stdlib-only import (knewrall_engrams has no
        # third-party dependencies) — cheaper than importing the full
        # knewrall_middleware (jsonschema, etc.) just to register a session.
        from src.knewrall_engrams import resolve_session
        resolve_session(
            root=knewrall_dir, session_id=session_id, harness="claude-code",
            cwd=payload.get("cwd"),
        )
    except Exception:
        pass


_payload = _read_hook_payload()
_refresh_index_best_effort()
_register_engram_session_best_effort(_payload)

CONTEXT = (
    "Knewrall personal knowledge graph is active in this workspace (knewrall/). "
    "Two behaviors are ALWAYS in effect, no user request needed:\n"
    "1. GROUND FIRST: before acting, and whenever you encounter a name, person, "
    "project, place, decision, or preference you might already know, run "
    "`python knewrall/bin/knewrall.py recall \"<term>\"` and use what you find — "
    "it returns full Neuron content (not just pointers) in one call. Use "
    "`search-graph \"<term>\"` instead only to check for duplicates before creating.\n"
    "2. SAVE DURABLE FACTS: when the user reveals something worth remembering, "
    "capture it as a Neuron via the Knewrall CLI (check for duplicates first with "
    "search-graph).\n"
    "Full operating instructions: read knewrall/INSTRUCTIONS.md."
)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": CONTEXT,
    }
}))
