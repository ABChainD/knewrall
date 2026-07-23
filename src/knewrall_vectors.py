"""
Knewrall Vector Store

Manages embedding storage and hybrid literal+semantic search using sqlite-vec.
All vectors live in the ephemeral, git-ignored .knewrall/vectors.db.

Design:
  - Embeddings are keyed by (kind, ref_id) with a content_hash guard so we
    only re-embed text that has actually changed.
  - 'kind' is one of: 'neuron' | 'note' | 'code_symbol'
  - Hybrid search uses Reciprocal Rank Fusion (RRF) to merge literal results
    (LIKE / FTS from the existing index) with vector KNN results.
  - Degrades gracefully: if sqlite-vec is not installed or vectors.db is empty,
    the module returns None and callers fall back to literal-only search.
"""

import hashlib
import json
import logging
import struct
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .paths import get_root

logger = logging.getLogger(__name__)

DEFAULT_VECTORS_DB = get_root() / ".knewrall" / "vectors.db"
META_DB_PATH = get_root() / ".knewrall" / "vectors.db"  # same file, different tables

# RRF constant (k=60 is the standard choice from the original paper)
_RRF_K = 60


# ── sqlite-vec loader ─────────────────────────────────────────────────────────

def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension.  Return True on success."""
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except ImportError:
        logger.warning(
            "sqlite-vec not installed — vector search disabled. "
            "Install with: pip install sqlite-vec"
        )
        return False
    except Exception as e:
        logger.warning(f"Failed to load sqlite-vec extension: {e}")
        return False


def _pack_vec(floats: List[float]) -> bytes:
    """Pack a list of floats into binary blob (little-endian float32)."""
    return struct.pack(f"{len(floats)}f", *floats)


def _unpack_vec(blob: bytes) -> List[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# ── Schema ────────────────────────────────────────────────────────────────────

def _create_schema(conn: sqlite3.Connection, dim: int):
    """Create meta table and vec0 virtual table for the given dimension."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS embedding_meta (
            rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL,
            ref_id      TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            model       TEXT NOT NULL,
            dim         INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE (kind, ref_id)
        )
    """)
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
            embedding float[{dim}]
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_meta_kind_ref ON embedding_meta(kind, ref_id)
    """)
    conn.commit()


# ── VectorStore ───────────────────────────────────────────────────────────────

