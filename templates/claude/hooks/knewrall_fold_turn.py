#!/usr/bin/env python3
"""
Knewrall UserPromptSubmit hook — per-turn fold-scan (Engram Layer Phase 3a).

Fires once per user turn. Registered in the SAME `UserPromptSubmit` hook
group as `teamwork_route.py` (install.py appends to the group rather than
replacing it — see install_claude_hardening()); this hook runs AFTER
teamwork's router, deliberately, not incidentally (documented here so a
future change doesn't reorder it without noticing).

Reads the prompt from the hook payload on stdin, extracts a handful of
identifier-shaped terms, runs `fold-scan` against the current session's
folded engrams, and — only if something ranked — injects up to ~2,000 chars
of "possibly relevant folded context" markers as additionalContext. Surfaces
POINTERS only, never engram content (unfold is always the model's own
decision).

Discipline: best-effort, hard subprocess timeout, bare `except: pass` at the
top level — exactly like knewrall_session_start.py's
_refresh_index_best_effort(). A hook failure must never block a turn.

Honest latency note (Kimi K3 critique I10): fold-scan's own internal
_FOLD_SCAN_DEADLINE (1.5s) is measured AFTER this hook's own Python
interpreter has already started and after knewrall_middleware's imports have
already run — cold interpreter start is ~0.4-1.0s on its own, and this hook
fires after teamwork_route.py already spent its own time in the same group.
Observed wall-clock per prompt is therefore closer to ~2.5s worst case, not
a tight 1.5s — that is the number to plan around, not the deadline constant
alone.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

_TERM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOPWORDS = {
    "the", "and", "for", "you", "with", "this", "that", "have", "from", "are",
    "can", "please", "could", "would", "should", "not", "but", "what", "how",
}


def _tokenize(prompt: str, cap: int = 12) -> list[str]:
    terms = []
    for tok in _TERM_RE.findall(prompt):
        low = tok.lower()
        if len(low) < 3 or low in _STOPWORDS:
            continue
        if low not in terms:
            terms.append(low)
        if len(terms) >= cap:
            break
    return terms


def run() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        payload = {}

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return

    # This hook is installed at <workspace>/.claude/hooks/knewrall_fold_turn.py
    # by install.py, always alongside a <workspace>/knewrall/ subfolder.
    workspace_root = Path(__file__).resolve().parents[2]
    launcher = workspace_root / "knewrall" / "bin" / "knewrall.py"
    if not launcher.is_file():
        return

    terms = _tokenize(prompt)
    if not terms:
        return

    cmd = [sys.executable, str(launcher), "fold-scan"] + terms
    session_id = payload.get("session_id")
    if session_id:
        cmd += ["--session", session_id]

    result = subprocess.run(cmd, capture_output=True, timeout=5, text=True, check=False)
    text = (result.stdout or "").strip()
    if not text:
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }))


def main() -> None:
    try:
        run()
    except Exception:
        pass  # a hook failure must never block a turn


if __name__ == "__main__":
    main()
