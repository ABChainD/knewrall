#!/usr/bin/env python3
"""
Knewrall PreToolUse enforcer — opt-in Engram Layer Phase 5, default OFF.

Fires before every `Bash` tool call. If the command matches one of
`engrams.enforce_patterns` (config.json — default: pytest, npm test, npm run
build, cargo test/build, go test/build, docker logs, tsc, webpack, make —
unambiguous command PREFIXES only, never intent-parsed flags like "grep -r
without -l") and isn't already wrapped in `fold-run`, denies the call with a
reason telling the agent the exact rewrite. This is the ONLY piece of the
Engram Layer that can actively obstruct the agent, which is why it ships
last and stays off by default (`engrams.enforce: false` in config.json, AND
this hook is only installed at all with `install.py --with-fold-enforcement`
— two independent gates).

Escape hatch (Kimi K3, Q7): deny-once-per-normalized-command-per-session. A
denied command's normalized form is recorded in the session's own directory;
the SAME command retried a second time is allowed through — a Claude model
that doesn't parse the deny reason as "rewrite via fold-run" and just retries
identically burns at most one wasted turn, not a loop. No separate
`--no-fold` flag needed.

Discipline: best-effort, bare `except: pass` at the top level, matching every
other Engram Layer hook — a hook failure must never block a tool call (fails
OPEN, not closed: an exception here means "allow", not "deny").
"""

import json
import re
import sys
from pathlib import Path


def _normalize_command(cmd: str) -> str:
    return re.sub(r"\s+", " ", cmd.strip())


def _is_already_wrapped(cmd: str) -> bool:
    low = cmd.lower()
    return "fold-run" in low or "knewrall.py" in low or re.match(r"^\s*knewrall(\s|$)", low) is not None


def run() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return  # fail open

    if payload.get("tool_name") != "Bash":
        return

    command = (payload.get("tool_input") or {}).get("command", "")
    if not command or _is_already_wrapped(command):
        return

    workspace_root = Path(__file__).resolve().parents[2]
    knewrall_dir = workspace_root / "knewrall"
    if not (knewrall_dir / "src").is_dir():
        return

    config_path = knewrall_dir / ".knewrall" / "config.json"
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8")).get("engrams", {})
    except (OSError, json.JSONDecodeError):
        cfg = {}

    if not cfg.get("enforce", False):
        return  # off by default — the primary gate

    enforce_patterns = set(cfg.get("enforce_patterns", []))
    if not enforce_patterns:
        return

    if str(knewrall_dir) not in sys.path:
        sys.path.insert(0, str(knewrall_dir))
    from src.knewrall_engrams import normalize_cmd_pattern, resolve_session, session_dir, get_root as _get_root

    pattern = normalize_cmd_pattern(command)
    if pattern not in enforce_patterns:
        return

    session = resolve_session(root=knewrall_dir, session_id=payload.get("session_id"), touch=False)
    sdir = session_dir(session, knewrall_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    denied_path = sdir / ".fold_enforce_denied.json"
    try:
        denied = set(json.loads(denied_path.read_text(encoding="utf-8"))) if denied_path.is_file() else set()
    except (OSError, json.JSONDecodeError):
        denied = set()

    normalized = _normalize_command(command)
    if normalized in denied:
        return  # already denied once this session — allow through (escape hatch)

    denied.add(normalized)
    try:
        denied_path.write_text(json.dumps(sorted(denied)), encoding="utf-8")
    except OSError:
        pass

    reason = (
        f"This command matches a pattern ({pattern}) whose output is verbose by default. "
        "Re-run it through fold-run instead, so the raw output never enters context:\n"
        f"  python knewrall/bin/knewrall.py fold-run --label \"<why>\" -- {command}\n"
        "(Retrying the identical command a second time will be allowed through — this is a one-time nudge.)"
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> None:
    try:
        run()
    except Exception:
        pass  # fail open — a hook failure must never block a tool call


if __name__ == "__main__":
    main()
