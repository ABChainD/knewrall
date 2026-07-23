#!/usr/bin/env python3
"""
Knewrall launcher — the single, path-independent entry point for agents.

Agents (and humans) should invoke Knewrall through this script rather than
``python -m src.cli``, because it works from any current working directory:

    python <workspace>/knewrall/bin/knewrall.py search "query"
    python <workspace>/knewrall/bin/knewrall.py stats

It bootstraps the import path and the ``KNEWRALL_ROOT`` environment variable
from its own location, so the engine always reads/writes the correct knowledge
base no matter where the command is run from.
"""

import os
import sys
from pathlib import Path

# This file lives at <knewrall_root>/bin/knewrall.py, so the root is two up.
ROOT = Path(__file__).resolve().parent.parent

# Make `src` importable and pin the knowledge-base root for the engine.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("KNEWRALL_ROOT", str(ROOT))

from src.cli import main  # noqa: E402  (import after sys.path setup)

if __name__ == "__main__":
    main()
