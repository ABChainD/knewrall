"""
Knewrall root resolution.

Every other module derives its data/index/schema directories from a single
"Knewrall root" so the engine works regardless of the current working directory
and regardless of whether Knewrall lives at the workspace root (standalone) or
inside a ``knewrall/`` subfolder (guest install).

Resolution order (first match wins):

1. ``KNEWRALL_ROOT`` environment variable — explicit override. The installer and
   the CLI set this to target a specific workspace.
2. Walk up from the current working directory looking for the manifest marker
   (``.knewrall/config.json``). This lets an agent run the CLI from anywhere
   inside the workspace.
3. The package's own parent directory (the folder that contains ``src/``). This
   is the Knewrall root in both the standalone and subfolder layouts.
"""

import os
from pathlib import Path

# The manifest that marks a directory as a Knewrall root.
MARKER = Path(".knewrall") / "config.json"


def find_root(start: Path | None = None) -> Path:
    """Resolve the Knewrall root directory. See module docstring for the order."""
    env = os.environ.get("KNEWRALL_ROOT")
    if env:
        return Path(env).expanduser().resolve()

    start = (start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / MARKER).is_file():
            return candidate

    # Fallback: the directory that contains this package's parent (i.e. the
    # folder holding ``src/``), which is the Knewrall root in every layout.
    return Path(__file__).resolve().parent.parent


def get_root() -> Path:
    """Return the resolved Knewrall root directory."""
    return find_root()
