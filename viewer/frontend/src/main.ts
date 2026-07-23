// Sole orchestrator: wires every graph/* and ui/* controller together. All
// shared state flows through store.ts; no module here talks to another
// feature module directly except by calling the exported init functions.

import './styles/app.css';

import { api } from './api';
import { initClustering } from './graph/clustering';
import { focusNode, initInteractions, setAutoRotate, toggleTopDown } from './graph/interactions';
import { initScene } from './graph/scene';
import { initDetailPanel } from './ui/detailPanel';
import { initPrivacyMode } from './ui/privacyMode';
import { initSearchBar } from './ui/searchBar';
import { initStatusBar } from './ui/statusBar';
import { $graphData, $semanticAvailable, $statusMessage } from './store';
import { PILLAR_COLORS } from './theme';

async function main(): Promise<void> {
  const graphContainer = document.getElementById('graph')!;
  const scene = initScene(graphContainer);
  initInteractions(scene);
  const clustering = initClustering(scene.graph, true);

  const onNavigate = (nodeId: string): void => {
    void focusNode(scene, nodeId);
  };

  initDetailPanel(document.getElementById('detail-panel')!, onNavigate);
  initSearchBar(document.getElementById('search-bar')!, onNavigate, (matchedIds) =>
    scene.applySearchFilter(matchedIds),
  );

  async function loadGraph(): Promise<void> {
    try {
      const data = await api.getGraph();
      $graphData.set(data);
      scene.setGraphData(data);
    } catch (e) {
      $statusMessage.set(`Could not load graph: ${(e as Error).message}`);
    }
  }

  initStatusBar(document.getElementById('status-bar')!, () => void loadGraph());

  api
    .getHealth()
    .then((health) => $semanticAvailable.set(health.semantic_available))
    .catch(() => $semanticAvailable.set(false));

  await loadGraph();

  wireToggles(scene, clustering);
  renderLegend();
}

function wireToggles(
  scene: ReturnType<typeof initScene>,
  clustering: ReturnType<typeof initClustering>,
): void {
  const toggles = document.getElementById('toggles')!;
  let topDown = false;
  let autoRotate = true;
  let clusteringEnabled = true;

  toggles.addEventListener('click', (ev) => {
    const btn = (ev.target as HTMLElement).closest<HTMLButtonElement>('.kw-toggle');
    if (!btn) return;
    const kind = btn.dataset['toggle'];

    if (kind === 'cluster') {
      clusteringEnabled = !clusteringEnabled;
      clustering.setEnabled(clusteringEnabled);
      btn.classList.toggle('kw-toggle-active', clusteringEnabled);
    } else if (kind === 'topdown') {
      topDown = !topDown;
      toggleTopDown(scene, topDown);
      btn.classList.toggle('kw-toggle-active', topDown);
    } else if (kind === 'rotate') {
      autoRotate = !autoRotate;
      setAutoRotate(scene, autoRotate);
      btn.classList.toggle('kw-toggle-active', autoRotate);
    }
  });

  // Reflect initial defaults (clustering + auto-rotate start enabled).
  toggles.querySelector('[data-toggle="cluster"]')?.classList.add('kw-toggle-active');
  toggles.querySelector('[data-toggle="rotate"]')?.classList.add('kw-toggle-active');

  initPrivacyMode(scene, toggles.querySelector('[data-toggle="privacy"]')!);
}

function renderLegend(): void {
  const legend = document.getElementById('legend')!;
  legend.innerHTML = Object.entries(PILLAR_COLORS)
    .map(
      ([pillar, colors]) =>
        `<div class="kw-legend-item"><span class="kw-legend-dot" style="background:${colors.base}"></span>${pillar}</div>`,
    )
    .join('');
}

void main();
