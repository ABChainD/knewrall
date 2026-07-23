"""
Knewrall CodeGraph Module

Parses code repositories under projects/ (or any configured root) using
tree-sitter, storing the resulting symbol/call/import/inheritance graph in a
dedicated, ephemeral SQLite database (.knewrall/codegraph.db).

This DB is a disposable cache — delete it and call rebuild_code_index() to
restore from source.  Nothing here writes to the files under projects/.

Stable symbol id format: "<repo_rel_path>::<qualified_name>"
e.g.  "src/foo.py::MyClass.my_method"
"""

import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .paths import get_root

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

DEFAULT_CODEGRAPH_DB = get_root() / ".knewrall" / "codegraph.db"
DEFAULT_PROJECTS_DIR = get_root() / "projects"

# Languages we support, mapped to their file extensions and query file names.
SUPPORTED_LANGUAGES: Dict[str, List[str]] = {
    "python": [".py"],
    "javascript": [".js", ".mjs", ".cjs"],
    "typescript": [".ts", ".tsx"],
}

# ── Tree-sitter helpers ───────────────────────────────────────────────────────

def _get_parser(lang: str):
    """Return a configured tree-sitter Parser for a supported language name."""
    try:
        from tree_sitter_language_pack import get_parser
        return get_parser(lang)
    except ImportError as e:
        raise ImportError(
            "tree-sitter-language-pack is required for code graph indexing. "
            "Install with: pip install tree-sitter tree-sitter-language-pack"
        ) from e


def _file_hash(path: Path) -> str:
    """SHA-256 hash of file content for incremental change detection."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _symbol_id(repo_rel_path: str, qualified_name: str) -> str:
    """Stable, deterministic symbol id."""
    return f"{repo_rel_path}::{qualified_name}"


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS code_repos (
    name        TEXT PRIMARY KEY,
    root_path   TEXT NOT NULL,
    indexed_at  TEXT
);

CREATE TABLE IF NOT EXISTS code_files (
    file_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    lang        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    indexed_at  TEXT NOT NULL,
    UNIQUE (repo, rel_path)
);

CREATE TABLE IF NOT EXISTS code_symbols (
    symbol_id       TEXT PRIMARY KEY,
    repo            TEXT NOT NULL,
    rel_path        TEXT NOT NULL,
    lang            TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- function | class | method | module
    name            TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,
    class_name      TEXT,            -- parent class for methods
    signature       TEXT,
    docstring       TEXT,
    start_line      INTEGER,
    end_line        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sym_repo ON code_symbols(repo);
CREATE INDEX IF NOT EXISTS idx_sym_path ON code_symbols(repo, rel_path);
CREATE INDEX IF NOT EXISTS idx_sym_name ON code_symbols(name);
CREATE INDEX IF NOT EXISTS idx_sym_kind ON code_symbols(kind);

CREATE TABLE IF NOT EXISTS code_edges (
    edge_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    src_symbol_id   TEXT NOT NULL,
    dst_symbol_id   TEXT,            -- NULL when target unresolved
    dst_name        TEXT NOT NULL,   -- raw name from source
    edge_type       TEXT NOT NULL,   -- call | import | inherits
    repo            TEXT NOT NULL,
    rel_path        TEXT NOT NULL,
    line            INTEGER
);

CREATE INDEX IF NOT EXISTS idx_edge_src  ON code_edges(src_symbol_id);
CREATE INDEX IF NOT EXISTS idx_edge_dst  ON code_edges(dst_symbol_id);
CREATE INDEX IF NOT EXISTS idx_edge_type ON code_edges(edge_type);

CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
    symbol_id UNINDEXED,
    repo,
    name,
    qualified_name,
    kind,
    signature,
    docstring,
    content=code_symbols,
    content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS code_symbols_ai AFTER INSERT ON code_symbols BEGIN
    INSERT INTO code_fts(rowid, symbol_id, repo, name, qualified_name, kind, signature, docstring)
    VALUES (new.rowid, new.symbol_id, new.repo, new.name, new.qualified_name, new.kind, new.signature, new.docstring);
END;

CREATE TRIGGER IF NOT EXISTS code_symbols_ad AFTER DELETE ON code_symbols BEGIN
    INSERT INTO code_fts(code_fts, rowid, symbol_id, repo, name, qualified_name, kind, signature, docstring)
    VALUES ('delete', old.rowid, old.symbol_id, old.repo, old.name, old.qualified_name, old.kind, old.signature, old.docstring);
END;
"""


