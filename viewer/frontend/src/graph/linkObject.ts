// Edge color/width/particles by predicate, certainty, and pillar of the two
// endpoints. Also owns hover-highlight state for edges (3d-force-graph
// re-invokes these accessors when you re-set them with the same reference,
// which is how `refreshHighlight` forces a repaint after a hover change).
//
// Simplification: true dashed geometry for hypothetical/alternative
// certainty would need a custom linkThreeObject (losing the built-in
// directional-particle support in the process) — instead those links render
// at reduced opacity/width, which reads as "less certain" just as clearly
// for this dataset's scale. Noted here rather than silently dropped.

import type { GraphEdge, GraphNode } from '../types';
import { EDGE_STYLE, pillarColor } from '../theme';

export const REIFICATION_PREDICATE = 'reification';

export type LinkDatum = GraphEdge & {
  source: GraphNode | string;
  target: GraphNode | string;
  _reification?: boolean;
};

function endpointNode(end: GraphNode | string): GraphNode | null {
  return typeof end === 'object' ? end : null;
}

function blendHex(a: string, b: string): string {
  const pa = parseInt(a.slice(1), 16);
  const pb = parseInt(b.slice(1), 16);
  const r = Math.round(((pa >> 16) + (pb >> 16)) / 2);
  const g = Math.round((((pa >> 8) & 0xff) + ((pb >> 8) & 0xff)) / 2);
  const bl = Math.round(((pa & 0xff) + (pb & 0xff)) / 2);
  return `rgb(${r}, ${g}, ${bl})`;
}

const state = {
  hoveredNodeId: null as string | null,
  selectedNodeId: null as string | null,
  visibleNodeIds: null as Set<string> | null,
};

function isHighlighted(link: LinkDatum): boolean {
  const s = endpointNode(link.source)?.id ?? link.source;
  const t = endpointNode(link.target)?.id ?? link.target;
  return (
    (state.hoveredNodeId !== null && (s === state.hoveredNodeId || t === state.hoveredNodeId)) ||
    (state.selectedNodeId !== null && (s === state.selectedNodeId || t === state.selectedNodeId))
  );
}

export function linkColorAccessor(link: LinkDatum): string {
  if (link._reification) {
    return `rgba(200, 200, 210, ${EDGE_STYLE.reificationConnector.opacity})`;
  }
  const sourceNode = endpointNode(link.source);
  const targetNode = endpointNode(link.target);
  const base = blendHex(
    pillarColor(sourceNode?.type ?? 'What').base,
    pillarColor(targetNode?.type ?? 'What').base,
  );
  const opacity = isHighlighted(link) ? EDGE_STYLE.hoverOpacity : EDGE_STYLE.baseOpacity;
  const uncertain = EDGE_STYLE.dashedCertainties.has(link.certainty);
  return base.replace('rgb', 'rgba').replace(')', `, ${uncertain ? opacity * 0.6 : opacity})`);
}

export function linkWidthAccessor(link: LinkDatum): number {
  if (link._reification) return EDGE_STYLE.reificationConnector.width;
  const base = EDGE_STYLE.widthByCertainty[link.certainty] ?? 1;
  return isHighlighted(link) ? base * 1.6 : base;
}

export function linkParticlesAccessor(link: LinkDatum): number {
  if (link._reification) return 0;
  return link.direction === 'outbound' || link.direction === 'bidirectional' ? 1 : 0;
}

export function setHoveredNode(id: string | null): void {
  state.hoveredNodeId = id;
}

export function setSelectedNode(id: string | null): void {
  state.selectedNodeId = id;
}

/** Search-driven graph filter: null means "show everything." A non-null set
 * restricts the scene to those node ids (search matches plus their direct
 * neighbors, computed by the caller — see scene.ts's applySearchFilter). */
export function setVisibleNodeIds(ids: Set<string> | null): void {
  state.visibleNodeIds = ids;
}

export function nodeVisibilityAccessor(node: GraphNode): boolean {
  return state.visibleNodeIds === null || state.visibleNodeIds.has(node.id);
}

export function linkVisibilityAccessor(link: LinkDatum): boolean {
  if (state.visibleNodeIds === null) return true;
  const s = endpointNode(link.source)?.id ?? (link.source as string);
  const t = endpointNode(link.target)?.id ?? (link.target as string);
  return state.visibleNodeIds.has(s) && state.visibleNodeIds.has(t);
}

/** Reification connectors: dashed-feeling, low-opacity edges from a Why/How
 * node to the source/target it reifies, per the plan review's ruling that
 * reification is meaningless in a graph viewer without them. */
export function buildReificationEdges(nodes: GraphNode[]): LinkDatum[] {
  const byId = new Set(nodes.map((n) => n.id));
  const extra: LinkDatum[] = [];
  for (const node of nodes) {
    for (const endpointId of [node.source_node_id, node.target_node_id]) {
      if (endpointId && byId.has(endpointId)) {
        extra.push({
          source: node.id,
          target: endpointId,
          predicate: REIFICATION_PREDICATE,
          direction: 'outbound',
          certainty: 'confirmed',
          _reification: true,
        });
      }
    }
  }
  return extra;
}
