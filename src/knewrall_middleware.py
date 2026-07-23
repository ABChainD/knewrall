"""
Knewrall Middleware API

Acts as the bridge between AI agents (like Roo Code) and the file system,
enforcing strict validation and deterministic formatting.

AI agents must use this middleware exclusively—no direct file writes.
"""

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Union
import logging
from difflib import SequenceMatcher

from jsonschema import validate, ValidationError
from .knewrall_crud import (
    deterministic_json, generate_uuid, save_node, load_node, update_node,
    neuron_json_path, neuron_md_path,
)
from .knewrall_indexer import KnewrallIndexer, DEFAULT_NOTES_DIR
from .paths import get_root
from .knewrall_codegraph import KnewrallCodeGraph, DEFAULT_PROJECTS_DIR
from .knewrall_env import load_dotenv_once

# Make provider API keys (OPENROUTER_API_KEY, etc.) reachable regardless of the
# invoking harness — the engine talks to the embedding provider on its own.
load_dotenv_once()

# Load schemas
SCHEMAS_DIR = get_root() / "schemas"
MASTER_SCHEMA_PATH = SCHEMAS_DIR / "master.schema.json"
CONCRETE_NODE_SCHEMA_PATH = SCHEMAS_DIR / "concrete-node.schema.json"
REIFIED_EDGE_SCHEMA_PATH = SCHEMAS_DIR / "reified-edge.schema.json"

# Module-level notes directory — override in tests via patch('src.knewrall_middleware.NOTES_DIR', ...)
NOTES_DIR = DEFAULT_NOTES_DIR

logger = logging.getLogger(__name__)

# Global singletons (lazy-loaded)
_indexer_instance = None
_codegraph_instance = None
_vector_store_instance = None
_embed_adapter_instance = None

def get_indexer() -> KnewrallIndexer:
    """Return a singleton indexer instance."""
    global _indexer_instance
    if _indexer_instance is None:
        _indexer_instance = KnewrallIndexer()
        _indexer_instance.connect()
    return _indexer_instance


def get_codegraph() -> KnewrallCodeGraph:
    """Return a singleton code graph instance."""
    global _codegraph_instance
    if _codegraph_instance is None:
        _codegraph_instance = KnewrallCodeGraph()
        _codegraph_instance.connect()
    return _codegraph_instance


def get_vector_store():
    """Return a singleton VectorStore instance, or None if sqlite-vec unavailable.

    Before vectors.db exists yet, probes the configured model's actual output
    dimension and prefers that over whatever is in config.json. sqlite-vec
    bakes the vec0 table's dimension in at creation time — trusting a
    stale/wrong config value here means every future embed silently fails
    with a dimension mismatch until someone deletes the file by hand. Once
    vectors.db already exists, its baked-in dim is authoritative and no
    probing happens (that would cost an API call on every call).
    """
    global _vector_store_instance
    if _vector_store_instance is None:
        try:
            from .knewrall_vectors import VectorStore
            _load_embed_config()
            dim = _embed_dim()
            db_path = _resolve_vectors_db_path()
            if not db_path.exists():
                probed = _probe_embed_dim()
                if probed and probed != dim:
                    logger.info(
                        f"Probed embedding dim={probed} differs from config.json's "
                        f"{dim}; using the probed value for the new vectors.db."
                    )
                    dim = probed
                    _persist_embed_dim(dim)
            _vector_store_instance = VectorStore(db_path=db_path, dim=dim)
            _vector_store_instance.connect()
        except Exception as e:
            logger.warning(f"VectorStore unavailable: {e}")
    return _vector_store_instance


def get_embed_adapter():
    """Return a singleton EmbeddingAdapter, or None if not configured."""
    global _embed_adapter_instance
    if _embed_adapter_instance is None:
        try:
            _load_embed_config()
            from .knewrall_embeddings import EmbeddingAdapter
            cfg = _embed_config()
            _embed_adapter_instance = EmbeddingAdapter(
                provider=cfg.get("provider", "openrouter"),
                model=cfg.get("model"),
                base_url=cfg.get("base_url"),
            )
        except Exception as e:
            logger.warning(f"EmbeddingAdapter unavailable: {e}")
    return _embed_adapter_instance


_embed_config_cache: Optional[Dict] = None

def _load_embed_config():
    global _embed_config_cache
    if _embed_config_cache is None:
        cfg_path = get_root() / ".knewrall" / "config.json"
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                full = json.load(f)
            _embed_config_cache = full.get("embeddings", {})
        except Exception:
            _embed_config_cache = {}


def _embed_config() -> Dict:
    _load_embed_config()
    return _embed_config_cache or {}


def _embed_dim() -> int:
    return _embed_config().get("dim", 1536)


def _resolve_vectors_db_path() -> Path:
    rel = _embed_config().get("db_path", ".knewrall/vectors.db")
    p = Path(rel)
    return p if p.is_absolute() else get_root() / p


def _probe_embed_dim() -> Optional[int]:
    """Best-effort: ask the configured provider/model its real output
    dimension. Returns None (never raises) on any failure — callers fall
    back to whatever config.json already has."""
    try:
        adapter = get_embed_adapter()
        if adapter and adapter.is_available():
            return adapter.detect_dim()
    except Exception as e:
        logger.debug(f"Dim probe failed, falling back to configured dim: {e}")
    return None


def _persist_embed_dim(dim: int) -> None:
    """Write a probed dim back into config.json's embeddings block (and the
    in-process cache), so the confirmed value is recorded for humans/future
    runs — not load-bearing for correctness (vec0's own schema is what's
    actually authoritative once the file exists), just keeps config honest."""
    global _embed_config_cache
    cfg_path = get_root() / ".knewrall" / "config.json"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            full = json.load(f)
        full.setdefault("embeddings", {})["dim"] = dim
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(full, f, indent=2)
            f.write("\n")
        if _embed_config_cache is not None:
            _embed_config_cache["dim"] = dim
    except Exception as e:
        logger.warning(f"Could not persist probed dim to config.json: {e}")