class VectorStore:
    """
    Manages .knewrall/vectors.db: stores embeddings and runs KNN + hybrid search.
    """

    def __init__(self, db_path: Optional[Path] = None, dim: int = 1536):
        self.db_path = db_path or DEFAULT_VECTORS_DB
        self.dim = dim
        self.conn: Optional[sqlite3.Connection] = None
        self._vec_available = False
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        if self.conn is None:
            self.conn = sqlite3.connect(str(self.db_path))
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self._vec_available = _load_sqlite_vec(self.conn)
            if self._vec_available:
                _create_schema(self.conn, self.dim)
            else:
                # create just the meta table so we can still check cache hits
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS embedding_meta (
                        rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind        TEXT NOT NULL,
                        ref_id      TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        model       TEXT NOT NULL,
                        dim         INTEGER NOT NULL,
                        created_at  TEXT NOT NULL,
                        UNIQUE (kind, ref_id)
                    )
                """)
                self.conn.commit()
        return self.conn

    def close(self):
        if self.conn:
            self.checkpoint()
            self.conn.close()
            self.conn = None

    def checkpoint(self) -> None:
        """
        Force a WAL checkpoint (TRUNCATE) so recent writes land in vectors.db
        itself instead of the -wal side file. Needed because a long-lived
        process (e.g. the MCP server) can hold the connection open
        indefinitely, and Syncthing only sees the main .db file (the -wal/-shm
        files are excluded from sync) — without an explicit checkpoint, the
        synced copy would be consistent but silently missing recent embeddings.
        """
        if self.conn is None:
            return
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError as e:
            logger.warning(f"WAL checkpoint failed: {e}")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    # ── Content hash ───────────────────────────────────────────────────────

    @staticmethod
    def text_hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def _needs_embed(self, kind: str, ref_id: str, content_hash: str) -> bool:
        """True if the embedding is absent or the content has changed."""
        conn = self.connect()
        row = conn.execute(
            "SELECT content_hash FROM embedding_meta WHERE kind=? AND ref_id=?",
            (kind, ref_id)
        ).fetchone()
        if row is None:
            return True
        return row["content_hash"] != content_hash

    # ── Upsert ─────────────────────────────────────────────────────────────

    def upsert_embedding(self, kind: str, ref_id: str, text: str,
                         vector: List[float], model: str) -> bool:
        """
        Store or update an embedding.  Returns True if inserted/updated,
        False if content was unchanged (skipped).
        """
        if not self._vec_available:
            return False

        conn = self.connect()
        content_hash = self.text_hash(text)

        if not self._needs_embed(kind, ref_id, content_hash):
            return False

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        # upsert meta row to get rowid
        conn.execute("""
            INSERT INTO embedding_meta (kind, ref_id, content_hash, model, dim, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, ref_id) DO UPDATE SET
                content_hash=excluded.content_hash,
                model=excluded.model,
                dim=excluded.dim,
                created_at=excluded.created_at
        """, (kind, ref_id, content_hash, model, len(vector), now))

        meta_rowid = conn.execute(
            "SELECT rowid FROM embedding_meta WHERE kind=? AND ref_id=?",
            (kind, ref_id)
        ).fetchone()["rowid"]

        # upsert vec0 row — vec0 uses rowid as primary key
        conn.execute(
            "DELETE FROM vec_embeddings WHERE rowid=?", (meta_rowid,)
        )
        conn.execute(
            "INSERT INTO vec_embeddings(rowid, embedding) VALUES (?, ?)",
            (meta_rowid, _pack_vec(vector))
        )
        conn.commit()
        return True

    # ── Batch embed helper ─────────────────────────────────────────────────

    def embed_and_store(self, items: List[Tuple[str, str, str]],
                        adapter, model: str) -> int:
        """
        Embed and store a batch of items.

        items: list of (kind, ref_id, text)
        adapter: EmbeddingAdapter instance
        model: model name string (for logging)
        Returns count of newly embedded items.
        """
        if not self._vec_available or not adapter.is_available():
            return 0

        # filter to only those needing embedding
        to_embed = [
            (kind, ref_id, text) for kind, ref_id, text in items
            if self._needs_embed(kind, ref_id, self.text_hash(text))
        ]
        if not to_embed:
            return 0

        texts = [t for _, _, t in to_embed]
        vectors = adapter.embed(texts)
        if vectors is None:
            logger.warning("Embedding batch failed — skipping.")
            return 0

        count = 0
        for (kind, ref_id, text), vec in zip(to_embed, vectors):
            if self.upsert_embedding(kind, ref_id, text, vec, model):
                count += 1
        if count:
            self.checkpoint()
        return count

    # ── Exact lookup (query-term cache) ────────────────────────────────────

    def get_embedding(self, kind: str, ref_id: str) -> Optional[List[float]]:
        """
        Fetch the stored vector for an exact (kind, ref_id), or None if not
        cached. Unlike knn_search (nearest-neighbour), this is a direct key
        lookup — used by the query-term cache to reuse a previously-embedded
        term's own vector instead of re-calling the embedding API for it.
        """
        if not self._vec_available:
            return None
        conn = self.connect()
        row = conn.execute(
            "SELECT rowid FROM embedding_meta WHERE kind=? AND ref_id=?",
            (kind, ref_id)
        ).fetchone()
        if row is None:
            return None
        vec_row = conn.execute(
            "SELECT embedding FROM vec_embeddings WHERE rowid=?", (row["rowid"],)
        ).fetchone()
        if vec_row is None:
            return None
        return _unpack_vec(vec_row["embedding"])

    # ── KNN search ─────────────────────────────────────────────────────────

    # Over-fetch multiplier when kind_filter is set: vec0's own `k = ?` limit
    # applies BEFORE any kind filtering, so if another kind (code_symbol,
    # note) dominates the raw top-k, a plain post-hoc filter can silently
    # lose genuine wanted-kind hits ranked just below the raw cutoff — no
    # amount of filtering after the fact recovers them. Asking vec0 for more
    # rows than needed, then filtering and truncating, makes that much less
    # likely without requiring a per-kind partition index.
    _KIND_FILTER_OVERFETCH = 5

    def knn_search(self, query_vector: List[float], k: int = 20,
                   kind_filter: Optional[str] = None) -> List[Dict]:
        """
        Return the k nearest neighbours to query_vector, optionally restricted
        to one `kind` (see the class docstring: 'neuron' | 'note' | 'code_symbol').

        Returns list of {"kind", "ref_id", "distance"} dicts.
        """
        if not self._vec_available:
            return []

        conn = self.connect()
        query_blob = _pack_vec(query_vector)
        raw_k = k * self._KIND_FILTER_OVERFETCH if kind_filter else k

        try:
            rows = conn.execute("""
                SELECT m.kind, m.ref_id, v.distance
                FROM vec_embeddings v
                JOIN embedding_meta m ON m.rowid = v.rowid
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY v.distance
            """, (query_blob, raw_k)).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"KNN query failed: {e}")
            return []

        results = [dict(r) for r in rows]
        if kind_filter:
            results = [r for r in results if r["kind"] == kind_filter][:k]
        return results

    # ── Hybrid search (RRF) ────────────────────────────────────────────────

    def hybrid_search(self,
                      literal_results: List[Dict],
                      query_vector: Optional[List[float]],
                      k: int = 20,
                      id_field: str = "id",
                      kind_filter: Optional[str] = None) -> List[Dict]:
        """
        Merge literal_results (from LIKE/FTS, already ranked) with semantic
        KNN results using Reciprocal Rank Fusion.

        literal_results: list of dicts, each with at least {id_field: ...}.
        query_vector: query embedding (None → return literal_results unchanged).
        id_field: key in literal_results that contains the neuron/note/symbol id.
        kind_filter: restrict the semantic side to one kind (e.g. 'neuron') —
            pass this whenever literal_results is itself kind-specific, so a
            dominant other kind (code_symbol, note) can't crowd it out of the
            KNN top-k (see knn_search's docstring on why this can't just be
            filtered after the fact).

        Returns a re-ranked list of the same literal_result dicts.
        """
        if not query_vector or not self._vec_available:
            return literal_results

        knn = self.knn_search(query_vector, k=k, kind_filter=kind_filter)

        # Build RRF score maps
        # Literal: each result gets rank 1..n
        lit_scores: Dict[str, float] = {}
        for rank, item in enumerate(literal_results, start=1):
            ref_id = item.get(id_field, "")
            lit_scores[ref_id] = lit_scores.get(ref_id, 0) + 1.0 / (_RRF_K + rank)

        # Semantic KNN: results are already rank-ordered by distance
        sem_scores: Dict[str, float] = {}
        sem_items: Dict[str, Dict] = {}
        for rank, item in enumerate(knn, start=1):
            ref_id = item["ref_id"]
            sem_scores[ref_id] = 1.0 / (_RRF_K + rank)
            sem_items[ref_id] = item

        # Union of all ids
        all_ids = set(lit_scores) | set(sem_scores)
        rrf: Dict[str, float] = {
            ref_id: lit_scores.get(ref_id, 0) + sem_scores.get(ref_id, 0)
            for ref_id in all_ids
        }

        # Build output: prefer literal_results dicts for data richness, but a
        # semantic-only hit (found by KNN, not by the literal LIKE query) must
        # still surface — that's the whole point of hybrid search, and is
        # often the common case (literal_results is frequently empty for a
        # query with no exact substring match). Callers can load_node() a
        # semantic-only id themselves for the full body.
        lit_map = {item.get(id_field, ""): item for item in literal_results}
        ranked = sorted(all_ids, key=lambda x: rrf[x], reverse=True)

        merged = []
        for ref_id in ranked[:k]:
            if ref_id in lit_map:
                merged.append(lit_map[ref_id])
            else:
                merged.append({id_field: ref_id, "_semantic_only": True})
        return merged

    # ── Stats ──────────────────────────────────────────────────────────────

    def stats(self) -> Dict:
        conn = self.connect()
        total = conn.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0]
        by_kind = {r["kind"]: r["cnt"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS cnt FROM embedding_meta GROUP BY kind"
        ).fetchall()}
        return {
            "total": total,
            "by_kind": by_kind,
            "vec_available": self._vec_available,
        }

    def delete_embedding(self, kind: str, ref_id: str) -> bool:
        conn = self.connect()
        row = conn.execute(
            "SELECT rowid FROM embedding_meta WHERE kind=? AND ref_id=?",
            (kind, ref_id)
        ).fetchone()
        if not row:
            return False
        meta_rowid = row["rowid"]
        conn.execute("DELETE FROM embedding_meta WHERE rowid=?", (meta_rowid,))
        if self._vec_available:
            conn.execute("DELETE FROM vec_embeddings WHERE rowid=?", (meta_rowid,))
        conn.commit()
        return True

    # ── Conflict reconciliation ────────────────────────────────────────────

    def reconcile(self, conflict_db_path: Path) -> int:
        """
        Recover embeddings that exist only in a Syncthing sync-conflict copy
        of vectors.db and are missing locally. Local state is always
        authoritative — an embedding already present locally (by kind+ref_id)
        is never overwritten, so this only fills gaps, it never merges or
        re-ranks anything. Uses two plain sqlite3 connections and copies raw
        rows/blobs by rowid (vec0 supports ordinary rowid SELECTs) rather than
        ATTACH, which is not reliable across sqlite-vec virtual tables.

        Returns the number of embeddings recovered.
        """
        if not conflict_db_path.is_file():
            return 0
        conn = self.connect()
        conflict_conn = sqlite3.connect(str(conflict_db_path))
        conflict_conn.row_factory = sqlite3.Row
        # A fresh sqlite3 connection never has sqlite-vec loaded, even to a
        # file that already contains a vec0 table — querying vec_embeddings
        # on it without this raises "no such module: vec0". Only bother if
        # the LOCAL store can even use vectors (self._vec_available); no
        # point loading it just to read blobs with nowhere to put them.
        conflict_vec_available = self._vec_available and _load_sqlite_vec(conflict_conn)
        recovered = 0
        try:
            rows = conflict_conn.execute(
                "SELECT rowid, kind, ref_id, content_hash, model, dim, created_at "
                "FROM embedding_meta"
            ).fetchall()
            for row in rows:
                exists = conn.execute(
                    "SELECT 1 FROM embedding_meta WHERE kind=? AND ref_id=?",
                    (row["kind"], row["ref_id"])
                ).fetchone()
                if exists:
                    continue
                vec_row = None
                if conflict_vec_available:
                    vec_row = conflict_conn.execute(
                        "SELECT embedding FROM vec_embeddings WHERE rowid=?", (row["rowid"],)
                    ).fetchone()
                    if vec_row is None:
                        continue
                # A conflict db from a different model/dim era would have a
                # differently-sized blob than this store's vec0[dim] column —
                # isolate each row so one such mismatch doesn't abort every
                # row already recovered in this call (nothing commits until
                # the loop finishes, so an uncaught error here would otherwise
                # roll back the whole batch, not just the bad row).
                try:
                    conn.execute("""
                        INSERT INTO embedding_meta (kind, ref_id, content_hash, model, dim, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (row["kind"], row["ref_id"], row["content_hash"], row["model"],
                          row["dim"], row["created_at"]))
                    if vec_row is not None:
                        new_rowid = conn.execute(
                            "SELECT rowid FROM embedding_meta WHERE kind=? AND ref_id=?",
                            (row["kind"], row["ref_id"])
                        ).fetchone()["rowid"]
                        conn.execute(
                            "INSERT INTO vec_embeddings(rowid, embedding) VALUES (?, ?)",
                            (new_rowid, vec_row["embedding"])
                        )
                except sqlite3.Error as row_err:
                    logger.warning(
                        f"reconcile: skipping {row['kind']}/{row['ref_id']} "
                        f"(likely dim mismatch from a different embedding era): {row_err}"
                    )
                    conn.execute(
                        "DELETE FROM embedding_meta WHERE kind=? AND ref_id=?",
                        (row["kind"], row["ref_id"])
                    )
                    continue
                recovered += 1
            conn.commit()
            if recovered:
                self.checkpoint()
        finally:
            conflict_conn.close()
        return recovered


