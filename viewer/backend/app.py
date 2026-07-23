"""FastAPI app: five read-only routes over the Knewrall knowledge graph, plus
static hosting for the built frontend. Every route delegates to engine.py /
graph.py / node_detail.py — no route here ever imports a mutating verb.

Run directly for local dev (uvicorn with reload); scripts/run.py is the
single-process "production" launcher that also serves the built frontend.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import engine, graph, node_detail
from .models import GraphResp, HealthResp, NodeDetailResp, SearchResp, SearchResultItem, StatsResp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("knewrall.viewer")

DEV_ORIGIN = "http://localhost:5173"
STATIC_DIR = Path(__file__).resolve().parent / "static"
SEARCH_TIMEOUT_SECONDS = 4.0

app = FastAPI(title="Knewrall Viewer API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[DEV_ORIGIN],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/graph", response_model=GraphResp)
def get_graph() -> GraphResp:
    try:
        return graph.build_graph()
    except engine.EngineUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/node/{node_id}", response_model=NodeDetailResp)
def get_node(node_id: str) -> NodeDetailResp:
    try:
        return node_detail.build_node_detail(node_id)
    except node_detail.NodeNotFound:
        raise HTTPException(status_code=404, detail=f"Neuron not found: {node_id}")
    except engine.EngineUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


def _to_item(raw: dict) -> SearchResultItem:
    return SearchResultItem(
        id=raw.get("id", ""),
        type=raw.get("type"),
        canonical_name=raw.get("canonical_name"),
        aliases=raw.get("aliases") or [],
        tags=raw.get("tags") or [],
    )


@app.get("/api/search", response_model=SearchResp)
def search(q: str, hybrid: bool = True, limit: int = 20) -> SearchResp:
    hybrid_applied = False
    if not q:
        results: list[dict] = []
    elif hybrid and engine.semantic_available():
        # engine.submit() dispatches to the engine's single dedicated thread
        # (see engine.py) — the fallback below uses literal_search(), which
        # opens its own connection instead of queuing behind the
        # still-running hybrid call on that thread, so it stays fast even on
        # timeout.
        future = engine.submit(engine.search_graph_raw, q, True, limit)
        try:
            results = future.result(timeout=SEARCH_TIMEOUT_SECONDS)
            hybrid_applied = True
        except concurrent.futures.TimeoutError:
            logger.warning(f"hybrid search timed out after {SEARCH_TIMEOUT_SECONDS}s, falling back to literal")
            results = engine.literal_search(q, limit=limit)
        except Exception as e:
            logger.warning(f"hybrid search failed, falling back to literal: {e}")
            results = engine.literal_search(q, limit=limit)
    else:
        results = engine.literal_search(q, limit=limit)

    items = [_to_item(r) for r in results]
    groups: dict[str, list[SearchResultItem]] = {}
    for item in items:
        groups.setdefault(item.type or "Unknown", []).append(item)

    return SearchResp(
        query=q, hybrid_requested=hybrid, hybrid_applied=hybrid_applied,
        groups=groups, flat=items,
    )


@app.get("/api/stats", response_model=StatsResp)
def stats() -> StatsResp:
    try:
        conn = engine.index_db_connection()
        try:
            counts = {
                name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("nodes", "edges", "aliases")
            }
        finally:
            conn.close()
    except engine.EngineUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))

    vstats = engine.vector_stats()
    embeddings_total = vstats.get("total", 0)
    embeddings_by_kind = vstats.get("by_kind", {})

    return StatsResp(
        nodes=counts["nodes"], edges=counts["edges"], aliases=counts["aliases"],
        embeddings_total=embeddings_total, embeddings_by_kind=embeddings_by_kind,
    )


@app.get("/api/health", response_model=HealthResp)
def health() -> HealthResp:
    cfg = engine.embeddings_config()
    return HealthResp(
        ok=True,
        semantic_available=engine.semantic_available(),
        model=cfg.get("model"),
        dim=cfg.get("dim"),
    )


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("KNEWRALL_VIEWER_PORT", "8798"))
    uvicorn.run(app, host="127.0.0.1", port=port)