def load_schema(path: Path) -> Dict:
    """Load a JSON schema from file."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to load schema {path}: {e}")
        raise RuntimeError(f"Schema loading failed: {e}")

def _fuzzy_match(a: str, b: str) -> float:
    """Return similarity ratio between two strings (0-1)."""
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


# ── Query-term embedding cache + time-boxed interactive embed calls ─────────
#
# Query text embedded at search time can't be precomputed in general (it's
# often novel text), but recall()/search_graph() terms are overwhelmingly
# names of things already in the graph -- so a cache keyed by the normalized
# term turns a repeat, or a pre-seeded (see embed_query_terms()), term into a
# zero-API-call hit. Combined with a hard wall-clock deadline on the live
# embed call, this bounds recall()/search_graph(hybrid=True) latency to a few
# seconds worst case instead of the tens-of-seconds-to-minutes a slow/
# unresponsive embedding provider could previously cause (see
# docs/PERF_FINDINGS.md for the investigation this replaced).

_QUERY_TERM_KIND = "query_term"

# Short, interactive-path settings: fail fast rather than inherit the
# generous bulk/offline defaults (30s timeout x 3 retries) meant for
# background indexing jobs -- a slow provider should degrade a live query to
# literal-only in seconds, not tens of seconds.
_INTERACTIVE_EMBED_TIMEOUT = 8
_INTERACTIVE_EMBED_MAX_RETRIES = 1
_INTERACTIVE_EMBED_DEADLINE = 9.0  # wall-clock cap layered on top of the above


def _normalize_query_term(term: str) -> str:
    return term.strip().lower()


def _query_cache_get(vs, term: str) -> Optional[List[float]]:
    if not vs:
        return None
    try:
        return vs.get_embedding(_QUERY_TERM_KIND, _normalize_query_term(term))
    except Exception as e:
        logger.warning(f"query-term cache read failed for {term!r}: {e}")
        return None


def _query_cache_put(vs, term: str, vector: List[float], model: str) -> None:
    if not vs or not vector:
        return
    try:
        key = _normalize_query_term(term)
        vs.upsert_embedding(_QUERY_TERM_KIND, key, key, vector, model)
    except Exception as e:
        logger.warning(f"query-term cache write failed for {term!r}: {e}")


def _embed_batch_with_deadline(adapter, texts: List[str], deadline_s: float,
                               timeout: int, max_retries: int
                               ) -> Tuple[Optional[List[List[float]]], bool]:
    """
    Run adapter.embed(texts) on a daemon thread with a wall-clock deadline.

    Returns (vectors_or_None, timed_out). On timeout, the HTTP call keeps
    running in the background on its daemon thread (dies with the process,
    never blocks interpreter exit -- unlike a concurrent.futures.
    ThreadPoolExecutor, whose worker threads are joined at interpreter exit)
    and its eventual result is simply discarded. Only the embed() call itself
    ever runs on that thread -- never VectorStore/KNN, whose sqlite3
    connection is opened with the default check_same_thread=True and is not
    safe to touch from another thread.
    """
    box: Dict[str, Any] = {}

    def _run():
        try:
            box["vectors"] = adapter.embed(texts, timeout=timeout, max_retries=max_retries)
        except Exception as e:
            logger.warning(f"Background embed call failed: {e}")
            box["vectors"] = None

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=deadline_s)
    if t.is_alive():
        return None, True
    return box.get("vectors"), False


def search_graph(query: str, hybrid: bool = False, limit: int = 20) -> List[Dict]:
    """
    Returns matching nodes from the SQLite index (canonical_name, aliases, tags).
    When hybrid=True and vectors are available, augments results with semantic KNN
    via Reciprocal Rank Fusion.

    Args:
        query: Search string (case-insensitive partial match).
        hybrid: If True, blend literal results with vector KNN (RRF).
        limit: Maximum number of results to return.

    Returns:
        List of node dictionaries with fields: id, canonical_name, type, aliases, tags.
    """
    if not query or not isinstance(query, str):
        return []

    indexer = get_indexer()
    conn = indexer.connect()
    cursor = conn.cursor()
    like_pattern = f"%{query}%"

    cursor.execute("""
        SELECT DISTINCT
            n.id,
            n.type,
            n.canonical_name,
            n.aliases,
            n.tags,
            n.full_legal_name
        FROM nodes n
        LEFT JOIN aliases a ON n.id = a.node_id
        WHERE n.canonical_name LIKE ?
           OR a.alias LIKE ?
           OR n.tags LIKE ?
        ORDER BY n.canonical_name
        LIMIT ?
    """, (like_pattern, like_pattern, like_pattern, limit))

    rows = cursor.fetchall()
    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "type": row["type"],
            "canonical_name": row["canonical_name"],
            "full_legal_name": row["full_legal_name"],
            "aliases": json.loads(row["aliases"]) if row["aliases"] else [],
            "tags": json.loads(row["tags"]) if row["tags"] else [],
        })

    if not hybrid:
        return results

    # ── Hybrid: blend with vector KNN ─────────────────────────────────────
    try:
        vs = get_vector_store()
        adapter = get_embed_adapter()
        if vs and adapter and adapter.is_available():
            qvec = _query_cache_get(vs, query)
            if qvec is None:
                vectors, timed_out = _embed_batch_with_deadline(
                    adapter, [query], _INTERACTIVE_EMBED_DEADLINE,
                    _INTERACTIVE_EMBED_TIMEOUT, _INTERACTIVE_EMBED_MAX_RETRIES,
                )
                if timed_out:
                    logger.info(f"search_graph: semantic leg timed out for {query!r}, using literal only")
                elif vectors and vectors[0]:
                    qvec = vectors[0]
                    _query_cache_put(vs, query, qvec, adapter.model)
            if qvec:
                results = vs.hybrid_search(
                    results, qvec, k=limit, id_field="id", kind_filter="neuron"
                )
                # hybrid_search() surfaces semantic-only hits (no literal match)
                # as thin {"id": ..., "_semantic_only": True} placeholders —
                # fill them out to the same 6-key shape every other result has,
                # so callers (the CLI printer, MCP tools) never see a partial dict.
                for i, r in enumerate(results):
                    if r.get("_semantic_only"):
                        row = indexer.get_node_by_id(r["id"])
                        results[i] = {
                            "id": r["id"],
                            "type": row["type"] if row else None,
                            "canonical_name": row["canonical_name"] if row else None,
                            "full_legal_name": row["full_legal_name"] if row else None,
                            "aliases": json.loads(row["aliases"]) if row and row["aliases"] else [],
                            "tags": json.loads(row["tags"]) if row and row["tags"] else [],
                        }
    except Exception as e:
        logger.warning(f"Hybrid search fell back to literal: {e}")

    return results


# Reciprocal-rank-fusion constant shared with knewrall_vectors.VectorStore.hybrid_search.
_RECALL_RRF_K = 60

# Bounded-output defaults for recall(). These exist specifically so a single
# recall() call can never reproduce the "read every matched file" token blowup
# it's meant to replace — full bodies are capped well below `limit` (which only
# governs candidate discovery), and neighbor summaries are capped independently.
_RECALL_MAX_FULL = 8
_RECALL_MAX_RELATED_PER_NEURON = 10
_RECALL_MAX_RELATED_TOTAL = 40
_RECALL_MAX_DESCRIPTION_CHARS = 600


def _truncate_text(value: Any, max_chars: int) -> Any:
    if not isinstance(value, str) or len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "…"


def _shape_neuron_for_recall(node: Dict, node_id: str, indexer: "KnewrallIndexer",
                             max_description_chars: int) -> Dict:
    """Flatten a raw neuron dict into a compact, recall-friendly shape.

    Drops system noise (checksum/version), truncates long free-text
    descriptions, and resolves each link's target_id to a human-readable
    `name (type)` instead of a bare UUID — so the agent never has to make a
    second call just to know what a link points at.
    """
    header = node.get("header", {}) or {}
    descriptions = node.get("descriptions", {}) or {}
    shaped: Dict[str, Any] = {
        "id": node_id,
        "type": header.get("type"),
        "canonical_name": header.get("canonical_name"),
        "aliases": header.get("aliases", []),
    }
    if header.get("full_legal_name"):
        shaped["full_legal_name"] = header["full_legal_name"]

    for key, value in descriptions.items():
        if value:
            shaped[f"description_{key}"] = _truncate_text(value, max_description_chars)

    properties = node.get("properties", {}) or {}
    if properties:
        flat_properties = {}
        for prop_key, entries in properties.items():
            if isinstance(entries, list) and entries:
                flat_properties[prop_key] = entries[0].get("value")
        if flat_properties:
            shaped["properties"] = flat_properties

    links = node.get("links", []) or []
    resolved_links = []
    for link in links:
        target_id = link.get("target_id")
        target_row = indexer.get_node_by_id(target_id) if target_id else None
        resolved_links.append({
            "predicate": link.get("predicate"),
            "direction": link.get("direction"),
            "target_name": target_row["canonical_name"] if target_row else target_id,
            "target_type": target_row["type"] if target_row else None,
            "target_id": target_id,
        })
    if resolved_links:
        shaped["links"] = resolved_links

    tags = node.get("tags", []) or []
    if tags:
        shaped["tags"] = tags

    return shaped


def recall(terms: Union[str, List[str]], depth: int = 1, limit: int = 20,
          hybrid: bool = True, max_full: int = _RECALL_MAX_FULL,
          max_related_per_neuron: int = _RECALL_MAX_RELATED_PER_NEURON,
          max_related_total: int = _RECALL_MAX_RELATED_TOTAL) -> Dict:
    """
    Consolidated retrieval for one or more keywords: a single call that finds,
    loads, and shapes the relevant neuron bodies (plus a bounded summary of
    their immediate links), so the caller does not need to read neuron files
    itself. Meant to replace the old search-graph -> per-id file-read loop.

    Ranking merges each term's literal + semantic hits via Reciprocal Rank
    Fusion, independently per term first (so one noisy/broad term can't drown
    out the others), then across terms. Output is deliberately bounded: only
    the top `max_full` matches get full bodies; everything else is dropped,
    not silently included, and `stats.truncated` says so.

    depth=0 returns only the matched neurons. depth=1 (default) also returns a
    capped list of their immediate link targets as light summaries (not full
    bodies) — the agent calls recall() again on a related name to expand it.

    Returns:
        {
          "query_terms": [...],
          "matched": [ ...shaped full neurons, most relevant first... ],
          "related": [ {id, canonical_name, type, predicate, via}, ... ],
          "stats": {terms_searched, candidates_found, matched_returned,
                     related_returned},
          "truncated": bool,  # more candidates existed than max_full returned
        }
    """
    if isinstance(terms, str):
        terms = [terms]
    terms = [t for t in terms if t and isinstance(t, str)]
    if not terms:
        return {
            "query_terms": [], "matched": [], "related": [],
            "stats": {"terms_searched": 0, "candidates_found": 0,
                      "matched_returned": 0, "related_returned": 0},
            "truncated": False,
        }

    indexer = get_indexer()

    # ── 1. Per-term literal search (independent, so each term gets its own
    #      rank list rather than one merged/diluted LIKE query). ─────────────
    per_term_literal: List[List[Dict]] = [search_graph(t, hybrid=False, limit=limit) for t in terms]

    # ── 2. Per-term semantic KNN, best-effort. ───────────────────────────────
    # Cache-miss terms are batched into ONE embedding API call (not one call
    # per term) and time-boxed, so a slow/unresponsive provider degrades to
    # literal-only in a few seconds rather than compounding N sequential
    # multi-second-to-many-second calls -- the root cause of the multi-minute
    # recall() calls this replaced (see docs/PERF_FINDINGS.md).
    per_term_semantic: List[List[Dict]] = [[] for _ in terms]
    semantic_pending = False
    if hybrid:
        try:
            vs = get_vector_store()
            adapter = get_embed_adapter()
            if vs and adapter and adapter.is_available():
                term_vectors: Dict[int, List[float]] = {}
                miss_idx: List[int] = []
                miss_terms: List[str] = []
                for idx, term in enumerate(terms):
                    cached = _query_cache_get(vs, term)
                    if cached is not None:
                        term_vectors[idx] = cached
                    else:
                        miss_idx.append(idx)
                        miss_terms.append(term)

                if miss_terms:
                    vectors, timed_out = _embed_batch_with_deadline(
                        adapter, miss_terms, _INTERACTIVE_EMBED_DEADLINE,
                        _INTERACTIVE_EMBED_TIMEOUT, _INTERACTIVE_EMBED_MAX_RETRIES,
                    )
                    if timed_out:
                        semantic_pending = True
                        logger.info(
                            f"recall: semantic leg timed out for {len(miss_terms)} term(s), "
                            "using literal only for those"
                        )
                    elif vectors:
                        for idx, term, vec in zip(miss_idx, miss_terms, vectors):
                            if vec:
                                term_vectors[idx] = vec
                                _query_cache_put(vs, term, vec, adapter.model)

                for idx, vec in term_vectors.items():
                    # kind_filter (not a post-hoc Python filter) so code/note
                    # embeddings can't crowd neuron hits out of the raw top-k
                    # before this ever sees them — see knn_search's docstring.
                    per_term_semantic[idx] = vs.knn_search(vec, k=limit, kind_filter="neuron")
        except Exception as e:
            logger.warning(f"recall: semantic search unavailable, using literal only: {e}")

    # ── 3. Cross-term RRF fusion: each of the 2*len(terms) ranked lists votes,
    #      by rank position within its own list, into one combined score. ────
    rrf_scores: Dict[str, float] = {}
    for result_list in (*per_term_literal, *per_term_semantic):
        for rank, item in enumerate(result_list, start=1):
            node_id = item.get("id") or item.get("ref_id")
            if not node_id:
                continue
            rrf_scores[node_id] = rrf_scores.get(node_id, 0.0) + 1.0 / (_RECALL_RRF_K + rank)

    ranked_ids = sorted(rrf_scores.keys(), key=lambda nid: rrf_scores[nid], reverse=True)
    truncated = len(ranked_ids) > max_full

    # ── 4. Load + shape full bodies for the top candidates only — this cap is
    #      what keeps recall() from reproducing the blowup it replaces. ──────
    matched: List[Dict] = []
    matched_ids = set()
    for node_id in ranked_ids[:max_full]:
        try:
            node = load_node(node_id)
        except Exception as e:
            # Stale index row (neuron deleted/moved, index not yet refreshed) —
            # skip it rather than fail the whole recall call.
            logger.warning(f"recall: could not load neuron {node_id}, skipping: {e}")
            continue
        matched.append(_shape_neuron_for_recall(node, node_id, indexer, _RECALL_MAX_DESCRIPTION_CHARS))
        matched_ids.add(node_id)

    # ── 5. Depth-1 related: capped summaries of matched neurons' links. ─────
    related: List[Dict] = []
    if depth >= 1:
        seen_related_keys = set()
        for neuron in matched:
            per_neuron_count = 0
            for link in neuron.get("links", []):
                if per_neuron_count >= max_related_per_neuron or len(related) >= max_related_total:
                    break
                target_id = link.get("target_id")
                if not target_id or target_id in matched_ids:
                    continue
                dedup_key = (target_id, link.get("predicate"))
                if dedup_key in seen_related_keys:
                    continue
                seen_related_keys.add(dedup_key)
                related.append({
                    "id": target_id,
                    "canonical_name": link.get("target_name"),
                    "type": link.get("target_type"),
                    "predicate": link.get("predicate"),
                    "via": neuron.get("canonical_name"),
                })
                per_neuron_count += 1
            if len(related) >= max_related_total:
                break

    stats = {
        "terms_searched": len(terms),
        "candidates_found": len(ranked_ids),
        "matched_returned": len(matched),
        "related_returned": len(related),
    }
    if semantic_pending:
        stats["semantic_pending"] = True

    return {
        "query_terms": terms,
        "matched": matched,
        "related": related,
        "stats": stats,
        "truncated": truncated,
    }


def refresh_index() -> Dict:
    """
    Orchestrates the incremental index refresh: runs
    KnewrallIndexer.refresh_index() (index-only — nodes/aliases/edges), then
    prunes vectors.db for anything it reported as deleted. The indexer itself
    has no dependency on the optional vector store, so that follow-up lives
    here, at the same integration layer as get_vector_store()/get_embed_adapter.

    Safe to call every session (e.g. from the SessionStart hook) — unlike
    rebuild-index, this only touches files whose content actually changed.
    """
    indexer = get_indexer()
    result = indexer.refresh_index()

    vectors_pruned = 0
    deleted_ids = result.get("deleted_neuron_ids", []) + result.get("deleted_note_source_ids", [])
    if deleted_ids:
        try:
            vs = get_vector_store()
            if vs:
                for node_id in result.get("deleted_neuron_ids", []):
                    if vs.delete_embedding("neuron", node_id):
                        vectors_pruned += 1
                for source_id in result.get("deleted_note_source_ids", []):
                    if vs.delete_embedding("note", source_id):
                        vectors_pruned += 1
        except Exception as e:
            logger.warning(f"refresh_index: vector pruning skipped: {e}")

    result["vectors_pruned"] = vectors_pruned
    return result


def propose_node(payload: Dict) -> Tuple[bool, str, Optional[str]]:
    """
    Validates payload against JSON schema, generates UUID, formats JSON deterministically,
    writes file to `neurons/`. Includes duplicate detection (fuzzy search >85% match returns warning).

    Args:
        payload: Dictionary representing a node (must contain 'header' and 'system' keys?).

    Returns:
        (success: bool, message: str, node_id: Optional[str])
        If success is True, node_id is the generated UUID.
        If success is False, node_id is None and message is error.
        If duplicate warning, success is True but message includes warning.
    """
    # Ensure system.id is not present (we generate)
    if "system" in payload and "id" in payload["system"]:
        # If ID provided, we could accept? Blueprint says generate UUID.
        # We'll treat as error because AI should not supply ID.
        return False, "Payload must not contain system.id; UUID will be generated.", None
    
    # Generate UUID and metadata
    node_id = generate_uuid()
    payload.setdefault("system", {})
    payload["system"]["id"] = node_id
    payload["system"]["version"] = 1  # enforce version 1
    
    # Add temporary last_updated for validation (save_node will overwrite with final)
    from .knewrall_crud import now_iso
    payload["system"]["last_updated"] = now_iso()
    
    # Now validate with the generated ID
    try:
        master_schema = load_schema(MASTER_SCHEMA_PATH)
        validate(instance=payload, schema=master_schema)
    except ValidationError as e:
        logger.warning(f"Schema validation failed: {e}")
        return False, f"Schema validation error: {e.message}", None
    except Exception as e:
        logger.error(f"Unexpected validation error: {e}")
        return False, f"Validation error: {e}", None

    # Duplicate detection: fuzzy search on canonical_name and aliases
    header = payload.get("header", {})
    canonical = header.get("canonical_name", "")
    aliases = header.get("aliases", [])
    all_names = [canonical] + aliases
    
    indexer = get_indexer()
    conn = indexer.connect()
    cursor = conn.cursor()
    
    # Fetch all existing canonical names and aliases for comparison
    cursor.execute("SELECT canonical_name FROM nodes")
    existing_canonicals = [row["canonical_name"] for row in cursor.fetchall()]
    cursor.execute("SELECT alias FROM aliases")
    existing_aliases = [row["alias"] for row in cursor.fetchall()]
    existing_all = existing_canonicals + existing_aliases
    
    warning = None
    for name in all_names:
        for existing in existing_all:
            similarity = _fuzzy_match(name, existing)
            if similarity > 0.85:
                # Find the node ID of the matched existing name
                cursor.execute("""
                    SELECT id FROM nodes WHERE canonical_name = ?
                    UNION
                    SELECT node_id FROM aliases WHERE alias = ?
                """, (existing, existing))
                row = cursor.fetchone()
                existing_id = row["id"] if row else "unknown"
                warning = f"Potential collision detected with {existing_id} (similarity {similarity:.2f}). Please populate the 'disambiguation' field to clarify the difference."
                break
        if warning:
            break
    
    # Save node using CRUD module
    try:
        file_path = save_node(payload)
        logger.info(f"Node saved: {file_path}")
    except Exception as e:
        logger.error(f"Failed to save node: {e}")
        return False, f"Failed to save node: {e}", None
    
    # Update index
    try:
        indexer.index_neuron_json(Path(file_path))
        conn.commit()
    except Exception as e:
        logger.warning(f"Node saved but index update failed: {e}")
        # Continue, because node is saved; index can be rebuilt later
    
    message = "Node created successfully."
    if warning:
        message = f"Node created successfully. Warning: {warning}"

    # Background: enqueue neuron for embedding (fire-and-forget, non-blocking)
    _try_embed_neuron(node_id, payload)

    return True, message, node_id

def update_node_fields(node_id: str, updates: Dict, mode: str = "replace") -> Tuple[bool, str]:
    """
    Applies a validated partial update to an existing node and re-saves it.

    Unlike propose_node (which creates), this loads the existing neuron, merges the
    given fields, validates the *complete resulting node* against the schema, and only
    then writes it back — so a bad update can never leave a neuron in an invalid state.

    Args:
        node_id: UUID of the neuron to update.
        updates: Dict of top-level fields to change. Supported keys: `header`,
                 `descriptions`, `spatiotemporal` (shallow-merged), `tags` (replaced
                 wholesale), and `properties` (see `mode`). Bare property values are
                 auto-wrapped as `{"value": ...}`; `links` are rejected — use
                 `propose_link` for those instead.
        mode: "replace" (default) — each properties key given fully replaces the
              existing value list, correcting a stale fact cleanly. "append" — new
              property values are added alongside existing ones instead (use this to
              preserve history, ideally tagging each value with `when`).

    Returns:
        (success: bool, message: str)
    """
    if mode not in ("replace", "append"):
        return False, "mode must be 'replace' or 'append'."

    if "system" in updates:
        return False, "updates must not contain 'system' — id/version/last_updated are middleware-managed."
    if "links" in updates:
        return False, "update-node does not modify links; use propose-link instead."

    try:
        node = load_node(node_id)
    except FileNotFoundError:
        return False, f"Node not found: {node_id}"

    merged = dict(node)

    if "properties" in updates:
        props = dict(merged.get("properties", {}))
        for key, val in updates["properties"].items():
            entries = val if isinstance(val, list) else [val]
            entries = [e if isinstance(e, dict) else {"value": e} for e in entries]
            if mode == "append" and key in props:
                existing = list(props[key])
                for e in entries:
                    if e not in existing:
                        existing.append(e)
                props[key] = existing
            else:
                props[key] = entries
        merged["properties"] = props

    for field in ("header", "descriptions", "spatiotemporal"):
        if field in updates:
            combined = dict(merged.get(field, {}))
            combined.update(updates[field])
            merged[field] = combined

    if "tags" in updates:
        merged["tags"] = updates["tags"]

    try:
        master_schema = load_schema(MASTER_SCHEMA_PATH)
        validate(instance=merged, schema=master_schema)
    except ValidationError as e:
        return False, f"Schema validation error: {e.message}"

    try:
        save_node(merged)
    except Exception as e:
        return False, f"Failed to save node: {e}"

    indexer = get_indexer()
    try:
        indexer.index_neuron_json(neuron_json_path(node_id))
        indexer.connect().commit()
    except Exception as e:
        logger.warning(f"Node updated but index refresh failed: {e}")

    # Background: re-embed since content changed (fire-and-forget, non-blocking)
    _try_embed_neuron(node_id, merged)

    return True, f"Node {node_id} updated ({mode} mode)."

def propose_link(source_id: str, target_id: str, relationship_type: str,
                 direction: str = "outbound", certainty: str = "confirmed", 
                 tags: Optional[List[str]] = None) -> Tuple[bool, str]:
    """
    Updates the respective JSON files' `links` arrays.
    Must validate that both nodes exist. For bidirectional links, updates both nodes.

    Args:
        source_id: UUID of source node.
        target_id: UUID of target node.
        relationship_type: Predicate string (e.g., "knows", "part_of").
        direction: "outbound", "inbound", or "bidirectional".
        certainty: "confirmed", "rumored", "hypothetical", "alternative".
        tags: Optional list of tags for the link.

    Returns:
        (success: bool, message: str)
    """
    # Validate UUID format (basic)
    for uid, label in [(source_id, "source_id"), (target_id, "target_id")]:
        try:
            uuid.UUID(uid)
        except ValueError:
            return False, f"{label} is not a valid UUID: {uid}"
    
    # Load both nodes to ensure existence
    try:
        source_node = load_node(source_id)
        target_node = load_node(target_id)
    except FileNotFoundError as e:
        return False, f"Node not found: {e}"
    except Exception as e:
        return False, f"Error loading node: {e}"
    
    tags = tags or []

    # Reject if this link already exists on the source node
    for existing_link in source_node.get("links", []):
        if (existing_link.get("target_id") == target_id and
                existing_link.get("predicate") == relationship_type):
            return False, (
                f"Link already exists between {source_id} and {target_id} "
                f"with predicate '{relationship_type}'"
            )

    def add_link_to_node(node_id: str, t_id: str, pred: str, dir: str, cert: str, tgs: List[str]):
        link_obj = {
            "target_id": t_id,
            "predicate": pred,
            "direction": dir,
            "certainty": cert,
            "tags": tgs
        }
        update_node(node_id, {"links": [link_obj]})

    try:
        # Add link to source node
        if direction in ["outbound", "bidirectional"]:
            add_link_to_node(source_id, target_id, relationship_type, direction, certainty, tags)

        # Add reverse link to target node for inbound/bidirectional.
        # "inbound" from source's perspective → "outbound" from target's perspective.
        if direction in ["inbound", "bidirectional"]:
            target_dir = "outbound" if direction == "inbound" else "bidirectional"
            add_link_to_node(target_id, source_id, relationship_type, target_dir, certainty, tags)
    except Exception as e:
        return False, f"Failed to update node: {e}"

    # Update index for both nodes
    indexer = get_indexer()
    try:
        indexer.index_neuron_json(neuron_json_path(source_id))
        indexer.index_neuron_json(neuron_json_path(target_id))
        indexer.connect().commit()
    except Exception as e:
        logger.warning(f"Links created but index update failed: {e}")
    
    return True, f"Link(s) created between {source_id} and {target_id} with predicate '{relationship_type}'"

def update_note_links(note_path: str, links: List[str]) -> Tuple[bool, str]:
    """
    Appends links to the original Markdown note in `notes/`.
    Uses standard markdown links to companion .md files for VS Code clickability:
    `[Canonical Name](../neurons/<shard>/[UUID].md)`

    Args:
        note_path: Relative path to note within notes/ directory (e.g., "2026/06/meeting.md").
        links: List of canonical names to link.

    Returns:
        (success: bool, message: str)
    """
    notes_dir = NOTES_DIR
    full_path = notes_dir / note_path
    if not full_path.exists():
        return False, f"Note file not found: {full_path}"

    # Read existing content
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError as e:
        return False, f"Cannot read note file: {e}"

    # Collect existing link display names to avoid duplicates.
    # Handles both [Display](target) markdown links and [[wikilink]] style.
    md_link_pattern = re.compile(r'\[([^\[\]]+)\]\(([^()]+)\)')
    wikilink_pattern = re.compile(r'\[\[([^\[\]\|]+)(?:\|[^\[\]]+)?\]\]')
    existing_normalized = set()
    for match in md_link_pattern.finditer(content):
        existing_normalized.add(match.group(1).strip())
    for match in wikilink_pattern.finditer(content):
        existing_normalized.add(match.group(1).strip())

    # Try to get a cursor for UUID resolution; failures fall back to [[wikilink]] format.
    cursor = None
    try:
        indexer = get_indexer()
        conn = indexer.connect()
        cursor = conn.cursor()
    except Exception:
        pass

    new_formatted_links = []
    for canonical in links:
        if canonical in existing_normalized:
            continue

        # Try to resolve canonical name to UUID for VS Code friendly link
        node_id = None
        if cursor is not None:
            try:
                cursor.execute("""
                    SELECT id FROM nodes WHERE canonical_name = ?
                    UNION
                    SELECT node_id FROM aliases WHERE alias = ?
                    LIMIT 1
                """, (canonical, canonical))
                row = cursor.fetchone()
                if row:
                    node_id = row["id"]
            except Exception:
                pass

        if node_id:
            # Build a relative link from this note's location to the neuron's
            # sharded companion .md. Using relpath keeps links correct no matter
            # how deep the note sits (e.g. notes/YYYY/MM/note.md).
            rel = os.path.relpath(neuron_md_path(node_id), full_path.parent)
            new_formatted_links.append(f"[{canonical}]({Path(rel).as_posix()})")
        else:
            new_formatted_links.append(f"[[{canonical}]]")
    
    if not new_formatted_links:
        return True, "All links already present in note."
    
    # Append new links at the end of file
    if content and not content.endswith('\n'):
        content += '\n'
    if content and not content.endswith('\n\n'):
        content += '\n'
    
    for link_str in new_formatted_links:
        content += f"{link_str}\n"
    
    # Write back
    try:
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except OSError as e:
        return False, f"Cannot write note file: {e}"
    
    return True, f"Added {len(new_formatted_links)} link(s) to note."

# CLI integration helpers
def cli_search(query: str) -> None:
    """CLI wrapper for search_graph."""
    results = search_graph(query)
    if not results:
        print("No matches.")
    else:
        for r in results:
            print(f"{r['id']} | {r['type']} | {r['canonical_name']} | aliases: {r['aliases']}")

def cli_propose_node(json_file: str) -> None:
    """CLI wrapper for propose_node."""
    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except Exception as e:
        print(f"Error reading JSON: {e}")
        return
    success, msg, node_id = propose_node(payload)
    if success:
        print(f"SUCCESS: {msg}")
        if node_id:
            print(f"Node ID: {node_id}")
    else:
        print(f"FAILURE: {msg}")

def cli_propose_link(source: str, target: str, predicate: str, direction: str = "outbound", certainty: str = "confirmed") -> None:
    """CLI wrapper for propose_link."""
    success, msg = propose_link(source, target, predicate, direction, certainty)
    if success:
        print(f"SUCCESS: {msg}")
    else:
        print(f"FAILURE: {msg}")

def cli_update_note_links(note_path: str, links: List[str]) -> None:
    """CLI wrapper for update_note_links."""
    success, msg = update_note_links(note_path, links)
    if success:
        print(f"SUCCESS: {msg}")
    else:
        print(f"FAILURE: {msg}")


# ── Embedding helpers ─────────────────────────────────────────────────────────

def _try_embed_neuron(node_id: str, node_data: dict) -> None:
    """Non-blocking: try to embed a neuron after it's saved. Silently skips failures."""
    try:
        vs = get_vector_store()
        adapter = get_embed_adapter()
        if not vs or not adapter or not adapter.is_available():
            return
        from .knewrall_vectors import neuron_to_embed_text
        text = neuron_to_embed_text(node_data)
        if not text:
            return
        model = _embed_config().get("model", "unknown")
        vec = adapter.embed_one(text)
        if vec and vs.upsert_embedding("neuron", node_id, text, vec, model):
            # embed_and_store() checkpoints after its batch; this single-shot
            # fire-and-forget path has no batch boundary of its own, so it
            # must checkpoint itself — otherwise a long-lived process (the
            # MCP server, a long session doing several propose-node calls)
            # accumulates writes only in the unsynced -wal file.
            vs.checkpoint()
    except Exception as e:
        logger.debug(f"embed_neuron skipped for {node_id}: {e}")


