// Full neuron expansion: header, every description, the properties table,
// tags, resolved links (outbound + inbound, chips), companion markdown, and
// linked notes. Every related entity — structured or free-text — is a
// clickable chip/link that calls back into the graph via `onNavigate`.

import { $nodeDetail, $panelOpen } from '../store';
import { pillarColor } from '../theme';
import type { NodeDetailResp } from '../types';
import { renderMarkdownWithLinks, renderPlainTextWithLinks, wireEntityLinkClicks } from './contentLinks';

interface PropertyEntry {
  value?: unknown;
  when?: string;
  where?: string;
  certainty?: string;
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

interface Disambiguation {
  context_hint?: string;
  similar_entities?: string[];
}

function renderHeader(detail: NodeDetailResp): string {
  const header = (detail.body['header'] as Record<string, unknown>) ?? {};
  const type = String(header['type'] ?? 'What');
  const colors = pillarColor(type);
  const aliases = (header['aliases'] as string[]) ?? [];
  // header.disambiguation is an object per schemas/master.schema.json
  // ({context_hint, similar_entities}), not a plain string.
  const disambiguation = header['disambiguation'] as Disambiguation | undefined;

  return `
    <div class="kw-panel-header" style="border-color:${colors.base}">
      <span class="kw-pillar-badge" style="background:${colors.base}">${type}</span>
      <h2>${escapeHtml(String(header['canonical_name'] ?? 'Untitled'))}</h2>
      ${aliases.length ? `<div class="kw-aliases">aka ${aliases.map(escapeHtml).join(', ')}</div>` : ''}
      ${disambiguation?.context_hint ? `<div class="kw-disambiguation">${escapeHtml(disambiguation.context_hint)}</div>` : ''}
    </div>
  `;
}

function renderDescriptions(detail: NodeDetailResp): string {
  const descriptions = (detail.body['descriptions'] as Record<string, string>) ?? {};
  const blocks = Object.entries(descriptions)
    .filter(([, value]) => value)
    .map(([key, value]) => {
      const spans = detail.entity_spans[`description:${key}`] ?? [];
      return `<div class="kw-desc"><span class="kw-desc-key">${key}</span><p>${renderPlainTextWithLinks(value, spans)}</p></div>`;
    });
  return blocks.length ? `<section><h3>Descriptions</h3>${blocks.join('')}</section>` : '';
}

function renderProperties(detail: NodeDetailResp): string {
  const properties = (detail.body['properties'] as Record<string, PropertyEntry[]>) ?? {};
  const rows = Object.entries(properties).flatMap(([key, entries]) =>
    (entries ?? []).map((entry) => {
      const spans = detail.entity_spans[`property:${key}`] ?? [];
      const valueText = String(entry.value ?? '');
      const valueHtml = renderPlainTextWithLinks(valueText, spans);
      const meta = [entry.when, entry.where, entry.certainty].filter(Boolean).join(' · ');
      return `<tr><td class="kw-prop-key">${escapeHtml(key)}</td><td>${valueHtml}${
        meta ? `<div class="kw-prop-meta">${escapeHtml(meta)}</div>` : ''
      }</td></tr>`;
    }),
  );
  return rows.length
    ? `<section><h3>Properties</h3><table class="kw-prop-table">${rows.join('')}</table></section>`
    : '';
}

function renderTags(detail: NodeDetailResp): string {
  const tags = (detail.body['tags'] as string[]) ?? [];
  return tags.length
    ? `<section><h3>Tags</h3><div class="kw-tags">${tags.map((t) => `<span class="kw-tag">${escapeHtml(t)}</span>`).join('')}</div></section>`
    : '';
}

function renderLinks(detail: NodeDetailResp): string {
  if (!detail.resolved_links.length) return '';
  const chips = detail.resolved_links
    .map((link) => {
      const colors = pillarColor(link.type ?? 'What');
      const arrow = link.direction === 'inbound' ? '←' : '→';
      return `<a href="#" class="link-chip" data-node-id="${link.node_id}" style="border-color:${colors.base}">
        <span class="kw-chip-arrow">${arrow}</span> ${escapeHtml(link.predicate)}: ${escapeHtml(link.name ?? link.node_id)}
      </a>`;
    })
    .join('');
  return `<section><h3>Links</h3><div class="kw-links">${chips}</div></section>`;
}

function renderNotes(detail: NodeDetailResp): string {
  if (!detail.linked_notes.length) return '';
  const items = detail.linked_notes.map((n) => `<li>${escapeHtml(n.rel_path)}</li>`).join('');
  return `<section><h3>Linked Notes</h3><ul class="kw-notes">${items}</ul></section>`;
}

function renderCompanionMd(detail: NodeDetailResp): string {
  if (!detail.companion_md) return '';
  const spans = detail.entity_spans['companion_md'] ?? [];
  return `<section><h3>Companion Note</h3><div class="kw-md">${renderMarkdownWithLinks(detail.companion_md, spans)}</div></section>`;
}

export function initDetailPanel(container: HTMLElement, onNavigate: (nodeId: string) => void): void {
  wireEntityLinkClicks(container, onNavigate);

  const closeBtn = container.querySelector<HTMLButtonElement>('.kw-panel-close');
  closeBtn?.addEventListener('click', () => $panelOpen.set(false));

  const body = container.querySelector<HTMLElement>('.kw-panel-body');

  function render(): void {
    const open = $panelOpen.get();
    container.classList.toggle('kw-panel-open', open);
    if (!open || !body) return;

    const detail = $nodeDetail.get();
    if (!detail) {
      body.innerHTML = '<div class="kw-panel-loading">Loading…</div>';
      return;
    }
    // Real neuron data has enough shape variety (e.g. header.disambiguation
    // being an object, not a string) that a render function hitting an
    // unexpected shape is a "when," not "if." Store listener exceptions are
    // swallowed silently by nanostores, so without this the panel would just
    // stay stuck on "Loading…" forever with no signal — this way it degrades
    // to a visible, dismissable message instead, and still logs for
    // debugging.
    try {
      body.innerHTML = [
        renderHeader(detail),
        renderDescriptions(detail),
        renderProperties(detail),
        renderLinks(detail),
        renderTags(detail),
        renderCompanionMd(detail),
        renderNotes(detail),
      ].join('');
    } catch (e) {
      console.error('detailPanel render failed for', detail.id, e);
      body.innerHTML = `<div class="kw-panel-loading">Could not render this neuron (${escapeHtml((e as Error).message)}).</div>`;
    }
  }

  $panelOpen.listen(render);
  $nodeDetail.listen(render);
  render();
}
