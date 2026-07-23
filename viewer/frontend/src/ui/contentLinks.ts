// Turns backend-resolved entity spans (see backend/linkify.py) into clickable
// graph-navigable <a> tags, for both plain-text fields (descriptions,
// properties) and the companion markdown (rendered via marked, sanitized via
// DOMPurify — data-node-id survives DOMPurify's default allow-list).

import DOMPurify from 'dompurify';
import { marked } from 'marked';

import type { EntitySpan } from '../types';

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function insertSpanLinks(text: string, spans: EntitySpan[], escapeOutsideSpans: boolean): string {
  if (!spans.length) return escapeOutsideSpans ? escapeHtml(text) : text;

  let out = '';
  let cursor = 0;
  for (const span of [...spans].sort((a, b) => a.start - b.start)) {
    if (span.start < cursor) continue; // defensive: skip any overlap that slipped through
    const before = text.slice(cursor, span.start);
    out += escapeOutsideSpans ? escapeHtml(before) : before;
    const label = text.slice(span.start, span.end);
    out += `<a href="#" class="entity-link" data-node-id="${span.node_id}">${escapeHtml(label)}</a>`;
    cursor = span.end;
  }
  const rest = text.slice(cursor);
  out += escapeOutsideSpans ? escapeHtml(rest) : rest;
  return out;
}

export function renderPlainTextWithLinks(text: string, spans: EntitySpan[]): string {
  return insertSpanLinks(text, spans, true);
}

export function renderMarkdownWithLinks(md: string, spans: EntitySpan[]): string {
  const withLinks = insertSpanLinks(md, spans, false);
  const html = marked.parse(withLinks, { async: false }) as string;
  return DOMPurify.sanitize(html, { ADD_ATTR: ['data-node-id'] });
}

export function wireEntityLinkClicks(container: HTMLElement, onNavigate: (nodeId: string) => void): void {
  container.addEventListener('click', (ev) => {
    const target = (ev.target as HTMLElement).closest<HTMLElement>('a.entity-link, a.link-chip');
    if (!target) return;
    ev.preventDefault();
    const nodeId = target.dataset['nodeId'];
    if (nodeId) onNavigate(nodeId);
  });
}
