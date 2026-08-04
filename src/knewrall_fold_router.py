"""
Engram Layer ContentRouter — classification + one digester per `kind`.

Local, deterministic, cheap: regex/structure sniffing on the first 4 KB plus
the invoking command, never an LLM call. `classify_and_digest()` is the
single entry point `fold_run()` (knewrall_middleware.py) calls; each digester
below is independently testable.

The `source_code` digester reuses knewrall_codegraph's existing tree-sitter
extractors (`_PythonExtractor` / `_JsExtractor`) when available, so the fold
digest for a source file is the same symbol outline `code-search` already
indexes — no new parsing dependency, matching vocabulary. When tree-sitter
isn't installed, it degrades to a regex-based signature scraper (imports +
class/def lines + first docstring line) rather than failing the fold.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DIGEST_MAX_CHARS = 1500

KIND_TEST_LOG = "test_log"
KIND_BUILD_LOG = "build_log"
KIND_RUN_LOG = "run_log"
KIND_JSON_OUTPUT = "json_output"
KIND_SEARCH_RESULTS = "search_results"
KIND_DIFF = "diff"
KIND_SOURCE_CODE = "source_code"
KIND_TABULAR = "tabular"
KIND_CONFIG = "config"
KIND_DIR_LISTING = "dir_listing"
KIND_PROSE = "prose"

ALL_KINDS = [
    KIND_TEST_LOG, KIND_BUILD_LOG, KIND_RUN_LOG, KIND_JSON_OUTPUT, KIND_SEARCH_RESULTS,
    KIND_DIFF, KIND_SOURCE_CODE, KIND_TABULAR, KIND_CONFIG, KIND_DIR_LISTING, KIND_PROSE,
]

_TEST_CMD_RE = re.compile(r"\b(pytest|py\.test|jest|cargo\s+test|go\s+test)\b")
_BUILD_CMD_RE = re.compile(r"\b(npm\s+run\s+build|webpack|tsc|make|cargo\s+build|go\s+build|mvn|gradle)\b")
_PASS_FAIL_RE = re.compile(r"\b(PASS\w*|FAIL\w*|assert\w*)\b")
_WARN_ERR_RE = re.compile(r"\b(warning|error)\s*:", re.I)
# Requires a dotted extension before the line number so an ISO timestamp
# ("2026-08-04T10:04:00Z") never collides with ripgrep/grep's path:line:
# prefix — both use colons as separators, but only one has a file extension.
_GREP_LINE_RE = re.compile(r"^[\w./\\-]+\.[A-Za-z0-9]+:\d+:")

_SOURCE_EXTENSIONS = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
}


def classify(text: str, source: Optional[Dict[str, Any]] = None) -> str:
    """Classify by cmd first (unambiguous when available), then content
    sniffing on the first 4KB, checked in the same priority order as the
    plan's §3.1 table: test_log, build_log, run_log, json_output,
    search_results, diff, source_code, tabular, config, dir_listing, prose.
    Order matters — several signals (a colon-separated ISO timestamp vs.
    ripgrep's path:line: prefix, a bare identifier vs. a dir-listing entry)
    can coincidentally match more than one heuristic."""
    source = source or {}
    cmd = source.get("cmd") or ""
    path = source.get("path") or ""
    sample = text[:4096]
    lines = sample.split("\n")
    n = max(1, len(lines))

    if _TEST_CMD_RE.search(cmd):
        return KIND_TEST_LOG
    if _BUILD_CMD_RE.search(cmd):
        return KIND_BUILD_LOG

    if sum(1 for l in lines if _PASS_FAIL_RE.search(l)) / n > 0.05:
        return KIND_TEST_LOG

    if sum(1 for l in lines if _WARN_ERR_RE.search(l)) / n > 0.05:
        return KIND_BUILD_LOG

    timestamp_hits = sum(1 for l in lines if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", l)
                          or re.search(r"\b(INFO|DEBUG|WARN|ERROR|TRACE)\b", l))
    if timestamp_hits / n > 0.2:
        return KIND_RUN_LOG

    stripped = sample.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(text)
            return KIND_JSON_OUTPUT
        except (json.JSONDecodeError, ValueError):
            pass

    if sum(1 for l in lines if _GREP_LINE_RE.match(l)) / n > 0.3:
        return KIND_SEARCH_RESULTS

    if re.search(r"^diff --git ", sample, re.M) or re.search(r"^@@ .* @@", sample, re.M):
        return KIND_DIFF

    if path:
        ext = Path(path).suffix.lower()
        if ext in _SOURCE_EXTENSIONS:
            return KIND_SOURCE_CODE

    delim_counts = Counter()
    for l in lines[:20]:
        for delim in ("\t", ",", "|"):
            if delim in l:
                delim_counts[delim] += 1
    if delim_counts and max(delim_counts.values()) / min(n, 20) > 0.6:
        return KIND_TABULAR

    if re.search(r"^\s*[\w.\-]+\s*[:=]\s*\S", sample, re.M) and sample.count("\n") > 3:
        colonish = sum(1 for l in lines if re.match(r"^\s*[\w.\-]+\s*[:=]\s*\S", l))
        if colonish / n > 0.4:
            return KIND_CONFIG

    dirlisting_hits = sum(1 for l in lines if re.match(r"^[\w./\\-]+/?\s*$", l.strip()))
    if dirlisting_hits / n > 0.7 and n > 3:
        return KIND_DIR_LISTING

    return KIND_PROSE


def _budget(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n... [digest truncated]"


# ── Digesters ────────────────────────────────────────────────────────────

def _digest_test_log(text: str, max_chars: int) -> str:
    lines = text.split("\n")
    failed = [l for l in lines if re.search(r"\bFAILED\b|\bERROR\b", l)]
    passed_m = re.search(r"(\d+)\s+passed", text)
    failed_m = re.search(r"(\d+)\s+failed", text)
    error_m = re.search(r"(\d+)\s+error", text)
    parts = []
    counts = []
    if passed_m:
        counts.append(f"{passed_m.group(1)} passed")
    if failed_m:
        counts.append(f"{failed_m.group(1)} failed")
    if error_m:
        counts.append(f"{error_m.group(1)} error")
    parts.append(", ".join(counts) if counts else f"{len(failed)} FAILED/ERROR lines")
    parts.extend(failed[:30])
    return _budget("\n".join(parts), max_chars)


def _digest_build_log(text: str, max_chars: int) -> str:
    lines = text.split("\n")
    issues = [l.strip() for l in lines if re.search(r"\b(warning|error)\s*:", l, re.I)]
    counts = Counter(issues)
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    out = [f"{len(issues)} warning/error lines ({len(counts)} distinct)"]
    for msg, n in ranked[:30]:
        out.append(f"x{n}  {msg}" if n > 1 else msg)
    return _budget("\n".join(out), max_chars)


def _digest_run_log(text: str, max_chars: int) -> str:
    lines = text.split("\n")
    out: List[str] = []
    prev = None
    repeat = 0
    for l in lines:
        is_warnplus = bool(re.search(r"\b(WARN|ERROR|CRITICAL|FATAL)\b", l))
        if l == prev:
            repeat += 1
            continue
        if repeat:
            out.append(f"  … x{repeat + 1}")
            repeat = 0
        if is_warnplus or prev is None:
            out.append(l)
        prev = l
    if repeat:
        out.append(f"  … x{repeat + 1}")
    return _budget("\n".join(out), max_chars)


def _skeleton(obj: Any, depth: int = 0, max_depth: int = 4) -> Any:
    if depth >= max_depth:
        return type(obj).__name__
    if isinstance(obj, dict):
        return {k: _skeleton(v, depth + 1, max_depth) for k, v in list(obj.items())[:20]}
    if isinstance(obj, list):
        sample = [_skeleton(v, depth + 1, max_depth) for v in obj[:3]]
        return {"len": len(obj), "sample": sample}
    return type(obj).__name__


def _digest_json_output(text: str, max_chars: int) -> str:
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _budget(text[:max_chars], max_chars)
    skeleton = _skeleton(obj)
    return _budget(json.dumps(skeleton, ensure_ascii=False, default=str), max_chars)


def _digest_search_results(text: str, max_chars: int) -> str:
    per_file: Dict[str, List[str]] = {}
    for line in text.split("\n"):
        m = re.match(r"^([^\s:]+):(\d+):(.*)$", line)
        if not m:
            continue
        path, lineno, rest = m.groups()
        per_file.setdefault(path, []).append(f"{lineno}: {rest.strip()}")
    out = [f"{len(per_file)} files, {sum(len(v) for v in per_file.values())} matches"]
    for path, matches in list(per_file.items())[:30]:
        out.append(f"{path} ({len(matches)}): {matches[0]}")
    return _budget("\n".join(out), max_chars)


def _digest_diff(text: str, max_chars: int) -> str:
    out: List[str] = []
    current_file = None
    plus = minus = 0

    def flush():
        if current_file:
            out.append(f"{current_file}  +{plus}/-{minus}")

    for line in text.split("\n"):
        if line.startswith("diff --git"):
            flush()
            m = re.search(r"b/(\S+)$", line)
            current_file = m.group(1) if m else line
            plus = minus = 0
        elif line.startswith("@@"):
            out.append(f"  {line.strip()}")
        elif line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    flush()
    return _budget("\n".join(out), max_chars)


def _digest_source_code_regex_fallback(text: str, max_chars: int, ext: str) -> str:
    lines = text.split("\n")
    out: List[str] = []
    if ext == ".py":
        for l in lines:
            if re.match(r"^\s*(import|from)\s+\S", l) or re.match(r"^\s*(def|class)\s+\w", l):
                out.append(l.rstrip())
    else:
        for l in lines:
            if re.match(r"^\s*(import|export)\s", l) or re.search(
                r"\b(function|class)\s+\w+|\b(const|let|var)\s+\w+\s*=\s*\(", l
            ):
                out.append(l.rstrip())
    return _budget("\n".join(out) or text[:max_chars], max_chars)


def _digest_source_code(text: str, max_chars: int, path: Optional[str]) -> str:
    ext = Path(path).suffix.lower() if path else ".py"
    lang = _SOURCE_EXTENSIONS.get(ext, "python")
    try:
        from .knewrall_codegraph import _get_parser, _PythonExtractor, _JsExtractor
        parser = _get_parser(lang)
        source_bytes = text.encode("utf-8", errors="replace")
        extractor_cls = _PythonExtractor if lang == "python" else _JsExtractor
        extractor = extractor_cls(source_bytes, parser)
        symbols, _edges = extractor.extract()
    except ImportError:
        return _digest_source_code_regex_fallback(text, max_chars, ext)
    except Exception:
        return _digest_source_code_regex_fallback(text, max_chars, ext)

    out: List[str] = []
    for sym in symbols:
        sig = sym.get("signature") or f"{sym['kind']} {sym['qualified_name']}"
        line = f"{sig}"
        if sym.get("docstring"):
            first_line = sym["docstring"].strip().split("\n")[0]
            line += f"  # {first_line}"
        out.append(line)
    if not out:
        return _digest_source_code_regex_fallback(text, max_chars, ext)
    return _budget("\n".join(out), max_chars)


def _digest_tabular(text: str, max_chars: int) -> str:
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return ""
    header = lines[0]
    body = lines[1:]
    out = [header]
    out.extend(body[:5])
    if len(body) > 7:
        out.append("...")
    out.extend(body[-2:] if len(body) > 5 else [])
    out.append(f"({len(body)} data rows)")
    return _budget("\n".join(out), max_chars)


def _digest_config(text: str, max_chars: int) -> str:
    lines = text.split("\n")
    keys = []
    max_depth = 0
    for l in lines:
        stripped = l.lstrip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        indent = len(l) - len(stripped)
        max_depth = max(max_depth, indent // 2)
        m = re.match(r"^([\w.\-]+)\s*[:=]", stripped)
        if m and indent == 0:
            keys.append(m.group(1))
    return _budget(f"top-level keys ({len(keys)}): {', '.join(keys[:40])}\nmax nesting depth: {max_depth}", max_chars)


def _digest_dir_listing(text: str, max_chars: int) -> str:
    entries = [l.strip() for l in text.split("\n") if l.strip()]
    ext_counts = Counter(Path(e).suffix or "(none)" for e in entries)
    deepest = sorted(entries, key=lambda e: e.count("/"), reverse=True)[:10]
    out = [f"{len(entries)} entries"]
    out.append("by extension: " + ", ".join(f"{ext}x{n}" for ext, n in ext_counts.most_common(15)))
    out.append("deepest paths: " + ", ".join(deepest))
    out.append("first 20: " + ", ".join(entries[:20]))
    return _budget("\n".join(out), max_chars)


def _digest_prose(text: str, max_chars: int) -> str:
    lines = text.split("\n")
    head = lines[:5]
    tail = lines[-5:] if len(lines) > 10 else []
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    first_sentences = []
    for p in paragraphs[:10]:
        m = re.match(r"\s*(.+?[.!?])(\s|$)", p.strip())
        first_sentences.append(m.group(1) if m else p.strip()[:100])
    out = ["head: " + " / ".join(head)]
    if tail:
        out.append("tail: " + " / ".join(tail))
    out.append("first sentences: " + " | ".join(first_sentences))
    return _budget("\n".join(out), max_chars)


_DIGESTERS = {
    KIND_TEST_LOG: lambda text, source, max_chars: _digest_test_log(text, max_chars),
    KIND_BUILD_LOG: lambda text, source, max_chars: _digest_build_log(text, max_chars),
    KIND_RUN_LOG: lambda text, source, max_chars: _digest_run_log(text, max_chars),
    KIND_JSON_OUTPUT: lambda text, source, max_chars: _digest_json_output(text, max_chars),
    KIND_SEARCH_RESULTS: lambda text, source, max_chars: _digest_search_results(text, max_chars),
    KIND_DIFF: lambda text, source, max_chars: _digest_diff(text, max_chars),
    KIND_SOURCE_CODE: lambda text, source, max_chars: _digest_source_code(text, max_chars, source.get("path")),
    KIND_TABULAR: lambda text, source, max_chars: _digest_tabular(text, max_chars),
    KIND_CONFIG: lambda text, source, max_chars: _digest_config(text, max_chars),
    KIND_DIR_LISTING: lambda text, source, max_chars: _digest_dir_listing(text, max_chars),
    KIND_PROSE: lambda text, source, max_chars: _digest_prose(text, max_chars),
}


_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{2,}")
_ERROR_CLASS_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Fault))\b")


def extract_keywords(text: str, source: Optional[Dict[str, Any]] = None, cap: int = 24) -> List[str]:
    """Split path components (src/auth/middleware.py -> auth, middleware) and
    snake_case/camelCase identifiers, harvest error-class names — the
    complexity budget for findability, per the plan's Q4 resolution (literal-
    only fold-scan; no second vector store)."""
    source = source or {}
    seen: List[str] = []

    def add(tok: str) -> bool:
        if len(tok) >= 3 and tok not in seen:
            seen.append(tok)
        return len(seen) >= cap

    path = source.get("path")
    if path:
        for part in re.split(r"[\\/]", path):
            stem = Path(part).stem if "." in part else part
            for piece in re.split(r"[_\-.]+", stem):
                if add(piece):
                    return seen
            for piece in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", stem):
                if add(piece):
                    return seen

    for m in _ERROR_CLASS_RE.finditer(text[:8192]):
        if add(m.group(1)):
            return seen

    for tok in _IDENTIFIER_RE.findall(text[:8192]):
        parts = re.split(r"[./_-]+", tok)
        for candidate in [tok] + parts:
            if add(candidate):
                return seen

    return seen


def digest_for(kind: str, text: str, source: Optional[Dict[str, Any]] = None,
               max_chars: int = DEFAULT_DIGEST_MAX_CHARS) -> str:
    """Run the digester for a specific (possibly caller-overridden) kind,
    never raising — a digester failure degrades to a truncated raw-text
    digest rather than blocking the fold."""
    source = source or {}
    digester = _DIGESTERS.get(kind, _DIGESTERS[KIND_PROSE])
    try:
        return digester(text, source, max_chars)
    except Exception:
        return _budget(text, max_chars)


def classify_and_digest(
    payload_bytes: bytes,
    source: Optional[Dict[str, Any]] = None,
    max_chars: int = DEFAULT_DIGEST_MAX_CHARS,
    kind_override: Optional[str] = None,
) -> Tuple[str, str, List[str]]:
    """The ContentRouter's single entry point: classify (unless overridden),
    then run the matching digester, then extract keywords. Returns (kind,
    digest, keywords). Never raises."""
    source = source or {}
    text = payload_bytes.decode("utf-8", errors="replace")
    kind = kind_override or classify(text, source)
    digest = digest_for(kind, text, source, max_chars)
    keywords = extract_keywords(text, source)
    return kind, digest, keywords
