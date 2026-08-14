"""
Knewrall Middleware API

Acts as the bridge between AI agents (like Roo Code) and the file system,
enforcing strict validation and deterministic formatting.

AI agents must use this middleware exclusively—no direct file writes.
"""

import fnmatch
import json
import os
import sys
import re
import threading
import uuid
from datetime import datetime as _datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Union
import logging
from difflib import SequenceMatcher

from jsonschema import validate, ValidationError, Draft7Validator, FormatChecker, RefResolver
from .knewrall_crud import (
    deterministic_json, generate_uuid, save_node, load_node, update_node,
    neuron_json_path, neuron_md_path,
)
from .knewrall_indexer import KnewrallIndexer, DEFAULT_NOTES_DIR
from .paths import get_root
from .knewrall_codegraph import KnewrallCodeGraph, DEFAULT_PROJECTS_DIR
from .knewrall_env import load_dotenv_once
from . import knewrall_engrams as _engrams
from . import knewrall_fold_router as _fold_router
from . import knewrall_reader as _reader

# Make provider API keys (OPENROUTER_API_KEY, etc.) reachable regardless of the
# invoking harness — the engine talks to the embedding provider on its own.
load_dotenv_once()

# jsonschema's built-in "date-time" format check is a silent no-op unless the
# optional `rfc3339-validator` package is installed — and it isn't here, on
# either machine. So a bare `validate(...)` treats `format: date-time` as
# decoration and accepts `valid_from="banana"`. Register a dependency-free
# RFC 3339 checker and validate THROUGH it, so the reification date fields
# (recorded_at / valid_from / valid_until) are actually enforced as the resolved
# open-question #3 requires — not left as the bare strings that answer rejected.
_RFC3339_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)
_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time")
def _is_rfc3339_datetime(value) -> bool:
    # Non-strings are the `type` keyword's job, not the format checker's.
    if not isinstance(value, str):
        return True
    # Shape first: requires a real time component + Z/offset, so a bare date
    # ("2026-08-08") is rejected.
    if not _RFC3339_DATETIME_RE.match(value):
        return False
    # Shape-only would accept impossible instants like 2026-13-45T99:99:99Z
    # (N4). Parse it to reject out-of-range dates/times. Normalize Z→+00:00 and
    # trim any over-long fractional part (the RE already vetted its shape) so a
    # legitimate value still range-checks across Python versions.
    probe = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00").replace("z", "+00:00"))
    try:
        _datetime.fromisoformat(probe)
    except ValueError:
        return False
    return True


def _validate_node(instance: Dict) -> None:
    """Schema-validate a node WITH format enforcement (see _FORMAT_CHECKER).
    Raises jsonschema.ValidationError on failure, exactly like validate()."""
    master_schema = load_schema(MASTER_SCHEMA_PATH)
    Draft7Validator(master_schema, format_checker=_FORMAT_CHECKER).validate(instance)