# ── Simple tree-sitter AST walker (tree-sitter 0.25+ API) ────────────────────
#
# tree-sitter 0.25 changed almost every attribute to a method call:
#   node.type          → node.kind()
#   node.start_byte    → node.start_byte()
#   node.end_byte      → node.end_byte()
#   node.start_point   → node.start_position()  (returns Point with .row/.column)
#   node.children      → [node.child(i) for i in range(node.child_count())]
#   tree.root_node     → tree.root_node()
#   parser.parse(bytes)→ parser.parse(str)

def _node_text(node, source_bytes: bytes) -> str:
    """Extract a node's text by tree-sitter's byte offsets.

    start_byte()/end_byte() are UTF-8 BYTE offsets, not character offsets —
    slicing a pre-decoded `str` with them silently corrupts everything after
    the first multi-byte character in the file (accented text, em dashes,
    this codebase's own '──' box-drawing comment separators: each is 2-3
    bytes but 1 character, so byte and char offsets drift apart). Slice the
    original bytes first, then decode just that slice.
    """
    return source_bytes[node.start_byte():node.end_byte()].decode("utf-8", errors="replace")


def _node_children(node) -> list:
    return [node.child(i) for i in range(node.child_count())]


def _first_child_by_type(node, type_name: str):
    for child in _node_children(node):
        if child.kind() == type_name:
            return child
    return None


_STRING_KINDS = frozenset(("string", "string_literal", "concatenated_string"))


def _extract_string(node, source_bytes: bytes) -> Optional[str]:
    """Strip quotes from a string node text, return clean content."""
    raw = _node_text(node, source_bytes).strip()
    for q in ('"""', "'''", '"', "'"):
        if raw.startswith(q) and raw.endswith(q) and len(raw) > 2 * len(q):
            return raw[len(q):-len(q)].strip()
    return raw if raw else None


def _first_docstring(body_node, source_bytes: bytes) -> Optional[str]:
    """Return the first string literal in a block/suite as a docstring.

    tree-sitter 0.25 may surface the string as a direct child of the block
    (not wrapped in expression_statement), so we check both forms.
    """
    if body_node is None:
        return None
    for child in _node_children(body_node):
        k = child.kind()
        if k in _STRING_KINDS:
            return _extract_string(child, source_bytes)
        if k == "expression_statement":
            for sub in _node_children(child):
                if sub.kind() in _STRING_KINDS:
                    return _extract_string(sub, source_bytes)
            return None  # first statement isn't a string — stop
        # skip punctuation / whitespace tokens (kind length <= 1 or pure whitespace)
        if len(k) > 1:
            return None  # first real statement is not a string
    return None


# ── Python extractor (pure AST walk — no .scm queries needed at runtime) ─────

