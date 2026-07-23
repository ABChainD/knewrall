"""Typed response shapes for every /api/* route. Field names are snake_case
end-to-end (mirrored verbatim by the frontend's types.ts) so no request/response
mapping layer is needed on either side."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

PILLARS = ("Who", "What", "Where", "When", "Why", "How")


class GraphNode(BaseModel):
    id: str
    type: str
    name: str
    aliases: list[str] = []
    tags: list[str] = []
    degree: int = 0
    preview: str = ""
    source_node_id: Optional[str] = None
    target_node_id: Optional[str] = None


class GraphEdge(BaseModel):
    source: str
    target: str
    predicate: str
    direction: str
    certainty: str


class GraphResp(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class ResolvedLink(BaseModel):
    direction: str
    predicate: str
    certainty: str
    node_id: str
    name: Optional[str] = None
    type: Optional[str] = None


class LinkedNote(BaseModel):
    rel_path: str


class EntitySpan(BaseModel):
    start: int
    end: int
    node_id: str
    name: str


class NodeDetailResp(BaseModel):
    id: str
    body: dict[str, Any]
    resolved_links: list[ResolvedLink]
    companion_md: Optional[str] = None
    linked_notes: list[LinkedNote] = []
    entity_spans: dict[str, list[EntitySpan]] = {}


class SearchResultItem(BaseModel):
    id: str
    type: Optional[str] = None
    canonical_name: Optional[str] = None
    aliases: list[str] = []
    tags: list[str] = []


class SearchResp(BaseModel):
    query: str
    hybrid_requested: bool
    hybrid_applied: bool
    groups: dict[str, list[SearchResultItem]]
    flat: list[SearchResultItem]


class StatsResp(BaseModel):
    nodes: int
    edges: int
    aliases: int
    embeddings_total: int = 0
    embeddings_by_kind: dict[str, int] = {}


class HealthResp(BaseModel):
    ok: bool
    semantic_available: bool
    model: Optional[str] = None
    dim: Optional[int] = None
