// Per-node three.Object3D: a pillar-colored emissive sphere, a cheap additive
// glow sprite behind it (stand-in for a full bloom pass — see clustering.ts
// header for why bloom itself is deferred), and a camera-facing name label
// shown only on hover/selection (interactions.ts toggles `.visible`).

import * as THREE from 'three';
import SpriteText from 'three-spritetext';

import type { GraphNode } from '../types';
import { NODE_SIZING, nodeRadius, pillarColor } from '../theme';

const glowTextureCache = new Map<string, THREE.Texture>();

function glowTexture(hexColor: string): THREE.Texture {
  const cached = glowTextureCache.get(hexColor);
  if (cached) return cached;

  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d')!;
  const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  gradient.addColorStop(0, `${hexColor}CC`);
  gradient.addColorStop(0.5, `${hexColor}44`);
  gradient.addColorStop(1, `${hexColor}00`);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, size, size);

  const texture = new THREE.CanvasTexture(canvas);
  glowTextureCache.set(hexColor, texture);
  return texture;
}

export interface NodeObjectHandle {
  group: THREE.Group;
  sphere: THREE.Mesh;
  glow: THREE.Sprite;
  label: SpriteText;
  setSelected(selected: boolean): void;
  setLabelVisible(visible: boolean): void;
  setPrivacy(enabled: boolean): void;
}

const handles = new WeakMap<THREE.Object3D, NodeObjectHandle>();

export function buildNodeObject(node: GraphNode): THREE.Object3D {
  const colors = pillarColor(node.type);
  const radius = nodeRadius(node.degree);

  const group = new THREE.Group();

  const sphere = new THREE.Mesh(
    new THREE.SphereGeometry(radius, 16, 16),
    new THREE.MeshStandardMaterial({
      color: colors.base,
      emissive: colors.glow,
      emissiveIntensity: 0.6,
      roughness: 0.4,
      metalness: 0.1,
    }),
  );
  group.add(sphere);

  const glow = new THREE.Sprite(
    new THREE.SpriteMaterial({
      map: glowTexture(colors.glow),
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    }),
  );
  glow.scale.set(radius * 4, radius * 4, 1);
  group.add(glow);

  const label = new SpriteText(node.name);
  label.textHeight = Math.max(2.2, radius * 0.9);
  label.color = '#F5F7FA';
  label.backgroundColor = 'rgba(5, 6, 11, 0.65)';
  label.padding = 1.5;
  label.borderRadius = 3;
  label.position.set(0, radius + label.textHeight, 0);
  label.visible = false;
  group.add(label);

  const handle: NodeObjectHandle = {
    group,
    sphere,
    glow,
    label,
    setSelected(selected: boolean) {
      const scale = selected ? NODE_SIZING.selectedScale : 1;
      sphere.scale.setScalar(scale);
      glow.scale.set(radius * (selected ? 5.2 : 4), radius * (selected ? 5.2 : 4), 1);
      (sphere.material as THREE.MeshStandardMaterial).emissiveIntensity = selected ? 1.2 : 0.6;
    },
    setLabelVisible(visible: boolean) {
      label.visible = visible;
    },
    setPrivacy(enabled: boolean) {
      // CSS blur can't reach into the WebGL canvas, so Privacy Mode masks the
      // in-canvas label text itself instead (DOM chrome is blurred via CSS —
      // see ui/privacyMode.ts).
      label.text = enabled ? '•••' : node.name;
    },
  };
  handles.set(group, handle);

  return group;
}

export function getNodeHandle(object: THREE.Object3D): NodeObjectHandle | undefined {
  return handles.get(object);
}
