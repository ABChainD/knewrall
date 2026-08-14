"""Query-conditioned, additive reading over caller-supplied text."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import get_root
from . import knewrall_reader_router as router


@dataclass
class ReaderResult:
    answer: str = ""
    keywords: List[str] = None
    tags: List[str] = None
    confidence: Optional[str] = None
    provider: str = ""
    model: str = ""
    transport: str = ""
    cached: bool = False
    partial: bool = False
    chunks_used: int = 0
    chunks_total: int = 0
    elapsed_s: float = 0.0
    error: Optional[str] = None
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        self.keywords = list(self.keywords or [])
        self.tags = list(self.tags or [])


def _config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return router._assistant_config(config)


def _cache_path(cfg: Dict[str, Any]) -> Path:
    path = Path(cfg.get("cache_db_path", ".knewrall/reader_cache.db"))
    return path if path.is_absolute() else get_root() / path


def _connect(cfg: Dict[str, Any]) -> sqlite3.Connection:
    path = _cache_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS reader_cache (content_hash TEXT, question_hash TEXT, provider TEXT, model TEXT, answer_json TEXT, created_at TEXT, PRIMARY KEY (content_hash, question_hash, provider, model))")
    conn.execute("CREATE TABLE IF NOT EXISTS reader_invocations (provider TEXT, model TEXT, elapsed_s REAL, cached INTEGER, error TEXT, chunks REAL, created_at TEXT)")
    conn.commit()
    return conn


def _question_hash(question: str) -> str:
    normalized = " ".join(question.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_completion(text: str) -> Dict[str, Any]:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    raw = match.group(1) if match else text.strip()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("completion JSON is not an object")
        return {
            "answer": str(data.get("answer", "")),
            "keywords": [str(v) for v in data.get("keywords", [])][:24],
            "tags": [str(v) for v in data.get("tags", [])][:24],
            "confidence": data.get("confidence") if data.get("confidence") in {"high", "medium", "low"} else None,
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"answer": text, "keywords": [], "tags": [], "confidence": "low", "error": "parse_error"}


def _chunks(text: str, cfg: Dict[str, Any], kind: str) -> List[str]:
    size = max(1, int(cfg.get("chunk_chars", 48000)))
    overlap = min(max(0, int(cfg.get("chunk_overlap_chars", 800))), size - 1)
    if len(text) <= size:
        return [text]
    if kind == "diff":
        pieces = re.split(r"(?=^diff --git )", text, flags=re.MULTILINE)
        if all(len(piece) <= size for piece in pieces if piece):
            return [piece for piece in pieces if piece]
    if kind == "json_output":
        try:
            value = json.loads(text)
            if isinstance(value, list):
                pieces, current = [], "["
                for item in value:
                    encoded = json.dumps(item, ensure_ascii=False)
                    if len(current) + len(encoded) + 2 > size and current != "[":
                        pieces.append(current + "]")
                        current = "["
                    current += ("," if current != "[" else "") + encoded
                if current != "[":
                    pieces.append(current + "]")
                if pieces:
                    return pieces
        except (TypeError, json.JSONDecodeError):
            pass
    result = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = text.rfind("\n\n", start, end)
            if boundary > start:
                end = boundary + 2
        result.append(text[start:end])
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return result


def _prompt(question: str, chunk: str, index: int, total: int) -> str:
    return ("Answer the QUESTION using only the TEXT below. If the TEXT does not contain the answer, say so explicitly. "
            "Return ONLY a fenced JSON block:\n"
            '{"answer": "...", "keywords": [...], "tags": [...], "confidence": "high"|"medium"|"low"}\n\n'
            f"QUESTION: {question}\n\nTEXT (chunk {index}/{total}):\n{chunk}")


def ask(text: str, question: str, *, config: Optional[Dict[str, Any]] = None,
        kind: str = "prose", provider: Optional[str] = None,
        deadline_s: Optional[float] = None, no_assistant: bool = False) -> ReaderResult:
    started = time.monotonic()
    cfg = _config(config)
    if no_assistant or os.environ.get("KNEWRALL_ARL_DISABLE") == "1" or not cfg.get("enabled", False):
        return ReaderResult(error="disabled")
    selected = router.resolve(config, provider=provider)
    if selected is None:
        return ReaderResult(error="no_provider")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    question_hash = _question_hash(question)
    conn = None
    try:
        conn = _connect(cfg)
        row = conn.execute("SELECT answer_json, created_at FROM reader_cache WHERE content_hash=? AND question_hash=? AND provider=? AND model=?", (content_hash, question_hash, selected.name, selected.model)).fetchone()
        if row and time.time() - float(row[1]) <= float(cfg.get("cache_ttl_hours", 720)) * 3600:
            data = json.loads(row[0])
            data.pop("question", None)
            data["cached"] = True
            data["elapsed_s"] = time.monotonic() - started
            result = ReaderResult(**data)
            conn.execute("INSERT INTO reader_invocations VALUES (?, ?, ?, ?, ?, ?, ?)", (selected.name, selected.model, result.elapsed_s, 1, result.error, result.chunks_total, str(time.time())))
            conn.commit()
            return result
        chunks = _chunks(text, cfg, kind)
        max_chunks = int(cfg.get("max_chunks", 4))
        if len(chunks) > max_chunks:
            return ReaderResult(provider=selected.name, model=selected.model, transport=selected.transport, chunks_total=len(chunks), error="content_too_large", detail="narrow the input with --grep or --lines")
        total_deadline = started + (deadline_s or float(cfg.get("total_deadline_seconds", 90)))
        mapped: List[Dict[str, Any]] = []
        partial = False
        error = None
        detail = None
        for index, chunk in enumerate(chunks, 1):
            if time.monotonic() >= total_deadline:
                partial, error = True, "timeout"
                break
            completion = router.complete(selected, _prompt(question, chunk, index, len(chunks)), deadline_s=max(0.1, total_deadline - time.monotonic()))
            if completion.error:
                if mapped:
                    partial = True
                error, detail = completion.error, completion.detail
                break
            parsed = _parse_completion(completion.text)
            mapped.append(parsed)
            if parsed.get("error"):
                error, detail = parsed["error"], "assistant output was not valid JSON"
        if not mapped:
            result = ReaderResult(provider=selected.name, model=selected.model, transport=selected.transport, partial=partial, chunks_total=len(chunks), error=error or "cli_error", detail=detail)
        else:
            if len(mapped) == 1:
                data = mapped[0]
            else:
                reduce_prompt = _prompt(question, "\n\n".join(json.dumps(item) for item in mapped), 1, 1)
                reduced = router.complete(selected, reduce_prompt, deadline_s=max(0.1, total_deadline - time.monotonic()))
                data = _parse_completion(reduced.text) if not reduced.error else mapped[0]
                if reduced.error:
                    error, detail = reduced.error, reduced.detail
                elif data.get("error") and not error:
                    error, detail = data["error"], "assistant output was not valid JSON"
                if reduced.error:
                    partial = True
            result = ReaderResult(**{key: data.get(key) for key in ("answer", "keywords", "tags", "confidence")}, provider=selected.name, model=selected.model, transport=selected.transport, partial=partial, chunks_used=len(mapped), chunks_total=len(chunks), error=error, detail=detail)
        if not result.partial and result.error is None:
            payload = asdict(result)
            payload.pop("cached", None)
            payload["question"] = question
            conn.execute("INSERT OR REPLACE INTO reader_cache VALUES (?, ?, ?, ?, ?, ?)", (content_hash, question_hash, selected.name, selected.model, json.dumps(payload, ensure_ascii=False), str(time.time())))
        result.elapsed_s = time.monotonic() - started
        conn.execute("INSERT INTO reader_invocations VALUES (?, ?, ?, ?, ?, ?, ?)", (selected.name, selected.model, result.elapsed_s, 0, result.error, result.chunks_total, str(time.time())))
        conn.commit()
        return result
    except Exception as exc:
        return ReaderResult(provider=selected.name, model=selected.model, transport=selected.transport, error="cli_error", detail=str(exc), elapsed_s=time.monotonic() - started)
    finally:
        if conn is not None:
            conn.close()


def stats(*, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    conn = _connect(_config(config))
    try:
        rows = conn.execute("SELECT provider, COUNT(*), SUM(cached), SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END), AVG(elapsed_s) FROM reader_invocations GROUP BY provider").fetchall()
        return {"providers": [{"provider": r[0], "invocations": r[1], "cache_hits": r[2] or 0, "errors": r[3] or 0, "avg_elapsed_s": r[4] or 0} for r in rows]}
    finally:
        conn.close()


def cache_gc(*, config: Optional[Dict[str, Any]] = None, older_than_hours: Optional[float] = None, dry_run: bool = False) -> Dict[str, int]:
    cfg = _config(config)
    cutoff = time.time() - (older_than_hours if older_than_hours is not None else float(cfg.get("cache_ttl_hours", 720))) * 3600
    conn = _connect(cfg)
    try:
        count = conn.execute("SELECT COUNT(*) FROM reader_cache WHERE CAST(created_at AS REAL) < ?", (cutoff,)).fetchone()[0]
        if not dry_run:
            conn.execute("DELETE FROM reader_cache WHERE CAST(created_at AS REAL) < ?", (cutoff,))
            conn.commit()
        return {"expired": count, "deleted": 0 if dry_run else count}
    finally:
        conn.close()
