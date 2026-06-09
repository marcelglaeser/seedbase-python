from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json_bundle(files: list[Path]) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(files):
        suffix = path.suffix.lower()
        if suffix == ".jsonl":
            tables[path.stem] = _load_jsonl(path)
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # {table: [...]} structure
            if all(isinstance(v, list) for v in data.values()):
                for table_name, rows in data.items():
                    tables[table_name] = _normalize_rows(rows)
            else:
                tables[path.stem] = [_normalize_row(data)]
        elif isinstance(data, list):
            tables[path.stem] = _normalize_rows(data)
        else:
            tables[path.stem] = [{"value": data}]
    return tables


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        rows.append(_normalize_row(json.loads(stripped)))
    return rows


def _normalize_rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [_normalize_row(row) for row in rows]


def _normalize_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    return {"value": row}
