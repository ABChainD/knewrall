// Debounced search box wired to /api/search. Semantic toggle grays out when
// /api/health says it's unavailable; a hybrid request that silently fell back
// to literal (timeout or no key) is called out with a status note rather than
// pretending semantic ran.

import { api } from '../api';
import { $searchQuery, $searchResults, $semanticAvailable, $statusMessage } from '../store';
import { pillarColor } from '../theme';
import type { SearchResultItem } from '../types';

const DEBOUNCE_MS = 250;

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function renderResultItem(item: SearchResultItem): string {
  const colors = pillarColor(item.type ?? 'What');
  return `<li class="kw-search-item" data-node-id="${item.id}" role="option" tabindex="0">
    <span class="kw-search-dot" style="background:${colors.base}"></span>
    <span>${escapeHtml(item.canonical_name ?? item.id)}</span>
  </li>`;
}

export function initSearchBar(
  container: HTMLElement,
  onNavigate: (nodeId: string) => void,
  onFilterChange: (matchedIds: string[] | null) => void,
): void {
  const input = container.querySelector<HTMLInputElement>('.kw-search-input')!;
  const hybridToggle = container.querySelector<HTMLInputElement>('.kw-search-hybrid')!;
  const filterToggle = container.querySelector<HTMLInputElement>('.kw-search-filter')!;
  const resultsEl = container.querySelector<HTMLElement>('.kw-search-results')!;

  $semanticAvailable.listen((available) => {
    hybridToggle.disabled = !available;
    if (!available) hybridToggle.checked = false;
  });

  let debounceHandle: ReturnType<typeof setTimeout> | null = null;

  async function runSearch(): Promise<void> {
    const query = input.value.trim();
    $searchQuery.set(query);
    if (!query) {
      $searchResults.set(null);
      resultsEl.innerHTML = '';
      resultsEl.classList.remove('kw-search-results-open');
      onFilterChange(null); // cleared search box -> back to the full graph
      return;
    }
    const resp = await api.search(query, hybridToggle.checked);
    $searchResults.set(resp);

    if (filterToggle.checked) {
      onFilterChange(resp.flat.map((item) => item.id));
    }

    if (resp.hybrid_requested && !resp.hybrid_applied) {
      $statusMessage.set('Semantic search unavailable — showing literal results.');
    }

    const groupsHtml = Object.entries(resp.groups)
      .map(
        ([pillar, items]) =>
          `<div class="kw-search-group"><div class="kw-search-group-label">${pillar}</div><ul>${items
            .map(renderResultItem)
            .join('')}</ul></div>`,
      )
      .join('');
    resultsEl.innerHTML = groupsHtml || '<div class="kw-search-empty">No matches.</div>';
    resultsEl.classList.add('kw-search-results-open');
  }

  input.addEventListener('input', () => {
    if (debounceHandle) clearTimeout(debounceHandle);
    debounceHandle = setTimeout(() => void runSearch(), DEBOUNCE_MS);
  });

  hybridToggle.addEventListener('change', () => void runSearch());

  filterToggle.addEventListener('change', () => {
    const results = $searchResults.get();
    if (filterToggle.checked && results) {
      onFilterChange(results.flat.map((item) => item.id));
    } else {
      onFilterChange(null);
    }
  });

  const activateResultItem = (item: HTMLElement | null): void => {
    const nodeId = item?.dataset['nodeId'];
    if (nodeId) {
      onNavigate(nodeId);
      resultsEl.classList.remove('kw-search-results-open');
      input.blur();
    }
  };

  resultsEl.addEventListener('click', (ev) => {
    activateResultItem((ev.target as HTMLElement).closest<HTMLElement>('.kw-search-item'));
  });

  resultsEl.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    const item = (ev.target as HTMLElement).closest<HTMLElement>('.kw-search-item');
    if (item) {
      ev.preventDefault();
      activateResultItem(item);
    }
  });

  document.addEventListener('click', (ev) => {
    if (!container.contains(ev.target as Node)) {
      resultsEl.classList.remove('kw-search-results-open');
    }
  });
}