def _validate_link_obj(link_obj: Dict) -> None:
    """Schema-validate a single link object against the master schema's
    `links.items` subschema, WITH format enforcement. Raises ValidationError.

    This is what actually guards the propose_link WRITE path: crud.update_node
    performs no schema validation, so without this a malformed field on a link
    (a non-UUID `via_node_id` (B1), an unknown assertion key or non-string date
    or null certainty (B2)) is written straight to the neuron file — poisoning
    it so every later update-node on that node is rejected. Validating the link
    in isolation (rather than the whole merged node) means it doesn't depend on
    the rest of the node already being schema-valid. A RefResolver rooted at the
    full schema lets the subschema's `$ref: #/$defs/assertion` resolve."""
    master_schema = load_schema(MASTER_SCHEMA_PATH)
    link_schema = master_schema["properties"]["links"]["items"]
    resolver = RefResolver.from_schema(master_schema)
    Draft7Validator(link_schema, resolver=resolver, format_checker=_FORMAT_CHECKER).validate(link_obj)

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
# PERF_FINDINGS.md for the investigation this replaced).

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
                             max_description_chars: int,
                             include_assertions: bool = False) -> Dict:
    """Flatten a raw neuron dict into a compact, recall-friendly shape.

    Drops system noise (checksum/version), truncates long free-text
    descriptions, and resolves each link's target_id to a human-readable
    `name (type)` instead of a bare UUID — so the agent never has to make a
    second call just to know what a link points at.

    Assertion blocks are elided by default (opt in via include_assertions)
    to keep recall output within token budget.
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
        property_assertions = {}
        for prop_key, entries in properties.items():
            if isinstance(entries, list) and entries:
                flat_properties[prop_key] = entries[0].get("value")
                # X6: property-value assertions were write-only — never surfaced
                # by recall even with include_assertions. Surface them so provenance
                # on a property is readable, symmetric with link assertions.
                if include_assertions and entries[0].get("assertion"):
                    property_assertions[prop_key] = entries[0]["assertion"]
        if flat_properties:
            shaped["properties"] = flat_properties
        if property_assertions:
            shaped["property_assertions"] = property_assertions

    links = node.get("links", []) or []
    resolved_links = []
    supersession_links = []
    for link in links:
        target_id = link.get("target_id")
        target_row = indexer.get_node_by_id(target_id) if target_id else None
        predicate = link.get("predicate")
        if predicate in RESERVED_PREDICATES:
            supersession_links.append({
                "predicate": predicate,
                "target_name": target_row["canonical_name"] if target_row else target_id,
                "target_id": target_id,
                "certainty": link.get("certainty"),
            })
            continue
        resolved_link = {
            "predicate": predicate,
            "direction": link.get("direction"),
            "target_name": target_row["canonical_name"] if target_row else target_id,
            "target_type": target_row["type"] if target_row else None,
            "target_id": target_id,
        }
        if include_assertions and link.get("assertion"):
            resolved_link["assertion"] = link["assertion"]
        if link.get("valid_from"):
            resolved_link["valid_from"] = link["valid_from"]
        if link.get("valid_until"):
            resolved_link["valid_until"] = link["valid_until"]
        resolved_links.append(resolved_link)
    if resolved_links:
        shaped["links"] = resolved_links
    if supersession_links:
        shaped["supersession"] = supersession_links

    # Reverse view (N3 / spec §7.4): reserved predicates are outbound-only, so a
    # claim that another neuron supersedes/contradicts/corroborates can only find
    # out via the INBOUND edge. Query edges pointing at this node and surface them
    # (relation phrased from this node's perspective) — otherwise a superseded
    # claim could never show what replaced it.
    _REVERSE_RELATION = {
        "supersedes": "superseded_by",
        "contradicts": "contradicted_by",
        "corroborates": "corroborated_by",
    }
    reverse_claims = []
    try:
        inbound_edges = indexer.get_edges(target_id=node_id, link_type="node_link")
        for edge in inbound_edges:
            pred = edge.get("predicate")
            if pred in RESERVED_PREDICATES:
                src_id = edge.get("source_id")
                src_row = indexer.get_node_by_id(src_id) if src_id else None
                reverse_claims.append({
                    "relation": _REVERSE_RELATION[pred],
                    "source_name": src_row["canonical_name"] if src_row else src_id,
                    "source_id": src_id,
                    "certainty": edge.get("certainty"),
                })
    except Exception:
        # Reverse view is best-effort enrichment; a missing/malformed edge index
        # (or a mock indexer in tests) must never break the core recall shape.
        reverse_claims = []
    if reverse_claims:
        shaped["reverse_claims"] = reverse_claims

    tags = node.get("tags", []) or []
    if tags:
        shaped["tags"] = tags

    return shaped


def recall(terms: Union[str, List[str]], depth: int = 1, limit: int = 20,
          hybrid: bool = True, max_full: int = _RECALL_MAX_FULL,
          max_related_per_neuron: int = _RECALL_MAX_RELATED_PER_NEURON,
          max_related_total: int = _RECALL_MAX_RELATED_TOTAL,
          include_assertions: bool = False) -> Dict:
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
    # recall() calls this replaced (see PERF_FINDINGS.md).
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
        matched.append(_shape_neuron_for_recall(node, node_id, indexer, _RECALL_MAX_DESCRIPTION_CHARS,
                                                include_assertions=include_assertions))
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
        _validate_node(payload)
    except ValidationError as e:
        logger.warning(f"Schema validation failed: {e}")
        return False, f"Schema validation error: {e.message}", None
    except Exception as e:
        logger.error(f"Unexpected validation error: {e}")
        return False, f"Validation error: {e}", None

    # Hold inline links to the same reification rules propose_link enforces
    # (reserved-predicate direction/certainty, assertion sources whitelist,
    # warn-not-block). The JSON schema alone can't express these, so a node
    # created with links[] would otherwise bypass them entirely (G1).
    for link in payload.get("links", []):
        rule_error = _validate_link_rules(link)
        if rule_error:
            return False, f"Invalid link ({link.get('predicate')!r} -> {link.get('target_id')!r}): {rule_error}", None

    # Symmetry (B3): property-value assertions get the same checks a link's does.
    prop_err = _validate_property_assertions(payload.get("properties"))
    if prop_err:
        return False, prop_err, None

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
    # X8: reject unknown top-level keys instead of silently ignoring them while
    # still reporting SUCCESS (a typo'd field would look applied but be dropped).
    _UPDATABLE_FIELDS = {"header", "descriptions", "spatiotemporal", "tags", "properties"}
    unknown = set(updates) - _UPDATABLE_FIELDS - {"system", "links"}
    if unknown:
        return False, (f"update-node does not support field(s): {sorted(unknown)}. "
                       f"Updatable: {sorted(_UPDATABLE_FIELDS)}.")

    try:
        node = load_node(node_id)
    except FileNotFoundError:
        return False, f"Node not found: {node_id}"

    merged = dict(node)

    if "properties" in updates:
        props = dict(merged.get("properties", {}))
        # Normalize the entries introduced by THIS update, keyed by property.
        new_entries_by_key = {}
        for key, val in updates["properties"].items():
            entries = val if isinstance(val, list) else [val]
            entries = [e if isinstance(e, dict) else {"value": e} for e in entries]
            new_entries_by_key[key] = entries
        # X1: hold property-value assertions to the same source-prefix / neuron-UUID
        # checks propose_node applies (update-node used to skip this entirely). But
        # validate ONLY the entries this call introduces, never the whole merged
        # block (Y2): a pre-existing bad legacy assertion on an untouched property
        # must not brick an unrelated update (tags, header, ...). Sorting sources
        # here — before the append dedup below — also makes --append idempotent
        # (Y3: a reordered payload no longer appends a byte-identical duplicate).
        prop_err = _validate_property_assertions(new_entries_by_key)
        if prop_err:
            return False, prop_err
        for key, entries in new_entries_by_key.items():
            if mode == "append" and key in props:
                existing = list(props[key])
                # W2: dedup by the CANONICAL (recursively deterministic) form, not
                # raw `e not in existing`. On save, crud._deterministic_sort
                # normalizes nested lists/dicts, so two entries differing only in
                # nested ordering are identical on disk — a raw comparison would
                # still append a byte-identical duplicate. Sorting only
                # `assertion.sources` (Y3) covered one field; this covers the class.
                def _canon(x):
                    # V2: some values can't be canonicalized (e.g. a nested list of
                    # mixed types _deterministic_sort can't sort). Never let that
                    # raise a raw traceback out of --append — return None so the
                    # entry skips dedup and _validate_node below rejects it with a
                    # clean (False, msg), matching replace-mode and propose-node.
                    try:
                        return deterministic_json(x)
                    except Exception:
                        return None
                existing_canon = {c for c in (_canon(x) for x in existing) if c is not None}
                for e in entries:
                    c = _canon(e)
                    if c is None or c not in existing_canon:
                        existing.append(e)
                        if c is not None:
                            existing_canon.add(c)
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
        _validate_node(merged)
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

RESERVED_PREDICATES = {"supersedes", "contradicts", "corroborates"}
VALID_SOURCE_PREFIXES = ("neuron:", "note:", "code:", "url:", "conv:", "text:")
VALID_CERTAINTY = ("confirmed", "rumored", "hypothetical", "alternative")
VALID_DIRECTIONS = ("outbound", "inbound", "bidirectional")


def _validate_assertion_block(assertion: Dict, node_id_for_warn: Optional[str] = None) -> Optional[str]:
    """Validate an assertion block's sources prefix whitelist and warn-not-block
    on unresolvable neuron:/code: refs. Returns a blocking-error string, or None."""
    # X4: a non-dict assertion (e.g. a bare string passed programmatically) must
    # be a clean rejection, not an AttributeError from .get() below.
    if not isinstance(assertion, dict):
        return "assertion must be an object"
    sources = assertion.get("sources", [])
    if not isinstance(sources, list):
        return "assertion.sources must be a list"
    for src in sources:
        if not isinstance(src, str) or not src.startswith(VALID_SOURCE_PREFIXES):
            return f"source {src!r} does not match a valid prefix: {VALID_SOURCE_PREFIXES}"
        if src.startswith("neuron:"):
            ref_id = src[len("neuron:"):]
            try:
                uuid.UUID(ref_id)
            except ValueError:
                return f"neuron: source has invalid UUID: {ref_id!r}"
            try:
                load_node(ref_id)
            except FileNotFoundError:
                logger.warning(f"assertion source {src!r} references non-existent neuron (warn-not-block)")
        elif src.startswith("code:"):
            # X2: the codegraph DB (codegraph.db) is per-machine and .stignore'd,
            # so on any root where `index-code` never ran the table doesn't exist.
            # That must stay warn-not-block (as documented), not crash the write
            # with an unhandled sqlite OperationalError.
            try:
                cg = get_codegraph()
                sym = cg.get_symbol(src[len("code:"):])
            except Exception as e:
                logger.warning(f"assertion source {src!r} could not be resolved against codegraph.db "
                               f"({e}); warn-not-block")
                sym = None
            if sym is None:
                logger.warning(f"assertion source {src!r} not found in codegraph.db (warn-not-block)")
    return None


def _validate_property_assertions(properties: Optional[Dict]) -> Optional[str]:
    """Validate (and deterministically sort the sources of) every assertion block
    attached to a property value. Returns a blocking-error string, or None.

    Shared by propose_node and update_node_fields so both hold property-value
    assertions to the same source-prefix / neuron-UUID checks a link assertion
    gets — the JSON schema only vets the source *prefix*, not the UUID after
    `neuron:` (B3/X1). update_node_fields previously skipped this entirely."""
    for prop_key, entries in (properties or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            prop_assertion = entry.get("assertion") if isinstance(entry, dict) else None
            if prop_assertion:
                warn = _validate_assertion_block(prop_assertion)
                if warn:
                    return f"Invalid assertion on property {prop_key!r}: {warn}"
                if isinstance(prop_assertion, dict) and prop_assertion.get("sources"):
                    prop_assertion["sources"] = sorted(prop_assertion["sources"])
    return None


def _validate_via_node_id(via_id: Optional[str]) -> Optional[str]:
    """Block on a non-string via_node_id; warn-not-block if it references a
    non-existent neuron. Returns a blocking-error string, or None."""
    if via_id is None:
        return None
    # W3: a truthy non-string via_node_id (int, dict, ...) used to crash with an
    # AttributeError from deep inside load_node instead of being rejected. Guard
    # the type here (the X4 class, one field over).
    if not isinstance(via_id, str):
        return f"via_node_id must be a string UUID, got {type(via_id).__name__}"
    # V5: an empty or non-UUID string used to slip through here (existence-only
    # check) and then get silently dropped by build_link_obj's `if via_node_id:`,
    # so propose_link returned SUCCESS having written nothing — while the inline
    # node path rejected it. Validate the UUID format at the rule layer so both
    # paths agree.
    try:
        uuid.UUID(via_id)
    except ValueError:
        return f"via_node_id is not a valid UUID: {via_id!r}"
    try:
        load_node(via_id)
    except FileNotFoundError:
        logger.warning(f"via_node_id {via_id!r} references non-existent neuron (warn-not-block)")
    return None


def _validate_link_rules(link: Dict) -> Optional[str]:
    """Enforce the reification link rules on a single link dict, and normalize it
    (sort assertion.sources for deterministic output). Returns a blocking error
    string, or None if the link is acceptable. Warn-not-block conditions
    (unresolvable sources / via_node_id) log and pass.

    This is the AUTHORITATIVE link validator, shared by propose_link and
    propose_node. It must not rely on downstream schema validation: the
    propose_link write path (crud.update_node) does NOT re-validate against the
    schema, so date/certainty enforcement that lived only in the JSON schema was
    silently skipped there — invalid `valid_from`/`recorded_at`/`certainty` got
    written to disk (N1). So those checks are duplicated here, at the choke point
    both paths pass through."""
    predicate = link.get("predicate")
    # V3: a non-string predicate (list/dict) is unhashable — `predicate in
    # RESERVED_PREDICATES` (a set) would raise TypeError. Reject the type first.
    if not isinstance(predicate, str) or not predicate:
        return f"predicate must be a non-empty string, got {predicate!r}"
    if predicate in RESERVED_PREDICATES:
        if link.get("direction") != "outbound":
            return f"Reserved predicate {predicate!r} requires direction='outbound'"
        if not link.get("certainty"):
            return f"Reserved predicate {predicate!r} requires certainty"
    # W4: an out-of-enum direction used to fall through the outbound/inbound/
    # bidirectional dispatch and write NOTHING while returning success. Validate
    # the enum here (symmetric with certainty) so it's a clean rejection.
    direction = link.get("direction")
    if direction is not None and direction not in VALID_DIRECTIONS:
        return f"direction {direction!r} not one of {VALID_DIRECTIONS}"
    certainty = link.get("certainty")
    if certainty is not None and certainty not in VALID_CERTAINTY:
        return f"certainty {certainty!r} not one of {VALID_CERTAINTY}"
    for field in ("valid_from", "valid_until"):
        val = link.get(field)
        # V8: require a present value to be a STRING that is valid RFC 3339.
        # _is_rfc3339_datetime returns True for non-strings (it defers those to
        # the schema's type keyword), so a falsy non-string like [] or {} slipped
        # this check and was then silently dropped by build_link_obj's truthiness
        # test — success with the bad value gone (the Y1/V5 class, on the date
        # fields). Reject any non-None value that isn't a valid RFC 3339 string.
        if val is not None and not (isinstance(val, str) and _is_rfc3339_datetime(val)):
            return f"{field} {val!r} is not an RFC 3339 date-time (e.g. 2026-08-08T10:00:00Z)"
    assertion = link.get("assertion")
    # Y1: `if assertion:` let FALSY non-dicts (0, '', False, []) slip through —
    # propose_link then silently dropped the provenance and returned SUCCESS,
    # while the node paths rejected the same value. Check `is not None` so any
    # present-but-non-dict assertion is rejected by _validate_assertion_block.
    if assertion is not None:
        warn = _validate_assertion_block(assertion)
        if warn:
            return f"Invalid assertion: {warn}"
        recorded_at = assertion.get("recorded_at")
        if recorded_at is not None and not _is_rfc3339_datetime(recorded_at):
            return f"assertion.recorded_at {recorded_at!r} is not an RFC 3339 date-time"
        if assertion.get("sources"):
            assertion["sources"] = sorted(assertion["sources"])
    if link.get("via_node_id") is not None:
        via_err = _validate_via_node_id(link["via_node_id"])
        if via_err:
            return via_err
    return None


def propose_link(source_id: str, target_id: str, relationship_type: str,
                 direction: str = "outbound", certainty: str = "confirmed", 
                 tags: Optional[List[str]] = None,
                 assertion: Optional[Dict] = None,
                 valid_from: Optional[str] = None,
                 valid_until: Optional[str] = None,
                 via_node_id: Optional[str] = None) -> Tuple[bool, str]:
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
        assertion: Optional assertion block (sources, recorded_at, recorded_by, note).
        valid_from: Optional ISO-8601 start of validity window.
        valid_until: Optional ISO-8601 end of validity window.
        via_node_id: Optional UUID of reifying Why/How neuron.

    Returns:
        (success: bool, message: str)
    """
    # Validate UUID format (basic). V3: a NON-string source_id/target_id makes
    # uuid.UUID() raise TypeError/AttributeError, not ValueError — widen the
    # catch so a non-string id rejects cleanly instead of crashing out of
    # propose_link, matching propose_node's inline path.
    for uid, label in [(source_id, "source_id"), (target_id, "target_id")]:
        try:
            uuid.UUID(uid)
        except (ValueError, TypeError, AttributeError):
            return False, f"{label} is not a valid UUID: {uid!r}"
    
    rule_error = _validate_link_rules({
        "predicate": relationship_type,
        "direction": direction,
        "certainty": certainty,
        "assertion": assertion,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "via_node_id": via_node_id,
    })
    if rule_error:
        return False, rule_error

    # Load both nodes to ensure existence
    try:
        source_node = load_node(source_id)
        target_node = load_node(target_id)
    except FileNotFoundError as e:
        return False, f"Node not found: {e}"
    except Exception as e:
        return False, f"Error loading node: {e}"
    
    tags = tags or []

    def build_link_obj(t_id: str, pred: str, dir: str, cert: str, tgs: List[str]) -> Dict:
        link_obj = {
            "target_id": t_id,
            "predicate": pred,
            "direction": dir,
            "certainty": cert,
            "tags": tgs
        }
        if assertion:
            link_obj["assertion"] = assertion
        if valid_from:
            link_obj["valid_from"] = valid_from
        if valid_until:
            link_obj["valid_until"] = valid_until
        if via_node_id:
            link_obj["via_node_id"] = via_node_id
        return link_obj

    # Build the write plan: (node_id, link to append, the node's current state).
    writes = []
    if direction in ["outbound", "bidirectional"]:
        writes.append((source_id, build_link_obj(target_id, relationship_type, direction, certainty, tags), source_node))
    # Reverse link on the target for inbound/bidirectional.
    # "inbound" from source's perspective → "outbound" from target's perspective.
    if direction in ["inbound", "bidirectional"]:
        target_dir = "outbound" if direction == "inbound" else "bidirectional"
        writes.append((target_id, build_link_obj(source_id, relationship_type, target_dir, certainty, tags), target_node))

    # W4 backstop: _validate_link_rules already rejects an out-of-enum direction,
    # but never report success having written nothing — if the plan is empty the
    # direction dispatch above matched neither branch.
    if not writes:
        return False, f"No link written: unrecognized direction {direction!r}"

    # Reject a duplicate (owner, target, predicate) on EACH node that will receive
    # a link — not just the source (X3). The edges index PK is
    # (source_id, target_id, predicate, link_type), so a second such edge with
    # different metadata would append to the file but collapse to one index row,
    # silently dropping the first's provenance. This is now reachable because an
    # inbound/bidirectional write lands on the target, which the old source-only
    # check never guarded.
    for node_id, link_obj, current in writes:
        for existing_link in current.get("links", []):
            if (existing_link.get("target_id") == link_obj["target_id"] and
                    existing_link.get("predicate") == link_obj["predicate"]):
                return False, (
                    f"Link already exists on {node_id} to {link_obj['target_id']} "
                    f"with predicate '{link_obj['predicate']}'"
                )

    # Schema-validate each link object BEFORE writing. crud.update_node performs
    # no schema validation, so this is the only thing standing between a malformed
    # field and a poisoned neuron file that then fails EVERY later update-node on
    # it. _validate_link_rules above covers the reserved-predicate semantics the
    # schema can't express; this closes everything the schema *can* — non-UUID
    # via_node_id (B1), unknown assertion keys / non-string dates / null certainty
    # (B2), etc.
    for node_id, link_obj, current in writes:
        try:
            _validate_link_obj(link_obj)
        except ValidationError as e:
            return False, f"Schema validation error: {e.message}"

    try:
        for node_id, link_obj, _current in writes:
            update_node(node_id, {"links": [link_obj]})
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
    and VECTOR_SEARCH.md). recall()/search-graph terms are overwhelmingly
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

    # Warn if symbol not in code graph. X2: guard the lookup — codegraph.db is
    # per-machine/.stignore'd, so on a root where `index-code` never ran the
    # table is absent; that must warn, not crash the write.
    try:
        cg = get_codegraph()
        sym = cg.get_symbol(symbol_id)
    except Exception as e:
        logger.warning(f"link_code: codegraph lookup failed ({e}); warn-not-block")
        sym = None
    warning = ""
    if sym is None:
        warning = (
            f" Warning: symbol_id {symbol_id!r} not found in codegraph.db — "
            "run 'index-code' to refresh, or verify the id."
        )

    new_ref = {"symbol_id": symbol_id, "repo": repo, "kind": kind}
    existing = list(node.get("code_refs", []))

    # Dedup by symbol_id
    if any(r.get("symbol_id") == symbol_id for r in existing):
        return True, f"code_ref already present on neuron {neuron_id}.{warning}"

    existing.append(new_ref)
    # X7: crud.update_node does no schema validation, so validate the resulting
    # node before writing — a bad code_ref (e.g. an out-of-enum `kind`) would
    # otherwise poison the neuron file and break every later update-node on it.
    prospective = dict(node)
    prospective["code_refs"] = existing
    try:
        _validate_node(prospective)
    except ValidationError as e:
        return False, f"Schema validation error: {e.message}"
    update_node(neuron_id, {"code_refs": existing})

    # Re-index so the neuron's new state is visible
    try:
        indexer = get_indexer()
        indexer.index_neuron_json(neuron_json_path(neuron_id))
        indexer.connect().commit()
    except Exception as e:
        logger.warning(f"link_code: index update failed: {e}")

    return True, f"code_ref {symbol_id!r} linked to neuron {neuron_id}.{warning}"


