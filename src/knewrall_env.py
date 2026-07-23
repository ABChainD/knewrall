"""
Harness-independent .env loader.

Embedding provider keys (OPENROUTER_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY,
KNEWRALL_EMBED_API_KEY) must reach the engine regardless of which agent harness
invoked it (Claude Code, opencode, pi, clawds, ...) and without any per-harness
config. This loads a plain KEY=VALUE .env file straight into os.environ so the
engine can talk to the provider on its own.

Looked up next to the Knewrall root (standalone layout) and one level above it
(guest install as a `knewrall/` subfolder of a larger workspace) — first file
found wins. Existing environment variables are never overridden (setdefault),
and values are never logged.
"""

from __future__ import annotations

import os
from pathlib import Path

from .paths import get_root

_loaded = False


def _parse_dotenv(path: Path) -> dict:
    values = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):  # common shell-sourceable .env convention
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # Unquoted value: strip a trailing " # comment" (space required
            # before the hash, so a value that legitimately contains '#'
            # with no preceding space is left alone). Quoted values keep any
            # '#' literally — it's inside the string, not a comment.
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos].rstrip()
        if key:
            values[key] = value
    return values


def load_dotenv_once() -> None:
    """Load the workspace .env into os.environ (setdefault). Safe to call repeatedly."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    root = get_root()
    for candidate in (root / ".env", root.parent / ".env"):
        if candidate.is_file():
            for key, value in _parse_dotenv(candidate).items():
                os.environ.setdefault(key, value)
            break
