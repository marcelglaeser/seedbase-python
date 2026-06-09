from __future__ import annotations

from pathlib import Path

from .mysql_parser import _parse_insert_dump


def parse_postgresql_dump(path: Path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    return _parse_insert_dump(text)
