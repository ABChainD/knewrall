// Shared reactive state. Every graph/* and ui/* module reads/writes only
// through these atoms — no hidden globals, no cross-module direct calls.

import { atom } from 'nanostores';

import type { GraphResp, NodeDetailResp, SearchResp } from './types';

export const $graphData = atom<GraphResp | null>(null);
export const $selectedId = atom<string | null>(null);
export const $nodeDetail = atom<NodeDetailResp | null>(null);
export const $searchQuery = atom<string>('');
export const $searchResults = atom<SearchResp | null>(null);
export const $privacyMode = atom<boolean>(false);
export const $panelOpen = atom<boolean>(false);
export const $clusteringEnabled = atom<boolean>(true);
export const $topDown = atom<boolean>(false);
export const $autoRotate = atom<boolean>(true);
export const $semanticAvailable = atom<boolean>(false);
export const $statusMessage = atom<string>('');
