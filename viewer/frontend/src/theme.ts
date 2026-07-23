// Single source of visual truth: node color, cluster hue, search-category
// headers, and edge tint all derive from PILLAR_COLORS.

import type { Pillar } from './types';

export const PILLAR_COLORS: Record<Pillar, { base: string; glow: string }> = {
  Who: { base: '#F5A623', glow: '#FFCF6B' },
  What: { base: '#4EA8FF', glow: '#8FD0FF' },
  Where: { base: '#2ED47A', glow: '#7BE8AE' },
  When: { base: '#B478FF', glow: '#D3B0FF' },
  Why: { base: '#FF5C7A', glow: '#FF9DB0' },
  How: { base: '#FF8A3D', glow: '#FFB57A' },
};

export const PILLAR_GLYPHS: Record<Pillar, string> = {
  Who: '\u{1F464}', // 👤
  What: '◆', // ◆
  Where: '\u{1F4CD}', // 📍
  When: '\u{1F550}', // 🕐
  Why: '✳', // ✳
  How: '⚙', // ⚙
};

export const UNKNOWN_COLOR = { base: '#8892A6', glow: '#B7BFCF' };

export function pillarColor(type: string): { base: string; glow: string } {
  return (PILLAR_COLORS as Record<string, { base: string; glow: string }>)[type] ?? UNKNOWN_COLOR;
}

export const NODE_SIZING = {
  minRadius: 3,
  maxRadius: 14,
  degreeCoefficient: 1.6,
  selectedScale: 1.3,
};

export function nodeRadius(degree: number): number {
  const r = NODE_SIZING.minRadius + NODE_SIZING.degreeCoefficient * Math.sqrt(Math.max(degree, 0));
  return Math.min(Math.max(r, NODE_SIZING.minRadius), NODE_SIZING.maxRadius);
}

export const EDGE_STYLE = {
  baseOpacity: 0.35,
  hoverOpacity: 1.0,
  widthByCertainty: {
    confirmed: 1.4,
    rumored: 0.9,
    hypothetical: 0.6,
    alternative: 0.6,
  } as Record<string, number>,
  dashedCertainties: new Set(['hypothetical', 'alternative']),
  reificationConnector: {
    opacity: 0.25,
    width: 0.6,
  },
};

export const CAMERA = {
  flyToDurationMs: 700,
  idleAutoRotateDegPerSec: 0.15,
};

export const BACKGROUND = {
  gradientFrom: '#05060B',
  gradientTo: '#0B0E17',
  fogColor: '#05060B',
  fogDensity: 0.0009,
};
