from __future__ import annotations

import re
from pathlib import Path
from typing import Any

INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+[`\"]?(?P<table>[a-zA-Z0-9_$]+)[`\"]?\s*(?:\((?P<columns>[^\)]+)\))?\s*VALUES\s*",
    re.IGNORECASE,
)


def parse_mysql_dump(path: Path) -> dict[str, list[dict[str, Any]]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return _parse_insert_dump(text)


def _extract_values_block(text: str, start: int) -> tuple[str, int]:
    chars: list[str] = []
    in_string = False
    escaped = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if escaped:
            chars.append(ch)
            escaped = False
        elif ch == "\\":
            chars.append(ch)
            escaped = True
        elif ch == "'":
            chars.append(ch)
            in_string = not in_string
        elif ch == ";" and not in_string:
            return "".join(chars), i + 1
        else:
            chars.append(ch)
        i += 1
    return "".join(chars), n


def _parse_insert_dump(text: str) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    pos = 0
    while True:
        match = INSERT_RE.search(text, pos)
        if not match:
            break
        table = match.group("table")
        columns_raw = match.group("columns")
        values_block, pos = _extract_values_block(text, match.end())
        columns = [c.strip().strip("`\"") for c in columns_raw.split(",")] if columns_raw else None
        for raw_tuple in _split_tuples(values_block):
            values = _split_values(raw_tuple)
            if columns is not None:
                row = {col: _normalize_value(values[idx]) if idx < len(values) else None for idx, col in enumerate(columns)}
            else:
                row = {f"col_{idx}": _normalize_value(v) for idx, v in enumerate(values)}
            tables.setdefault(table, []).append(row)
    return tables


def _split_tuples(values_block: str) -> list[str]:
    tuples: list[str] = []
    current = []
    depth = 0
    in_string = False
    escaped = False
    for ch in values_block.strip():
        current.append(ch)
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "'":
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                tuples.append("".join(current).strip().lstrip(","))
                current = []
    return [t for t in tuples if t]


def _split_values(raw_tuple: str) -> list[str]:
    inner = raw_tuple.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    values: list[str] = []
    current = []
    in_string = False
    escaped = False
    for ch in inner:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\":
            current.append(ch)
            escaped = True
            continue
        if ch == "'":
            current.append(ch)
            in_string = not in_string
            continue
        if ch == "," and not in_string:
            values.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        values.append("".join(current).strip())
    return values


def _normalize_value(token: str):
    stripped = token.strip()
    if stripped.upper() == "NULL":
        return None
    if stripped.startswith("'") and stripped.endswith("'"):
        return stripped[1:-1].replace("\\'", "'").replace("''", "'")
    if re.fullmatch(r"[+-]?\d+", stripped):
        return int(stripped)
    if re.fullmatch(r"[+-]?\d+\.\d+", stripped):
        return float(stripped)
    return stripped
