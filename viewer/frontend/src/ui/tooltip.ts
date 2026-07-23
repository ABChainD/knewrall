// Lightweight hover preview — uses the `preview` text already delivered in
// the /api/graph payload, so showing it costs zero network round-trips.

import { pillarColor } from '../theme';
import type { GraphNode } from '../types';

let el: HTMLDivElement | null = null;

function ensureEl(): HTMLDivElement {
  if (el) return el;
  el = document.createElement('div');
  el.className = 'kw-tooltip';
  el.style.display = 'none';
  document.body.appendChild(el);
  return el;
}

export function showTooltip(node: GraphNode, mouse: { x: number; y: number }): void {
  const tooltip = ensureEl();
  const colors = pillarColor(node.type);
  tooltip.style.borderColor = colors.base;
  tooltip.innerHTML = `
    <div class="kw-tooltip-title" style="color:${colors.base}">${node.type} · ${escapeHtml(node.name)}</div>
    <div class="kw-tooltip-preview">${escapeHtml(node.preview || 'No description yet.')}</div>
    <div class="kw-tooltip-meta">${node.degree} connection${node.degree === 1 ? '' : 's'}</div>
  `;
  tooltip.style.left = `${mouse.x + 16}px`;
  tooltip.style.top = `${mouse.y + 16}px`;
  tooltip.style.display = 'block';
}

export function hideTooltip(): void {
  if (el) el.style.display = 'none';
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
