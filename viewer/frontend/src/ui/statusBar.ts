// Node/edge/embedding counts plus a manual refresh (data is live-read from
// index.db/vectors.db on every /api/graph call, so "refresh" just re-fetches
// it — reflecting any CLI `refresh-index`/`embed` run since page load).

import { api } from '../api';
import { $statusMessage } from '../store';
import type { StatsResp } from '../types';

export function initStatusBar(
  container: HTMLElement,
  onRefresh: () => void,
): void {
  const statsEl = container.querySelector<HTMLElement>('.kw-status-stats')!;
  const messageEl = container.querySelector<HTMLElement>('.kw-status-message')!;
  const refreshBtn = container.querySelector<HTMLButtonElement>('.kw-status-refresh')!;

  function renderStats(stats: StatsResp): void {
    statsEl.textContent = `${stats.nodes} nodes · ${stats.edges} edges · ${stats.embeddings_total} embedded`;
  }

  async function load(): Promise<void> {
    try {
      const stats = await api.getStats();
      renderStats(stats);
    } catch (e) {
      statsEl.textContent = 'stats unavailable';
      $statusMessage.set((e as Error).message);
    }
  }

  refreshBtn.addEventListener('click', () => {
    void load();
    onRefresh();
  });

  $statusMessage.listen((msg) => {
    messageEl.textContent = msg;
    messageEl.classList.toggle('kw-status-message-visible', Boolean(msg));
    if (msg) setTimeout(() => $statusMessage.set(''), 5000);
  });

  void load();
}
