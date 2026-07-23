"""Builds the /api/graph payload: one bulk read of index.db, not a per-node
round trip. Fine at 86 nodes today and still fine at 1000+."""

from __future__ import annotations

import json

from . import engine
from .models import GraphEdge, GraphNode, GraphResp

PREVIEW_MAX_CHARS = 160
_DESC_PRECEDENCE = ("conceptual", "physical", "psychological")


def _truncate_on_word_boundary(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > max_chars * 0.6:  # don't chop off most of the string
        cut = cut[:last_space]
    return cut.rstrip() + "…"


def _preview_from_descriptions(descriptions_json: str | None) -> str | None:
    if not descriptions_json:
        return None
    try:
        descriptions = json.loads(descriptions_json)
    except (json.JSONDecodeError, TypeError):
        return None
    for key in _DESC_PRECEDENCE:
        value = descriptions.get(key)
        if value:
            return _truncate_on_word_boundary(value, PREVIEW_MAX_CHARS)
    return None


def build_graph() -> GraphResp:
    conn = engine.index_db_connection()
    try:
        node_rows = conn.execute(
            "SELECT id, type, canonical_name, aliases, tags, descriptions, "
            "source_node_id, target_node_id FROM nodes"
        ).fetchall()
        edge_rows = conn.execute(
            "SELECT source_id, target_id, predicate, direction, certainty "
            "FROM edges WHERE link_type = 'node_link'"
        ).fetchall()
    finally:
        conn.close()

    node_ids = {row["id"] for row in node_rows}
    degree: dict[str, int] = {nid: 0 for nid in node_ids}

    edges: list[GraphEdge] = []
    first_edge_summary: dict[str, str] = {}
    for row in edge_rows:
        source, target = row["source_id"], row["target_id"]
        if source not in node_ids or target not in node_ids:
            continue  # dangling endpoint (stale row, deleted node not yet pruned)
        degree[source] = degree.get(source, 0) + 1
        degree[target] = degree.get(target, 0) + 1
        edges.append(GraphEdge(
            source=source, target=target, predicate=row["predicate"],
            direction=row["direction"], certainty=row["certainty"],
        ))
        first_edge_summary.setdefault(source, f"{row['predicate']} → {target}")

    nodes: list[GraphNode] = []
    for row in node_rows:
        preview = _preview_from_descriptions(row["descriptions"])
        if not preview:
            summary = first_edge_summary.get(row["id"])
            preview = _truncate_on_word_boundary(summary, PREVIEW_MAX_CHARS) if summary else ""
        nodes.append(GraphNode(
            id=row["id"],
            type=row["type"],
            name=row["canonical_name"],
            aliases=json.loads(row["aliases"]) if row["aliases"] else [],
            tags=json.loads(row["tags"]) if row["tags"] else [],
            degree=degree.get(row["id"], 0),
            preview=preview,
            source_node_id=row["source_node_id"],
            target_node_id=row["target_node_id"],
        ))

    return GraphResp(nodes=nodes, edges=edges)
