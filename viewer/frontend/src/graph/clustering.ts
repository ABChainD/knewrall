// Per-pillar attraction force (Knewrall's analogue of ivgraph's "knowledge
// clusters," keyed on Who/What/Where/When/Why/How instead of Notion database
// types), plus near-zero link strength for reification connectors so they
// never fight the main layout (per the plan review's ruling on §10.7).
//
// Bloom/post-processing is intentionally not used here (see nodeObject.ts's
// glow-sprite comment) — this module only touches force strengths, not
// rendering.

import type { ForceGraph3DInstance, NodeObject } from '3d-force-graph';

import type { GraphNode } from '../types';
import { REIFICATION_PREDICATE, type LinkDatum } from './linkObject';

const PILLAR_ORDER: GraphNode['type'][] = ['Who', 'What', 'Where', 'When', 'Why', 'How'];
const CLUSTER_RADIUS = 160;
const CLUSTER_STRENGTH = 0.12;

function pillarAnchors(): Record<string, { x: number; y: number; z: number }> {
  const anchors: Record<string, { x: number; y: number; z: number }> = {};
  PILLAR_ORDER.forEach((pillar, i) => {
    const angle = (i / PILLAR_ORDER.length) * Math.PI * 2;
    anchors[pillar] = {
      x: Math.cos(angle) * CLUSTER_RADIUS,
      y: 0,
      z: Math.sin(angle) * CLUSTER_RADIUS,
    };
  });
  return anchors;
}

interface SimNode extends GraphNode {
  x?: number;
  y?: number;
  z?: number;
  vx?: number;
  vy?: number;
  vz?: number;
}

function pillarClusterForce(strengthRef: { value: number }) {
  let nodes: SimNode[] = [];
  const anchors = pillarAnchors();

  function force(alpha: number): void {
    if (strengthRef.value === 0) return;
    for (const n of nodes) {
      const anchor = anchors[n.type];
      if (!anchor || n.x === undefined || n.y === undefined || n.z === undefined) continue;
      n.vx = (n.vx ?? 0) + (anchor.x - n.x) * alpha * strengthRef.value;
      n.vy = (n.vy ?? 0) + (anchor.y - n.y) * alpha * strengthRef.value;
      n.vz = (n.vz ?? 0) + (anchor.z - n.z) * alpha * strengthRef.value;
    }
  }
  // three-forcegraph's ForceFn only guarantees the loose NodeObject shape at
  // the type level; at runtime these are always our own GraphNode-derived
  // sim nodes, since we're the ones who called graph.graphData(...).
  force.initialize = (ns: NodeObject[]): void => {
    nodes = ns as SimNode[];
  };
  return force;
}

export function initClustering(graph: ForceGraph3DInstance, enabled: boolean): { setEnabled(v: boolean): void } {
  const strengthRef = { value: enabled ? CLUSTER_STRENGTH : 0 };
  graph.d3Force('pillarCluster', pillarClusterForce(strengthRef));

  const linkForce = graph.d3Force('link') as
    | { strength(fn: (link: LinkDatum) => number): void }
    | undefined;
  linkForce?.strength((link: LinkDatum) => (link.predicate === REIFICATION_PREDICATE ? 0.02 : 1));

  return {
    setEnabled(v: boolean): void {
      strengthRef.value = v ? CLUSTER_STRENGTH : 0;
      graph.d3ReheatSimulation();
    },
  };
}
