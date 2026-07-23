"""
Token-Oriented Object Notation (TOON) — output-only encoder.

Encodes a Python dict/list/scalar tree into an indentation-based, brace-free
text format that costs fewer tokens than JSON for typical `recall` payloads —
especially uniform arrays of objects (e.g. `related` neuron summaries, flat
`properties`), which collapse to one header row + one row per item instead of
repeating every key name in every element.

Deterministic: sorted object keys, stable formatting, byte-identical output for
identical input. There is no decoder — this format is never read back by the
engine (neuron files stay JSON, the source of truth); adding a decoder would
double the surface for a need that doesn't exist.

Escaping rule (the actual correctness hazard for a hand-rolled, indentation-based
encoder): any scalar string containing a newline, or one that *starts* with a
character that would be ambiguous against the format's own syntax (a leading
`-`/`#`/quote, or a colon-space that looks like `key: value`), is JSON-encoded
inline (`json.dumps`) rather than emitted raw. Plain single-line strings without
those hazards are emitted unescaped for readability and token efficiency.
"""

from __future__ import annotations

import json
from typing import Any


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _needs_escape(s: str, in_table: bool = False) -> bool:
    if "\n" in s or "\r" in s:
        return True
    if s == "":
        return False
    if s[0] in ("-", "#", '"', "'"):
        return True
    if ": " in s or s.endswith(":"):
        return True
    # The tabular block's own row values are comma-delimited (see
    # _tabular_block) — a bare comma there (a realistic canonical_name like
    # "Roca, SA de CV", or any free-form property value) silently produces a
    # row with more fields than the {...} header declared, misaligning every
    # column after it. Outside a table a comma is unambiguous in `key: value`
    # form, so this rule only applies there.
    if in_table and "," in s:
        return True
    return False


def _scalar(value: Any, in_table: bool = False) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if _needs_escape(s, in_table=in_table):
        return json.dumps(s, ensure_ascii=False)
    return s


def _is_uniform_object_array(items: list) -> bool:
    """True if every item is a dict and they share (a subset of) the same flat scalar keys."""
    if not items or not all(isinstance(i, dict) for i in items):
        return False
    for item in items:
        for v in item.values():
            if not _is_scalar(v):
                return False
    return True


def _tabular_block(items: list, indent: str) -> list[str]:
    keys: list[str] = []
    for item in items:
        for k in item.keys():
            if k not in keys:
                keys.append(k)
    keys = sorted(keys)
    lines = [f"{indent}[{len(items)}]{{{','.join(keys)}}}:"]
    row_indent = indent + "  "
    for item in items:
        row = ",".join(_scalar(item.get(k), in_table=True) for k in keys)
        lines.append(f"{row_indent}{row}")
    return lines


def _encode_value(key: str | None, value: Any, indent: str, lines: list[str]) -> None:
    prefix = f"{indent}{key}: " if key is not None else indent

    if _is_scalar(value):
        lines.append(f"{prefix}{_scalar(value)}")
        return

    if isinstance(value, dict):
        if not value:
            lines.append(f"{prefix}{{}}")
            return
        if key is not None:
            lines.append(f"{indent}{key}:")
            child_indent = indent + "  "
        else:
            child_indent = indent
        for k in sorted(value.keys()):
            _encode_value(k, value[k], child_indent, lines)
        return

    if isinstance(value, list):
        if not value:
            lines.append(f"{prefix}[]")
            return
        if _is_uniform_object_array(value):
            if key is not None:
                lines.append(f"{indent}{key}:")
                lines.extend(_tabular_block(value, indent + "  "))
            else:
                lines.extend(_tabular_block(value, indent))
            return
        # Non-uniform list: indented "- " items.
        if key is not None:
            lines.append(f"{indent}{key}:")
        item_indent = indent + "  "
        for item in value:
            if _is_scalar(item):
                lines.append(f"{item_indent}- {_scalar(item)}")
            else:
                lines.append(f"{item_indent}-")
                _encode_value(None, item, item_indent + "  ", lines)
        return

    # Fallback for unexpected types: stringify.
    lines.append(f"{prefix}{_scalar(str(value))}")


def encode(obj: Any) -> str:
    """Deterministically encode obj (dict/list/scalar) as TOON text."""
    lines: list[str] = []
    _encode_value(None, obj, "", lines)
    return "\n".join(lines) + ("\n" if lines else "")
