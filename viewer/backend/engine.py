"""
The only module in viewer/backend allowed to import the Knewrall engine or
open a database connection. Every route in app.py calls through here.

Read-only by construction for the two connections this module opens itself
(index.db, vectors.db) — both use the SQLite URI `mode=ro`, never `mode=rwc`
or a plain path. It also imports the *read* verbs from the existing engine
(search_graph, load_node, KnewrallIndexer's getters) — never propose_node,
update_node, propose_link, or refresh_index. No code path here can write to
neurons/notes/media/archive or either database.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Optional, TypeVar

# ── Bootstrap: mirror bin/knewrall.py / mcp_server.py so the engine resolves
# the correct KB root regardless of the process's cwd.
KNEWRALL_ROOT = Path(__file__).resolve().parent.parent.parent
if str(KNEWRALL_ROOT) not in sys.path:
    sys.path.insert(0, str(KNEWRALL_ROOT))
import os
os.environ.setdefault("KNEWRALL_ROOT", str(KNEWRALL_ROOT))

from src.knewrall_middleware import (  # noqa: E402
    search_graph as _search_graph,
    get_embed_adapter,
)
from src.knewrall_crud import load_node as _load_node, neuron_md_path  # noqa: E402

logger = logging.getLogger("knewrall.viewer")

INDEX_DB_PATH = KNEWRALL_ROOT / ".knewrall" / "index.db"
VECTORS_DB_PATH = KNEWRALL_ROOT / ".knewrall" / "vectors.db"
CONFIG_PATH = KNEWRALL_ROOT / ".knewrall" / "config.json"

T = TypeVar("T")

# Almost everything in this module opens its OWN sqlite3 connection per call
# (index_db_connection/vectors_db_connection below) — safe from any thread,
# since a connection that's opened, used, and closed within a single function
# call never crosses threads. The one exception is search_graph_raw's hybrid
# path: it calls into the shared Knewrall engine's get_vector_store()/
# get_embed_adapter() singletons (needed to reuse the real KNN + RRF logic
# rather than reimplementing it — see the plan this viewer was built from),
# and those singletons lazily bind a sqlite3 connection to whichever thread
# first calls them. Since FastAPI/Starlette runs each sync route handler on a
# worker thread from its own pool (not guaranteed to be the same thread
# across requests), calling a singleton-bound connection from a different
# thread than the one that created it raises "SQLite objects created in a
# thread can only be used in that same thread." Routing ONLY the hybrid-search
# call through this one dedicated thread keeps that invariant true, while
# scoping the bottleneck to actual semantic-search work (a slow/stuck
# embedding API call) instead of serializing every route behind it.
_ENGINE_THREAD = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="knewrall-engine"
)


def submit(fn, *args, **kwargs) -> concurrent.futures.Future:
    """Schedule fn on the dedicated engine thread; returns the Future so the
    caller can apply its own wait/timeout (see app.py's /api/search, which
    needs a shorter timeout than the call might take to actually finish)."""
    return _ENGINE_THREAD.submit(fn, *args, **kwargs)


class EngineUnavailable(Exception):
    """Raised when a read-only DB connection can't be opened (locked, missing, etc.)."""


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    """Open a strictly read-only connection to an existing SQLite file.

    Uses mode=ro (not immutable=1, which would risk serving a stale/corrupt
    snapshot under a concurrent writer) so this always sees a consistent,
    current view. A WAL-mode DB with no -shm file yet, or one that's
    momentarily locked, can raise SQLITE_READONLY_CANTINIT / "database is
    locked" here — surfaced as EngineUnavailable so callers degrade
    gracefully instead of 500ing.
    """
    if not db_path.is_file():
        raise EngineUnavailable(f"{db_path.name} does not exist yet")
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1")  # force the open, surface locking errors now
        return conn
    except sqlite3.OperationalError as e:
        raise EngineUnavailable(f"could not open {db_path.name} read-only: {e}") from e


