// Privacy Mode: blurs DOM chrome (panel, tooltip, search results) via a body
// class + CSS, and masks in-canvas node labels via each node's three object
// handle (CSS blur cannot reach WebGL canvas content — see nodeObject.ts).

import type * as THREE from 'three';

import type { SceneHandle } from '../graph/scene';
import { getNodeHandle } from '../graph/nodeObject';
import { $privacyMode } from '../store';
import type { GraphNode } from '../types';

type NodeWithObj = GraphNode & { __threeObj?: THREE.Object3D };

export function initPrivacyMode(scene: SceneHandle, toggleButton: HTMLElement): void {
  toggleButton.addEventListener('click', () => {
    $privacyMode.set(!$privacyMode.get());
  });

  $privacyMode.listen((enabled) => {
    document.body.classList.toggle('kw-privacy', enabled);
    toggleButton.classList.toggle('kw-toggle-active', enabled);

    const nodes = scene.graph.graphData().nodes as NodeWithObj[];
    for (const node of nodes) {
      if (node.__threeObj) getNodeHandle(node.__threeObj)?.setPrivacy(enabled);
    }
  });
}
