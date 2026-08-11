"""
Knewrall CLI

Command-line interface for Knewrall system operations.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .knewrall_indexer import KnewrallIndexer, rebuild_index_command
from .knewrall_middleware import (
    search_graph,
    recall,
    refresh_index,
    propose_node,
    propose_link,
    update_node_fields,
    update_note_links,
    # Code graph
    index_code,
    code_search,
    code_defs,
    code_callers,
    code_callees,
    code_imports,
    code_stats,
    link_code,
    # Embeddings
    embed_neurons,
    embed_code_symbols,
    embed_query_terms,
    embed_reconcile,
    # Engram Layer (short-term memory / context folding)
    fold,
    fold_run,
    unfold,
    list_folds,
    fold_scan,
    consolidate_engram,
    fold_gc,
    fold_stats,
)
from .knewrall_toon import encode as encode_toon


def _parse_duration_hours(text: str) -> float:
    """Parse a duration like '24h' / '3d' / '90m' into hours."""
    text = text.strip().lower()
    units = {"h": 1.0, "d": 24.0, "m": 1.0 / 60.0}
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)  # bare number -> hours


def main():
    parser = argparse.ArgumentParser(
        prog="knewrall",
        description="Knewrall Local-First Knowledge Graph CLI"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # rebuild-index command
    rebuild_parser = subparsers.add_parser("rebuild-index", help="Rebuild the ephemeral SQLite index from scratch")
    rebuild_parser.add_argument("--full", action="store_true", default=True,
                                help="Drop tables and recreate schema (default)")
    rebuild_parser.add_argument("--incremental", action="store_false", dest="full",
                                help="Only clear data, keep schema (not recommended)")

    # refresh-index command — incremental, content-hash based; run this routinely.
    subparsers.add_parser(
        "refresh-index",
        help="Incrementally sync the index to disk (hash-based; use rebuild-index --full for a hard reset)")

    # search command
    search_parser = subparsers.add_parser("search", help="Search nodes in the index")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=int, default=20, help="Maximum results")

    # stats command
    stats_parser = subparsers.add_parser("stats", help="Show index statistics")

    # middleware search command (uses search_graph)
    search_mw_parser = subparsers.add_parser("search-graph", help="Search nodes via middleware (canonical_name, aliases, tags)")
    search_mw_parser.add_argument("query", help="Search query")
    search_mw_parser.add_argument("--hybrid", action="store_true",
                                  help="Blend literal results with semantic vector KNN (requires embeddings)")
    search_mw_parser.add_argument("--limit", type=int, default=20, help="Maximum results")

    # recall — consolidated retrieval: search + load + shape in one call, so
    # the caller doesn't need a follow-up file read per matched neuron.
    recall_parser = subparsers.add_parser(
        "recall",
        help="Consolidated retrieval: find, load, and shape matching neurons in one call (TOON by default)")
    recall_parser.add_argument("terms", nargs="+", help="One or more search terms")
    recall_parser.add_argument("--depth", type=int, default=1, choices=[0, 1],
                               help="0 = matched neurons only; 1 = also include capped related-link summaries (default)")
    recall_parser.add_argument("--limit", type=int, default=20,
                               help="Max candidates considered per term before ranking/capping (default 20)")
    recall_parser.add_argument("--format", choices=["toon", "json"], default="toon",
                               help="Output encoding (default toon)")
    recall_parser.add_argument("--no-hybrid", action="store_false", dest="hybrid", default=True,
                               help="Disable semantic vector search; literal match only")
    recall_parser.add_argument("--include-assertions", action="store_true", default=False,
                               help="Include assertion blocks (sources, recorded_at, etc.) in output")

    # ── Code graph commands ───────────────────────────────────────────────────

    # index-code
    ic_parser = subparsers.add_parser("index-code",
                                      help="Build/refresh the code symbol graph for repos under projects/")
    ic_parser.add_argument("path", nargs="?", default=None,
                           help="Path to a specific repo to index (default: all projects/ subdirs)")
    ic_parser.add_argument("--full", action="store_true",
                           help="Drop and rebuild from scratch")

    # code-search
    cs_parser = subparsers.add_parser("code-search", help="Full-text search over code symbols")
    cs_parser.add_argument("query", help="Search query")
    cs_parser.add_argument("--limit", type=int, default=20, help="Maximum results")
    cs_parser.add_argument("--hybrid", action="store_true",
                           help="Include semantic vector search results")

    # code-defs
    cd_parser = subparsers.add_parser("code-defs", help="Look up a symbol definition by name")
    cd_parser.add_argument("name", help="Function / class / method name")
    cd_parser.add_argument("--repo", default=None, help="Limit to a specific repo")

    # code-callers
    cc_parser = subparsers.add_parser("code-callers", help="Find what calls a symbol")
    cc_parser.add_argument("symbol_id", help="Symbol id (rel_path::qualified_name)")

    # code-callees
    cce_parser = subparsers.add_parser("code-callees", help="Find what a symbol calls")
    cce_parser.add_argument("symbol_id", help="Symbol id (rel_path::qualified_name)")

    # code-imports
    ci_parser = subparsers.add_parser("code-imports", help="List imports from a source file")
    ci_parser.add_argument("rel_path", help="Repo-relative path to the source file")
    ci_parser.add_argument("--repo", default=None, help="Limit to a specific repo")

    # code-stats
    subparsers.add_parser("code-stats", help="Show code graph statistics")

    # link-code
    lc_parser = subparsers.add_parser("link-code",
                                      help="Attach a code symbol reference to a neuron")
    lc_parser.add_argument("neuron_id", help="UUID of the target neuron")
    lc_parser.add_argument("symbol_id", help="Code symbol id (rel_path::qualified_name)")
    lc_parser.add_argument("repo", help="Repository name (subfolder under projects/)")
    lc_parser.add_argument("--kind", default="function",
                           choices=["function", "class", "method", "module", "other"],
                           help="Symbol kind (default: function)")

    # ── Embedding commands ────────────────────────────────────────────────────

    # embed
    embed_parser = subparsers.add_parser("embed",
                                         help="Generate / refresh embeddings for neurons and/or code symbols")
    embed_parser.add_argument("--neurons", action="store_true", default=False,
                              help="Embed neurons (default: both if no flag given)")
    embed_parser.add_argument("--code", action="store_true", default=False,
                              help="Embed code symbols")
    embed_parser.add_argument("--terms", action="store_true", default=False,
                              help="Pre-seed the query-term cache from canonical_name/alias/tag "
                                   "strings (default: included when no flags given)")
    embed_parser.add_argument("--repo", default=None,
                              help="Limit code symbol embedding to a specific repo")
    embed_parser.add_argument("--reconcile", default=None, metavar="CONFLICT_DB",
                              help="Recover embeddings missing locally from a vectors.db "
                                   "sync-conflict file (e.g. after a Syncthing conflict); "
                                   "never overwrites a local embedding")

    # propose-node command — accepts a file path, inline JSON, or stdin.
    propose_node_parser = subparsers.add_parser(
        "propose-node",
        help="Propose a new node with schema validation (from a file, --json, or stdin)")
    propose_node_parser.add_argument(
        "json_file", nargs="?", default=None,
        help="Path to a JSON file with the node payload. Use '-' or omit to read stdin.")
    propose_node_parser.add_argument(
        "--json", dest="json_inline", default=None,
        help="Inline JSON payload string (alternative to a file).")

    # update-node command — accepts a file path, inline JSON, or stdin.
    update_node_parser = subparsers.add_parser(
        "update-node",
        help="Apply a partial update to an existing node (from a file, --json, or stdin)")
    update_node_parser.add_argument("node_id", help="UUID of the node to update")
    update_node_parser.add_argument(
        "json_file", nargs="?", default=None,
        help="Path to a JSON file with the partial update payload. Use '-' or omit to read stdin.")
    update_node_parser.add_argument(
        "--json", dest="json_inline", default=None,
        help="Inline JSON payload string (alternative to a file).")
    update_node_parser.add_argument(
        "--append", action="store_const", const="append", dest="mode", default="replace",
        help="Append new property values instead of replacing existing ones for the given keys (keeps history).")

    # propose-link command
    propose_link_parser = subparsers.add_parser("propose-link", help="Create a link between two existing nodes")
    propose_link_parser.add_argument("source_id", help="UUID of source node")
    propose_link_parser.add_argument("target_id", help="UUID of target node")
    propose_link_parser.add_argument("predicate", help="Relationship type (e.g., knows, part_of)")
    propose_link_parser.add_argument("--direction", default="outbound",
                                     choices=["outbound", "inbound", "bidirectional"],
                                     help="Link direction (default: outbound)")
    propose_link_parser.add_argument("--certainty", default="confirmed",
                                     choices=["confirmed", "rumored", "hypothetical", "alternative"],
                                     help="Certainty level (default: confirmed)")
    propose_link_parser.add_argument("--valid-from", default=None, dest="valid_from",
                                     help="ISO-8601 start of validity window")
    propose_link_parser.add_argument("--valid-until", default=None, dest="valid_until",
                                     help="ISO-8601 end of validity window")
    propose_link_parser.add_argument("--via-node-id", default=None, dest="via_node_id",
                                     help="UUID of reifying Why/How neuron")
    propose_link_parser.add_argument("--source-ref", action="append", default=None, dest="source_refs",
                                     help="Prefixed provenance source (e.g. 'note:path/to/note.md'). Repeatable.")
    propose_link_parser.add_argument("--recorded-at", default=None, dest="recorded_at",
                                     help="ISO-8601 transaction time for this claim")
    propose_link_parser.add_argument("--recorded-by", default=None, dest="recorded_by",
                                     help="User identity that recorded this claim")
    propose_link_parser.add_argument("--note", default=None, dest="assertion_note",
                                     help="Free-form annotation on this specific claim")

    # update-note-links command
    update_note_parser = subparsers.add_parser("update-note-links", help="Append wikilinks to a markdown note")
    update_note_parser.add_argument("note_path", help="Relative path to note within notes/ directory")
    update_note_parser.add_argument("links", nargs="+", help="Canonical names to link")

    # ── Engram Layer (short-term memory / context folding) ──────────────────

    # fold — fold content you already have (stdin) or a file you're about to read
    fold_parser = subparsers.add_parser(
        "fold", help="Fold content (stdin) or a file into an engram; no-ops below the size floor")
    fold_parser.add_argument("--label", default="", help="Why you folded this")
    fold_parser.add_argument("--kind", default=None, help="Content kind override")
    fold_parser.add_argument("--file", default=None, help="Fold this file instead of reading stdin")
    fold_parser.add_argument("--quiet", action="store_true", help="Print only the retrieval key")
    fold_parser.add_argument("--session", dest="session_id", default=None, help="Explicit session id override")

    # fold-run — run a command, fold its full output, print head+tail+digest+marker
    fold_run_parser = subparsers.add_parser(
        "fold-run",
        help="Run a command, fold its full output; the raw output never enters your context")
    fold_run_parser.add_argument("--label", default="", help="Why you ran this")
    fold_run_parser.add_argument("--kind", default=None, help="Content kind override")
    fold_run_parser.add_argument("--keep-head", type=int, default=None, dest="keep_head")
    fold_run_parser.add_argument("--keep-tail", type=int, default=None, dest="keep_tail")
    fold_run_parser.add_argument("--quiet", action="store_true")
    fold_run_parser.add_argument("--session", dest="session_id", default=None, help="Explicit session id override")
    # dest="cmd_args" (NOT "command") — a positional named "command" here would
    # clobber the top-level subparsers' dest="command" in the shared Namespace,
    # since argparse merges all parsed args into one Namespace object.
    fold_run_parser.add_argument("cmd_args", nargs=argparse.REMAINDER,
                                 help="-- <command and args to run>")

    # unfold — read folded content back
    unfold_parser = subparsers.add_parser("unfold", help="Read folded content back by key")
    unfold_parser.add_argument("key")
    unfold_parser.add_argument("--grep", default=None, help="Matching lines with --context")
    unfold_parser.add_argument("--context", type=int, default=0, dest="grep_context")
    unfold_parser.add_argument("--lines", default=None, help="A-B byte-cheap slice")
    unfold_parser.add_argument("--head", type=int, default=None)
    unfold_parser.add_argument("--tail", type=int, default=None)
    unfold_parser.add_argument("--max-chars", type=int, default=None, dest="max_chars")
    unfold_parser.add_argument("--meta", action="store_true", dest="meta_only", help="Header only (TOON)")
    unfold_parser.add_argument("--session", dest="session_id", default=None, help="Explicit session id override")

    # folds — list this session's engrams
    folds_parser = subparsers.add_parser("folds", help="List this session's engrams (metadata only, TOON)")
    folds_parser.add_argument("--session", dest="session_id", default=None, help="Explicit session id override")
    folds_parser.add_argument("--kind", default=None)
    folds_parser.add_argument("--grep", default=None)
    folds_parser.add_argument("--limit", type=int, default=20)
    folds_parser.add_argument("--all", action="store_true", dest="all_sessions", help="List across all live sessions")

    # fold-scan — budgeted relevance check: which folded content might matter to these terms
    fold_scan_parser = subparsers.add_parser(
        "fold-scan", help="Budgeted relevance check: which folded content might matter to these terms")
    fold_scan_parser.add_argument("terms", nargs="+", help="One or more search terms")
    fold_scan_parser.add_argument("--session", dest="session_id", default=None, help="Explicit session id override")

    # consolidate — promote an engram into the durable graph
    consolidate_parser = subparsers.add_parser(
        "consolidate", help="Promote an engram into the durable graph (wraps propose-node/propose-link)")
    consolidate_parser.add_argument("key", help="Engram key to consolidate")
    consolidate_parser.add_argument(
        "json_file", nargs="?", default=None,
        help="Path to a JSON file with the node payload. Use '-' or omit (with --json unset) to read stdin.")
    consolidate_parser.add_argument("--json", dest="json_inline", default=None,
                                    help="Inline JSON payload string (alternative to a file/stdin).")
    consolidate_parser.add_argument("--suggest", action="store_true",
                                    help="Draft a propose-node payload from the engram's metadata; never writes.")
    consolidate_parser.add_argument("--archive-only", action="store_true", dest="archive_only",
                                    help="Copy the raw blob into archive/ without creating a Neuron.")
    consolidate_parser.add_argument("--archive", action="store_true",
                                    help="Also copy the raw blob into archive/ (combine with --json).")
    consolidate_parser.add_argument("--link", nargs=2, metavar=("TARGET_ID", "PREDICATE"), default=None,
                                    help="Also link the newly-created neuron to an existing one.")
    consolidate_parser.add_argument("--session", dest="session_id", default=None, help="Explicit session id override")

    # fold-gc — discard engrams
    fold_gc_parser = subparsers.add_parser("fold-gc", help="Discard engrams (TTL sweeps also run opportunistically)")
    fold_gc_parser.add_argument("--session", dest="session_id", default=None, help="Explicit session id override")
    fold_gc_parser.add_argument("--all", action="store_true", dest="all_sessions", help="Operate across all live sessions")
    fold_gc_parser.add_argument("--older-than", default=None, dest="older_than",
                                help="Duration, e.g. 24h / 3d — only discard engrams older than this")
    fold_gc_parser.add_argument("--keep-consolidated", dest="keep_consolidated", action="store_true", default=True,
                                help="Preserve consolidated/archived engrams indefinitely (default on)")
    fold_gc_parser.add_argument("--purge-consolidated", action="store_true", dest="purge_consolidated",
                                help="Also discard consolidated/archived and protected engrams")
    fold_gc_parser.add_argument("--dry-run", action="store_true", dest="dry_run")

    # fold-stats — per-kind fold/unfold counts, unfold rate, disk usage
    subparsers.add_parser("fold-stats", help="Per-kind fold/unfold counts, unfold rate, adaptive settings, disk usage")

    args = parser.parse_args()

    if args.command == "rebuild-index":
        try:
            rebuild_index_command(args.full)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "search":
        indexer = KnewrallIndexer()
        results = indexer.search_nodes(args.query, limit=args.limit)
        if not results:
            print("No matches found.")
        else:
            for r in results:
                print(f"{r['id']} | {r['type']} | {r['canonical_name']}")
    elif args.command == "stats":
        indexer = KnewrallIndexer()
        conn = indexer.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM nodes")
        node_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM edges")
        edge_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM aliases")
        alias_count = cursor.fetchone()[0]
        print(f"Nodes: {node_count}")
        print(f"Edges: {edge_count}")
        print(f"Aliases: {alias_count}")
        indexer.close()
    elif args.command == "refresh-index":
        result = refresh_index()
        print(
            f"Neurons: +{result['neurons_added']} ~{result['neurons_updated']} "
            f"={result['neurons_unchanged']} -{result['neurons_deleted']}"
        )
        print(
            f"Notes:   +{result['notes_added']} ~{result['notes_updated']} "
            f"={result['notes_unchanged']} -{result['notes_deleted']}"
        )
        if result.get("vectors_pruned"):
            print(f"Vectors pruned: {result['vectors_pruned']}")
    elif args.command == "search-graph":
        limit = getattr(args, "limit", 20)
        hybrid = getattr(args, "hybrid", False)
        results = search_graph(args.query, hybrid=hybrid, limit=limit)
        if not results:
            print("No matches found.")
        else:
            for r in results:
                print(f"{r['id']} | {r['type']} | {r['canonical_name']} | aliases: {r['aliases']}")
    elif args.command == "recall":
        result = recall(args.terms, depth=args.depth, limit=args.limit, hybrid=args.hybrid,
                        include_assertions=args.include_assertions)
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(encode_toon(result), end="")
    elif args.command == "propose-node":
        # Resolve the payload source: --json string, a file path, or stdin ('-'/omitted).
        try:
            if args.json_inline is not None:
                raw = args.json_inline
                source = "--json"
            elif args.json_file and args.json_file != "-":
                with open(args.json_file, 'r', encoding='utf-8') as f:
                    raw = f.read()
                source = args.json_file
            else:
                raw = sys.stdin.read()
                source = "stdin"
            if not raw.strip():
                print(f"Error: no JSON payload provided via {source}.", file=sys.stderr)
                sys.exit(1)
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON from {source}: {e}", file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            print(f"Error reading JSON file: {e}", file=sys.stderr)
            sys.exit(1)
        success, msg, node_id = propose_node(payload)
        if success:
            print(f"SUCCESS: {msg}")
            if node_id:
                print(f"Node ID: {node_id}")
        else:
            print(f"FAILURE: {msg}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "update-node":
        # Resolve the payload source: --json string, a file path, or stdin ('-'/omitted).
        try:
            if args.json_inline is not None:
                raw = args.json_inline
                source = "--json"
            elif args.json_file and args.json_file != "-":
                with open(args.json_file, 'r', encoding='utf-8') as f:
                    raw = f.read()
                source = args.json_file
            else:
                raw = sys.stdin.read()
                source = "stdin"
            if not raw.strip():
                print(f"Error: no JSON payload provided via {source}.", file=sys.stderr)
                sys.exit(1)
            updates = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON from {source}: {e}", file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            print(f"Error reading JSON file: {e}", file=sys.stderr)
            sys.exit(1)
        success, msg = update_node_fields(args.node_id, updates, mode=args.mode)
        if success:
            print(f"SUCCESS: {msg}")
        else:
            print(f"FAILURE: {msg}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "propose-link":
        assertion = None
        if args.source_refs or args.recorded_at or args.recorded_by or args.assertion_note:
            assertion = {}
            if args.source_refs:
                assertion["sources"] = sorted(args.source_refs)
            if args.recorded_at:
                assertion["recorded_at"] = args.recorded_at
            if args.recorded_by:
                assertion["recorded_by"] = args.recorded_by
            if args.assertion_note:
                assertion["note"] = args.assertion_note
        success, msg = propose_link(args.source_id, args.target_id, args.predicate,
                                    direction=args.direction, certainty=args.certainty,
                                    assertion=assertion,
                                    valid_from=args.valid_from,
                                    valid_until=args.valid_until,
                                    via_node_id=args.via_node_id)
        if success:
            print(f"SUCCESS: {msg}")
        else:
            print(f"FAILURE: {msg}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "update-note-links":
        success, msg = update_note_links(args.note_path, args.links)
        if success:
            print(f"SUCCESS: {msg}")
        else:
            print(f"FAILURE: {msg}", file=sys.stderr)
            sys.exit(1)

    # ── Code graph commands ───────────────────────────────────────────────────

    elif args.command == "index-code":
        success, msg = index_code(repo_path=args.path, full=args.full)
        if success:
            print(f"SUCCESS: {msg}")
        else:
            print(f"FAILURE: {msg}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "code-search":
        results = code_search(args.query, limit=args.limit)
        if not results:
            print("No symbols found.")
        else:
            for r in results:
                loc = f"{r.get('rel_path','')}:{r.get('start_line','')}"
                print(f"{r['kind']:8} | {r['qualified_name']:40} | {r['repo']} | {loc}")
                if r.get("docstring"):
                    print(f"         | {r['docstring'][:80]}")

    elif args.command == "code-defs":
        results = code_defs(args.name, repo=getattr(args, "repo", None))
        if not results:
            print(f"No definitions found for {args.name!r}.")
        else:
            for r in results:
                print(f"{r['kind']:8} | {r['symbol_id']} | {r.get('rel_path','')}:{r.get('start_line','')}")
                if r.get("signature"):
                    print(f"         | {r['signature']}")

    elif args.command == "code-callers":
        results = code_callers(args.symbol_id)
        if not results:
            print(f"No callers found for {args.symbol_id!r}.")
        else:
            for r in results:
                print(f"{r['kind']:8} | {r['symbol_id']}")

    elif args.command == "code-callees":
        results = code_callees(args.symbol_id)
        if not results:
            print(f"No callees found for {args.symbol_id!r}.")
        else:
            for r in results:
                print(f"{r['kind']:8} | {r['symbol_id']}")

    elif args.command == "code-imports":
        results = code_imports(args.rel_path, repo=getattr(args, "repo", None))
        if not results:
            print(f"No imports found in {args.rel_path!r}.")
        else:
            for r in results:
                print(f"line {r.get('line','?'):4} | {r['edge_type']:8} | {r['dst_name']}")

    elif args.command == "code-stats":
        s = code_stats()
        print(f"Repos:   {s.get('repos', 0)}")
        print(f"Files:   {s.get('files', 0)}")
        print(f"Symbols: {s.get('symbols', 0)}")
        print(f"Edges:   {s.get('edges', 0)}")

    elif args.command == "link-code":
        success, msg = link_code(args.neuron_id, args.symbol_id, args.repo,
                                 kind=getattr(args, "kind", "function"))
        if success:
            print(f"SUCCESS: {msg}")
        else:
            print(f"FAILURE: {msg}", file=sys.stderr)
            sys.exit(1)

    # ── Embedding commands ────────────────────────────────────────────────────

    elif args.command == "embed":
        if getattr(args, "reconcile", None):
            success, msg = embed_reconcile(args.reconcile)
            print(f"SUCCESS: {msg}" if success else f"FAILURE: {msg}", file=sys.stderr if not success else sys.stdout)
            if not success:
                sys.exit(1)
            return

        no_flags = not args.neurons and not args.code and not args.terms
        do_neurons = args.neurons or no_flags
        do_code = args.code or no_flags
        do_terms = args.terms or no_flags

        if do_neurons:
            embedded, skipped = embed_neurons()
            print(f"Neurons: embedded={embedded} skipped/unchanged={skipped}")

        if do_code:
            embedded, skipped = embed_code_symbols(repo=getattr(args, "repo", None))
            print(f"Code symbols: embedded={embedded} skipped/unchanged={skipped}")

        if do_terms:
            embedded, skipped = embed_query_terms()
            print(f"Query terms: embedded={embedded} skipped/unchanged={skipped}")

    # ── Engram Layer commands ─────────────────────────────────────────────────

    elif args.command == "fold":
        content = None
        if args.file is None:
            content = sys.stdin.read()
        result = fold(content=content, file=args.file, label=args.label, kind=args.kind,
                      session_id=args.session_id, quiet=args.quiet)
        if result.get("passthrough"):
            sys.stdout.write(result["content"])
            if result.get("warning"):
                print(result["warning"], file=sys.stderr)
        else:
            print(result["marker"])

    elif args.command == "fold-run":
        command = args.cmd_args
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            print("Error: fold-run requires a command after --, e.g. "
                  "`fold-run --label \"...\" -- pytest -q tests/`", file=sys.stderr)
            sys.exit(2)
        result = fold_run(command, label=args.label, kind=args.kind,
                          keep_head=args.keep_head, keep_tail=args.keep_tail,
                          session_id=args.session_id, quiet=args.quiet)
        print(result["output"])
        sys.exit(result["exit_code"])

    elif args.command == "unfold":
        lines_tuple = None
        if args.lines:
            try:
                a, b = args.lines.split("-", 1)
                lines_tuple = (int(a), int(b))
            except ValueError:
                print(f"Error: --lines must be A-B (got {args.lines!r})", file=sys.stderr)
                sys.exit(1)
        result = unfold(args.key, session_id=args.session_id, grep=args.grep,
                        grep_context=args.grep_context, lines=lines_tuple,
                        head=args.head, tail=args.tail, meta_only=args.meta_only,
                        max_chars=args.max_chars)
        if not result["found"]:
            print(result["error"], file=sys.stderr)
            sys.exit(1)
        if args.meta_only:
            print(encode_toon(result["meta"]), end="")
        else:
            print(result["content"])
            if result.get("truncated"):
                print("[truncated: use --lines or --grep to target]", file=sys.stderr)

    elif args.command == "folds":
        result = list_folds(session_id=args.session_id, kind=args.kind, grep=args.grep,
                            limit=args.limit, all_sessions=args.all_sessions)
        print(encode_toon(result), end="")

    elif args.command == "fold-scan":
        result = fold_scan(args.terms, session_id=args.session_id)
        if result["text"]:
            print(result["text"])

    elif args.command == "consolidate":
        json_payload = None
        if not args.suggest and not args.archive_only:
            try:
                if args.json_inline is not None:
                    raw = args.json_inline
                    source = "--json"
                elif args.json_file and args.json_file != "-":
                    with open(args.json_file, 'r', encoding='utf-8') as f:
                        raw = f.read()
                    source = args.json_file
                else:
                    raw = sys.stdin.read()
                    source = "stdin"
                if not raw.strip():
                    print(f"Error: no JSON payload provided via {source}.", file=sys.stderr)
                    sys.exit(1)
                json_payload = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"Error parsing JSON from {source}: {e}", file=sys.stderr)
                sys.exit(1)
            except OSError as e:
                print(f"Error reading JSON file: {e}", file=sys.stderr)
                sys.exit(1)

        link = tuple(args.link) if args.link else None
        result = consolidate_engram(
            args.key, json_payload=json_payload, suggest=args.suggest,
            archive=args.archive, archive_only=args.archive_only,
            link=link, session_id=args.session_id,
        )
        if not result.get("success"):
            print(f"FAILURE: {result.get('message')}", file=sys.stderr)
            sys.exit(1)
        if result.get("mode") == "suggest":
            print(encode_toon(result["draft"]), end="")
        elif result.get("mode") == "archive-only":
            print(f"SUCCESS: archived to {result['archived_path']}")
        else:
            print(f"SUCCESS: {result['message']}")
            print(f"Node ID: {result['node_id']}")
            if result.get("archived_path"):
                print(f"Archived: {result['archived_path']}")
            if result.get("link_message"):
                print(f"Link: {result['link_message']}")

    elif args.command == "fold-gc":
        older_than_hours = _parse_duration_hours(args.older_than) if args.older_than else None
        result = fold_gc(
            session_id=args.session_id, all_sessions=args.all_sessions,
            older_than_hours=older_than_hours, keep_consolidated=args.keep_consolidated,
            purge_consolidated=args.purge_consolidated, dry_run=args.dry_run,
        )
        print(
            f"Sessions: scanned={result['sessions_scanned']} deleted={result['sessions_deleted']} | "
            f"Engrams: deleted={result['engrams_deleted']} kept={result['engrams_kept']} | "
            f"Freed: {result['bytes_freed']} bytes"
        )

    elif args.command == "fold-stats":
        result = fold_stats()
        print(encode_toon(result), end="")

    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()