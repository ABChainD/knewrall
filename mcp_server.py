#!/usr/bin/env python3
"""
Knewrall MCP server (stdio) — minimal read-only surface.

Exposes these tools to MCP clients (e.g. OpenCode):
  - knewrall_search        : full-text search of the SQLite index nodes
  - knewrall_search_graph  : graph search over canonical_name / aliases / tags
  - knewrall_recall        : consolidated retrieval — full neuron bodies +
                              related-link summaries in one call, TOON-encoded
  - knewrall_stats         : node / edge / alias counts
  - knewrall_unfold        : read back folded short-term-memory content (an
                              engram) by key — read-only (marking unfolds/
                              extending ttl_at in session.json is a state
                              mutation of the *ephemeral* store, not the
                              knowledge base itself, so this stays within the
                              server's stated read-only contract). This is the
                              only automation harnesses without Claude-Code-
                              style hooks (e.g. OpenCode) get for the Engram
                              Layer — see short-term-memory-layer-plan.md §2.1.

Writes nothing to the knowledge base; all mutating verbs stay on the CLI.

Run directly:
    python knewrall/mcp_server.py
Registered in opencode.json as a local MCP server.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ── Bootstrap: mirror bin/knewrall.py so the engine resolves the correct KB root
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("KNEWRALL_ROOT", str(ROOT))

# Force UTF-8 stdio so non-ASCII node names/aliases survive the MCP wire on Windows.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from mcp.server.fastmcp import FastMCP

from src.knewrall_middleware import search_graph as _search_graph, recall as _recall, unfold as _unfold
from src.knewrall_indexer import KnewrallIndexer
from src.knewrall_toon import encode as _encode_toon

mcp = FastMCP("knewrall")


def _json(obj) -> str:
    """Serialize to a UTF-8 JSON string; FastMCP tool results are text content."""
    return json.dumps(obj, ensure_ascii=False, default=str)


@mcp.tool()
def knewrall_search(query: str, limit: int = 20) -> str:
    """Full-text search of Knewrall index nodes by query string.

    Args:
        query: Search string (partial, case-insensitive).
        limit: Maximum number of results to return (default 20).

    Returns:
        JSON array of nodes (id, type, canonical_name). Empty array if no matches.
    """
    indexer = KnewrallIndexer()
    try:
        results = indexer.search_nodes(query, limit=limit)
    finally:
        indexer.close()
    return _json(results)


@mcp.tool()
def knewrall_search_graph(query: str) -> str:
    """Search Knewrall nodes by canonical name, aliases, or tags.

    Use this to check for existing entities (people, projects, places, decisions)
    before proposing new ones. Matches are partial and case-insensitive.

    Args:
        query: Search string.

    Returns:
        JSON array of nodes (id, type, canonical_name, aliases, tags,
        full_legal_name). Empty array if no matches.
    """
    return _json(_search_graph(query))


@mcp.tool()
def knewrall_recall(terms: list[str], depth: int = 1) -> str:
    """Consolidated Knewrall retrieval: find, load, and shape matching neurons
    in one call — no follow-up file read needed per match.

    Prefer this over knewrall_search_graph when you actually need the neuron's
    content (descriptions/properties/links), not just its id. Output is
    deliberately bounded (a handful of full bodies plus capped related-link
    summaries) and TOON-encoded, since MCP responses have their own size
    limits — call again with a related name to expand it, rather than
    expecting one call to return the whole neighborhood.

    Args:
        terms: One or more search terms.
        depth: 0 = matched neurons only; 1 (default) = also include a capped
            list of their immediate link targets as light summaries.

    Returns:
        TOON-encoded text: query_terms, matched (full neuron bodies),
        related (summaries), stats, truncated.
    """
    result = _recall(terms, depth=depth)
    return _encode_toon(result)


@mcp.tool()
def knewrall_unfold(
    key: str,
    grep: str | None = None,
    context: int = 0,
    lines: str | None = None,
    head: int | None = None,
    tail: int | None = None,
) -> str:
    """Read back folded short-term-memory content (an "engram") by its
    retrieval key — the Engram Layer's context-folding subsystem.

    Prefer `grep`/`lines` over a bare call: an unwindowed unfold is capped at
    `unfold_max_chars` (enforced here, server-side, since MCP responses are
    injected whole — same cap the CLI's `unfold` applies by default).

    Args:
        key: The engram key from a fold marker (e.g. "9c2b41d7f0").
        grep: Return only matching lines, with `context` lines around each.
        context: Lines of context around each grep match (default 0).
        lines: "A-B" — a byte-cheap line-range slice (1-indexed, inclusive).
        head: Return only the first N lines.
        tail: Return only the last N lines.

    Returns:
        JSON: {"content": str, "truncated": bool, "meta": {...}} on success,
        or {"error": str} if the key doesn't resolve (expired, discarded, or
        never existed on this machine — engrams are per-machine and never
        synced to a different machine or remote task).
    """
    lines_tuple = None
    if lines:
        try:
            a, b = lines.split("-", 1)
            lines_tuple = (int(a), int(b))
        except ValueError:
            return _json({"error": f"lines must be 'A-B' (got {lines!r})"})
    result = _unfold(key, grep=grep, grep_context=context, lines=lines_tuple, head=head, tail=tail)
    if not result["found"]:
        return _json({"error": result["error"]})
    return _json({"content": result["content"], "truncated": result["truncated"], "meta": result["meta"]})


@mcp.tool()
def knewrall_stats() -> str:
    """Return Knewrall index statistics: node, edge, and alias counts.

    Returns:
        JSON object {"nodes": int, "edges": int, "aliases": int}.
    """
    indexer = KnewrallIndexer()
    try:
        conn = indexer.connect()
        cur = conn.cursor()
        counts = {}
        for name in ("nodes", "edges", "aliases"):
            cur.execute(f"SELECT COUNT(*) FROM {name}")
            counts[name] = cur.fetchone()[0]
    finally:
        indexer.close()
    return _json(counts)


if __name__ == "__main__":
    mcp.run()