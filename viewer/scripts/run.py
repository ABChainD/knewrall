#!/usr/bin/env python3
"""One-shot local "production" launcher: serves the built frontend (from
backend/static/, produced by `npm run build`) plus the API, as a single
process. Zero external services are contacted at runtime except the
OpenRouter query-embedding call during a hybrid search — and only if an API
key is configured (see backend/engine.py / README.md).

Run from anywhere:
    python knewrall/viewer/scripts/run.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

VIEWER_DIR = Path(__file__).resolve().parent.parent
if str(VIEWER_DIR) not in sys.path:
    sys.path.insert(0, str(VIEWER_DIR))

from backend.app import app, STATIC_DIR  # noqa: E402


PID_FILE = VIEWER_DIR / ".viewer.pid"


def main() -> None:
    import uvicorn

    if not STATIC_DIR.is_dir():
        print(
            f"warning: {STATIC_DIR} does not exist yet — run "
            "`npm run build` in knewrall/viewer/frontend first.",
            file=sys.stderr,
        )

    # Under Git Bash/MSYS2 on Windows, bash's own $! for a directly-exec'd
    # native python.exe does not reliably match the real OS PID (confirmed by
    # testing — they can differ). launch-viewer.sh reads this file instead of
    # trusting $!, so it can actually find and stop this exact process.
    own_pid = str(os.getpid())
    PID_FILE.write_text(own_pid, encoding="utf-8")
    try:
        port = int(os.environ.get("KNEWRALL_VIEWER_PORT", "8798"))
        print(f"Knewrall viewer: http://127.0.0.1:{port}")
        uvicorn.run(app, host="127.0.0.1", port=port)
    finally:
        # Only remove the pidfile if it still names THIS process. If a
        # second instance started later (shouldn't normally happen —
        # launch-viewer.sh checks first — but did happen once during
        # testing after a transient port-bind race), it would have
        # overwritten the file with its own pid; blindly unlinking here
        # would then orphan that still-running first instance's tracking.
        try:
            if PID_FILE.read_text(encoding="utf-8").strip() == own_pid:
                PID_FILE.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
