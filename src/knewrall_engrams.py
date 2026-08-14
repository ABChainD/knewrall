"""
Knewrall Engram Layer store — short-term memory / context folding.

engrams/<YYYY>/<MM-DD>/<session_short>/ holds one write-once, content-addressed
`.engram` file per folded record plus a `session.json` manifest that carries all
*mutable* per-engram state (unfolds, last_unfold_at, ttl_at, consolidated_to,
archived_path). `.engram` files are never rewritten after creation — bumping a
stat in a variable-length JSON header line would force a full atomic rewrite of
a file that can be up to `max_session_bytes` on every single `unfold`, so all
mutable state lives in session.json instead (already a small, rebuildable cache
rewritten by every verb below). See
_projects/knewrall-dev/plans/short-term-memory-layer-plan.md §1.4/§1.5.

Protection is PATH-based, not checksum-based: `system.checksum` is defined in
master.schema.json but its computation is commented out in knewrall_crud.py, so
no neuron has a populated checksum today. An engram whose `source.path` resolves
under neurons/, notes/, or archive/ is marked `protected: true` at fold time
instead — deterministic, O(1), and covers the real case (folding a durable
file's content).

engrams/ is never scanned by KnewrallIndexer (it only globs neurons_dir/
notes_dir — see knewrall_indexer.py's refresh_index()), so this whole subsystem
is invisible to refresh-index/rebuild-index by construction. No exclusion code
is needed here; the regression test lives in test_engrams_store.py.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import time
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import get_root

# ── Tunables (mirrors the plan's engrams config block; CLI/config.json may
#    override these per-call, but these are the shipped defaults) ──────────

DEFAULT_TTL_HOURS = 72
DEFAULT_SESSION_IDLE_HOURS = 8
DEFAULT_MAX_SESSION_BYTES = 256 * 1024 * 1024  # 256 MB (drops to 128MB once Phase 4 gzip lands)
KEY_PREFIX_LEN = 10  # hex chars of sha256 used as the initial content-addressed key
HEADER_SCHEMA_VERSION = 1
SESSION_SCHEMA_VERSION = 2

# Paths that mark an engram as durable-already (path-based protection, fixing
# the plan's originally-unimplementable checksum-based rule 6).
_PROTECTED_DIR_NAMES = ("neurons", "notes", "archive")


class EngramStoreFull(Exception):
    """Raised by write_engram() when max_session_bytes is exceeded and nothing
    evictable (non-protected, non-consolidated) remains to free space."""


class EngramNotFound(Exception):
    """Raised by read_engram()/read_meta() when a key resolves to nothing —
    the CLI layer turns this into the user-facing 'expired or discarded'
    (or 'never synced', on agents-hq) message."""


# ── Time helpers ─────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _est_tokens(num_bytes: int) -> int:
    return math.ceil(num_bytes / 4)


# ── Atomic writes ────────────────────────────────────────────────────────

def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """temp-file + os.replace so a crash never leaves a partial file, and so
    concurrent writers never observe a half-written file — the property that
    lets .engram reads skip locking entirely."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _atomic_write_json(path: Path, obj: Any) -> None:
    payload = (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, payload)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ── Folder layout ────────────────────────────────────────────────────────

def engrams_root(root: Optional[Path] = None) -> Path:
    return (root or get_root()) / "engrams"


def _sessions_meta_dir(root: Optional[Path] = None) -> Path:
    return (root or get_root()) / ".knewrall" / "sessions"


def _session_current_path(root: Optional[Path] = None) -> Path:
    return (root or get_root()) / ".knewrall" / "session_current"


def _session_record_path(session_id: str, root: Optional[Path] = None) -> Path:
    return _sessions_meta_dir(root) / f"{session_id}.json"


# ── Session identity ─────────────────────────────────────────────────────

@dataclass
class Session:
    session_id: str
    session_short: str
    dir: str  # relative to engrams/, e.g. "2026/08-03/a3f19c2b" — fixed at session start
    started_at: str
    last_seen_at: str
    harness: str = "cli"
    cwd: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_short": self.session_short,
            "dir": self.dir,
            "started_at": self.started_at,
            "last_seen_at": self.last_seen_at,
            "harness": self.harness,
            "cwd": self.cwd,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Session":
        return cls(
            session_id=d["session_id"],
            session_short=d.get("session_short", d["session_id"][:8]),
            dir=d["dir"],
            started_at=d["started_at"],
            last_seen_at=d.get("last_seen_at", d["started_at"]),
            harness=d.get("harness", "cli"),
            cwd=d.get("cwd", ""),
        )