def embed_neurons(node_ids: Optional[List[str]] = None) -> Tuple[int, int]:
    """
    Embed all neurons (or a specific list).  Skips unchanged content.
    Returns (embedded_count, skipped_count).
    """
    vs = get_vector_store()
    adapter = get_embed_adapter()
    if not vs or not adapter or not adapter.is_available():
        return 0, 0

    from .knewrall_vectors import neuron_to_embed_text
    from .knewrall_crud import list_nodes, load_node as _load

    ids = node_ids or list_nodes()
    model = _embed_config().get("model", "unknown")
    items = []
    for nid in ids:
        try:
            data = _load(nid)
            text = neuron_to_embed_text(data)
            if text:
                items.append(("neuron", nid, text))
        except Exception:
            pass

    embedded = vs.embed_and_store(items, adapter, model)
    return embedded, len(items) - embedded


def embed_code_symbols(repo: Optional[str] = None) -> Tuple[int, int]:
    """
    Embed code symbols from codegraph.db.  Skips unchanged content.
    Returns (embedded_count, skipped_count).
    """
    vs = get_vector_store()
    adapter = get_embed_adapter()
    if not vs or not adapter or not adapter.is_available():
        return 0, 0

    from .knewrall_vectors import code_symbol_to_embed_text
    cg = get_codegraph()
    conn = cg.connect()
    q = "SELECT symbol_id, name, qualified_name, signature, docstring FROM code_symbols"
    params: list = []
    if repo:
        q += " WHERE repo=?"
        params.append(repo)
    rows = conn.execute(q, params).fetchall()

    model = _embed_config().get("model", "unknown")
    items = []
    for row in rows:
        text = code_symbol_to_embed_text(dict(row))
        if text:
            items.append(("code_symbol", row["symbol_id"], text))

    embedded = vs.embed_and_store(items, adapter, model)
    return embedded, len(items) - embedded


