// Typed fetch client for the five backend routes. Centralizes error handling
// so callers never have to check `res.ok` themselves.

import type { GraphResp, HealthResp, NodeDetailResp, SearchResp, StatsResp } from './types';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new ApiError(res.status, detail || res.statusText);
  }
  return (await res.json()) as T;
}

export const api = {
  getGraph: (): Promise<GraphResp> => getJson('/api/graph'),

  getNode: (id: string): Promise<NodeDetailResp> => getJson(`/api/node/${encodeURIComponent(id)}`),

  search: (q: string, hybrid: boolean, limit = 20): Promise<SearchResp> =>
    getJson(
      `/api/search?q=${encodeURIComponent(q)}&hybrid=${hybrid ? 'true' : 'false'}&limit=${limit}`,
    ),

  getStats: (): Promise<StatsResp> => getJson('/api/stats'),

  getHealth: (): Promise<HealthResp> => getJson('/api/health'),
};
