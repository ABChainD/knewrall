"""Resolves entity mentions inside free-text neuron content into clickable
graph-navigable spans. Convenience layer only — structured `links[]` and note
wikilinks remain the authoritative link surface (see node_detail.py).

Heuristic (per plan review): match canonical names + aliases, >=4 chars,
word-boundary, longest-match-wins, first occurrence per section (each call to
find_entity_spans is one "section"), never inside code spans/blocks or URLs.
"""

from __future__ import annotations

import re
from typing import Optional

from . import engine

_MIN_NAME_LEN = 4
_CODE_OR_URL = re.compile(r"`[^`]*`|https?://\S+")


def build_name_index(exclude_id: Optional[str] = None) -> list[tuple[str, str, str]]:
    """Return [(name, node_id, type), ...] sorted longest-name-first, so a
    longer match is always attempted before a shorter one that could be a
    substring of it (e.g. "Grupo Roca" before "Roca")."""
    conn = engine.index_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, canonical_name, type FROM nodes"
            + (" WHERE id != ?" if exclude_id else ""),
            (exclude_id,) if exclude_id else (),
        ).fetchall()
        alias_rows = conn.execute(
            "SELECT a.alias, n.id, n.type FROM aliases a JOIN nodes n ON n.id = a.node_id"
            + (" WHERE n.id != ?" if exclude_id else ""),
            (exclude_id,) if exclude_id else (),
        ).fetchall()
    finally:
        conn.close()

    names: list[tuple[str, str, str]] = []
    for row in rows:
        if row["canonical_name"] and len(row["canonical_name"]) >= _MIN_NAME_LEN:
            names.append((row["canonical_name"], row["id"], row["type"]))
    for row in alias_rows:
        if row["alias"] and len(row["alias"]) >= _MIN_NAME_LEN:
            names.append((row["alias"], row["id"], row["type"]))

    names.sort(key=lambda t: len(t[0]), reverse=True)
    return names


def _mask_code_and_urls(text: str) -> str:
    chars = list(text)
    for m in _CODE_OR_URL.finditer(text):
        for i in range(*m.span()):
            chars[i] = " "
    return "".join(chars)


def find_entity_spans(text: str, name_index: list[tuple[str, str, str]]) -> list[dict]:
    """One "section" of free text in, a list of non-overlapping
    {start, end, node_id, name} spans out (sorted by position)."""
    if not text:
        return []

    masked = _mask_code_and_urls(text)
    taken: list[tuple[int, int]] = []
    used_names: set[str] = set()
    spans: list[dict] = []

    for name, node_id, _type in name_index:
        key = name.lower()
        if key in used_names:
            continue
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
        match = pattern.search(masked)
        if not match:
            continue
        start, end = match.span()
        if any(not (end <= t_start or start >= t_end) for t_start, t_end in taken):
            continue  # overlaps a span already claimed by a longer match
        taken.append((start, end))
        used_names.add(key)
        spans.append({"start": start, "end": end, "node_id": node_id, "name": text[start:end]})

    spans.sort(key=lambda s: s["start"])
    return spans