class _PythonExtractor:
    """Extract symbols, calls, imports, and inheritance from Python source."""

    def __init__(self, source: bytes, parser):
        self.source_bytes = source
        self.source_str = source.decode("utf-8", errors="replace")
        self.tree = parser.parse(self.source_str)
        self.symbols: List[Dict] = []
        self.edges: List[Dict] = []

    def extract(self):
        self._walk_module(self.tree.root_node())
        return self.symbols, self.edges

    def _text(self, node) -> str:
        return _node_text(node, self.source_bytes)

    def _line(self, node) -> int:
        return node.start_position().row + 1

    def _walk_module(self, mod_node, class_ctx=None):
        for child in _node_children(mod_node):
            t = child.kind()
            if t == "function_definition":
                self._handle_function(child, class_ctx)
            elif t == "decorated_definition":
                inner = _first_child_by_type(child, "function_definition") or \
                        _first_child_by_type(child, "class_definition")
                if inner:
                    if inner.kind() == "function_definition":
                        self._handle_function(inner, class_ctx)
                    else:
                        self._handle_class(inner)
            elif t == "class_definition":
                self._handle_class(child)
            elif t in ("import_statement", "import_from_statement"):
                self._handle_import(child)

    def _handle_function(self, node, class_ctx=None):
        name_node = _first_child_by_type(node, "identifier")
        if not name_node:
            return
        name = self._text(name_node)
        params_node = _first_child_by_type(node, "parameters")
        sig = f"def {name}({self._text(params_node) if params_node else ''})"
        body = _first_child_by_type(node, "block")
        doc = _first_docstring(body, self.source_bytes)
        qname = f"{class_ctx}.{name}" if class_ctx else name
        kind = "method" if class_ctx else "function"
        self.symbols.append({
            "kind": kind, "name": name, "qualified_name": qname,
            "class_name": class_ctx, "signature": sig, "docstring": doc,
            "start_line": self._line(node), "end_line": node.end_position().row + 1,
        })
        if body:
            self._collect_calls(body, qname)

    def _handle_class(self, node):
        name_node = _first_child_by_type(node, "identifier")
        if not name_node:
            return
        name = self._text(name_node)
        body = _first_child_by_type(node, "block")
        doc = _first_docstring(body, self.source_bytes)
        self.symbols.append({
            "kind": "class", "name": name, "qualified_name": name,
            "class_name": None, "signature": None, "docstring": doc,
            "start_line": self._line(node), "end_line": node.end_position().row + 1,
        })
        # superclasses → inheritance edges
        args_node = _first_child_by_type(node, "argument_list")
        if args_node:
            for arg in _node_children(args_node):
                if arg.kind() == "identifier":
                    self.edges.append({
                        "src_qualified": name, "dst_name": self._text(arg),
                        "edge_type": "inherits", "line": self._line(node),
                    })
        if body:
            self._walk_module(body, class_ctx=name)

    def _handle_import(self, node):
        ntype = node.kind()
        if ntype == "import_statement":
            for child in _node_children(node):
                if child.kind() == "dotted_name":
                    self.edges.append({
                        "src_qualified": None, "dst_name": self._text(child),
                        "edge_type": "import", "line": self._line(node),
                    })
        elif ntype == "import_from_statement":
            module = None
            for child in _node_children(node):
                ck = child.kind()
                if ck == "dotted_name" and module is None:
                    module = self._text(child)
                elif ck in ("dotted_name", "identifier") and module:
                    self.edges.append({
                        "src_qualified": None, "dst_name": f"{module}.{self._text(child)}",
                        "edge_type": "import", "line": self._line(node),
                    })
            if module and not any(e["dst_name"].startswith(module) and "." in e["dst_name"]
                                  for e in self.edges[-5:]):
                self.edges.append({
                    "src_qualified": None, "dst_name": module,
                    "edge_type": "import", "line": self._line(node),
                })

    def _collect_calls(self, node, caller_qname: str):
        if node.kind() == "call":
            func = None
            for child in _node_children(node):
                ck = child.kind()
                if ck == "identifier":
                    func = self._text(child)
                    break
                elif ck == "attribute":
                    for sub in _node_children(child):
                        if sub.kind() == "identifier":
                            func = self._text(sub)
                    break
            if func:
                self.edges.append({
                    "src_qualified": caller_qname, "dst_name": func,
                    "edge_type": "call", "line": self._line(node),
                })
        for child in _node_children(node):
            self._collect_calls(child, caller_qname)


# ── JS/TS extractor ───────────────────────────────────────────────────────────

