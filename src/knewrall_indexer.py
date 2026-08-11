"""
Knewrall Indexer Module

Ephemeral SQLite index that serves as a materialized view of the file system.
The index is rebuildable from scratch and should be treated as a cache.
"""

import hashlib
import sqlite3
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple, Any, Set
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Default directory constants — derived from the resolved Knewrall root so the
# engine works from any working directory and in both standalone/subfolder layouts.
from .paths import get_root

DEFAULT_NEURONS_DIR = get_root() / "neurons"
DEFAULT_NOTES_DIR = get_root() / "notes"
DEFAULT_INDEX_DB_PATH = get_root() / ".knewrall" / "index.db"

def _scalar_or_none(value):
    """SQLite can only bind str/int/float/bytes/bool/None. A non-scalar (list or
    dict) on a field meant to hold a scalar raises sqlite3.ProgrammingError, and
    without a per-file guard that aborts indexing for the WHOLE corpus. Coerce
    anything non-bindable to None so a single malformed on-disk field can't take
    down refresh-index/rebuild-index (W1)."""
    return value if isinstance(value, (str, int, float, bool, bytes, type(None))) else None


# Wikilink pattern: [[canonical_name]] or [[canonical_name|display]]
WIKILINK_PATTERN = re.compile(r'\[\[([^\[\]\|]+)(?:\|([^\[\]]+))?\]\]')
# Standard markdown link pattern: [display](path)
MARKDOWN_LINK_PATTERN = re.compile(r'\[([^\[\]]+)\]\(([^()]+)\)')


def _hash_file(path: Path) -> str:
    """SHA-256 of file bytes, used by refresh_index() as the sole change
    signal — deliberately not mtime, which Syncthing-synced files make
    unreliable (preserved timestamps, clock skew, same-size rewrites)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _peek_neuron_id(path: Path) -> Optional[str]:
    """Read just system.id from a neuron JSON file. Must be TOTAL (never raise):
    refresh_index() calls it on files BEFORE they're validated — including
    malformed ones — so it can preserve a present-but-unindexable file's prior
    index row (V4). A non-dict payload (e.g. a JSON-array file) parses fine but
    then `data.get(...)` would raise AttributeError; guarding only
    JSONDecodeError/OSError let that abort the whole refresh (class-A, one
    function over). Return None on anything unexpected."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        system = data.get("system")
        if not isinstance(system, dict):
            return None
        return system.get("id")
    except Exception:
        return None