def _new_session(root: Path, harness: str, cwd: str) -> Session:
    session_id = _uuid.uuid4().hex
    short = session_id[:8]
    now = _now()
    started = _iso(now)
    day_dir = f"{now:%Y}/{now:%m-%d}/{short}"
    session = Session(
        session_id=session_id, session_short=short, dir=day_dir,
        started_at=started, last_seen_at=started, harness=harness, cwd=cwd,
    )
    _save_session_record(session, root)
    _set_session_current(session, root)
    return session


def _save_session_record(session: Session, root: Path) -> None:
    _atomic_write_json(_session_record_path(session.session_id, root), session.to_dict())


def _set_session_current(session: Session, root: Path) -> None:
    _atomic_write_json(_session_current_path(root), session.to_dict())


def _load_session_record(session_id: str, root: Path) -> Optional[Session]:
    d = _read_json(_session_record_path(session_id, root))
    if d is None:
        return None
    try:
        return Session.from_dict(d)
    except KeyError:
        return None


def resolve_session(
    root: Optional[Path] = None,
    session_id: Optional[str] = None,
    harness: str = "cli",
    cwd: Optional[str] = None,
    idle_hours: float = DEFAULT_SESSION_IDLE_HOURS,
    touch: bool = True,
) -> Session:
    """Resolve the active session per the plan's §7.1 precedence:

    1. `session_id` param (an explicit `--session` override from the caller) —
       wins over everything else.
    2. `KNEWRALL_SESSION` env var — explicit, harness-agnostic.
    3. `.knewrall/session_current` pointer, if not stale (last_seen_at within
       `idle_hours`).
    4. Rolling fallback — start a fresh session.

    A resolved *explicit* id (1 or 2) whose session record does not exist yet
    is created fresh (first reference); one that already exists is loaded and
    its recorded `dir` is used verbatim — writes always go to the session's
    recorded (start-date) directory, never to "today's" directory (this is
    what makes midnight-spanning sessions not split).
    """
    root = root or get_root()
    cwd = cwd if cwd is not None else os.getcwd()

    explicit_id = session_id or os.environ.get("KNEWRALL_SESSION")
    if explicit_id:
        existing = _load_session_record(explicit_id, root)
        if existing is not None:
            if touch:
                existing.last_seen_at = _iso(_now())
                existing.cwd = cwd
                _save_session_record(existing, root)
                _set_session_current(existing, root)
            return existing
        now = _now()
        short = explicit_id[:8]
        started = _iso(now)
        session = Session(
            session_id=explicit_id, session_short=short,
            dir=f"{now:%Y}/{now:%m-%d}/{short}",
            started_at=started, last_seen_at=started, harness=harness, cwd=cwd,
        )
        _save_session_record(session, root)
        _set_session_current(session, root)
        return session

    pointer = _read_json(_session_current_path(root))
    if pointer:
        try:
            candidate = Session.from_dict(pointer)
            record = _load_session_record(candidate.session_id, root) or candidate
            age = _now() - _parse_iso(record.last_seen_at)
            if age <= timedelta(hours=idle_hours):
                if touch:
                    record.last_seen_at = _iso(_now())
                    record.cwd = cwd
                    _save_session_record(record, root)
                    _set_session_current(record, root)
                return record
        except (KeyError, ValueError):
            pass  # corrupt pointer — fall through to a fresh session

    return _new_session(root, harness, cwd)


def session_dir(session: Session, root: Optional[Path] = None) -> Path:
    return engrams_root(root) / session.dir


# ── Path-based protection (fixes the plan's unimplementable checksum rule) ──

def is_protected_path(path: Optional[str], root: Optional[Path] = None) -> bool:
    """True if `path` resolves under neurons/, notes/, or archive/ — the
    content is already durable, so the resulting engram must never be TTL'd.
    Deterministic, O(1): compares resolved path parents, never hashes."""
    if not path:
        return False
    root = root or get_root()
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for name in _PROTECTED_DIR_NAMES:
        try:
            protected_dir = (root / name).resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == protected_dir or protected_dir in resolved.parents:
            return True
    return False


# ── Engram header (write-once) ──────────────────────────────────────────

