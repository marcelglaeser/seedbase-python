from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import urlparse


def parse_db_connection(connection_string: str, sample_limit: int = 1000) -> dict[str, list[dict[str, Any]]]:
    parsed = urlparse(connection_string)
    scheme = parsed.scheme.lower()
    if scheme in {"sqlite", "sqlite3"}:
        return _parse_sqlite_connection(connection_string, sample_limit)
    if scheme in {"postgres", "postgresql", "postgresql+psycopg"}:
        return _parse_postgresql_connection(connection_string, sample_limit)
    raise ValueError(f"Unsupported DB connection scheme: {scheme}")


def _parse_sqlite_connection(connection_string: str, sample_limit: int) -> dict[str, list[dict[str, Any]]]:
    parsed = urlparse(connection_string)
    db_path = parsed.path
    if not db_path:
        raise ValueError("SQLite connection string must contain a database path")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = [row[0] for row in cursor.fetchall()]
        result: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            rows = conn.execute(f'SELECT * FROM "{table}" LIMIT ?', (sample_limit,)).fetchall()
            result[table] = [dict(row) for row in rows]
        return result
    finally:
        conn.close()


def _parse_postgresql_connection(connection_string: str, sample_limit: int) -> dict[str, list[dict[str, Any]]]:
    try:
        import psycopg
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("psycopg is required for PostgreSQL DB connect") from exc

    query_tables = """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema='public' AND table_type='BASE TABLE'
        ORDER BY table_name
    """

    result: dict[str, list[dict[str, Any]]] = {}
    with psycopg.connect(connection_string) as conn:
        with conn.cursor() as cur:
            cur.execute(query_tables)
            tables = [row[0] for row in cur.fetchall()]

        for table in tables:
            with conn.cursor() as cur:
                cur.execute(f'SELECT * FROM "{table}" LIMIT %s', (sample_limit,))
                columns = [desc.name for desc in cur.description]
                rows = cur.fetchall()
                result[table] = [dict(zip(columns, row)) for row in rows]
    return result
