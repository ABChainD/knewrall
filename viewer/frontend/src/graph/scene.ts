// Creates and configures the ForceGraph3D instance: camera, background/fog,
// node/link rendering. Everything else (hover/click behavior, clustering
// forces) is wired on top of the returned instance by interactions.ts /
// clustering.ts — this module only builds the scene itself.

import ForceGraph3D, { type ForceGraph3DInstance } from '3d-force-graph';
import * as THREE from 'three';

import type { GraphResp, GraphNode } from '../types';
import { BACKGROUND } from '../theme';
import {
  buildReificationEdges,
  linkColorAccessor,
  linkParticlesAccessor,
  linkVisibilityAccessor,
  linkWidthAccessor,
  nodeVisibilityAccessor,
  setVisibleNodeIds,
  type LinkDatum,
} from './linkObject';
import { buildNodeObject } from './nodeObject';

export interface SceneHandle {
  graph: ForceGraph3DInstance;
  setGraphData(data: GraphResp): void;
  /** Restricts the scene to the given node ids plus their direct neighbors
   * (search-as-filter). Pass null/empty to show the full graph again. Only
   * touches node/link visibility — the simulation and camera are untouched,
   * so clearing the filter doesn't re-layout or reset the view. */
  applySearchFilter(matchedIds: string[] | null): void;
  destroy(): void;
}

export function initScene(container: HTMLElement): SceneHandle {
  const graph = new ForceGraph3D(container)
    .backgroundColor(BACKGROUND.gradientFrom)
    .nodeThreeObject((node) => buildNodeObject(node as GraphNode))
    .nodeThreeObjectExtend(false)
    .nodeLabel(() => '') // we render our own DOM tooltip (ui/tooltip.ts)
    .nodeVisibility(nodeVisibilityAccessor as (n: object) => boolean)
    .linkVisibility(linkVisibilityAccessor as (l: object) => boolean)
    .linkColor(linkColorAccessor as (l: object) => string)
    .linkWidth(linkWidthAccessor as (l: object) => number)
    .linkDirectionalParticles(linkParticlesAccessor as (l: object) => number)
    .linkDirectionalParticleWidth(1.4)
    .linkDirectionalParticleSpeed(0.006)
    .enableNodeDrag(false);

  const scene = graph.scene();
  scene.fog = new THREE.FogExp2(BACKGROUND.fogColor, BACKGROUND.fogDensity);
  scene.add(new THREE.HemisphereLight(0xbfd4ff, 0x0a0a12, 0.9));

  addStarfield(scene);

  const controls = graph.controls() as { autoRotate: boolean; autoRotateSpeed: number };
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.4;

  return {
    graph,
    setGraphData(data: GraphResp): void {
      graph.graphData({
        nodes: data.nodes,
        links: [...data.edges, ...buildReificationEdges(data.nodes)],
      });
    },
    applySearchFilter(matchedIds: string[] | null): void {
      if (!matchedIds || matchedIds.length === 0) {
        setVisibleNodeIds(null);
      } else {
        const links = graph.graphData().links as LinkDatum[];
        const matched = new Set(matchedIds);
        const visible = new Set(matched);
        for (const link of links) {
          const s = endpointId(link.source);
          const t = endpointId(link.target);
          if (matched.has(s)) visible.add(t);
          if (matched.has(t)) visible.add(s);
        }
        setVisibleNodeIds(visible);
      }
      // refresh() re-digests visibility off the accessors above without
      // touching graphData/the simulation — the layout stays put, only
      // which nodes/links are rendered changes.
      graph.refresh();
    },
    destroy(): void {
      container.replaceChildren();
    },
  };
}

// three-forcegraph mutates link.source/target from a plain id string into the
// resolved node object once the simulation initializes — this widens the
// static `string` link.source/target type in practice, so the param here is
// declared independently rather than narrowed from LinkDatum's field type
// (which TS otherwise collapses to just `string`, same as interactions.ts's
// endpointId).
function endpointId(end: GraphNode | string): string {
  return typeof end === 'object' ? end.id : end;
}

function addStarfield(scene: THREE.Scene): void {
  const starCount = 800;
  const positions = new Float32Array(starCount * 3);
  for (let i = 0; i < starCount; i++) {
    const radius = 600 + Math.random() * 800;
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    positions[i * 3] = radius * Math.sin(phi) * Math.cos(theta);
    positions[i * 3 + 1] = radius * Math.sin(phi) * Math.sin(theta);
    positions[i * 3 + 2] = radius * Math.cos(phi);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  const material = new THREE.PointsMaterial({ color: 0x8899bb, size: 1.2, sizeAttenuation: true });
  scene.add(new THREE.Points(geometry, material));
}