# ── Engram Layer: fold / unfold / folds ─────────────────────────────────────
#
# Short-term memory / context folding. See
# _projects/knewrall-dev/plans/short-term-memory-layer-plan.md for the full
# design. Thin wrappers over src/knewrall_engrams.py (the store) exported for
# both cli.py and mcp_server.py's read-only knewrall_unfold tool.

_DEFAULT_ENGRAMS_CONFIG: Dict[str, Any] = {
    "ttl_hours": _engrams.DEFAULT_TTL_HOURS,
    "session_idle_hours": _engrams.DEFAULT_SESSION_IDLE_HOURS,
    "max_session_bytes": _engrams.DEFAULT_MAX_SESSION_BYTES,
    "min_fold_bytes": 2048,
    "min_fold_lines": 40,
    "keep_head_lines": 20,
    "keep_tail_lines": 20,
    "unfold_max_chars": 40000,
    "promote_hint_threshold": 2,
    "never_fold_globs": [
        "knewrall/schemas/**", "**/INSTRUCTIONS.md", "**/CLAUDE.md", "**/AGENTS.md",
        "**/GEMINI.md", "**/.clinerules", "**/SKILL.md", ".claude/settings*.json",
    ],
    "enforce": False,
    "enforce_patterns": ["pytest", "npm test", "cargo build", "docker logs", "tsc"],
}


