// Hover -> preview tooltip. Click -> camera fly-to + full node expand.
// Also the single navigation entrypoint (`focusNode`) used by search results
// and in-content entity links, so every way of reaching a node behaves the
// same way.

import type * as THREE from 'three';

import { api } from '../api';
import { CAMERA } from '../theme';
import type { GraphNode } from '../types';
import { $nodeDetail, $panelOpen, $selectedId, $statusMessage } from '../store';
import { hideTooltip, showTooltip } from '../ui/tooltip';
import { getNodeHandle } from './nodeObject';
import { setHoveredNode, setSelectedNode } from './linkObject';
import type { SceneHandle } from './scene';

type NodeWithObj = GraphNode & { __threeObj?: THREE.Object3D; x?: number; y?: number; z?: number };

let hoveredObject: THREE.Object3D | null = null;
let selectedObject: THREE.Object3D | null = null;
let lastMouse = { x: 0, y: 0 };

function refreshLinkStyles(scene: SceneHandle): void {
  scene.graph.linkColor(scene.graph.linkColor());
  scene.graph.linkWidth(scene.graph.linkWidth());
}

export async function focusNode(scene: SceneHandle, nodeId: string): Promise<void> {
  const graphData = scene.graph.graphData();
  const node = (graphData.nodes as NodeWithObj[]).find((n) => n.id === nodeId);
  if (!node) return;

  if (selectedObject) getNodeHandle(selectedObject)?.setSelected(false);
  const obj = node.__threeObj;
  if (obj) {
    getNodeHandle(obj)?.setSelected(true);
    selectedObject = obj;
  }
  setSelectedNode(nodeId);
  refreshLinkStyles(scene);

  $selectedId.set(nodeId);
  $panelOpen.set(true);

  if (node.x !== undefined && node.y !== undefined && node.z !== undefined) {
    const distance = 160;
    const ratio = 1 + distance / Math.hypot(node.x, node.y, node.z || 1);
    scene.graph.cameraPosition(
      { x: node.x * ratio, y: node.y * ratio, z: node.z * ratio },
      { x: node.x, y: node.y, z: node.z },
      CAMERA.flyToDurationMs,
    );
  }

  try {
    const detail = await api.getNode(nodeId);
    $nodeDetail.set(detail);
  } catch (e) {
    $statusMessage.set(`Could not load neuron: ${(e as Error).message}`);
  }
}

export function initInteractions(scene: SceneHandle): { destroy(): void } {
  const container = scene.graph.renderer().domElement;

  const onMouseMove = (ev: MouseEvent): void => {
    lastMouse = { x: ev.clientX, y: ev.clientY };
    if (hoveredObject) showTooltip(currentHoveredNode!, lastMouse);
  };
  container.addEventListener('mousemove', onMouseMove);

  let currentHoveredNode: GraphNode | null = null;

  scene.graph.onNodeHover((obj) => {
    const node = obj as NodeWithObj | null;

    if (hoveredObject) {
      getNodeHandle(hoveredObject)?.setLabelVisible(false);
    }
    hoveredObject = node?.__threeObj ?? null;
    currentHoveredNode = node ?? null;

    if (node && hoveredObject) {
      getNodeHandle(hoveredObject)?.setLabelVisible(true);
      showTooltip(node, lastMouse);
      setHoveredNode(node.id);
    } else {
      hideTooltip();
      setHoveredNode(null);
    }
    refreshLinkStyles(scene);
    container.style.cursor = node ? 'pointer' : 'default';
  });

  scene.graph.onNodeClick((obj) => {
    const node = obj as NodeWithObj;
    void focusNode(scene, node.id);
  });

  scene.graph.onBackgroundClick(() => {
    $panelOpen.set(false);
    $selectedId.set(null);
    if (selectedObject) {
      getNodeHandle(selectedObject)?.setSelected(false);
      selectedObject = null;
    }
    setSelectedNode(null);
    refreshLinkStyles(scene);
  });

  return {
    destroy(): void {
      container.removeEventListener('mousemove', onMouseMove);
    },
  };
}

export function toggleTopDown(scene: SceneHandle, enabled: boolean): void {
  const graphData = scene.graph.graphData();
  const nodes = graphData.nodes as NodeWithObj[];
  const maxExtent =
    nodes.reduce((max, n) => Math.max(max, Math.hypot(n.x ?? 0, n.z ?? 0)), 0) || 200;

  if (enabled) {
    scene.graph.cameraPosition({ x: 0, y: maxExtent * 1.6, z: 0.1 }, { x: 0, y: 0, z: 0 }, CAMERA.flyToDurationMs);
  } else {
    scene.graph.cameraPosition({ x: maxExtent, y: maxExtent * 0.5, z: maxExtent }, { x: 0, y: 0, z: 0 }, CAMERA.flyToDurationMs);
  }
}

export function setAutoRotate(scene: SceneHandle, enabled: boolean): void {
  const controls = scene.graph.controls() as { autoRotate: boolean };
  controls.autoRotate = enabled;
}