def index_db_connection() -> sqlite3.Connection:
    return _ro_connect(INDEX_DB_PATH)


def vectors_db_connection() -> Optional[sqlite3.Connection]:
    try:
        return _ro_connect(VECTORS_DB_PATH)
    except EngineUnavailable as e:
        logger.info(f"vectors.db unavailable: {e}")
        return None


def embeddings_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("embeddings", {})
    except OSError:
        return {}


def search_graph_raw(query: str, hybrid: bool = False, limit: int = 20) -> list[dict]:
    """Touches the shared indexer/vector-store singletons — must be called
    via `run()`, never directly."""
    return _search_graph(query, hybrid=hybrid, limit=limit)


def literal_search(query: str, limit: int = 20) -> list[dict]:
    """Thread-safe on its own: opens a fresh read-only connection instead of
    touching the shared singleton. Used both for plain literal search and as
    the always-fast fallback when a hybrid search on the engine thread is
    still running past its timeout (see app.py's /api/search) — duplicates
    the shared engine's literal-match query rather than queuing behind
    whatever the engine thread is doing."""
    if not query:
        return []
    conn = index_db_connection()
    try:
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT DISTINCT n.id, n.type, n.canonical_name, n.aliases, n.tags, n.full_legal_name "
            "FROM nodes n LEFT JOIN aliases a ON n.id = a.node_id "
            "WHERE n.canonical_name LIKE ? OR a.alias LIKE ? OR n.tags LIKE ? "
            "ORDER BY n.canonical_name LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r["id"],
            "type": r["type"],
            "canonical_name": r["canonical_name"],
            "full_legal_name": r["full_legal_name"],
            "aliases": json.loads(r["aliases"]) if r["aliases"] else [],
            "tags": json.loads(r["tags"]) if r["tags"] else [],
        }
        for r in rows
    ]


def load_node(node_id: str) -> dict:
    return _load_node(node_id)


def companion_md_path(node_id: str) -> Path:
    return neuron_md_path(node_id)


def get_node_by_id(node_id: str) -> Optional[dict]:
    """Per-call read-only connection — safe from any thread. Mirrors
    KnewrallIndexer.get_node_by_id's query without touching its singleton."""
    conn = index_db_connection()
    try:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_edges(
    source_id: Optional[str] = None,
    target_id: Optional[str] = None,
    link_type: Optional[str] = None,
) -> list[dict]:
    """Per-call read-only connection — safe from any thread. Mirrors
    KnewrallIndexer.get_edges's query without touching its singleton."""
    conn = index_db_connection()
    try:
        conditions, params = [], []
        if source_id:
            conditions.append("source_id = ?")
            params.append(source_id)
        if target_id:
            conditions.append("target_id = ?")
            params.append(target_id)
        if link_type:
            conditions.append("link_type = ?")
            params.append(link_type)
        where = " AND ".join(conditions) if conditions else "1=1"
        rows = conn.execute(f"SELECT * FROM edges WHERE {where}", params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def vector_stats() -> dict:
    """Per-call read-only connection against vectors.db directly — avoids the
    shared VectorStore singleton (and its thread-affine connection) entirely
    for this simple read."""
    conn = vectors_db_connection()
    if conn is None:
        return {}
    try:
        total = conn.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0]
        by_kind = {
            r["kind"]: r["cnt"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) AS cnt FROM embedding_meta GROUP BY kind"
            ).fetchall()
        }
        return {"total": total, "by_kind": by_kind}
    except sqlite3.OperationalError:
        return {}  # embedding_meta table doesn't exist yet (no embeddings run)
    finally:
        conn.close()


def semantic_available() -> bool:
    """get_embed_adapter() just wraps HTTP client config (no sqlite
    connection), so unlike search_graph_raw's KNN path this is safe to call
    directly, no engine-thread routing needed."""
    try:
        adapter = get_embed_adapter()
        return bool(adapter and adapter.is_available())
    except Exception:
        return False
