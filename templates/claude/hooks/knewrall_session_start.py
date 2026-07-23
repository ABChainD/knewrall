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
"""

import json
import subprocess
import sys
from pathlib import Path


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


_refresh_index_best_effort()

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