def _engrams_config() -> Dict[str, Any]:
    """Read the `engrams` tuning block from .knewrall/config.json, falling
    back to the shipped defaults for anything missing. Read fresh on every
    call (the file is tiny) so config edits take effect without a restart —
    the same tradeoff recall()'s budgeting constants make by being module
    constants, except this one source is user-editable, so freshness wins."""
    cfg = dict(_DEFAULT_ENGRAMS_CONFIG)
    config_path = get_root() / ".knewrall" / "config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg.update(data.get("engrams", {}))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def _assistant_config() -> Dict[str, Any]:
    try:
        with open(get_root() / ".knewrall" / "config.json", "r", encoding="utf-8") as fh:
            return json.load(fh).get("assistant", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _assistant_result(result: Any) -> Dict[str, Any]:
    return {
        "answer": result.answer, "keywords": result.keywords, "tags": result.tags,
        "confidence": result.confidence, "provider": result.provider, "model": result.model,
        "transport": result.transport, "cached": result.cached, "partial": result.partial,
        "chunks_used": result.chunks_used, "chunks_total": result.chunks_total,
    }


def _is_never_fold_path(path: Optional[str], globs: List[str], root: Path) -> bool:
    """Rule 2 (instruction-context protection): these files ARE the system
    prompt in this workspace — fold/fold-run must refuse to fold them.

    Matching is against EVERY path suffix, not just (basename,
    relative-to-knewrall-root, absolute). `fnmatch` requires a full match, so
    anchoring matters: the shipped `never_fold_globs` mix patterns written
    relative to the *workspace* root (`knewrall/schemas/**`,
    `.claude/settings*.json` — both one level ABOVE knewrall/, so
    `relative_to(root)` raises and the absolute path can't full-match either)
    with `**/`-prefixed ones (`**/INSTRUCTIONS.md`). Testing all suffixes makes
    every pattern in that list work regardless of which root it was written
    against, which is what a user editing `never_fold_globs` will expect.
    `root` is retained for call-site compatibility (a suffix match subsumes
    the relative-to-root case)."""
    if not path:
        return False
    p = Path(path)
    try:
        abs_posix = p.resolve().as_posix()
    except (OSError, RuntimeError):
        abs_posix = p.as_posix()
    segments = [s for s in abs_posix.split("/") if s]
    candidates = [abs_posix, p.as_posix(), p.name]
    candidates += ["/".join(segments[i:]) for i in range(len(segments))]
    for pattern in globs:
        for candidate in candidates:
            if fnmatch.fnmatch(candidate, pattern):
                return True
    return False


def _is_knewrall_invocation(cmd: Optional[str]) -> bool:
    """Rule 3 (knewrall-output protection): never fold Knewrall's own output
    — recall() is already budgeted, and folding it would recompress a
    compressed artifact (and risks an obvious recursion)."""
    if not cmd:
        return False
    return "knewrall.py" in cmd or bool(re.match(r"^\s*knewrall(\s|$)", cmd))


def render_fold_marker(header: Dict[str, Any], *, quiet: bool = False) -> str:
    """What actually lands in the agent's context after a fold — see plan
    §4.1. `--quiet` drops everything but the retrieval key."""
    key = header["key"]
    if quiet:
        return f"↩ {key}"
    lines = [
        f"[folded → engram {key} ({header['kind']}, {header['bytes']} bytes ≈ {header['est_tokens']} tok)]",
    ]
    if header.get("digest"):
        lines.append(f"digest: {header['digest']}")
    lines.append(f"retrieve: python knewrall/bin/knewrall.py unfold {key} [--grep PATTERN] [--lines A-B]")
    return "\n".join(lines)


def _fold_payload(
    payload_bytes: bytes,
    source: Dict[str, Any],
    *,
    label: str = "",
    kind: Optional[str] = None,
    session: Optional["_engrams.Session"] = None,
    session_id: Optional[str] = None,
    root: Optional[Path] = None,
    quiet: bool = False,
) -> Dict[str, Any]:
    """Shared core of `fold()` and `precompact_ingest()`: apply protection
    rules 1-3/6, classify+digest, and write the engram. Pulled out so the
    transcript ingester (Phase 3b) reuses the exact same protection logic
    instead of a second, potentially-drifting copy of it.

    Returns a dict:
      passthrough=True  -> {"passthrough": True, "content": str, "warning"?: str}
      passthrough=False -> {"passthrough": False, "key": str, "marker": str, "meta": dict}
    """
    root = root or get_root()
    cfg = _engrams_config()

    # Rule 3: never re-fold Knewrall's own output.
    if _is_knewrall_invocation(source.get("cmd")):
        return {"passthrough": True, "content": payload_bytes.decode("utf-8", errors="replace")}

    # Rule 2: instruction-context files ARE the system prompt — refuse.
    if _is_never_fold_path(source.get("path"), cfg["never_fold_globs"], root):
        return {
            "passthrough": True,
            "content": payload_bytes.decode("utf-8", errors="replace"),
            "warning": f"refused to fold {source.get('path')} — instruction-context file, never folded",
        }

    num_lines = payload_bytes.count(b"\n") + (1 if payload_bytes and not payload_bytes.endswith(b"\n") else 0)
    # Rule 1: size floor — the single most important protection. Below it,
    # folding would cost more (the ~30-token marker) than it saves.
    if len(payload_bytes) < cfg["min_fold_bytes"] and num_lines < cfg["min_fold_lines"]:
        return {"passthrough": True, "content": payload_bytes.decode("utf-8", errors="replace")}

    # Rule 6: path-based (not checksum-based) already-durable protection.
    protected = _engrams.is_protected_path(source.get("path"), root=root)

    detected_kind, digest, keywords = _fold_router.classify_and_digest(
        payload_bytes, source, kind_override=kind,
    )
    meta = {
        "kind": detected_kind,
        "source": source,
        "label": label,
        "digest": digest,
        "keywords": keywords,
        "protected": protected,
    }

    session = session or _engrams.resolve_session(
        root=root, session_id=session_id, idle_hours=cfg["session_idle_hours"], harness="cli",
    )
    try:
        _engrams.sweep_expired(root=root, ttl_hours=cfg["ttl_hours"])  # opportunistic, best-effort
    except Exception:
        pass

    try:
        key = _engrams.write_engram(
            payload_bytes, meta, session=session, root=root,
            ttl_hours=cfg["ttl_hours"], max_session_bytes=cfg["max_session_bytes"],
        )
    except _engrams.EngramStoreFull as e:
        return {
            "passthrough": True,
            "content": payload_bytes.decode("utf-8", errors="replace"),
            "warning": f"fold refused: {e}",
        }

    header = _engrams.read_meta(key, session=session, root=root)
    _engrams.record_adaptive_fold(root, detected_kind, source.get("cmd"))
    marker = render_fold_marker(header, quiet=quiet)
    return {"passthrough": False, "key": key, "marker": marker, "meta": header}


def fold(
    *,
    content: Optional[str] = None,
    file: Optional[str] = None,
    label: str = "",
    kind: Optional[str] = None,
    session_id: Optional[str] = None,
    quiet: bool = False,
) -> Dict[str, Any]:
    """Fold content the agent already has (stdin) or a file it's about to
    read into an engram, returning a compact retrieval marker instead of the
    raw content. No-ops (passthrough, no engram created) below the size
    floor, or for protected instruction-context / knewrall-output paths —
    see plan §3.2 rules 1-3.

    Returns a dict:
      passthrough=True  -> {"passthrough": True, "content": str, "warning"?: str}
      passthrough=False -> {"passthrough": False, "key": str, "marker": str, "meta": dict}
    """
    root = get_root()

    if file is not None:
        try:
            with open(file, "rb") as f:
                payload_bytes = f.read()
        except OSError as e:
            return {"passthrough": True, "content": "", "warning": f"cannot read {file}: {e}"}
        source = {"tool": "Read", "cmd": None, "path": str(Path(file).resolve()), "cwd": os.getcwd()}
    else:
        payload_bytes = (content or "").encode("utf-8")
        source = {"tool": "stdin", "cmd": None, "path": None, "cwd": os.getcwd()}

    return _fold_payload(
        payload_bytes, source, label=label, kind=kind, session_id=session_id, root=root, quiet=quiet,
    )


def unfold(
    key: str,
    *,
    session_id: Optional[str] = None,
    grep: Optional[str] = None,
    grep_context: int = 0,
    lines: Optional[Tuple[int, int]] = None,
    head: Optional[int] = None,
    tail: Optional[int] = None,
    meta_only: bool = False,
    max_chars: Optional[int] = None,
    ask: Optional[str] = None,
    ask_strict: bool = False,
    no_assistant: bool = False,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Read a folded payload back, windowed by default (unfold_max_chars) —
    see plan §4.2. `unfold_max_chars` is enforced HERE (not left to the
    caller) so both the CLI and the MCP `knewrall_unfold` tool get the same
    cap automatically, since MCP responses are injected whole.

    Returns {"found": False, "error": str} on an unknown key, or
    {"found": True, "meta": dict, "content": str|None, "truncated": bool}.
    """
    root = get_root()
    cfg = _engrams_config()

    session = _engrams.resolve_session(root=root, session_id=session_id, touch=False)

    try:
        _engrams.sweep_expired(root=root, ttl_hours=cfg["ttl_hours"])  # opportunistic, best-effort
    except Exception:
        pass

    try:
        if meta_only:
            meta = _engrams.read_meta(key, session=session, root=root)
            return {"found": True, "meta": meta, "content": None, "truncated": False}

        # A bare unfold (no --grep/--lines) is capped at unfold_max_chars;
        # a windowed one is already byte-cheap and left uncapped.
        cap = max_chars
        if cap is None and grep is None and lines is None and head is None and tail is None:
            cap = cfg["unfold_max_chars"]

        result = _engrams.read_engram(
            key, session=session, root=root, head=head, tail=tail, lines=lines,
            grep=grep, grep_context=grep_context, max_chars=cap,
        )
    except _engrams.EngramNotFound:
        return {
            "found": False,
            "error": (
                f"engram {key} not found (expired or discarded — or never existed on this "
                "machine: engrams are per-machine and never synced; if this key came from "
                "another machine, `consolidate` it there first)"
            ),
        }

    _engrams.mark_unfolded(key, session=session, root=root, ttl_hours=cfg["ttl_hours"])
    header_kind = result.get("meta", {}).get("kind", "prose")
    header_cmd = (result.get("meta", {}).get("source") or {}).get("cmd")
    _engrams.record_adaptive_unfold(root, header_kind, header_cmd)
    result["found"] = True
    if ask is not None and not no_assistant:
        assistant_cfg = _assistant_config()
        if not assistant_cfg.get("enabled", False):
            result["assistant"] = None
            result["assistant_error"] = "disabled"
        else:
            full_result = _engrams.read_engram(key, session=session, root=root)
            if len(full_result["content"]) >= int(assistant_cfg.get("min_chars", 20000)):
                full_text = full_result["content"]
                rr = _reader.ask(full_text, ask, config={"assistant": assistant_cfg}, kind=header_kind, provider=provider)
                if rr.error:
                    result["assistant"] = None
                    result["assistant_error"] = rr.error
                    if rr.detail:
                        print(f"assistant provider failure: {rr.detail}", file=sys.stderr)
                else:
                    result["assistant"] = _assistant_result(rr)
                    qhash = _reader._question_hash(ask)
                    meta = result["meta"]
                    answers = dict(meta.get("assistant_answers") or {})
                    answers[qhash] = {**result["assistant"], "question": ask, "created_at": _engrams._iso(_engrams._now())}
                    keywords = list(dict.fromkeys((meta.get("assistant_keywords") or []) + rr.keywords))[:24]
                    _engrams.update_engram_state(key, session=session, root=root, assistant_answers=answers, assistant_keywords=keywords)
                    meta["assistant_answers"] = answers
                    meta["assistant_keywords"] = keywords
            else:
                result["assistant"] = None
                result["assistant_error"] = "below_threshold"
    return result


def _shape_fold_summary(header: Dict[str, Any]) -> Dict[str, Any]:
    """Flat, scalar-only subset for `folds` output — see plan §4.3's example
    columns (key,kind,label,lines,est_tokens,unfolds,created_at). Flat/scalar
    is also what unlocks knewrall_toon's compact tabular block for a uniform
    array; the full merged header (with nested `source`, `keywords` list) is
    still available via `unfold --meta`/read_meta()."""
    return {
        "key": header["key"],
        "kind": header["kind"],
        "label": header.get("label", ""),
        "lines": header.get("lines", 0),
        "est_tokens": header.get("est_tokens", 0),
        "unfolds": header.get("unfolds", 0),
        "created_at": header.get("created_at", ""),
    }


# ── PreCompact transcript ingestion (Phase 3b) ──────────────────────────────
#
# Makes the harness's own context-shedding lossless: fold tool results out of
# the transcript BEFORE Claude Code compacts them away, so compaction becomes
# reversible instead of destructive. Every token here has already been paid
# for — the buy is reversibility, not savings (plan §2.1 mechanism C, §9
# Phase 3b). Format gated on parsing real transcripts from
# ~/.claude/projects/**/*.jsonl (not a hand-written fixture) since the .jsonl
# shape is undocumented. Confirmed shape, per-line JSON records:
#   {"type": "assistant", "message": {"content": [{"type": "tool_use",
#     "id": ..., "name": "Bash"|"Read"|..., "input": {"command"|"file_path": ...}}]}}
#   {"type": "user", "promptId": ..., "cwd": ..., "message": {"content":
#     [{"type": "tool_result", "tool_use_id": ..., "content": <str or list
#       of {"type":"text","text":...}/{"type":"tool_reference",...} blocks>}]}}
# `assistant` records never carry `promptId` (only `user` records do); "turn"
# boundaries are therefore reconstructed from the distinct promptIds seen on
# `user` records, in file order.

def _iter_transcript_records(transcript_path: str):
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue  # unrecognized/corrupt line — degrade to no ingestion for it, not a crash


def _extract_tool_result_text(content: Any) -> Optional[str]:
    """`content` is either a plain string, or a list of content blocks — only
    `text` blocks contribute (e.g. `tool_reference` blocks, seen when a
    deferred-tool search result is fed back, carry no foldable text)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts) if parts else None
    return None


def precompact_ingest(
    transcript_path: str,
    *,
    session_id: Optional[str] = None,
    root: Optional[Path] = None,
    keep_tail_turns: int = 3,
    deadline_seconds: float = 10.0,
) -> Dict[str, int]:
    """Fold a transcript's tool results into engrams before the harness
    compacts them away. Best-effort and defensive throughout: an unrecognized
    record shape is skipped (never raised), a missing/unreadable file returns
    zero counters, and a hard wall-clock deadline caps total work on a large
    transcript. Reuses `_fold_payload()` — same protection rules 1-3/6 as
    every other fold path, so an ingested tool result can no more evade the
    size floor or instruction-context refusal than a manual `fold` call.

    Skips the last `keep_tail_turns` turns entirely (protection rule 4 — the
    model still needs that content verbatim; "turn" = one distinct
    `promptId`, in file order).
    """
    import time as _time

    root = root or get_root()
    stats = {
        "records_seen": 0, "tool_results_seen": 0, "folded": 0,
        "skipped_tail": 0, "skipped_other": 0, "parse_errors": 0,
    }

    try:
        records = list(_iter_transcript_records(transcript_path))
    except OSError:
        return stats

    prompt_order: List[str] = []
    for rec in records:
        pid = rec.get("promptId")
        if pid and (not prompt_order or prompt_order[-1] != pid):
            prompt_order.append(pid)
    tail_prompt_ids = set(prompt_order[-keep_tail_turns:]) if keep_tail_turns > 0 else set()

    session = _engrams.resolve_session(root=root, session_id=session_id, touch=False)
    tool_use_map: Dict[str, Tuple[Optional[str], Dict[str, Any]]] = {}
    start = _time.monotonic()

    for rec in records:
        if _time.monotonic() - start > deadline_seconds:
            break
        stats["records_seen"] += 1
        try:
            rec_type = rec.get("type")
            if rec_type == "assistant":
                msg = rec.get("message") or {}
                for block in (msg.get("content") or []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_use_map[block.get("id")] = (block.get("name"), block.get("input") or {})
                continue

            if rec_type != "user":
                continue
            msg = rec.get("message") or {}
            content_blocks = msg.get("content")
            if not isinstance(content_blocks, list):
                continue

            pid = rec.get("promptId")
            for block in content_blocks:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                stats["tool_results_seen"] += 1
                if pid and pid in tail_prompt_ids:
                    stats["skipped_tail"] += 1
                    continue

                text = _extract_tool_result_text(block.get("content"))
                if not text:
                    stats["skipped_other"] += 1
                    continue

                tool_use_id = block.get("tool_use_id")
                tool_name, tool_input = tool_use_map.get(tool_use_id, (None, {}))
                source = {
                    "tool": tool_name,
                    "cmd": tool_input.get("command"),
                    "path": tool_input.get("file_path") or tool_input.get("path"),
                    "cwd": rec.get("cwd"),
                }
                result = _fold_payload(
                    text.encode("utf-8", errors="replace"), source,
                    label=f"transcript ingestion ({tool_name or 'tool'})",
                    session=session, root=root,
                )
                if result.get("passthrough"):
                    stats["skipped_other"] += 1
                else:
                    stats["folded"] += 1
        except Exception:
            stats["parse_errors"] += 1
            continue

    return stats


def fold_run(
    command: List[str],
    *,
    label: str = "",
    kind: Optional[str] = None,
    keep_head: Optional[int] = None,
    keep_tail: Optional[int] = None,
    session_id: Optional[str] = None,
    quiet: bool = False,
) -> Dict[str, Any]:
    """Run `command`, fold its full (stdout+stderr, combined) output as an
    engram, and return head + tail + a type-aware digest + a retrieval
    marker — the raw output never enters the caller's context at all. See
    plan §2.1 mechanism A and §4.1's marker format.

    Streams the subprocess's combined output to a temp file rather than
    buffering it in memory (a `docker logs` can be gigabytes), decodes with
    errors="replace", and passes exit codes through unchanged.

    Returns:
      {"exit_code": int, "passthrough": bool, "output": str, "key"?: str, "meta"?: dict}
    `output` is exactly what the caller should print — either the raw
    command output (passthrough) or the folded transcript (head/tail/digest/
    marker).
    """
    import subprocess
    import tempfile

    root = get_root()
    cfg = _engrams_config()
    display_cmd = " ".join(command)

    if _is_knewrall_invocation(display_cmd):
        return {
            "exit_code": 1, "passthrough": True,
            "output": f"$ {display_cmd}\n[refused: fold-run does not wrap Knewrall's own CLI]",
        }

    with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            proc = subprocess.run(command, stdout=tmp, stderr=subprocess.STDOUT, cwd=os.getcwd())
            exit_code = proc.returncode
        except OSError as e:
            tmp_path.unlink(missing_ok=True)
            return {"exit_code": 127, "passthrough": True, "output": f"$ {display_cmd}\n[error: {e}]"}

    try:
        payload_bytes = tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)

    text = payload_bytes.decode("utf-8", errors="replace")
    num_lines = payload_bytes.count(b"\n") + (1 if payload_bytes and not payload_bytes.endswith(b"\n") else 0)

    if len(payload_bytes) < cfg["min_fold_bytes"] and num_lines < cfg["min_fold_lines"]:
        # Rule 1: size floor — the command already ran; nothing to fold.
        return {"exit_code": exit_code, "passthrough": True, "output": f"$ {display_cmd}\n{text}"}

    source = {"tool": "Bash", "cmd": display_cmd, "path": None, "cwd": os.getcwd()}
    detected_kind, digest, keywords = _fold_router.classify_and_digest(
        payload_bytes, source, kind_override=kind,
    )
    meta = {
        "kind": detected_kind, "source": source, "label": label,
        "digest": digest, "keywords": keywords, "protected": False,
    }

    session = _engrams.resolve_session(
        root=root, session_id=session_id, idle_hours=cfg["session_idle_hours"], harness="cli",
    )
    try:
        _engrams.sweep_expired(root=root, ttl_hours=cfg["ttl_hours"])
    except Exception:
        pass

    try:
        key = _engrams.write_engram(
            payload_bytes, meta, session=session, root=root,
            ttl_hours=cfg["ttl_hours"], max_session_bytes=cfg["max_session_bytes"],
        )
    except _engrams.EngramStoreFull as e:
        return {
            "exit_code": exit_code, "passthrough": True,
            "output": f"$ {display_cmd}\n{text}\n[fold refused: {e}]",
        }

    header = _engrams.read_meta(key, session=session, root=root)
    _engrams.record_adaptive_fold(root, detected_kind, display_cmd)

    # TOIN-lite adaptive widening (plan §5.2): if this (kind, cmd_pattern)
    # pair's unfold_rate has run hot over enough samples, keep more head/tail
    # for FUTURE folds of it — explicit caller overrides always win.
    default_head, default_tail = cfg["keep_head_lines"], cfg["keep_tail_lines"]
    if keep_head is None or keep_tail is None:
        adaptive_head, adaptive_tail = _engrams.adaptive_keep_lines(
            root, detected_kind, display_cmd, default_head, default_tail,
        )
    head_n = keep_head if keep_head is not None else adaptive_head
    tail_n = keep_tail if keep_tail is not None else adaptive_tail
    lines = text.split("\n")
    head_block = "\n".join(lines[:head_n])
    tail_block = "\n".join(lines[-tail_n:]) if tail_n else ""

    if quiet:
        transcript = [f"$ {display_cmd}", head_block, f"↩ {key}"]
    else:
        transcript = [
            f"$ {display_cmd}",
            head_block,
            f"[…{header['lines']} lines folded → engram {key} "
            f"({header['kind']}, {_fmt_bytes(header['bytes'])} ≈ {header['est_tokens']:,} tok)…]",
        ]
        if digest:
            transcript.append(f"digest: {digest}")
        if tail_block:
            transcript.append(tail_block)
        transcript.append(f"retrieve: python knewrall/bin/knewrall.py unfold {key} [--grep PATTERN] [--lines A-B]")

    return {
        "exit_code": exit_code, "passthrough": False,
        "output": "\n".join(transcript), "key": key, "meta": header,
    }


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


_FOLD_SCAN_MAX_ITEMS = 4
_FOLD_SCAN_MAX_CHARS = 2000
_FOLD_SCAN_DEADLINE = 1.5  # wall-clock cap on the ranking work itself, measured
                            # from inside this function (post-import) — the
                            # observed hook-to-hook latency also includes
                            # interpreter cold-start, which this deadline does
                            # NOT cover (see knewrall_fold_turn.py's docstring).


def _score_engram(header: Dict[str, Any], terms_lower: List[str]) -> Tuple[float, List[str]]:
    """Weighted literal overlap — keyword hit > label hit > digest hit —
    boosted by recency and by unfolds. Deliberately no IDF (plan §6, Kimi K3
    I4): at a session's corpus size (tens of engrams), IDF is sampling noise,
    not signal."""
    keywords = [k.lower() for k in header.get("keywords", [])]
    assistant_keywords = [k.lower() for k in header.get("assistant_keywords", [])]
    label = (header.get("label") or "").lower()
    digest = (header.get("digest") or "").lower()

    score = 0.0
    matched: List[str] = []
    for t in terms_lower:
        hit = False
        if any(t == kw or t in kw or kw in t for kw in keywords):
            score += 3
            hit = True
        if any(t == kw or t in kw or kw in t for kw in assistant_keywords):
            score += 2
            hit = True
        if t in label:
            score += 2
            hit = True
        if t in digest:
            score += 1
            hit = True
        if hit:
            matched.append(t)

    if not matched:
        return 0.0, []

    try:
        created = _engrams._parse_iso(header["created_at"])
        age_hours = (_engrams._now() - created).total_seconds() / 3600.0
        recency_boost = max(0.0, 2.0 - (age_hours / 24.0))  # decays over ~2 days
    except (KeyError, ValueError):
        recency_boost = 0.0

    unfolds_boost = min(3, header.get("unfolds", 0)) * 0.5
    return score + recency_boost + unfolds_boost, matched


def fold_scan(
    terms: List[str],
    *,
    session_id: Optional[str] = None,
    root: Optional[Path] = None,
    max_items: int = _FOLD_SCAN_MAX_ITEMS,
    max_chars: int = _FOLD_SCAN_MAX_CHARS,
    deadline_seconds: float = _FOLD_SCAN_DEADLINE,
    min_age_seconds: float = 3.0,
) -> Dict[str, Any]:
    """The Context Tracker analogue (plan §6): rank the current session's
    engram HEADERS (never blobs) by relevance to `terms`, emit at most
    `max_items` one-line markers within `max_chars` total. Surfaces pointers
    only — never content; unfolding is always the model's own decision.

    `min_age_seconds` implements protection rule 4 (own-turn protection): an
    engram folded moments ago (effectively "this turn", since fold-scan fires
    at the START of the next turn) is excluded — the model still has that
    content fresh, it doesn't need a pointer back to it.
    """
    import time

    root = root or get_root()
    start = time.monotonic()
    terms_lower = [t.lower() for t in terms if t]
    if not terms_lower:
        return {"markers": [], "text": ""}

    session = _engrams.resolve_session(root=root, session_id=session_id, touch=False)
    # A high limit rather than "unlimited": ranking needs every candidate in
    # scope, and a session's engram count is realistically tens-to-hundreds.
    headers = _engrams.list_engrams(session=session, root=root, limit=100000)

    now = _engrams._now()
    scored = []
    for header in headers:
        if time.monotonic() - start > deadline_seconds:
            break
        try:
            created = _engrams._parse_iso(header["created_at"])
            if (now - created).total_seconds() < min_age_seconds:
                continue
        except (KeyError, ValueError):
            pass
        score, matched = _score_engram(header, terms_lower)
        if score > 0:
            scored.append((score, header, matched))

    scored.sort(key=lambda x: -x[0])

    lines: List[str] = []
    emitted: List[str] = []
    total_chars = 0
    for score, header, matched in scored:
        if len(emitted) >= max_items:
            break
        label_or_digest = header.get("label") or header.get("digest", "")
        preview = label_or_digest[:40]
        line = (
            f"  {header['key']} {header['kind']} \"{preview}\" — "
            f"matches: {', '.join(matched)}  → unfold {header['key']}"
        )
        if total_chars + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total_chars += len(line) + 1
        emitted.append(header["key"])

    if not lines:
        return {"markers": [], "text": ""}

    text = "possibly relevant folded context:\n" + "\n".join(lines)
    return {"markers": emitted, "text": text}


def list_folds(
    *,
    session_id: Optional[str] = None,
    kind: Optional[str] = None,
    grep: Optional[str] = None,
    limit: int = 20,
    all_sessions: bool = False,
) -> Dict[str, Any]:
    """This session's fold index (metadata only — never payloads), plus a
    token-savings total. See plan §4.3."""
    root = get_root()
    session = None
    if not all_sessions:
        session = _engrams.resolve_session(root=root, session_id=session_id, touch=False)

    engrams = _engrams.list_engrams(session=session, root=root, kind=kind, grep=grep, limit=limit)

    folded_tokens = sum(e.get("est_tokens", 0) for e in engrams)
    # Retained ~= what actually stays in context per fold: the marker
    # overhead (~30 tok) plus the digest text. Rough, deliberately crude,
    # matching est_tokens' own bytes/4 house style.
    retained_tokens = sum(30 + (len(e.get("digest", "")) // 4) for e in engrams)

    return {
        "session": session.session_short if session else "all",
        "engrams": [_shape_fold_summary(e) for e in engrams],
        "totals": {
            "engrams": len(engrams),
            "folded_tokens": folded_tokens,
            "retained_tokens": retained_tokens,
            "saved_tokens": max(0, folded_tokens - retained_tokens),
        },
    }


# ── consolidate: the bridge back to the durable graph (Phase 4) ────────────
#
# Deliberately a thin wrapper over propose_node()/propose_link() — creates
# nothing the graph doesn't already understand (plan §5.1). No new
# node-creation code path, no new schema, no new validation.

_KIND_TO_PILLAR_HINT = {
    # INSTRUCTIONS §2's pillar-mapping for dev-evolution concepts, applied to
    # engram kinds: most folded content describes an artifact/concept (What).
    "diff": "How", "run_log": "How",
}


def _suggest_node_payload(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Draft a `propose-node` payload from an engram's kind/label/digest/
    keywords. NEVER writes — the agent reviews, edits, and passes it to
    `propose-node` itself (plan §5.1's `--suggest`)."""
    kind = meta.get("kind", "prose")
    label = meta.get("label") or f"{kind} engram {meta.get('key', '')}"
    digest = meta.get("digest", "")
    keywords = meta.get("keywords") or []
    pillar = _KIND_TO_PILLAR_HINT.get(kind, "What")
    payload = {
        "header": {"type": pillar, "canonical_name": label, "aliases": []},
        "descriptions": {"conceptual": digest},
        "properties": {
            "source_kind": [{"value": kind}],
            "keywords": [{"value": kw} for kw in keywords],
        },
        "tags": [kind, "from-engram"],
    }
    if meta.get("assistant_answers"):
        payload["properties"]["assistant_claim"] = [{"value": "machine-derived", "certainty": "hypothetical"}]
        payload["properties"]["assistant_claim"][0]["assertion"] = {
            "sources": [f"text:assistant:{answer.get('provider', 'unknown')}/{answer.get('model', 'unknown')}:{meta.get('key', '')}"]
        }
    return payload


def consolidate_engram(
    key: str,
    *,
    json_payload: Optional[Dict[str, Any]] = None,
    suggest: bool = False,
    archive: bool = False,
    archive_only: bool = False,
    link: Optional[Tuple[str, str]] = None,
    session_id: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Promote an engram into the durable graph. Three modes (plan §5.1):
      - `suggest=True`            -> draft payload only, never writes.
      - `archive_only=True`       -> copy raw blob to archive/, no Neuron.
      - `json_payload=<dict>`     -> propose_node(payload) (+ optional
                                      --archive, + optional --link).
    """
    root = root or get_root()
    session = _engrams.resolve_session(root=root, session_id=session_id, touch=False)

    try:
        meta = _engrams.read_meta(key, session=session, root=root)
    except _engrams.EngramNotFound:
        return {"success": False, "message": f"engram {key} not found (expired, discarded, or never existed on this machine)"}

    if suggest:
        return {"success": True, "mode": "suggest", "draft": _suggest_node_payload(meta)}

    engram_path = _engrams._find_engram_path(key, session, root)

    if archive_only:
        _, payload_bytes = _engrams._read_header_and_payload(engram_path)
        archived_path = _engrams.archive_engram(key, meta, payload_bytes, root)
        _engrams.update_engram_state(key, session=session, root=root, archived_path=archived_path)
        return {"success": True, "mode": "archive-only", "archived_path": archived_path}

    if json_payload is None:
        return {"success": False, "message": "consolidate requires one of --json/--suggest/--archive-only"}

    if "system" in json_payload:
        return {"success": False, "message": "payload must not contain system — propose_node generates it"}

    success, msg, node_id = propose_node(json_payload)
    if not success:
        return {"success": False, "message": msg}

    archived_path = None
    if archive:
        _, payload_bytes = _engrams._read_header_and_payload(engram_path)
        archived_path = _engrams.archive_engram(key, meta, payload_bytes, root)
        update_node_fields(node_id, {"properties": {"source_artifact": archived_path}})

    _engrams.update_engram_state(key, session=session, root=root, consolidated_to=node_id, archived_path=archived_path)

    link_message = None
    if link:
        target_id, predicate = link
        _, link_message = propose_link(node_id, target_id, predicate)

    return {
        "success": True, "mode": "json", "node_id": node_id, "message": msg,
        "archived_path": archived_path, "link_message": link_message,
    }


# ── fold-gc: explicit discard (Phase 4) ─────────────────────────────────────

def fold_gc(
    *,
    session_id: Optional[str] = None,
    all_sessions: bool = False,
    older_than_hours: Optional[float] = None,
    keep_consolidated: bool = True,
    purge_consolidated: bool = False,
    dry_run: bool = False,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Discard engrams. `--keep-consolidated` (default on) preserves engrams
    with a non-null consolidated_to/archived_path INDEFINITELY, until an
    explicit `--purge-consolidated` — never a "survives one extra cycle"
    half-measure (plan §7.2, Kimi K3 I5). Protected engrams are never swept
    here either, matching sweep_expired()'s own invariant."""
    import shutil as _shutil

    root = root or get_root()
    eroot = _engrams.engrams_root(root)
    stats = {
        "sessions_scanned": 0, "sessions_deleted": 0, "engrams_deleted": 0,
        "engrams_kept": 0, "bytes_freed": 0,
    }
    if not eroot.is_dir():
        return stats

    if all_sessions:
        session_dirs = sorted({p.parent for p in eroot.glob("*/*/*/session.json")})
    else:
        session = _engrams.resolve_session(root=root, session_id=session_id, touch=False)
        session_dirs = [_engrams.session_dir(session, root)]

    now = _engrams._now()

    for sdir in session_dirs:
        manifest_path = sdir / "session.json"
        manifest = _engrams._read_json(manifest_path)
        if manifest is None:
            continue
        stats["sessions_scanned"] += 1
        engrams = dict(manifest.get("engrams", {}))

        for key, state in list(engrams.items()):
            is_consolidated = bool(state.get("consolidated_to")) or bool(state.get("archived_path"))
            if is_consolidated and keep_consolidated and not purge_consolidated:
                stats["engrams_kept"] += 1
                continue
            if state.get("protected") and not purge_consolidated:
                stats["engrams_kept"] += 1
                continue

            engram_path = sdir / f"{key}.engram"
            if older_than_hours is not None:
                if not engram_path.exists():
                    continue
                try:
                    header = _engrams._read_header_only(engram_path)
                    created = _engrams._parse_iso(header["created_at"])
                except (OSError, KeyError, ValueError, __import__("json").JSONDecodeError):
                    continue
                age_hours = (now - created).total_seconds() / 3600.0
                if age_hours < older_than_hours:
                    stats["engrams_kept"] += 1
                    continue

            if dry_run:
                stats["engrams_deleted"] += 1
                continue

            size = engram_path.stat().st_size if engram_path.exists() else 0
            try:
                if engram_path.exists():
                    engram_path.unlink()
            except OSError:
                continue
            engrams.pop(key, None)
            stats["engrams_deleted"] += 1
            stats["bytes_freed"] += size

        if dry_run:
            continue

        manifest["engrams"] = engrams
        manifest["engram_count"] = len(engrams)
        # Recompute the byte/token totals from the SURVIVING engrams' headers
        # rather than leaving them at their pre-GC values. write_engram()'s
        # byte-cap check reads `bytes_folded` directly, so a stale (inflated)
        # total makes a long-lived session evict live engrams — or refuse new
        # folds outright with EngramStoreFull — long before it has actually
        # reached max_session_bytes. Recomputing (instead of decrementing) is
        # also self-healing for a manifest that already drifted.
        surviving_bytes = 0
        surviving_tokens = 0
        for key in engrams:
            try:
                header = _engrams._read_header_only(sdir / f"{key}.engram")
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            surviving_bytes += header.get("bytes", 0)
            surviving_tokens += header.get("est_tokens", 0)
        manifest["bytes_folded"] = surviving_bytes
        manifest["est_tokens_saved"] = surviving_tokens
        if not engrams:
            try:
                _shutil.rmtree(sdir)
                stats["sessions_deleted"] += 1
            except OSError:
                pass
        else:
            _engrams._atomic_write_bytes(
                manifest_path,
                (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            )

    return stats


# ── fold-stats: visibility instead of a circuit-breaker (Phase 4, Q2) ──────

def fold_stats(*, root: Optional[Path] = None) -> Dict[str, Any]:
    """Per-kind fold/unfold counts, unfold rate, adaptive digest settings,
    disk usage — across ALL live sessions. This is the visibility the plan
    relies on instead of a token-based circuit-breaker (resolved Q2): the
    real failure mode (a misleading digest) shows up here as an unusually
    high unfold_rate for a (kind, cmd_pattern) pair, not as a token count."""
    root = root or get_root()
    eroot = _engrams.engrams_root(root)
    stats: Dict[str, Any] = {
        "sessions": 0, "engrams": 0, "bytes_folded": 0, "unfold_count": 0,
        "consolidated": 0, "protected": 0, "by_kind": {}, "adaptive": {},
        "assistant": {"engrams": 0, "answers": 0, "questions": 0, "providers": [], "models": []},
    }
    if not eroot.is_dir():
        return stats

    for manifest_path in eroot.glob("*/*/*/session.json"):
        manifest = _engrams._read_json(manifest_path)
        if manifest is None:
            continue
        stats["sessions"] += 1
        sdir = manifest_path.parent
        for key, state in manifest.get("engrams", {}).items():
            engram_path = sdir / f"{key}.engram"
            try:
                header = _engrams._read_header_only(engram_path)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            stats["engrams"] += 1
            stats["bytes_folded"] += header.get("bytes", 0)
            unfolds = state.get("unfolds", 0)
            stats["unfold_count"] += unfolds
            if state.get("consolidated_to"):
                stats["consolidated"] += 1
            if state.get("protected"):
                stats["protected"] += 1
            answers = state.get("assistant_answers") or {}
            stats["assistant"]["engrams"] += bool(answers)
            stats["assistant"]["answers"] += len(answers)
            stats["assistant"]["questions"] += len(answers)
            for answer in answers.values():
                if answer.get("provider"):
                    stats["assistant"]["providers"].append(answer["provider"])
                if answer.get("model"):
                    stats["assistant"]["models"].append(answer["model"])
            kind = header.get("kind", "prose")
            bucket = stats["by_kind"].setdefault(kind, {"folds": 0, "unfolds": 0})
            bucket["folds"] += 1
            bucket["unfolds"] += unfolds

    for kind, bucket in stats["by_kind"].items():
        bucket["unfold_rate"] = round(bucket["unfolds"] / bucket["folds"], 3) if bucket["folds"] else 0.0

    adaptive = _engrams._read_json(_engrams._adaptive_stats_path(root))
    if adaptive:
        stats["adaptive"] = adaptive

    stats["assistant"]["providers"] = sorted(set(stats["assistant"]["providers"]))
    stats["assistant"]["models"] = sorted(set(stats["assistant"]["models"]))

    return stats