class _JsExtractor:
    """Generic JS/TS extractor using simple AST walk."""

    def __init__(self, source: bytes, parser):
        self.source_bytes = source
        self.source_str = source.decode("utf-8", errors="replace")
        self.tree = parser.parse(self.source_str)
        self.symbols: List[Dict] = []
        self.edges: List[Dict] = []

    def extract(self):
        self._walk(self.tree.root_node(), class_ctx=None)
        return self.symbols, self.edges

    def _text(self, node) -> str:
        return _node_text(node, self.source_bytes)

    def _line(self, node) -> int:
        return node.start_position().row + 1

    def _walk(self, node, class_ctx=None):
        t = node.kind()
        if t in ("function_declaration", "function_definition"):
            name_node = _first_child_by_type(node, "identifier")
            if name_node:
                name = self._text(name_node)
                qname = f"{class_ctx}.{name}" if class_ctx else name
                params = _first_child_by_type(node, "formal_parameters")
                sig = f"function {name}({self._text(params) if params else ''})"
                self.symbols.append({
                    "kind": "method" if class_ctx else "function",
                    "name": name, "qualified_name": qname, "class_name": class_ctx,
                    "signature": sig, "docstring": None,
                    "start_line": self._line(node), "end_line": node.end_position().row + 1,
                })
                body = _first_child_by_type(node, "statement_block")
                if body:
                    self._collect_calls(body, qname)
                return  # don't re-walk children
        elif t == "class_declaration":
            name_node = _first_child_by_type(node, "identifier") or \
                        _first_child_by_type(node, "type_identifier")
            if name_node:
                name = self._text(name_node)
                self.symbols.append({
                    "kind": "class", "name": name, "qualified_name": name,
                    "class_name": None, "signature": None, "docstring": None,
                    "start_line": self._line(node), "end_line": node.end_position().row + 1,
                })
                # superclass — look for class_heritage containing identifier/type_identifier
                for child in _node_children(node):
                    if child.kind() == "class_heritage":
                        for sub in _node_children(child):
                            if sub.kind() in ("identifier", "type_identifier"):
                                self.edges.append({
                                    "src_qualified": name, "dst_name": self._text(sub),
                                    "edge_type": "inherits", "line": self._line(node),
                                })
                                break
                        break
                body = _first_child_by_type(node, "class_body")
                if body:
                    self._walk(body, class_ctx=name)
                return
        elif t == "method_definition":
            name_node = _first_child_by_type(node, "property_identifier")
            if name_node and class_ctx:
                name = self._text(name_node)
                qname = f"{class_ctx}.{name}"
                params = _first_child_by_type(node, "formal_parameters")
                sig = f"{name}({self._text(params) if params else ''})"
                self.symbols.append({
                    "kind": "method", "name": name, "qualified_name": qname,
                    "class_name": class_ctx, "signature": sig, "docstring": None,
                    "start_line": self._line(node), "end_line": node.end_position().row + 1,
                })
                body = _first_child_by_type(node, "statement_block")
                if body:
                    self._collect_calls(body, qname)
                return
        elif t == "import_statement":
            src = None
            for child in _node_children(node):
                if child.kind() == "string":
                    src = self._text(child).strip('"\'')
            if src:
                self.edges.append({
                    "src_qualified": None, "dst_name": src,
                    "edge_type": "import", "line": self._line(node),
                })

        for child in _node_children(node):
            self._walk(child, class_ctx=class_ctx)

    def _collect_calls(self, node, caller_qname: str):
        if node.kind() == "call_expression":
            func = _first_child_by_type(node, "identifier")
            if not func:
                for child in _node_children(node):
                    if child.kind() == "member_expression":
                        children_rev = list(reversed(_node_children(child)))
                        for sub in children_rev:
                            if sub.kind() == "property_identifier":
                                func = sub
                                break
                        break
            if func:
                self.edges.append({
                    "src_qualified": caller_qname, "dst_name": self._text(func),
                    "edge_type": "call", "line": self._line(node),
                })
        for child in _node_children(node):
            self._collect_calls(child, caller_qname)