class KnewrallIndexer:
    """Manages the SQLite index for Knewrall nodes and edges."""

    def __init__(self, db_path: Optional[Path] = None,
                 neurons_dir: Optional[Path] = None,
                 notes_dir: Optional[Path] = None):
        """
        Initialize the indexer.

        Args:
            db_path: Path to SQLite database file. Defaults to .knewrall/index.db
            neurons_dir: Path to neurons directory. Defaults to "neurons".
            notes_dir: Path to notes directory. Defaults to "notes".
        """
        self.db_path = db_path or DEFAULT_INDEX_DB_PATH
        self.neurons_dir = neurons_dir or DEFAULT_NEURONS_DIR
        self.notes_dir = notes_dir or DEFAULT_NOTES_DIR
        self.conn = None
        self._ensure_index_dir()

    def _ensure_index_dir(self) -> None:
        """Create the .knewrall directory if it doesn't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Establish a connection to the SQLite database."""
        if self.conn is None:
            self.conn = sqlite3.connect(str(self.db_path))
            self.conn.row_factory = sqlite3.Row
        return self.conn

    def close(self) -> None:
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def create_schema(self, drop: bool = True) -> None:
        """
        Create the SQLite tables as per blueprint:
        - nodes
        - aliases
        - edges
        - index_files

        drop=True (default, used by rebuild_index(full=True)) drops and
        recreates everything. drop=False creates only whatever is missing,
        touching no existing data — used by refresh_index() so it also works
        against a brand-new db_path without requiring a prior create_schema()
        call, and doesn't need its own separate copy of this DDL.
        """
        conn = self.connect()
        cursor = conn.cursor()

        if drop:
            cursor.execute("DROP TABLE IF EXISTS edges")
            cursor.execute("DROP TABLE IF EXISTS aliases")
            cursor.execute("DROP TABLE IF EXISTS nodes")
            cursor.execute("DROP TABLE IF EXISTS index_files")

        # Create nodes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                full_legal_name TEXT,
                aliases TEXT,  -- JSON array
                tags TEXT,     -- JSON array
                descriptions TEXT,  -- JSON object
                properties TEXT,    -- JSON object
                -- Spatiotemporal fields
                start_timestamp TEXT,
                end_timestamp TEXT,
                is_relative BOOLEAN,
                anchor_node_id TEXT,
                latitude REAL,
                longitude REAL,
                address TEXT,
                parent_location_id TEXT,
                -- Agency fields
                role_history TEXT,  -- JSON array
                -- Why/How fields
                source_node_id TEXT,
                target_node_id TEXT,
                -- Metadata
                version INTEGER NOT NULL,
                last_updated TEXT NOT NULL,
                checksum TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create aliases table for fast lookup
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS aliases (
                alias TEXT NOT NULL,
                node_id TEXT NOT NULL,
                PRIMARY KEY (alias, node_id),
                FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_aliases_node_id ON aliases(node_id)")

        # Create edges table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                predicate TEXT NOT NULL,
                direction TEXT NOT NULL,
                certainty TEXT NOT NULL,
                tags TEXT,  -- JSON array
                link_type TEXT NOT NULL,  -- 'node_link' or 'wikilink'
                valid_from TEXT,
                valid_until TEXT,
                via_node_id TEXT,
                recorded_at TEXT,
                sources TEXT,  -- JSON array of prefixed source strings
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_id, target_id, predicate, link_type),
                FOREIGN KEY (source_id) REFERENCES nodes(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES nodes(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_predicate ON edges(predicate)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_link_type ON edges(link_type)")
        # On a pre-existing edges table the CREATE TABLE above is a no-op, so the
        # reification columns are still absent — add them before the indexes that
        # reference them, or CREATE INDEX raises "no such column: via_node_id"
        # and aborts ensure_schema()/refresh_index() on every legacy index (F1).
        self._add_missing_edge_columns(cursor)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_via_node_id ON edges(via_node_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_recorded_at ON edges(recorded_at)")

        self._create_index_files_table(cursor)

        conn.commit()
        logger.info("Database schema created.")

    def _create_index_files_table(self, cursor) -> None:
        """
        File-state table backing incremental refresh_index(). One row per
        indexed source file (rel_path relative to this instance's own
        neurons_dir/notes_dir, matching refresh_index()'s own convention —
        NOT the global Knewrall root — so it stays stable across machines
        even though this table itself is a local, unsynced cache). `node_id`
        holds the neuron's UUID for kind='neuron',
        or the synthetic wikilink source_id (see index_markdown_note) for
        kind='note' — reusing the column lets refresh_index prune wikilink
        edges for a deleted note without recomputing that id.
        """
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS index_files (
                rel_path TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                node_id TEXT,
                content_hash TEXT NOT NULL,
                indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    def ensure_schema(self) -> None:
        """
        Idempotent, non-destructive: create any missing tables (nodes,
        aliases, edges, index_files) without touching existing data. Migrates
        an index.db created before index_files existed, and lets refresh_index()
        run against a brand-new db_path without a prior create_schema() call.
        Also adds new columns to existing edges tables if missing.
        """
        self.create_schema(drop=False)
        self._migrate_edges_columns()

    # Reification columns added to `edges` after the original schema shipped.
    # A pre-existing index.db predates these, so they must be ALTER-added
    # before anything (notably the idx_edges_via_node_id / _recorded_at
    # indexes) references them.
    _EDGE_MIGRATION_COLUMNS = {
        "valid_from": "TEXT",
        "valid_until": "TEXT",
        "via_node_id": "TEXT",
        "recorded_at": "TEXT",
        "sources": "TEXT",
    }

    def _add_missing_edge_columns(self, cursor) -> None:
        """Idempotently ALTER-add any missing reification columns to an existing
        `edges` table, operating on the caller's cursor. Called both from
        create_schema() — before the new-column indexes, so a legacy table
        doesn't raise "no such column" and abort refresh_index() (F1) — and
        from _migrate_edges_columns()."""
        cursor.execute("PRAGMA table_info(edges)")
        existing_cols = {row["name"] for row in cursor.fetchall()}
        for col, col_type in self._EDGE_MIGRATION_COLUMNS.items():
            if col not in existing_cols:
                cursor.execute(f"ALTER TABLE edges ADD COLUMN {col} {col_type}")

    def _migrate_edges_columns(self) -> None:
        """Add new columns to existing edges table if they don't exist yet."""
        conn = self.connect()
        cursor = conn.cursor()
        self._add_missing_edge_columns(cursor)
        conn.commit()

    def index_neuron_json(self, file_path: Path) -> bool:
        """
        Parse a single neuron JSON file and insert its data into the index.

        Args:
            file_path: Path to the JSON file in neurons/

        Returns:
            True if successful, False otherwise.
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to read JSON file {file_path}: {e}")
            return False

        # Validate required fields
        system = data.get('system')
        header = data.get('header')
        if not system or not header:
            logger.warning(f"Missing system or header in {file_path}")
            return False

        node_id = system.get('id')
        node_type = header.get('type')
        canonical_name = header.get('canonical_name')
        if not node_id or not node_type or not canonical_name:
            logger.warning(f"Missing required fields in {file_path}")
            return False

        # Prepare data for nodes table
        full_legal_name = header.get('full_legal_name')
        # Sort aliases for deterministic output
        aliases_sorted = sorted(header.get('aliases', []))
        aliases_json = json.dumps(aliases_sorted, sort_keys=True)
        tags_sorted = sorted(data.get('tags', []))
        tags_json = json.dumps(tags_sorted, sort_keys=True)
        descriptions_json = json.dumps(data.get('descriptions', {}), sort_keys=True)
        properties_json = json.dumps(data.get('properties', {}), sort_keys=True)
        
        # Spatiotemporal fields
        st = data.get('spatiotemporal', {})
        start_ts = st.get('start_timestamp')
        end_ts = st.get('end_timestamp')
        is_rel = st.get('is_relative')
        anchor_id = st.get('anchor_node_id')
        coords = st.get('coordinates', {})
        lat = coords.get('lat')
        lon = coords.get('long')
        addr = st.get('address')
        parent_loc = st.get('parent_location_id')
        
        # Agency and Edge fields
        role_history_json = json.dumps(header.get('role_history', []), sort_keys=True)
        source_node_id = header.get('source_node_id')
        target_node_id = header.get('target_node_id')
        
        version = system.get('version', 1)
        last_updated = system.get('last_updated')
        checksum = system.get('checksum')

        conn = self.connect()
        cursor = conn.cursor()

        # W1: one malformed on-disk file must never abort indexing for the WHOLE
        # corpus. A non-scalar on any bindable field (or any other surprise)
        # raises inside the driver; catch per-file, roll back this neuron's
        # partial writes, and return False so refresh/rebuild skip it and carry
        # on. The _scalar_or_none coercions below are the first line of defence
        # (the link still indexes, just without the malformed field); this
        # try/except is the backstop for anything they don't anticipate.
        try:
            # Insert or replace node
            cursor.execute("""
                INSERT OR REPLACE INTO nodes
                (id, type, canonical_name, full_legal_name, aliases, tags, descriptions, properties,
                 start_timestamp, end_timestamp, is_relative, anchor_node_id, latitude, longitude,
                 address, parent_location_id, role_history, source_node_id, target_node_id,
                 version, last_updated, checksum)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (node_id, node_type, canonical_name, full_legal_name, aliases_json,
                  tags_json, descriptions_json, properties_json,
                  _scalar_or_none(start_ts), _scalar_or_none(end_ts), is_rel, _scalar_or_none(anchor_id),
                  _scalar_or_none(lat), _scalar_or_none(lon),
                  _scalar_or_none(addr), _scalar_or_none(parent_loc), role_history_json,
                  _scalar_or_none(source_node_id), _scalar_or_none(target_node_id),
                  version, _scalar_or_none(last_updated), _scalar_or_none(checksum)))

            # Insert aliases
            cursor.execute("DELETE FROM aliases WHERE node_id = ?", (node_id,))
            for alias in header.get('aliases', []):
                if isinstance(alias, str) and alias.strip():
                    cursor.execute("INSERT OR IGNORE INTO aliases (alias, node_id) VALUES (?, ?)",
                                   (alias.strip(), node_id))

            # Insert edges from links array
            cursor.execute("DELETE FROM edges WHERE source_id = ? AND link_type = 'node_link'", (node_id,))
            for link in data.get('links', []):
                target_id = link.get('target_id')
                predicate = link.get('predicate')
                direction = link.get('direction')
                certainty = link.get('certainty')
                tags = json.dumps(link.get('tags', []), sort_keys=True)
                # Coerce every scalar-typed edge field so a non-scalar on disk
                # (list/dict) nulls out rather than aborting the run (W1 — the
                # same class Z1 fixed for `assertion`, now applied field-wide).
                valid_from = _scalar_or_none(link.get('valid_from'))
                valid_until = _scalar_or_none(link.get('valid_until'))
                via_node_id = _scalar_or_none(link.get('via_node_id'))
                # A malformed assertion (non-dict, or non-list `sources`) is
                # tolerated: the link indexes without assertion metadata rather
                # than crashing the corpus or indexing garbage chars (Z1).
                assertion = link.get('assertion')
                recorded_at = None
                sources = None
                if isinstance(assertion, dict):
                    recorded_at = _scalar_or_none(assertion.get('recorded_at'))
                    src = assertion.get('sources')
                    if isinstance(src, list):
                        sources = json.dumps(sorted(s for s in src if isinstance(s, str)))

                if target_id and predicate and direction and certainty:
                    cursor.execute("""
                        INSERT OR REPLACE INTO edges
                        (source_id, target_id, predicate, direction, certainty, tags, link_type,
                         valid_from, valid_until, via_node_id, recorded_at, sources)
                        VALUES (?, ?, ?, ?, ?, ?, 'node_link', ?, ?, ?, ?, ?)
                    """, (node_id, target_id, predicate, direction, certainty, tags,
                          valid_from, valid_until, via_node_id, recorded_at, sources))

            conn.commit()
        except Exception as e:
            logger.error(f"Failed to index neuron {node_id} from {file_path}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            return False

        logger.debug(f"Indexed neuron {node_id} ({canonical_name})")
        return True

    def _safe_index_neuron(self, json_file: Path) -> bool:
        """Index one neuron with a guarantee that an exception ANYWHERE in the
        parse/extract/insert — not just the DB section index_neuron_json already
        guards — is contained to this one file. A single malformed neuron (a
        non-dict `header`/`system`, a JSON-array file, a non-sortable `tags`,
        etc.) must never abort refresh-index/rebuild-index for the whole corpus
        (V1). Rolls back this file's partial transaction on failure and returns
        False so the caller skips it and carries on."""
        try:
            return self.index_neuron_json(json_file)
        except Exception as e:
            logger.error(f"Failed to index neuron file {json_file}: {e}")
            try:
                self.connect().rollback()
            except Exception:
                pass
            return False

    def _safe_index_note(self, md_file: Path) -> bool:
        """Notes counterpart to _safe_index_neuron (V7): a single malformed note
        must never abort refresh-index/rebuild-index for the whole corpus. Most
        notably index_markdown_note does `f.read()` with a strict utf-8 decode, so
        a note with invalid bytes raises UnicodeDecodeError — but ANY exception is
        contained here, rolled back, and reported as False so the caller carries
        on."""
        try:
            return self.index_markdown_note(md_file)
        except Exception as e:
            logger.error(f"Failed to index note file {md_file}: {e}")
            try:
                self.connect().rollback()
            except Exception:
                pass
            return False

    def index_markdown_note(self, file_path: Path) -> bool:
        """
        Parse a markdown note, extract wikilinks and standard links,
        and add them as edges of type 'wikilink'.
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except OSError as e:
            logger.error(f"Failed to read markdown file {file_path}: {e}")
            return False

        # Extract both wikilinks and standard markdown links
        wikilinks = WIKILINK_PATTERN.findall(content)
        md_links = MARKDOWN_LINK_PATTERN.findall(content)
        
        all_links = []
        # Wikilinks: target=group[0], display=group[1]
        for target, display in wikilinks:
            all_links.append((target.strip(), display.strip() if display else target.strip()))
        
        # Markdown links: display=group[0], target=group[1]
        for display, target in md_links:
            all_links.append((target.strip(), display.strip()))

        if not all_links:
            logger.debug(f"No links found in {file_path}")
            return True

        conn = self.connect()
        cursor = conn.cursor()

        # Use the file path as a pseudo-source ID (since notes are not nodes)
        source_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(file_path)))
        
        # Clear existing wikilink edges for this note
        cursor.execute("DELETE FROM edges WHERE source_id = ? AND link_type = 'wikilink'", (source_id,))

        for i, (target, display) in enumerate(all_links):
            if not target:
                continue

            target_ids = []
            
            # Check if target is a path like ../neurons/<shard>/[UUID].md/.json
            # (the shard sub-folder is optional for resilience). Case-insensitive.
            path_match = re.search(
                r'neurons/(?:[0-9a-f]{2}/)?([a-f0-9\-]{36})(?:\.(?:md|json))?',
                target, re.IGNORECASE)
            if path_match:
                node_id = path_match.group(1)
                cursor.execute("SELECT id FROM nodes WHERE id = ?", (node_id,))
                if cursor.fetchone():
                    target_ids.append(node_id)
                else:
                    logger.warning(f"Path-based link target ID '{node_id}' not found in nodes table (file: {file_path})")
            else:
                # Standard lookup by canonical name or alias
                cursor.execute("""
                    SELECT id AS node_id FROM nodes WHERE canonical_name = ?
                    UNION
                    SELECT node_id FROM aliases WHERE alias = ?
                """, (target, target))
                rows = cursor.fetchall()
                if not rows:
                    # Log as debug only to reduce noise during rebuilds
                    logger.debug(f"Link target '{target}' not found in index (file: {file_path})")
                    continue
                target_ids = [row['node_id'] for row in rows]

            # For each matching node, create a wikilink edge from the note.
            for target_id in target_ids:
                predicate = f"references:{i}:{target}"
                cursor.execute("""
                    INSERT OR REPLACE INTO edges
                    (source_id, target_id, predicate, direction, certainty, tags, link_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (source_id, target_id, predicate, 'outbound', 'confirmed', '[]', 'wikilink'))

        conn.commit()
        logger.debug(f"Indexed {len(all_links)} links from {file_path}")
        return True

    def rebuild_index(self, full: bool = True) -> None:
        """
        Rebuild the entire index from scratch.

        Args:
            full: If True, drops tables and recreates schema. If False, only clears data.
        """
        logger.info("Starting index rebuild...")
        if full:
            self.create_schema()
        else:
            conn = self.connect()
            cursor = conn.cursor()
            self._create_index_files_table(cursor)
            cursor.execute("DELETE FROM edges")
            cursor.execute("DELETE FROM aliases")
            cursor.execute("DELETE FROM nodes")
            cursor.execute("DELETE FROM index_files")
            conn.commit()

        # Index all JSON neurons (recursing through shard sub-folders).
        json_files = [f for f in self.neurons_dir.rglob("*.json")
                      if ".deleted" not in f.parts]
        logger.info(f"Found {len(json_files)} neuron JSON files.")
        for json_file in json_files:
            self._safe_index_neuron(json_file)  # V1: one bad file never aborts the rebuild

        # Index all markdown notes (recursing through date sub-folders).
        # Skip folder READMEs, which are documentation, not knowledge notes.
        md_files = [f for f in self.notes_dir.rglob("*.md")
                    if f.name.lower() != "readme.md"]
        logger.info(f"Found {len(md_files)} markdown note files.")
        for md_file in md_files:
            self._safe_index_note(md_file)  # V7: one bad note never aborts the rebuild

        logger.info("Index rebuild completed.")

    def get_node_by_id(self, node_id: str) -> Optional[Dict]:
        """Retrieve a node by its UUID."""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def search_nodes(self, query: str, limit: int = 20) -> List[Dict]:
        """
        Search nodes by canonical_name, aliases, or tags.

        Args:
            query: Search string (case-insensitive substring).
            limit: Maximum number of results.

        Returns:
            List of node dictionaries.
        """
        conn = self.connect()
        cursor = conn.cursor()
        pattern = f"%{query}%"
        cursor.execute("""
            SELECT * FROM nodes
            WHERE canonical_name LIKE ?
               OR full_legal_name LIKE ?
               OR id IN (SELECT node_id FROM aliases WHERE alias LIKE ?)
            LIMIT ?
        """, (pattern, pattern, pattern, limit))
        return [dict(row) for row in cursor.fetchall()]

    def search_by_time(self, start: Optional[str] = None, end: Optional[str] = None) -> List[Dict]:
        """
        Search nodes active within a temporal range.
        
        Args:
            start: ISO-8601 start bound.
            end: ISO-8601 end bound.
        """
        conn = self.connect()
        cursor = conn.cursor()
        query = "SELECT * FROM nodes WHERE start_timestamp IS NOT NULL"
        params = []
        if start:
            query += " AND (start_timestamp >= ? OR end_timestamp >= ?)"
            params.extend([start, start])
        if end:
            query += " AND (start_timestamp <= ? OR end_timestamp <= ?)"
            params.extend([end, end])
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def search_by_location(self, lat: float, lon: float, radius: float = 0.1) -> List[Dict]:
        """
        Search nodes within a spatial bounding box.
        
        Args:
            lat: Center latitude.
            lon: Center longitude.
            radius: Degree offset for bounding box.
        """
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM nodes 
            WHERE latitude BETWEEN ? AND ? 
              AND longitude BETWEEN ? AND ?
        """, (lat - radius, lat + radius, lon - radius, lon + radius))
        return [dict(row) for row in cursor.fetchall()]

    def get_edges(self, source_id: Optional[str] = None,
                  target_id: Optional[str] = None,
                  link_type: Optional[str] = None) -> List[Dict]:
        """
        Retrieve edges with optional filters.

        Args:
            source_id: Filter by source node ID.
            target_id: Filter by target node ID.
            link_type: Filter by link_type ('node_link' or 'wikilink').

        Returns:
            List of edge dictionaries.
        """
        conn = self.connect()
        cursor = conn.cursor()
        conditions = []
        params = []
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
        cursor.execute(f"SELECT * FROM edges WHERE {where}", params)
        return [dict(row) for row in cursor.fetchall()]

    def incremental_update(self, changed_files: List[Path]) -> None:
        """
        Update index for a list of changed files.

        Args:
            changed_files: List of file paths that have changed.
        """
        for file_path in changed_files:
            if file_path.suffix == '.json' and self.neurons_dir in file_path.parents:
                self.index_neuron_json(file_path)
            elif file_path.suffix == '.md' and self.notes_dir in file_path.parents:
                self.index_markdown_note(file_path)
            else:
                logger.debug(f"Ignoring unknown file: {file_path}")

    def delete_node(self, node_id: str) -> None:
        """
        Remove a node and everything that references it. FK ON DELETE CASCADE
        is declared in the schema but SQLite only enforces it when
        `PRAGMA foreign_keys=ON` has been set on the connection, which this
        engine does not do (existing index_neuron_json already deletes
        aliases/edges explicitly rather than relying on it) — so this deletes
        from all three tables explicitly, matching that convention.
        """
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?", (node_id, node_id))
        cursor.execute("DELETE FROM aliases WHERE node_id = ?", (node_id,))
        cursor.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        conn.commit()

    def refresh_index(self) -> Dict[str, int]:
        """
        Incrementally sync the index to the current state of neurons/ and
        notes/ on disk, instead of the always-full drop+rebuild that
        rebuild_index() does. Content-hash is checked for every file (not an
        mtime pre-filter — mtimes are unreliable once files travel through
        Syncthing: preserved timestamps, clock skew, same-size rewrites), but
        only files whose hash actually changed get re-parsed and re-indexed.
        Files that vanished from disk since the last refresh have their
        index rows pruned via delete_node() / a wikilink-edge cleanup.

        This does NOT touch vectors.db — the caller (refresh_index_command /
        the `refresh-index` CLI verb) is responsible for pruning any
        embeddings for node ids reported in `deleted_neuron_ids`, since the
        indexer has no dependency on the optional vector-store module.

        rel_path is computed relative to this instance's own neurons_dir /
        notes_dir (not the global Knewrall root), matching the constructor's
        existing support for custom directories — needed so this method is
        testable in isolation the same way index_neuron_json etc. already are.

        Returns a stats dict including `deleted_neuron_ids` and
        `deleted_note_source_ids` for that follow-up pruning.
        """
        self.ensure_schema()
        conn = self.connect()
        cursor = conn.cursor()

        stats = {
            "neurons_added": 0, "neurons_updated": 0, "neurons_unchanged": 0,
            "neurons_deleted": 0,
            "notes_added": 0, "notes_updated": 0, "notes_unchanged": 0,
            "notes_deleted": 0,
            "deleted_neuron_ids": [],
            "deleted_note_source_ids": [],
        }

        seen_rel_paths: Set[str] = set()
        # Ground truth for the neuron-deletion pass below: every node_id this
        # scan actually found backed by a file on disk, regardless of whether
        # that file was ever tracked in index_files before (see the deletion
        # pass for why index_files alone isn't a reliable source here).
        seen_node_ids: Set[str] = set()

        json_files = [f for f in self.neurons_dir.rglob("*.json") if ".deleted" not in f.parts]
        for json_file in json_files:
            rel_path = str(json_file.relative_to(self.neurons_dir))
            seen_rel_paths.add(rel_path)
            content_hash = _hash_file(json_file)
            cursor.execute("SELECT content_hash, node_id FROM index_files WHERE rel_path = ?", (rel_path,))
            row = cursor.fetchone()
            if row and row["content_hash"] == content_hash:
                stats["neurons_unchanged"] += 1
                if row["node_id"]:
                    seen_node_ids.add(row["node_id"])
                continue
            is_new = row is None
            # Peek the id BEFORE indexing so a present-but-unindexable file can
            # still be marked "seen" (V4): otherwise the deletion pass below would
            # treat this on-disk file as vanished, delete its existing index row,
            # and prune its embedding — trading a loud failure for silent loss.
            peeked_id = _peek_neuron_id(json_file) or (row["node_id"] if row else None)
            if not self._safe_index_neuron(json_file):  # V1: contained per-file
                if peeked_id:
                    seen_node_ids.add(peeked_id)
                logger.warning(f"Neuron {json_file} present but failed to index; "
                               f"keeping its prior index row (not pruning).")
                continue
            node_id = _peek_neuron_id(json_file)
            if node_id:
                seen_node_ids.add(node_id)
            cursor.execute("""
                INSERT INTO index_files (rel_path, kind, node_id, content_hash, indexed_at)
                VALUES (?, 'neuron', ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(rel_path) DO UPDATE SET
                    node_id=excluded.node_id, content_hash=excluded.content_hash,
                    indexed_at=CURRENT_TIMESTAMP
            """, (rel_path, node_id, content_hash))
            conn.commit()
            stats["neurons_added" if is_new else "neurons_updated"] += 1

        md_files = [f for f in self.notes_dir.rglob("*.md") if f.name.lower() != "readme.md"]
        for md_file in md_files:
            rel_path = str(md_file.relative_to(self.notes_dir))
            seen_rel_paths.add(rel_path)
            content_hash = _hash_file(md_file)
            cursor.execute("SELECT content_hash FROM index_files WHERE rel_path = ?", (rel_path,))
            row = cursor.fetchone()
            if row and row["content_hash"] == content_hash:
                stats["notes_unchanged"] += 1
                continue
            is_new = row is None
            if not self._safe_index_note(md_file):  # V7: contained per-file
                continue
            # Must match index_markdown_note's own formula exactly (same Path object).
            note_source_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(md_file)))
            cursor.execute("""
                INSERT INTO index_files (rel_path, kind, node_id, content_hash, indexed_at)
                VALUES (?, 'note', ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(rel_path) DO UPDATE SET
                    node_id=excluded.node_id, content_hash=excluded.content_hash,
                    indexed_at=CURRENT_TIMESTAMP
            """, (rel_path, note_source_id, content_hash))
            conn.commit()
            stats["notes_added" if is_new else "notes_updated"] += 1

        # Deletion pass, notes: wikilink edges are only ever created by this
        # method's own index_markdown_note() calls, so index_files tracking
        # is a complete picture here — nothing else indexes a note.
        cursor.execute("SELECT rel_path, node_id FROM index_files WHERE kind = 'note'")
        for row in cursor.fetchall():
            if row["rel_path"] in seen_rel_paths:
                continue
            if row["node_id"]:
                cursor.execute(
                    "DELETE FROM edges WHERE source_id = ? AND link_type = 'wikilink'",
                    (row["node_id"],)
                )
                stats["deleted_note_source_ids"].append(row["node_id"])
            stats["notes_deleted"] += 1
            cursor.execute("DELETE FROM index_files WHERE rel_path = ?", (row["rel_path"],))

        # Deletion pass, neurons: diff the `nodes` table itself against what
        # this scan actually found on disk — NOT just index_files. Unlike
        # notes, `nodes` rows are also written by index_neuron_json() calls
        # from propose_node/update_node_fields/rebuild_index(), none of which
        # touch index_files. A neuron created or updated that way, whose file
        # is later deleted outside of a refresh_index() cycle, would leave a
        # permanent, unprunable ghost row if this only consulted index_files
        # (as it used to) — diffing the real table catches those too.
        cursor.execute("SELECT id FROM nodes")
        orphaned_ids = {r["id"] for r in cursor.fetchall()} - seen_node_ids
        for node_id in orphaned_ids:
            self.delete_node(node_id)
            stats["deleted_neuron_ids"].append(node_id)
            stats["neurons_deleted"] += 1
        # Drop stale index_files rows for neuron files no longer on disk too
        # (a row with no node_id, e.g. from a failed parse, wouldn't have
        # matched by id above, so this is not fully redundant with it).
        cursor.execute("SELECT rel_path FROM index_files WHERE kind = 'neuron'")
        for row in cursor.fetchall():
            if row["rel_path"] not in seen_rel_paths:
                cursor.execute("DELETE FROM index_files WHERE rel_path = ?", (row["rel_path"],))

        conn.commit()
        return stats

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def rebuild_index_command(full: bool = True) -> None:
    """CLI entry point for rebuild-index."""
    indexer = KnewrallIndexer()
    try:
        indexer.rebuild_index(full)
        print("Index rebuilt successfully.")
    except Exception as e:
        logger.error(f"Failed to rebuild index: {e}")
        raise


if __name__ == "__main__":
    # For testing
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "rebuild-index":
        rebuild_index_command()
    else:
        print("Usage: python knewrall_indexer.py rebuild-index")