def _header_line(header: Dict[str, Any]) -> bytes:
    return json.dumps(header, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"


def _read_header_and_payload(path: Path) -> Tuple[Dict[str, Any], bytes]:
    with open(path, "rb") as f:
        header_line = f.readline()
        payload = f.read()
    header = json.loads(header_line.decode("utf-8"))
    return header, payload


def _read_header_only(path: Path) -> Dict[str, Any]:
    with open(path, "rb") as f:
        header_line = f.readline()
    return json.loads(header_line.decode("utf-8"))


# ── session.json (mutable per-engram state) ─────────────────────────────

def _session_manifest_path(session: Session, root: Optional[Path] = None) -> Path:
    return session_dir(session, root) / "session.json"


def _load_session_manifest(session: Session, root: Optional[Path] = None) -> Dict[str, Any]:
    d = _read_json(_session_manifest_path(session, root))
    if d is not None:
        return d
    return {
        "v": SESSION_SCHEMA_VERSION,
        "session_id": session.session_id,
        "session_short": session.session_short,
        "harness": session.harness,
        "started_at": session.started_at,
        "last_seen_at": session.last_seen_at,
        "cwd": session.cwd,
        "dir": session.dir,
        "engram_count": 0,
        "bytes_folded": 0,
        "est_tokens_saved": 0,
        "unfold_count": 0,
        "status": "active",
        "engrams": {},
    }


def _save_session_manifest(session: Session, manifest: Dict[str, Any], root: Optional[Path] = None) -> None:
    _atomic_write_json(_session_manifest_path(session, root), manifest)


def rebuild_session_manifest(session: Session, root: Optional[Path] = None) -> Dict[str, Any]:
    """Rebuild session.json from the .engram headers on disk when it's
    missing/corrupt. Static fields are recovered exactly; MUTABLE fields
    (unfolds, ttl_at extensions, consolidated_to, archived_path) reset to
    their fold-time defaults — a lost/corrupted session.json means "this
    session's engrams look freshly folded again," not data loss of the
    underlying content (plan §1.5)."""
    sdir = session_dir(session, root)
    manifest = {
        "v": SESSION_SCHEMA_VERSION,
        "session_id": session.session_id,
        "session_short": session.session_short,
        "harness": session.harness,
        "started_at": session.started_at,
        "last_seen_at": session.last_seen_at,
        "cwd": session.cwd,
        "dir": session.dir,
        "engram_count": 0,
        "bytes_folded": 0,
        "est_tokens_saved": 0,
        "unfold_count": 0,
        "status": "active",
        "engrams": {},
    }
    if sdir.is_dir():
        for engram_path in sorted(sdir.glob("*.engram")):
            try:
                header = _read_header_only(engram_path)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            created_at = header.get("created_at", session.started_at)
            ttl_at = _iso(_parse_iso(created_at) + timedelta(hours=DEFAULT_TTL_HOURS))
            manifest["engrams"][header["key"]] = {
                "unfolds": 0,
                "last_unfold_at": None,
                "ttl_at": ttl_at,
                "protected": bool(header.get("protected", False)),
                "consolidated_to": None,
                "archived_path": None,
                "assistant_answers": {},
                "assistant_keywords": [],
            }
            manifest["engram_count"] += 1
            manifest["bytes_folded"] += header.get("bytes", 0)
            manifest["est_tokens_saved"] += header.get("est_tokens", 0)
    _save_session_manifest(session, manifest, root)
    return manifest


# ── write_engram ─────────────────────────────────────────────────────────

def write_engram(
    payload_bytes: bytes,
    meta: Dict[str, Any],
    *,
    session: Optional[Session] = None,
    root: Optional[Path] = None,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    max_session_bytes: int = DEFAULT_MAX_SESSION_BYTES,
) -> str:
    """Write a new engram (or return the existing key if the exact same
    payload bytes were already folded in this session — free dedupe).

    `meta` supported keys: kind, source (dict), label, digest, keywords
    (list, capped to 24), protected (bool — normally computed by the caller
    via is_protected_path() and passed through here).
    """
    root = root or get_root()
    session = session or resolve_session(root=root)
    sdir = session_dir(session, root)
    sdir.mkdir(parents=True, exist_ok=True)

    full_hash = hashlib.sha256(payload_bytes).hexdigest()
    key_len = KEY_PREFIX_LEN
    path = sdir / f"{full_hash[:key_len]}.engram"
    while path.exists():
        try:
            _, existing_payload = _read_header_and_payload(path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            existing_payload = None
        if existing_payload == payload_bytes:
            return full_hash[:key_len]  # true dedupe — zero bytes written
        key_len += 2
        if key_len > len(full_hash):
            raise RuntimeError("engram key disambiguation exhausted (sha256 collision?)")
        path = sdir / f"{full_hash[:key_len]}.engram"
    key = full_hash[:key_len]

    manifest = _load_session_manifest(session, root)
    new_bytes = len(payload_bytes)
    if manifest["bytes_folded"] + new_bytes > max_session_bytes:
        _evict_for_space(session, manifest, root, needed_bytes=new_bytes, max_session_bytes=max_session_bytes)

    created_at = _now()
    ttl_at = created_at + timedelta(hours=ttl_hours)
    protected = bool(meta.get("protected", False))
    header = {
        "v": HEADER_SCHEMA_VERSION,
        "key": key,
        "session": session.session_id,
        "created_at": _iso(created_at),
        "kind": meta.get("kind", "prose"),
        "source": meta.get("source") or {},
        "label": meta.get("label", ""),
        "bytes": new_bytes,
        "lines": payload_bytes.count(b"\n") + (1 if payload_bytes and not payload_bytes.endswith(b"\n") else 0),
        "est_tokens": _est_tokens(new_bytes),
        "digest": meta.get("digest", ""),
        "keywords": list(meta.get("keywords") or [])[:24],
        "protected": protected,
    }
    if meta.get("encoding"):
        header["encoding"] = meta["encoding"]

    _atomic_write_bytes(path, _header_line(header) + payload_bytes)

    manifest["engrams"][key] = {
        "unfolds": 0,
        "last_unfold_at": None,
        "ttl_at": _iso(ttl_at),
        "protected": protected,
        "consolidated_to": None,
        "archived_path": None,
        "assistant_answers": {},
        "assistant_keywords": [],
    }
    manifest["engram_count"] += 1
    manifest["bytes_folded"] += new_bytes
    manifest["est_tokens_saved"] += header["est_tokens"]
    manifest["last_seen_at"] = _iso(_now())
    _save_session_manifest(session, manifest, root)

    return key


def _evict_for_space(
    session: Session, manifest: Dict[str, Any], root: Path, *, needed_bytes: int, max_session_bytes: int
) -> None:
    """Evict engrams by oldest last-touched (max(created_at, last_unfold_at)),
    skipping protected/consolidated engrams, until there's room — or refuse
    (EngramStoreFull) once everything resident is untouchable."""
    sdir = session_dir(session, root)
    candidates = []
    for key, state in manifest["engrams"].items():
        if state.get("protected") or state.get("consolidated_to"):
            continue
        engram_path = sdir / f"{key}.engram"
        if not engram_path.exists():
            continue
        try:
            header = _read_header_only(engram_path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        created_at = _parse_iso(header.get("created_at", session.started_at))
        last_unfold = state.get("last_unfold_at")
        last_touched = _parse_iso(last_unfold) if last_unfold else created_at
        last_touched = max(created_at, last_touched)
        candidates.append((last_touched, key, engram_path, header.get("bytes", 0)))

    candidates.sort(key=lambda c: c[0])
    freed = 0
    for _, key, engram_path, size in candidates:
        if manifest["bytes_folded"] - freed + needed_bytes <= max_session_bytes:
            break
        try:
            engram_path.unlink()
        except OSError:
            continue
        freed += size
        manifest["engrams"].pop(key, None)

    manifest["bytes_folded"] = max(0, manifest["bytes_folded"] - freed)

    if manifest["bytes_folded"] + needed_bytes > max_session_bytes:
        raise EngramStoreFull(
            f"session {session.session_short}: max_session_bytes ({max_session_bytes}) exceeded "
            "and no evictable (non-protected, non-consolidated) engrams remain"
        )


# ── Resolution across sessions ("session-first, then across all live sessions") ──

_KEY_RE = re.compile(r"^[0-9a-f]+$")


def is_valid_key(key: str) -> bool:
    """Keys are lowercase hex (a sha256 prefix). Validating before resolution
    matters because resolution GLOBS for `<key>.engram` across all live
    sessions: an unvalidated key containing glob metacharacters (`unfold '*'`)
    would silently resolve to an arbitrary other engram instead of producing
    the 'not found (expired or discarded)' message the plan (§4.2) requires to
    be distinguishable from an empty result."""
    return bool(key) and bool(_KEY_RE.match(key))


def _find_engram_path(key: str, session: Optional[Session], root: Path) -> Optional[Path]:
    if not is_valid_key(key):
        return None
    if session is not None:
        candidate = session_dir(session, root) / f"{key}.engram"
        if candidate.exists():
            return candidate
    eroot = engrams_root(root)
    if not eroot.is_dir():
        return None
    for match in eroot.glob(f"*/*/*/{key}.engram"):
        return match
    return None


def _find_owning_session_dir(key: str, root: Path, *, session: Optional[Session] = None) -> Optional[Path]:
    """Resolve the session directory that owns `key`. Session-scoped first
    (matches `_find_engram_path`'s precedence) — content-addressing means two
    *different* sessions that fold byte-identical content independently land
    on the same key, so a plain global glob can silently pick the wrong
    session's directory when both exist. Only fall back to the global glob
    (arbitrary match among sessions sharing that key) when no session is
    given, e.g. a bare `unfold <key>` with no `--session`."""
    if not is_valid_key(key):
        return None
    if session is not None:
        candidate = session_dir(session, root) / f"{key}.engram"
        if candidate.exists():
            return candidate.parent
    eroot = engrams_root(root)
    if not eroot.is_dir():
        return None
    for match in eroot.glob(f"*/*/*/{key}.engram"):
        return match.parent
    return None


# ── read_meta / read_engram ──────────────────────────────────────────────

def read_meta(key: str, *, session: Optional[Session] = None, root: Optional[Path] = None) -> Dict[str, Any]:
    """Static header fields merged with the current mutable state from the
    owning session's session.json (unfolds, last_unfold_at, ttl_at,
    consolidated_to, archived_path)."""
    root = root or get_root()
    path = _find_engram_path(key, session, root)
    if path is None:
        raise EngramNotFound(key)
    header = _read_header_only(path)
    sdir = path.parent
    manifest = _read_json(sdir / "session.json") or {}
    state = (manifest.get("engrams") or {}).get(key, {})
    merged = dict(header)
    merged.update({
        "unfolds": state.get("unfolds", 0),
        "last_unfold_at": state.get("last_unfold_at"),
        "ttl_at": state.get("ttl_at"),
        "consolidated_to": state.get("consolidated_to"),
        "archived_path": state.get("archived_path"),
        "assistant_answers": state.get("assistant_answers", {}),
        "assistant_keywords": state.get("assistant_keywords", []),
    })
    return merged


def _grep_window(text: str, pattern: str, context: int) -> str:
    lines = text.split("\n")
    regex = re.compile(pattern)
    keep = set()
    for i, line in enumerate(lines):
        if regex.search(line):
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                keep.add(j)
    if not keep:
        return ""
    ordered = sorted(keep)
    out = []
    prev = None
    for idx in ordered:
        if prev is not None and idx != prev + 1:
            out.append("--")
        out.append(lines[idx])
        prev = idx
    return "\n".join(out)


def read_engram(
    key: str,
    *,
    session: Optional[Session] = None,
    root: Optional[Path] = None,
    head: Optional[int] = None,
    tail: Optional[int] = None,
    lines: Optional[Tuple[int, int]] = None,
    grep: Optional[str] = None,
    grep_context: int = 0,
    max_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Read a folded payload back, optionally windowed. Returns
    {"meta": <header+mutable state>, "content": str, "truncated": bool}.

    Window precedence when multiple are given: grep > lines > head/tail > full
    (matching the CLI's documented `unfold` flag precedence).
    """
    root = root or get_root()
    path = _find_engram_path(key, session, root)
    if path is None:
        raise EngramNotFound(key)
    header, payload = _read_header_and_payload(path)
    text = payload.decode("utf-8", errors="replace")

    truncated = False
    if grep is not None:
        content = _grep_window(text, grep, grep_context)
    elif lines is not None:
        all_lines = text.split("\n")
        start, end = lines
        content = "\n".join(all_lines[max(0, start - 1):end])
    elif head is not None or tail is not None:
        all_lines = text.split("\n")
        parts = []
        if head:
            parts.append("\n".join(all_lines[:head]))
        if tail:
            parts.append("\n".join(all_lines[-tail:]))
        content = "\n...\n".join(parts) if len(parts) == 2 else (parts[0] if parts else "")
    else:
        content = text

    if max_chars is not None and len(content) > max_chars:
        half = max_chars // 2
        content = content[:half] + "\n...\n" + content[-half:]
        truncated = True

    sdir = path.parent
    manifest = _read_json(sdir / "session.json") or {}
    state = (manifest.get("engrams") or {}).get(key, {})
    meta = dict(header)
    meta.update({
        "unfolds": state.get("unfolds", 0),
        "last_unfold_at": state.get("last_unfold_at"),
        "ttl_at": state.get("ttl_at"),
        "consolidated_to": state.get("consolidated_to"),
        "archived_path": state.get("archived_path"),
        "assistant_answers": state.get("assistant_answers", {}),
        "assistant_keywords": state.get("assistant_keywords", []),
    })
    return {"meta": meta, "content": content, "truncated": truncated}


# ── mark_unfolded / state updates (mutable — session.json only) ─────────

def mark_unfolded(key: str, *, session: Optional[Session] = None, root: Optional[Path] = None, ttl_hours: float = DEFAULT_TTL_HOURS) -> None:
    """Increment unfolds, stamp last_unfold_at, and extend ttl_at — entirely
    within session.json. The .engram file itself is never touched."""
    root = root or get_root()
    sdir = _find_owning_session_dir(key, root, session=session)
    if sdir is None:
        raise EngramNotFound(key)
    manifest_path = sdir / "session.json"
    manifest = _read_json(manifest_path)
    if manifest is None:
        return
    state = manifest.get("engrams", {}).get(key)
    if state is None:
        return
    now = _now()
    state["unfolds"] = state.get("unfolds", 0) + 1
    state["last_unfold_at"] = _iso(now)
    state["ttl_at"] = _iso(now + timedelta(hours=ttl_hours))
    manifest["unfold_count"] = manifest.get("unfold_count", 0) + 1
    _atomic_write_bytes(manifest_path, (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def update_engram_state(key: str, *, session: Optional[Session] = None, root: Optional[Path] = None, **fields: Any) -> None:
    """Generic setter for mutable per-engram fields (protected,
    consolidated_to, archived_path) used by consolidate/fold-gc. Never
    touches the .engram file."""
    root = root or get_root()
    sdir = _find_owning_session_dir(key, root, session=session)
    if sdir is None:
        raise EngramNotFound(key)
    manifest_path = sdir / "session.json"
    manifest = _read_json(manifest_path)
    if manifest is None:
        return
    state = manifest.get("engrams", {}).get(key)
    if state is None:
        return
    state.update(fields)
    _atomic_write_bytes(manifest_path, (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


# ── list_engrams / session_summary ───────────────────────────────────────

def list_engrams(
    *,
    session: Optional[Session] = None,
    root: Optional[Path] = None,
    kind: Optional[str] = None,
    grep: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """List engram metadata (headers merged with mutable state), newest
    first. Reads only header lines — never payloads."""
    root = root or get_root()
    sdirs: List[Path]
    if session is not None:
        sdirs = [session_dir(session, root)]
    else:
        eroot = engrams_root(root)
        sdirs = [p.parent for p in eroot.glob("*/*/*/session.json")] if eroot.is_dir() else []

    results = []
    for sdir in sdirs:
        manifest = _read_json(sdir / "session.json") or {}
        engram_states = manifest.get("engrams", {})
        for engram_path in sdir.glob("*.engram"):
            try:
                header = _read_header_only(engram_path)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if kind is not None and header.get("kind") != kind:
                continue
            state = engram_states.get(header["key"], {})
            if grep is not None:
                haystack = " ".join([
                    header.get("label", ""), header.get("digest", ""),
                    " ".join(header.get("keywords", [])),
                ]).lower()
                if grep.lower() not in haystack:
                    continue
            merged = dict(header)
            merged.update({
                "unfolds": state.get("unfolds", 0),
                "last_unfold_at": state.get("last_unfold_at"),
                "ttl_at": state.get("ttl_at"),
                "consolidated_to": state.get("consolidated_to"),
                "archived_path": state.get("archived_path"),
                "assistant_answers": state.get("assistant_answers", {}),
                "assistant_keywords": state.get("assistant_keywords", []),
            })
            results.append(merged)

    results.sort(key=lambda h: h.get("created_at", ""), reverse=True)
    return results[:limit]


def session_summary(session: Optional[Session] = None, root: Optional[Path] = None) -> Dict[str, Any]:
    root = root or get_root()
    session = session or resolve_session(root=root, touch=False)
    manifest = _read_json(_session_manifest_path(session, root))
    if manifest is None:
        return {
            "session_id": session.session_id, "session_short": session.session_short,
            "engram_count": 0, "bytes_folded": 0, "est_tokens_saved": 0, "unfold_count": 0,
        }
    return manifest


# ── TTL sweep ─────────────────────────────────────────────────────────────

def sweep_expired(
    *, root: Optional[Path] = None, ttl_hours: float = DEFAULT_TTL_HOURS, deadline_seconds: float = 0.2
) -> Dict[str, int]:
    """Opportunistic, best-effort sweep — called by fold/unfold/folds. Caps
    total work at `deadline_seconds` and never raises (caller wraps this in
    a bare except anyway, per the SessionStart hook's own discipline, but the
    function is defensive on its own too).

    Deletes a session directory only when EVERY engram in it is both past its
    own (possibly unfold-extended) ttl_at AND not protected/consolidated —
    never a blind directory-name-age bulk delete, since that would ignore
    unfold-driven ttl extension and risk deleting still-useful content."""
    stats = {"sessions_scanned": 0, "sessions_deleted": 0, "engrams_deleted": 0}
    root = root or get_root()
    eroot = engrams_root(root)
    if not eroot.is_dir():
        return stats

    deadline = time.monotonic() + deadline_seconds
    now = _now()

    try:
        session_dirs = list(eroot.glob("*/*/*/session.json"))
    except OSError:
        return stats

    for manifest_path in session_dirs:
        if time.monotonic() > deadline:
            break
        sdir = manifest_path.parent
        manifest = _read_json(manifest_path)
        if manifest is None:
            continue
        stats["sessions_scanned"] += 1
        engrams = manifest.get("engrams", {})
        if not engrams:
            # Empty session with no engrams left — safe to drop once clearly stale.
            try:
                started = _parse_iso(manifest.get("started_at", _iso(now)))
            except ValueError:
                continue
            if now - started > timedelta(hours=ttl_hours):
                try:
                    shutil.rmtree(sdir)
                    stats["sessions_deleted"] += 1
                except OSError:
                    pass
            continue

        all_expired = True
        for state in engrams.values():
            if state.get("protected") or state.get("consolidated_to"):
                all_expired = False
                break
            ttl_at = state.get("ttl_at")
            try:
                if ttl_at is None or now <= _parse_iso(ttl_at):
                    all_expired = False
                    break
            except ValueError:
                all_expired = False
                break

        if all_expired:
            try:
                shutil.rmtree(sdir)
                stats["sessions_deleted"] += 1
                stats["engrams_deleted"] += len(engrams)
            except OSError:
                pass

    return stats


# ── Archive copy (Phase 4) ───────────────────────────────────────────────
#
# `archive/` is documented as "static, non-multimedia files kept as immutable
# records" but has no CLI verb that writes to it — `consolidate --archive`
# (knewrall_middleware.consolidate_engram()) becomes that verb, calling
# archive_engram() below to copy the raw blob, date-sharded to match
# archive/'s own declared `date: YYYY/MM` scheme.

_ARCHIVE_EXT_BY_KIND = {
    "test_log": ".log", "build_log": ".log", "run_log": ".log",
    "json_output": ".json", "diff": ".diff", "tabular": ".csv",
    "search_results": ".txt", "config": ".txt", "dir_listing": ".txt",
    "source_code": ".txt", "prose": ".txt",
}


def _archive_slug(header: Dict[str, Any]) -> str:
    label = header.get("label") or header.get("kind") or "engram"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", label).strip("-").lower()
    return (slug[:40] or "engram")


def archive_engram(key: str, header: Dict[str, Any], payload_bytes: bytes, root: Optional[Path] = None) -> str:
    """Copy a folded engram's raw payload into archive/YYYY/MM/<key>-<slug>.<ext>.
    Returns the path RELATIVE to root (POSIX separators), suitable for both
    the engram's `archived_path` (session.json) and the new Neuron's
    `properties.source_artifact`. Not atomic-critical (archive/ has no
    concurrent writer for a given key — a fold's key is content-addressed and
    stable), but still a full-write-then-rename for consistency."""
    root = root or get_root()
    created = _parse_iso(header["created_at"])
    ext = _ARCHIVE_EXT_BY_KIND.get(header.get("kind"), ".txt")
    slug = _archive_slug(header)
    rel_dir = Path(f"{created:%Y}") / f"{created:%m}"
    archive_dir = root / "archive" / rel_dir
    filename = f"{key}-{slug}{ext}"
    dest = archive_dir / filename
    _atomic_write_bytes(dest, payload_bytes)
    return (Path("archive") / rel_dir / filename).as_posix()


# ── TOIN-lite adaptive digest widths (Phase 4) ───────────────────────────
#
# `.knewrall/engram_stats.json` — per-machine, gitignored, stignored,
# accumulates {kind::cmd_pattern -> {folds, unfolds}}. When a (kind,
# cmd_pattern) pair's unfold_rate exceeds ADAPTIVE_THRESHOLD over
# >= ADAPTIVE_MIN_SAMPLES folds, fold_run()'s keep_head/keep_tail widen for
# future folds of that pair — the layer backing off because its compression
# was too aggressive for that specific command. Inversely, a near-zero
# unfold_rate allows a tighter digest. Clamped at both ends
# [ADAPTIVE_KEEP_MIN, ADAPTIVE_KEEP_MAX]. Fully local, observation-only, no
# cross-machine sharing (plan §5.2) — this file must never go in vectors.db
# or any synced JSON.

ADAPTIVE_THRESHOLD = 0.5
ADAPTIVE_MIN_SAMPLES = 5
ADAPTIVE_KEEP_MIN = 5
ADAPTIVE_KEEP_MAX = 60
_ADAPTIVE_WIDEN_FACTOR = 1.5
_ADAPTIVE_TIGHTEN_FACTOR = 0.7
_ADAPTIVE_TIGHTEN_UNFOLD_RATE = 0.1

_KNOWN_CMD_PATTERNS = (
    "pytest", "py.test", "npm test", "npm run build", "cargo test", "cargo build",
    "go test", "go build", "docker logs", "tsc", "webpack", "make",
)


def _adaptive_stats_path(root: Optional[Path] = None) -> Path:
    return (root or get_root()) / ".knewrall" / "engram_stats.json"


def normalize_cmd_pattern(cmd: Optional[str]) -> str:
    """Reduces a shell command to a stable bucket key — matches against the
    same unambiguous-prefix vocabulary Phase 5's enforcer uses, falling back
    to the invoked program's basename, or "none" for fold()'s no-command
    (stdin/file) case."""
    if not cmd:
        return "none"
    low = cmd.strip().lower()
    for pattern in _KNOWN_CMD_PATTERNS:
        if low.startswith(pattern):
            return pattern
    tokens = cmd.strip().split()
    return Path(tokens[0]).name if tokens else "unknown"


def _adaptive_key(kind: str, cmd_pattern: str) -> str:
    return f"{kind}::{cmd_pattern}"


def _adaptive_bump(root: Optional[Path], kind: str, cmd: Optional[str], field: str) -> None:
    path = _adaptive_stats_path(root)
    data = _read_json(path) or {}
    key = _adaptive_key(kind, normalize_cmd_pattern(cmd))
    entry = data.setdefault(key, {"folds": 0, "unfolds": 0})
    entry[field] = entry.get(field, 0) + 1
    try:
        _atomic_write_json(path, data)
    except OSError:
        pass


def record_adaptive_fold(root: Optional[Path], kind: str, cmd: Optional[str]) -> None:
    _adaptive_bump(root, kind, cmd, "folds")


def record_adaptive_unfold(root: Optional[Path], kind: str, cmd: Optional[str]) -> None:
    _adaptive_bump(root, kind, cmd, "unfolds")


def adaptive_keep_lines(
    root: Optional[Path], kind: str, cmd: Optional[str], default_head: int, default_tail: int
) -> Tuple[int, int]:
    """Returns (head, tail) — widened/tightened from the defaults if this
    (kind, cmd_pattern) pair has enough samples to justify it, else the
    defaults unchanged."""
    data = _read_json(_adaptive_stats_path(root)) or {}
    entry = data.get(_adaptive_key(kind, normalize_cmd_pattern(cmd)))
    if not entry:
        return default_head, default_tail
    total = entry.get("folds", 0)
    if total < ADAPTIVE_MIN_SAMPLES:
        return default_head, default_tail
    unfold_rate = entry.get("unfolds", 0) / total
    if unfold_rate > ADAPTIVE_THRESHOLD:
        factor = _ADAPTIVE_WIDEN_FACTOR
    elif unfold_rate < _ADAPTIVE_TIGHTEN_UNFOLD_RATE:
        factor = _ADAPTIVE_TIGHTEN_FACTOR
    else:
        return default_head, default_tail
    head = max(ADAPTIVE_KEEP_MIN, min(ADAPTIVE_KEEP_MAX, round(default_head * factor)))
    tail = max(ADAPTIVE_KEEP_MIN, min(ADAPTIVE_KEEP_MAX, round(default_tail * factor)))
    return head, tail
