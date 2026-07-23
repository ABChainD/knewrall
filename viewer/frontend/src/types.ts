// Mirrors backend/models.py field-for-field. snake_case end-to-end, no mapping layer.

export type Pillar = 'Who' | 'What' | 'Where' | 'When' | 'Why' | 'How';

export interface GraphNode {
  id: string;
  type: Pillar;
  name: string;
  aliases: string[];
  tags: string[];
  degree: number;
  preview: string;
  source_node_id: string | null;
  target_node_id: string | null;
}

export interface GraphEdge {
  source: string;
  target: string;
  predicate: string;
  direction: 'outbound' | 'inbound' | 'bidirectional';
  certainty: 'confirmed' | 'rumored' | 'hypothetical' | 'alternative';
}

export interface GraphResp {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface ResolvedLink {
  direction: string;
  predicate: string;
  certainty: string;
  node_id: string;
  name: string | null;
  type: string | null;
}

export interface LinkedNote {
  rel_path: string;
}

export interface EntitySpan {
  start: number;
  end: number;
  node_id: string;
  name: string;
}

export interface NodeDetailResp {
  id: string;
  body: Record<string, unknown>;
  resolved_links: ResolvedLink[];
  companion_md: string | null;
  linked_notes: LinkedNote[];
  entity_spans: Record<string, EntitySpan[]>;
}

export interface SearchResultItem {
  id: string;
  type: Pillar | null;
  canonical_name: string | null;
  aliases: string[];
  tags: string[];
}

export interface SearchResp {
  query: string;
  hybrid_requested: boolean;
  hybrid_applied: boolean;
  groups: Record<string, SearchResultItem[]>;
  flat: SearchResultItem[];
}

export interface StatsResp {
  nodes: number;
  edges: number;
  aliases: number;
  embeddings_total: number;
  embeddings_by_kind: Record<string, number>;
}

export interface HealthResp {
  ok: boolean;
  semantic_available: boolean;
  model: string | null;
  dim: number | null;
}