# ── Convenience: text extractor for neurons ───────────────────────────────────

def neuron_to_embed_text(node_data: dict) -> str:
    """
    Flatten a neuron dict to a single string for embedding.
    Includes canonical_name, aliases, descriptions, and key properties.
    """
    parts: List[str] = []
    header = node_data.get("header", {})
    parts.append(header.get("canonical_name", ""))
    aliases = header.get("aliases", [])
    if aliases:
        parts.append("also known as " + ", ".join(aliases))
    desc = node_data.get("descriptions", {})
    for k in ("conceptual", "physical", "psychological"):
        v = desc.get(k)
        if v:
            parts.append(v)
    props = node_data.get("properties", {})
    for key, vals in props.items():
        if isinstance(vals, list):
            for v in vals:
                if isinstance(v, dict) and "value" in v:
                    parts.append(f"{key}: {v['value']}")
        elif vals:
            parts.append(f"{key}: {vals}")
    return " | ".join(p for p in parts if p).strip()


def code_symbol_to_embed_text(sym: dict) -> str:
    """Flatten a code symbol dict to a string for embedding."""
    parts = [
        sym.get("qualified_name", ""),
        sym.get("signature", ""),
        sym.get("docstring", ""),
    ]
    return " | ".join(p for p in parts if p).strip()
