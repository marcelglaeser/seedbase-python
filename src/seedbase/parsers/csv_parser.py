from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def load_csv_table(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append({k: _clean(v) for k, v in row.items() if k is not None})
    return rows


def load_csv_bundle(files: list[Path]) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(files):
        tables[path.stem] = load_csv_table(path)
    return tables


def _clean(value: str | None):
    if value is None:
        return None
    stripped = value.strip()
    if stripped == "" or stripped.upper() == "NULL":
        return None
    return stripped
