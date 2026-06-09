from __future__ import annotations

from pathlib import Path
from typing import Any

from .csv_parser import load_csv_bundle
from .db_connect_parser import parse_db_connection
from .json_parser import load_json_bundle
from .mysql_parser import parse_mysql_dump
from .postgresql_parser import parse_postgresql_dump


def parse_source_files(
    files: list[Path],
    source_type: str,
    *,
    connection_string: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    normalized = source_type.lower()
    if normalized == "csv":
        csv_files = [path for path in files if path.suffix.lower() == ".csv"]
        if not csv_files:
            raise ValueError("No CSV files found for source_type=csv")
        return load_csv_bundle(csv_files)
    if normalized == "json":
        json_files = [path for path in files if path.suffix.lower() in {".json", ".jsonl"}]
        if not json_files:
            raise ValueError("No JSON/JSONL files found for source_type=json")
        return load_json_bundle(json_files)
    if normalized == "sql_dump":
        if not files:
            raise ValueError("No SQL dump files provided")
        tables: dict[str, list[dict[str, Any]]] = {}
        for path in files:
            lower_name = path.name.lower()
            parsed = (
                parse_postgresql_dump(path)
                if "postgres" in lower_name or path.suffix.lower() == ".psql"
                else parse_mysql_dump(path)
            )
            for table, rows in parsed.items():
                tables.setdefault(table, []).extend(rows)
        return tables
    if normalized == "db_connect":
        if not connection_string:
            raise ValueError("connection_string is required for source_type=db_connect")
        return parse_db_connection(connection_string)
    raise ValueError(f"Unsupported source_type: {source_type}")
