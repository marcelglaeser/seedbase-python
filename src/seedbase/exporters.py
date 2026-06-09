from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path
from typing import Any


def export_dataset(
    dataset: dict[str, list[dict[str, Any]]],
    output_dir: str | Path,
    export_format: str,
) -> list[Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)

    normalized = export_format.lower()
    if normalized == "json":
        return [_export_json(dataset, root)]
    if normalized == "jsonl":
        return _export_jsonl(dataset, root)
    if normalized == "csv":
        return _export_csv(dataset, root)
    if normalized == "postgresql":
        return [_export_sql(dataset, root / "dataset.postgresql.sql", dialect="postgresql")]
    if normalized == "mysql":
        return [_export_sql(dataset, root / "dataset.mysql.sql", dialect="mysql")]
    if normalized == "mariadb":
        return [_export_sql(dataset, root / "dataset.mariadb.sql", dialect="mariadb")]
    if normalized == "sqlite":
        return [_export_sql(dataset, root / "dataset.sqlite.sql", dialect="sqlite")]
    if normalized in {"mssql", "sqlserver", "ms_sql"}:
        return [_export_sql(dataset, root / "dataset.mssql.sql", dialect="mssql")]
    raise ValueError(f"Unsupported export format: {export_format}")


def _export_json(dataset: dict[str, list[dict[str, Any]]], output_dir: Path) -> Path:
    target = output_dir / "dataset.json"
    with target.open("w", encoding="utf-8") as handle:
        json.dump(dataset, handle, indent=2, ensure_ascii=False)
    return target


def _export_csv(dataset: dict[str, list[dict[str, Any]]], output_dir: Path) -> list[Path]:
    exported: list[Path] = []
    for table_name in sorted(dataset.keys()):
        rows = dataset[table_name]
        columns = _all_columns(rows)
        target = output_dir / f"{table_name}.csv"
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({col: _to_csv_value(row.get(col)) for col in columns})
        exported.append(target)

    if len(exported) > 1:
        archive = output_dir / "dataset.csv.zip"
        with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in exported:
                zf.write(path, arcname=path.name)
        return [archive]
    return exported


def _export_jsonl(dataset: dict[str, list[dict[str, Any]]], output_dir: Path) -> list[Path]:
    exported: list[Path] = []
    for table_name in sorted(dataset.keys()):
        target = output_dir / f"{table_name}.jsonl"
        rows = dataset[table_name]
        with target.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False))
                handle.write("\n")
        exported.append(target)
    return exported


def _all_columns(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for column in row.keys():
            if column not in seen:
                seen.add(column)
                ordered.append(column)
    return ordered


def _to_csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _export_sql(dataset: dict[str, list[dict[str, Any]]], target: Path, dialect: str) -> Path:
    if dialect == "postgresql":
        id_open, id_close = ('"', '"')
    elif dialect in {"mysql", "mariadb"}:
        id_open, id_close = ("`", "`")
    elif dialect == "sqlite":
        id_open, id_close = ('"', '"')
    elif dialect == "mssql":
        id_open, id_close = ("[", "]")
    else:
        raise ValueError(f"Unsupported SQL dialect: {dialect}")

    def quote_ident(name: Any) -> str:
        return id_open + str(name).replace(id_close, id_close + id_close) + id_close

    lines: list[str] = []
    for table_name in sorted(dataset.keys()):
        rows = dataset[table_name]
        if not rows:
            continue
        columns = _all_columns(rows)
        quoted_table = quote_ident(table_name)
        if dialect == "mssql" and "id" in columns:
            lines.append(f"SET IDENTITY_INSERT {quoted_table} ON;")
        col_sql = ", ".join(quote_ident(col) for col in columns)
        values_sql = ",\n".join(
            "  (" + ", ".join(_sql_literal(row.get(col), dialect) for col in columns) + ")"
            for row in rows
        )
        if dialect == "sqlite":
            lines.append(
                f"INSERT OR REPLACE INTO {quoted_table} ({col_sql}) VALUES"
            )
        else:
            lines.append(f"INSERT INTO {quoted_table} ({col_sql}) VALUES")
        lines.append(values_sql + ";")
        if dialect == "mssql" and "id" in columns:
            lines.append(f"SET IDENTITY_INSERT {quoted_table} OFF;")
        lines.append("")

    with target.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")
    return target


def _sql_literal(value: Any, dialect: str) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        if dialect == "postgresql":
            return "TRUE" if value else "FALSE"
        if dialect == "sqlite":
            return "1" if value else "0"
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)

    text = str(value).replace("'", "''")
    return f"'{text}'"