def embed_query_terms() -> Tuple[int, int]:
    """
    Proactively embeds every canonical_name + alias + tag string already in
    the graph as a query_term cache entry (see _query_cache_get/_put above
    and docs/VECTOR_SEARCH.md). recall()/search-graph terms are overwhelmingly
    names of things already in the graph, so this turns the FIRST search for
    an already-known entity into a cache hit too, not just repeats.

    Wired into the `embed` CLI command alongside embed_neurons/
    embed_code_symbols — intentionally NOT run automatically during
    refresh-index, which stays local-only and must not depend on embedding-
    provider availability. Returns (embedded_count, skipped_count).
    """
    vs = get_vector_store()
    adapter = get_embed_adapter()
    if not vs or not adapter or not adapter.is_available():
        return 0, 0

    indexer = get_indexer()
    conn = indexer.connect()
    rows = conn.execute("SELECT canonical_name, aliases, tags FROM nodes").fetchall()

    # Dedup by normalized key so case-variant duplicates (e.g. a canonical_name
    # that's also listed as its own alias with different casing) aren't
    # embedded twice.
    terms_by_key: Dict[str, str] = {}
    for row in rows:
        candidates = [row["canonical_name"]] if row["canonical_name"] else []
        candidates.extend(json.loads(row["aliases"]) if row["aliases"] else [])
        candidates.extend(json.loads(row["tags"]) if row["tags"] else [])
        for term in candidates:
            if term:
                terms_by_key.setdefault(_normalize_query_term(term), term)

    model = _embed_config().get("model", "unknown")
    items = [(_QUERY_TERM_KIND, key, term) for key, term in terms_by_key.items()]
    embedded = vs.embed_and_store(items, adapter, model)
    return embedded, len(items) - embedded


