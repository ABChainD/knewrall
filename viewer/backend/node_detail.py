"""Builds the /api/node/{id} payload: the full raw neuron body plus every
resolved, clickable connection into the rest of the graph (structured links
both directions, note wikilinks, and free-text entity mentions)."""

from __future__ import annotations

from . import engine, linkify
from .models import EntitySpan, LinkedNote, NodeDetailResp, ResolvedLink


class NodeNotFound(Exception):
    pass


def _note_rel_path(conn, source_id: str) -> str | None:
    row = conn.execute(
        "SELECT rel_path FROM index_files WHERE kind = 'note' AND node_id = ?",
        (source_id,),
    ).fetchone()
    return row["rel_path"] if row else None


def _flatten_property_texts(properties: dict) -> dict[str, str]:
    """Each property key's entries collapsed to one string per key, so each
    gets its own linkify "section" (first-occurrence-per-section applies per
    property, not globally)."""
    out: dict[str, str] = {}
    for key, entries in (properties or {}).items():
        if not isinstance(entries, list):
            continue
        values = [str(e.get("value")) for e in entries if isinstance(e, dict) and e.get("value") is not None]
        if values:
            out[f"property:{key}"] = "; ".join(values)
    return out


def build_node_detail(node_id: str) -> NodeDetailResp:
    try:
        body = engine.load_node(node_id)
    except FileNotFoundError:
        raise NodeNotFound(node_id)

    # ── Outbound: this neuron's own links[] array ──────────────────────────
    resolved_links: list[ResolvedLink] = []
    outbound_targets: dict[tuple[str, str], bool] = {}
    for link in body.get("links", []) or []:
        target_id = link.get("target_id")
        if not target_id:
            continue
        target_row = engine.get_node_by_id(target_id)
        resolved_links.append(ResolvedLink(
            direction=link.get("direction", "outbound"),
            predicate=link.get("predicate", ""),
            certainty=link.get("certainty", "confirmed"),
            node_id=target_id,
            name=target_row["canonical_name"] if target_row else None,
            type=target_row["type"] if target_row else None,
        ))
        outbound_targets[(target_id, link.get("predicate", ""))] = True

    # ── Inbound: other neurons whose own links[] name this one as target ───
    # (see node_detail module notes: "outbound"-only links live solely in the
    # source's file, so this edge-table query is the only way to discover
    # them from here). Skip pairs already shown via a bidirectional outbound
    # entry above, to avoid displaying the same relationship twice.
    for edge in engine.get_edges(target_id=node_id, link_type="node_link"):
        source_id = edge["source_id"]
        if source_id == node_id:
            continue
        if (source_id, edge["predicate"]) in outbound_targets:
            continue
        source_row = engine.get_node_by_id(source_id)
        resolved_links.append(ResolvedLink(
            direction="inbound",
            predicate=edge["predicate"],
            certainty=edge["certainty"],
            node_id=source_id,
            name=source_row["canonical_name"] if source_row else None,
            type=source_row["type"] if source_row else None,
        ))

    # ── Companion markdown ──────────────────────────────────────────────────
    companion_md = None
    md_path = engine.companion_md_path(node_id)
    if md_path.is_file():
        companion_md = md_path.read_text(encoding="utf-8")

    # ── Notes that wikilink to this neuron ──────────────────────────────────
    linked_notes: list[LinkedNote] = []
    conn = engine.index_db_connection()
    try:
        for edge in engine.get_edges(target_id=node_id, link_type="wikilink"):
            rel_path = _note_rel_path(conn, edge["source_id"])
            if rel_path:
                linked_notes.append(LinkedNote(rel_path=rel_path))
    finally:
        conn.close()

    # ── Free-text entity linkification, one section at a time ──────────────
    name_index = linkify.build_name_index(exclude_id=node_id)
    entity_spans: dict[str, list[EntitySpan]] = {}

    for key, value in (body.get("descriptions", {}) or {}).items():
        if isinstance(value, str) and value:
            spans = linkify.find_entity_spans(value, name_index)
            if spans:
                entity_spans[f"description:{key}"] = [EntitySpan(**s) for s in spans]

    for section, text in _flatten_property_texts(body.get("properties", {})).items():
        spans = linkify.find_entity_spans(text, name_index)
        if spans:
            entity_spans[section] = [EntitySpan(**s) for s in spans]

    if companion_md:
        spans = linkify.find_entity_spans(companion_md, name_index)
        if spans:
            entity_spans["companion_md"] = [EntitySpan(**s) for s in spans]

    return NodeDetailResp(
        id=node_id,
        body=body,
        resolved_links=resolved_links,
        companion_md=companion_md,
        linked_notes=linked_notes,
        entity_spans=entity_spans,
    )
