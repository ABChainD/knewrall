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
)
from .knewrall_toon import encode as encode_toon

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

    # update-note-links command
    update_note_parser = subparsers.add_parser("update-note-links", help="Append wikilinks to a markdown note")
    update_note_parser.add_argument("note_path", help="Relative path to note within notes/ directory")
    update_note_parser.add_argument("links", nargs="+", help="Canonical names to link")

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
        result = recall(args.terms, depth=args.depth, limit=args.limit, hybrid=args.hybrid)
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
        success, msg = propose_link(args.source_id, args.target_id, args.predicate)
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

    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()