def embed_reconcile(conflict_db_path: str) -> Tuple[bool, str]:
    """
    Recover embeddings from a Syncthing sync-conflict copy of vectors.db that
    are missing locally (never overwrites a local embedding). See
    VectorStore.reconcile() for why this can't just be a plain file copy.
    """
    vs = get_vector_store()
    if not vs:
        return False, "Vector store unavailable (sqlite-vec not installed?)."
    path = Path(conflict_db_path)
    recovered = vs.reconcile(path)
    return True, f"Recovered {recovered} embedding(s) from {path.name}."


# ── Code graph middleware functions ───────────────────────────────────────────

def index_code(repo_path: Optional[str] = None, full: bool = False) -> Tuple[bool, str]:
    """
    Build or refresh the code symbol graph.

    Args:
        repo_path: Path to a specific repo to index (default: all projects/ subdirs).
        full: If True, drop and rebuild from scratch.

    Returns:
        (success, message)
    """
    cg = get_codegraph()
    try:
        if repo_path:
            p = Path(repo_path)
            counts = cg.index_repo(p, force=full)
            msg = f"Indexed repo {p.name}: {counts}"
        else:
            if full:
                counts = cg.rebuild_code_index()
            else:
                roots = cg._default_roots()
                counts: dict = {"repos": 0, "indexed": 0, "skipped": 0, "errors": 0}
                for root in roots:
                    if root.is_dir():
                        c = cg.index_repo(root)
                        counts["repos"] += 1
                        for k in ("indexed", "skipped", "errors"):
                            counts[k] += c[k]
            msg = f"Code index updated: {counts}"
        return True, msg
    except Exception as e:
        return False, f"Code index failed: {e}"


