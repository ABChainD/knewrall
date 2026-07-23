#!/usr/bin/env bash
# Single-command launcher for the Knewrall 3D Graph Viewer: sets up the
# backend venv and frontend build on first run (idempotent — skips anything
# already present), starts the server, and opens it in the default browser.
#
# Usage:
#   ./launch-viewer.sh              start (or reuse an already-running server)
#   ./launch-viewer.sh --rebuild    also force a fresh `npm run build`
#                                   (use after editing frontend/src/**)
#   ./launch-viewer.sh --stop       stop a server started by this script,
#                                   from any terminal (reads the pidfile)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIEWER_DIR="$ROOT/viewer"
PORT="${KNEWRALL_VIEWER_PORT:-8798}"
URL="http://127.0.0.1:${PORT}"
PID_FILE="$VIEWER_DIR/.viewer.pid"

# Under Git Bash/MSYS2 on Windows, bash's own $! for a directly-exec'd native
# python.exe does NOT reliably match the real OS PID (confirmed by testing —
# they can differ), which also means POSIX `kill` sent via that PID silently
# does nothing to the real process. scripts/run.py writes its own
# os.getpid() to $PID_FILE at startup specifically so cleanup/--stop always
# have a PID that's actually correct to hand to taskkill. Plain POSIX kill is
# correct and sufficient on real Linux/macOS.
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
  *) IS_WINDOWS=0 ;;
esac

kill_pid() {
  local pid="$1"
  if [ "$IS_WINDOWS" = "1" ]; then
    taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
  else
    kill -TERM "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || return 0
      sleep 0.2
    done
    kill -KILL "$pid" 2>/dev/null || true
  fi
}

if [ "${1:-}" = "--stop" ]; then
  if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$PID" ]; then
      echo "[knewrall-viewer] Stopping server (pid $PID)..."
      kill_pid "$PID"
    fi
    rm -f "$PID_FILE"
    echo "[knewrall-viewer] Stopped."
  else
    echo "[knewrall-viewer] Not running (no $PID_FILE)."
  fi
  exit 0
fi

REBUILD=0
[ "${1:-}" = "--rebuild" ] && REBUILD=1

cd "$VIEWER_DIR"

# Retries (not a single shot): a lone curl attempt can hit a transient
# hiccup and falsely report "not running," which would let a second
# scripts/run.py start, briefly clobber $PID_FILE with its own pid while it
# fails to bind the port, then delete it on exit -- orphaning the first,
# genuinely-running server's pidfile (this happened during testing).
server_already_up() {
  for _ in 1 2 3; do
    curl -s -o /dev/null --max-time 1 "$URL/api/health" 2>/dev/null && return 0
    sleep 0.3
  done
  return 1
}

if server_already_up; then
  echo "[knewrall-viewer] Already running at $URL — opening browser."
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 &
  elif command -v start >/dev/null 2>&1; then start "$URL" >/dev/null 2>&1 &
  else echo "[knewrall-viewer] Open $URL in your browser."; fi
  exit 0
fi

# ── Python venv (isolated from any shared/global Python env — see README) ──
if [ ! -f ".venv/bin/python" ] && [ ! -f ".venv/Scripts/python.exe" ]; then
  echo "[knewrall-viewer] Creating backend venv..."
  python3 -m venv .venv 2>/dev/null || python -m venv .venv
fi
if [ -f ".venv/bin/python" ]; then
  VENV_PY=".venv/bin/python"
else
  VENV_PY=".venv/Scripts/python.exe"
fi
if ! "$VENV_PY" -c "import fastapi, uvicorn" 2>/dev/null; then
  echo "[knewrall-viewer] Installing backend dependencies..."
  "$VENV_PY" -m pip install --quiet -r requirements.txt -r ../requirements.txt
fi

# ── Frontend build (skips if already built; --rebuild forces it) ──
if [ ! -d "frontend/node_modules" ]; then
  echo "[knewrall-viewer] Installing frontend dependencies (npm install)..."
  (cd frontend && npm install)
fi
if [ "$REBUILD" = "1" ] || [ ! -f "backend/static/index.html" ]; then
  echo "[knewrall-viewer] Building frontend..."
  (cd frontend && npm run build)
fi

# ── Start the server, then open the browser once it's actually up ──
rm -f "$PID_FILE"
echo "[knewrall-viewer] Starting server on $URL ..."
"$VENV_PY" scripts/run.py &
SERVER_PID=$!

cleanup() {
  local real_pid="$SERVER_PID"
  [ -f "$PID_FILE" ] && real_pid="$(cat "$PID_FILE" 2>/dev/null || echo "$SERVER_PID")"
  kill_pid "$real_pid"
  rm -f "$PID_FILE"
}
trap 'cleanup; exit 0' INT TERM
trap cleanup EXIT

for _ in $(seq 1 30); do
  if curl -s -o /dev/null --max-time 1 "$URL/api/health" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 &
elif command -v open >/dev/null 2>&1; then
  open "$URL" >/dev/null 2>&1 &
elif command -v start >/dev/null 2>&1; then
  start "$URL" >/dev/null 2>&1 &
else
  echo "[knewrall-viewer] Open $URL in your browser."
fi

echo "[knewrall-viewer] Running at $URL — press Ctrl+C to stop (or run: ./launch-viewer.sh --stop)."
wait "$SERVER_PID"