# ── Extractor dispatcher ──────────────────────────────────────────────────────

def _extract_file(path: Path, lang: str) -> Tuple[List[Dict], List[Dict]]:
    source = path.read_bytes()
    parser = _get_parser(lang)
    if lang == "python":
        ext = _PythonExtractor(source, parser)
    else:
        ext = _JsExtractor(source, parser)
    return ext.extract()


def _detect_lang(path: Path) -> Optional[str]:
    suffix = path.suffix.lower()
    for lang, exts in SUPPORTED_LANGUAGES.items():
        if suffix in exts:
            return lang
    return None


# ── CodeGraph indexer ─────────────────────────────────────────────────────────

class KnewrallCodeGraph:
    """Manages the code symbol/edge graph in .knewrall/codegraph.db."""

    def __init__(self, db_path: Optional[Path] = None,
                 projects_dir: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_CODEGRAPH_DB
        self.projects_dir = projects_dir or DEFAULT_PROJECTS_DIR
        self.conn: Optional[sqlite3.Connection] = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Connection ─────────────────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        if self.conn is None:
            self.conn = sqlite3.connect(str(self.db_path))
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
        return self.conn

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    # ── Schema ─────────────────────────────────────────────────────────────

    def create_schema(self, drop: bool = False):
        conn = self.connect()
        if drop:
            # executescript() commits any open transaction first, so drop manually
            for tbl in ("code_fts", "code_edges", "code_symbols", "code_files", "code_repos"):
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            conn.commit()
        # executescript handles multi-statement SQL including BEGIN...END triggers
        conn.executescript(SCHEMA_SQL)

    # ── Per-file indexing ───────────────────────────────────────────────────

    def _index_file(self, repo: str, rel_path: str, abs_path: Path, lang: str,
                    force: bool = False) -> bool:
        conn = self.connect()
        content_hash = _file_hash(abs_path)

        # incremental: skip if unchanged
        if not force:
            row = conn.execute(
                "SELECT content_hash FROM code_files WHERE repo=? AND rel_path=?",
                (repo, rel_path)
            ).fetchone()
            if row and row["content_hash"] == content_hash:
                return False  # unchanged

        try:
            symbols, edges = _extract_file(abs_path, lang)
        except Exception as e:
            logger.warning(f"Failed to parse {abs_path}: {e}")
            return False

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        # Remove old data for this file. code_edges has its own repo/rel_path
        # columns, so delete by those directly rather than by src_symbol_id —
        # import/inherits edges are keyed to a synthetic "<module>" id that is
        # never itself a row in code_symbols, so a symbol-id-based delete
        # would never sweep them: every re-index of the same file would just
        # keep ACCUMULATING duplicate import/inherits edges alongside the
        # fresh ones, forever, rather than replacing them.
        conn.execute("DELETE FROM code_edges WHERE repo=? AND rel_path=?", (repo, rel_path))
        conn.execute("DELETE FROM code_symbols WHERE repo=? AND rel_path=?", (repo, rel_path))

        # upsert file record
        conn.execute("""
            INSERT INTO code_files (repo, rel_path, lang, content_hash, indexed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(repo, rel_path) DO UPDATE SET
                lang=excluded.lang, content_hash=excluded.content_hash,
                indexed_at=excluded.indexed_at
        """, (repo, rel_path, lang, content_hash, now))

        # insert symbols
        sym_id_map: Dict[str, str] = {}  # qualified_name → symbol_id
        for sym in symbols:
            sid = _symbol_id(rel_path, sym["qualified_name"])
            sym_id_map[sym["qualified_name"]] = sid
            conn.execute("""
                INSERT OR REPLACE INTO code_symbols
                (symbol_id, repo, rel_path, lang, kind, name, qualified_name,
                 class_name, signature, docstring, start_line, end_line)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (sid, repo, rel_path, lang, sym["kind"], sym["name"],
                  sym["qualified_name"], sym.get("class_name"),
                  sym.get("signature"), sym.get("docstring"),
                  sym.get("start_line"), sym.get("end_line")))

        # insert edges
        module_sid = _symbol_id(rel_path, "<module>")
        for edge in edges:
            src_qname = edge["src_qualified"]
            src_sid = sym_id_map.get(src_qname) if src_qname else module_sid
            if src_sid is None:
                src_sid = module_sid
            dst_name = edge["dst_name"]
            dst_sid = sym_id_map.get(dst_name)  # may be None (cross-file)
            conn.execute("""
                INSERT INTO code_edges
                (src_symbol_id, dst_symbol_id, dst_name, edge_type, repo, rel_path, line)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (src_sid, dst_sid, dst_name, edge["edge_type"], repo, rel_path,
                  edge.get("line")))

        conn.commit()
        return True

    # ── Repo indexing ───────────────────────────────────────────────────────

    def index_repo(self, repo_path: Path, repo_name: Optional[str] = None,
                   languages: Optional[List[str]] = None, force: bool = False) -> Dict:
        if not repo_path.is_dir():
            raise ValueError(f"Repo path not found: {repo_path}")
        repo_name = repo_name or repo_path.name
        langs = set(languages or list(SUPPORTED_LANGUAGES.keys()))

        self.create_schema()
        conn = self.connect()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO code_repos (name, root_path, indexed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET root_path=excluded.root_path, indexed_at=excluded.indexed_at
        """, (repo_name, str(repo_path), now))
        conn.commit()

        counts = {"indexed": 0, "skipped": 0, "errors": 0}
        for lang in langs:
            for ext in SUPPORTED_LANGUAGES.get(lang, []):
                for abs_path in repo_path.rglob(f"*{ext}"):
                    # skip common noise dirs
                    parts = abs_path.parts
                    if any(p in parts for p in (
                        "node_modules", "__pycache__", ".git", ".venv", "venv",
                        "dist", "build", ".mypy_cache", ".pytest_cache"
                    )):
                        continue
                    rel_path = abs_path.relative_to(repo_path).as_posix()
                    try:
                        changed = self._index_file(repo_name, rel_path, abs_path, lang, force)
                        if changed:
                            counts["indexed"] += 1
                        else:
                            counts["skipped"] += 1
                    except Exception as e:
                        logger.warning(f"Error indexing {abs_path}: {e}")
                        counts["errors"] += 1

        logger.info(f"Code graph: {repo_name} indexed={counts['indexed']} "
                    f"skipped={counts['skipped']} errors={counts['errors']}")
        return counts

    def rebuild_code_index(self, roots: Optional[List[Path]] = None,
                           languages: Optional[List[str]] = None) -> Dict:
        """Drop and rebuild the full code graph from configured roots."""
        self.create_schema(drop=True)
        roots = roots or self._default_roots()
        totals = {"repos": 0, "indexed": 0, "skipped": 0, "errors": 0}
        for root in roots:
            if not root.is_dir():
                logger.debug(f"Skipping non-existent root: {root}")
                continue
            counts = self.index_repo(root, languages=languages, force=True)
            totals["repos"] += 1
            for k in ("indexed", "skipped", "errors"):
                totals[k] += counts[k]
        return totals

    def _default_roots(self) -> List[Path]:
        """Return repo-like subdirectories of projects/ as default roots."""
        if not self.projects_dir.is_dir():
            return []
        return [p for p in self.projects_dir.iterdir()
                if p.is_dir() and p.name != "README.md"]

    # ── Query helpers ───────────────────────────────────────────────────────

    def search_symbols(self, query: str, limit: int = 20) -> List[Dict]:
        """FTS5 full-text search over symbol names, qualified names, and docstrings."""
        conn = self.connect()
        # escape FTS5 special chars
        safe = re.sub(r'[^\w\s]', ' ', query)
        try:
            rows = conn.execute("""
                SELECT s.symbol_id, s.repo, s.rel_path, s.kind, s.name,
                       s.qualified_name, s.signature, s.docstring, s.start_line
                FROM code_fts f
                JOIN code_symbols s USING (rowid)
                WHERE code_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (safe, limit)).fetchall()
        except sqlite3.OperationalError:
            # fallback to LIKE if FTS table not populated
            rows = conn.execute("""
                SELECT symbol_id, repo, rel_path, kind, name, qualified_name,
                       signature, docstring, start_line
                FROM code_symbols
                WHERE name LIKE ? OR qualified_name LIKE ?
                LIMIT ?
            """, (f"%{query}%", f"%{query}%", limit)).fetchall()
        return [dict(r) for r in rows]

    def get_symbol(self, symbol_id: str) -> Optional[Dict]:
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM code_symbols WHERE symbol_id=?", (symbol_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_definitions(self, name: str, repo: Optional[str] = None) -> List[Dict]:
        conn = self.connect()
        q = "SELECT * FROM code_symbols WHERE (name=? OR qualified_name=?)"
        params: list = [name, name]
        if repo:
            q += " AND repo=?"
            params.append(repo)
        return [dict(r) for r in conn.execute(q, params).fetchall()]

    def get_callers(self, symbol_id: str) -> List[Dict]:
        """Return symbols that call this symbol."""
        conn = self.connect()
        rows = conn.execute("""
            SELECT s.* FROM code_edges e
            JOIN code_symbols s ON s.symbol_id = e.src_symbol_id
            WHERE e.dst_symbol_id=? AND e.edge_type='call'
        """, (symbol_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_callees(self, symbol_id: str) -> List[Dict]:
        """Return symbols called by this symbol (resolved only)."""
        conn = self.connect()
        rows = conn.execute("""
            SELECT s.*, e.line FROM code_edges e
            JOIN code_symbols s ON s.symbol_id = e.dst_symbol_id
            WHERE e.src_symbol_id=? AND e.edge_type='call'
        """, (symbol_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_imports(self, rel_path: str, repo: Optional[str] = None) -> List[Dict]:
        """Return import edges from a given file."""
        conn = self.connect()
        q = "SELECT * FROM code_edges WHERE rel_path=? AND edge_type='import'"
        params: list = [rel_path]
        if repo:
            q += " AND repo=?"
            params.append(repo)
        return [dict(r) for r in conn.execute(q, params).fetchall()]

    def get_subclasses(self, parent_name: str) -> List[Dict]:
        """Return symbols that inherit from this class."""
        conn = self.connect()
        rows = conn.execute("""
            SELECT s.* FROM code_edges e
            JOIN code_symbols s ON s.symbol_id = e.src_symbol_id
            WHERE e.dst_name=? AND e.edge_type='inherits'
        """, (parent_name,)).fetchall()
        return [dict(r) for r in rows]

    def resolve_code_refs(self, code_refs: List[Dict]) -> List[Dict]:
        """
        Given a neuron's code_refs list, look up each symbol in the DB.
        Returns enriched dicts with a 'resolved' key (True/False).
        Warns on misses — never raises.
        """
        results = []
        for ref in code_refs:
            sid = ref.get("symbol_id", "")
            sym = self.get_symbol(sid)
            if sym:
                results.append({**ref, **sym, "resolved": True})
            else:
                logger.warning(f"code_ref not found in codegraph.db: {sid!r}")
                results.append({**ref, "resolved": False})
        return results

    def stats(self) -> Dict:
        conn = self.connect()
        repos   = conn.execute("SELECT COUNT(*) FROM code_repos").fetchone()[0]
        symbols = conn.execute("SELECT COUNT(*) FROM code_symbols").fetchone()[0]
        edges   = conn.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0]
        files   = conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0]
        return {"repos": repos, "files": files, "symbols": symbols, "edges": edges}