def code_search(query: str, limit: int = 20) -> List[Dict]:
    """FTS search over code symbols."""
    cg = get_codegraph()
    return cg.search_symbols(query, limit=limit)


def code_defs(name: str, repo: Optional[str] = None) -> List[Dict]:
    """Return symbol definitions matching name."""
    return get_codegraph().get_definitions(name, repo)


def code_callers(symbol_id: str) -> List[Dict]:
    """Return symbols that call this symbol."""
    return get_codegraph().get_callers(symbol_id)


def code_callees(symbol_id: str) -> List[Dict]:
    """Return symbols called by this symbol."""
    return get_codegraph().get_callees(symbol_id)


def code_imports(rel_path: str, repo: Optional[str] = None) -> List[Dict]:
    """Return import edges from a file."""
    return get_codegraph().get_imports(rel_path, repo)


def code_stats() -> Dict:
    """Return code graph statistics."""
    return get_codegraph().stats()


def link_code(neuron_id: str, symbol_id: str, repo: str,
              kind: str = "function") -> Tuple[bool, str]:
    """
    Attach a code_ref entry to an existing neuron.
    Warns (but does not block) if symbol_id is not found in codegraph.db.

    Args:
        neuron_id: UUID of the target neuron.
        symbol_id: Code symbol id (rel_path::qualified_name).
        repo: Repository name.
        kind: Symbol kind (function|class|method|module|other).

    Returns:
        (success, message)
    """
    try:
        node = load_node(neuron_id)
    except FileNotFoundError:
        return False, f"Neuron not found: {neuron_id}"

    # Warn if symbol not in code graph
    cg = get_codegraph()
    sym = cg.get_symbol(symbol_id)
    warning = ""
    if sym is None:
        warning = (
            f" Warning: symbol_id {symbol_id!r} not found in codegraph.db — "
            "run 'index-code' to refresh, or verify the id."
        )

    new_ref = {"symbol_id": symbol_id, "repo": repo, "kind": kind}
    existing = node.get("code_refs", [])

    # Dedup by symbol_id
    if any(r.get("symbol_id") == symbol_id for r in existing):
        return True, f"code_ref already present on neuron {neuron_id}.{warning}"

    existing.append(new_ref)
    update_node(neuron_id, {"code_refs": existing})

    # Re-index so the neuron's new state is visible
    try:
        indexer = get_indexer()
        indexer.index_neuron_json(neuron_json_path(neuron_id))
        indexer.connect().commit()
    except Exception as e:
        logger.warning(f"link_code: index update failed: {e}")

    return True, f"code_ref {symbol_id!r} linked to neuron {neuron_id}.{warning}"