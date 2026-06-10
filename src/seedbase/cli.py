from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ._ssl import CERTIFI_HINT, create_ssl_context, is_cert_verify_error

API_URL = "https://seedba.se/api/v1"
CONFIG_PATH = Path.home() / ".seedbase" / "config.json"
PROJECT_CONFIG_PATH = Path.cwd() / ".seedbase.json"
CACHE_ROOT = Path.home() / ".seedbase" / "cache"
REQUEST_TIMEOUT = 30.0


class CLIError(RuntimeError):
    pass


@dataclass
class Config:
    token: str | None
    api_url: str
    project: str | None
    target: str | None


_QUIET = False


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("seedbase")
    except Exception:
        return "0.0.0"


def _info(message: str = "", **kwargs: Any) -> None:
    if not _QUIET:
        print(message, **kwargs)


def main(argv: list[str] | None = None) -> None:
    global _QUIET
    parser = _build_parser()
    args = parser.parse_args(argv)
    _QUIET = bool(getattr(args, "quiet", False))
    if not hasattr(args, "func"):
        parser.print_help()
        raise SystemExit(0)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        raise SystemExit(130) from None
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seedbase",
        description="Seedbase CLI — pull realistic test data from the platform straight into your database.",
    )
    parser.add_argument("--version", action="version", version=f"seedbase {_version()}")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress progress messages")
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser("login", help="Authenticate via browser (one-time setup)")
    login.add_argument("--no-open", action="store_true", help="Print URL instead of opening browser")
    login.set_defaults(func=_cmd_login)

    logout = sub.add_parser("logout", help="Remove saved token")
    logout.set_defaults(func=_cmd_logout)

    status = sub.add_parser("status", help="Show login, project and target configuration")
    status.add_argument("--json", action="store_true", help="Output as JSON")
    status.set_defaults(func=_cmd_status)

    projects = sub.add_parser("projects", help="List your projects")
    projects.add_argument("--json", action="store_true", help="Output as JSON")
    projects.set_defaults(func=_cmd_projects)

    connections = sub.add_parser("connections", help="List your database connections")
    connections.add_argument("--json", action="store_true", help="Output as JSON")
    connections.set_defaults(func=_cmd_connections)

    init = sub.add_parser("init", help="Set up project config (.seedbase.json)")
    init.add_argument("--project", default=None, help="Project ID (non-interactive)")
    init.add_argument("--target", default=None, help="Database URI (non-interactive)")
    init.add_argument("--yes", "-y", action="store_true", help="Don't prompt; use the given flags")
    init.set_defaults(func=_cmd_init)

    gens = sub.add_parser("generations", help="List the generated datasets of a project")
    gens.add_argument("--project", default=None, help="Project ID (defaults to .seedbase.json)")
    gens.add_argument("--json", action="store_true", help="Output as JSON")
    gens.set_defaults(func=_cmd_generations)

    pull = sub.add_parser("pull", help="Pull schema/data from the platform into your database")
    pull_sub = pull.add_subparsers(dest="what")
    pull_sub.required = True

    ps = pull_sub.add_parser("schema", help="Create the project's tables in your target database")
    ps.add_argument("--project", default=None, help="Project ID (defaults to .seedbase.json)")
    ps.add_argument("--target", default=None, help="Database URI (defaults to .seedbase.json)")
    ps.add_argument("--drop", action="store_true", help="Drop existing tables first")
    ps.add_argument("--dry-run", action="store_true", help="Show what would happen without writing")
    ps.add_argument("--to-file", default=None, help="Write DDL to a file instead of the database")
    ps.add_argument("--force", action="store_true", help="Overwrite an existing --to-file output file")
    ps.add_argument("--wait-for-db", type=int, default=0, help="Wait up to N seconds for the database to become ready")
    ps.set_defaults(func=_cmd_pull_schema)

    pd = pull_sub.add_parser("data", help="Write a generated dataset into your target database")
    pd.add_argument("--project", default=None, help="Project ID (defaults to .seedbase.json)")
    pd.add_argument("--target", default=None, help="Database URI (defaults to .seedbase.json)")
    pd.add_argument("--generation", "-g", default=None, help="Generation ID or name (default: newest)")
    pd.add_argument("--replace", action="store_true", help="Empty the tables before writing (avoids duplicates)")
    pd.add_argument("--dry-run", action="store_true", help="Show what would happen without writing")
    pd.add_argument("--to-file", default=None, help="Write the SQL to a file instead of the database")
    pd.add_argument("--force", action="store_true", help="Overwrite an existing --to-file output file")
    pd.add_argument("--wait-for-db", type=int, default=0, help="Wait up to N seconds for the database to become ready")
    pd.set_defaults(func=_cmd_pull_data)

    psub = pull_sub.add_parser("subset", help="Carve a FK-consistent subset from a connected database")
    psub.add_argument("--from-database", dest="from_database", required=True, help="Database connection ID")
    psub.add_argument("--root-table", dest="root_table", required=True, help="Table to draw root rows from")
    psub.add_argument("--root-count", dest="root_count", type=int, default=None, help="Number of root rows")
    psub.add_argument("--root-fraction", dest="root_fraction", type=float, default=None, help="Fraction of root rows (0-1)")
    psub.add_argument("--include-children", dest="include_children", action="store_true", help="Also pull dependent child rows")
    psub.add_argument("--format", default="postgresql", choices=["postgresql", "mysql", "mariadb", "sqlite"])
    psub.add_argument("--max-rows", dest="max_rows", type=int, default=None, help="Safety cap on total rows")
    psub.add_argument("--to-file", default=None, help="Write the SQL to a file instead of the database")
    psub.add_argument("--force", action="store_true", help="Overwrite an existing --to-file output file")
    psub.add_argument("--target", default=None, help="Database URI (defaults to .seedbase.json)")
    psub.add_argument("--wait-for-db", type=int, default=0, help="Wait up to N seconds for the database to become ready")
    psub.set_defaults(func=_cmd_pull_subset)

    pa = pull_sub.add_parser("all", help="Pull schema + a dataset in one step")
    pa.add_argument("--project", default=None, help="Project ID (defaults to .seedbase.json)")
    pa.add_argument("--target", default=None, help="Database URI (defaults to .seedbase.json)")
    pa.add_argument("--drop", action="store_true", help="Drop existing tables first")
    pa.add_argument("--dry-run", action="store_true", help="Show what would happen without writing")
    pa.add_argument("--generation", "-g", default=None, help="Generation ID or name (default: newest)")
    pa.add_argument("--wait-for-db", type=int, default=0, help="Wait up to N seconds for the database to become ready")
    pa.set_defaults(func=_cmd_pull_all)

    generate = sub.add_parser("generate", help="Trigger a remote generation (useful for CI/CD)")
    generate.add_argument("--project", default=None, help="Project ID (defaults to .seedbase.json)")
    generate.add_argument("--format", default=None, choices=["postgresql", "mysql", "mariadb", "sqlite", "csv", "json"], help="Export format for the generation")
    generate.add_argument("--seed", type=int, default=None, help="Deterministic generation seed")
    generate.add_argument("--rows", type=int, default=None, help="Row count per table")
    generate.add_argument("--wait", type=int, default=300, help="Seconds to wait for completion (default: 300)")
    generate.set_defaults(func=_cmd_generate)

    mask = sub.add_parser("mask", help="Mask PII columns in-place in a connected database")
    mask.add_argument("connection", help="Database connection ID (see 'seedbase connections')")
    mask.add_argument("--table", required=True, help="Table containing the columns to mask")
    mask.add_argument("--columns", required=True, help="Comma-separated column names (e.g. email,name)")
    mask.add_argument("--dry-run", action="store_true", help="Preview what would be masked without writing")
    mask.set_defaults(func=_cmd_mask)

    db_push = sub.add_parser("db-push", help="Push a generated dataset into a connected database")
    db_push.add_argument("connection", help="Database connection ID (see 'seedbase connections')")
    db_push.add_argument("--generation", "-g", default=None, help="Generation ID (default: newest completed)")
    db_push.set_defaults(func=_cmd_db_push)

    test = sub.add_parser("test", help="Test target database connectivity")
    test.set_defaults(func=_cmd_test)

    diff = sub.add_parser("diff", help="Show schema changes since last pull")
    diff.add_argument("--project", default=None, help="Project ID (defaults to .seedbase.json)")
    diff.set_defaults(func=_cmd_diff)

    export = sub.add_parser("export", help="Export project configuration as a versionable file")
    export_sub = export.add_subparsers(dest="what")
    export_sub.required = True
    ec = export_sub.add_parser("config", help="Export the engine_config (config-as-code)")
    ec.add_argument("--project", default=None, help="Project ID (defaults to .seedbase.json)")
    ec.add_argument("--to-file", default=None, help="Write the engine_config to a file instead of stdout")
    ec.add_argument("--force", action="store_true", help="Overwrite an existing --to-file output file")
    ec.set_defaults(func=_cmd_export_config)

    imp = sub.add_parser("import", help="Import project configuration from a file")
    import_sub = imp.add_subparsers(dest="what")
    import_sub.required = True
    ic = import_sub.add_parser("config", help="Import the engine_config (config-as-code)")
    ic.add_argument("--project", default=None, help="Project ID (defaults to .seedbase.json)")
    ic.add_argument("--from-file", required=True, help="Read the engine_config from this file")
    ic.set_defaults(func=_cmd_import_config)

    return parser


# ── Commands ──────────────────────────────────────────────────────────

def _cmd_login(args: argparse.Namespace) -> None:
    cfg = _load_config()
    data = _api("POST", cfg.api_url, "/cli/auth/initiate/", token=None, payload={})
    code = data.get("code")
    browser_url = data.get("browser_url")
    poll_url = data.get("poll_url")
    if not code or not poll_url:
        raise CLIError("Unexpected login response from server")

    _info("Seedbase CLI\n")
    _info(f"Open this URL to authenticate:\n{browser_url}\n")

    if browser_url and not args.no_open:
        try:
            import webbrowser
            webbrowser.open(browser_url)
        except Exception:
            pass

    show_progress = sys.stdout.isatty()
    _info("Waiting for authorization", end="" if show_progress else "\n", flush=True)
    poll_path = str(poll_url)
    if poll_path.startswith(cfg.api_url):
        poll_path = poll_path[len(cfg.api_url):]
    if poll_path.startswith("/api/v1/"):
        poll_path = poll_path[len("/api/v1"):]

    for _ in range(150):
        time.sleep(2)
        poll = _api("GET", cfg.api_url, poll_path, token=None, allow_non_2xx={404, 410})
        status = str(poll.get("status") or "")
        if status == "authorized":
            token = poll.get("token")
            if not token:
                raise CLIError("Authorization finished but token missing")
            cfg.token = token
            path = _save_config(cfg)
            _info(" ... authorized" if show_progress else "Authorized.")
            _info(f"Token saved to {path}")
            return
        if status == "expired":
            if show_progress:
                _info()
            raise CLIError("Authorization code expired")
        if show_progress:
            _info(".", end="", flush=True)

    if show_progress:
        _info()
    raise CLIError("Authorization timed out")


def _cmd_logout(_args: argparse.Namespace) -> None:
    if not CONFIG_PATH.exists():
        print("No saved login found.")
        return
    cfg = _load_config()
    cfg.token = None
    _save_config(cfg)
    print(f"Token removed from {CONFIG_PATH}")


def _cmd_status(args: argparse.Namespace) -> None:
    cfg = _load_config()
    info: dict[str, Any] = {
        "api_url": cfg.api_url,
        "logged_in": bool(cfg.token),
        "email": None,
        "plan": None,
        "subscription_status": None,
        "project": cfg.project,
        "project_name": None,
        "target": cfg.target,
    }
    if cfg.token:
        try:
            me = _api("GET", cfg.api_url, "/auth/me/", token=cfg.token)
            info["email"] = me.get("email")
            info["plan"] = me.get("plan")
            info["subscription_status"] = me.get("subscription_status")
        except CLIError:
            info["logged_in"] = False
    if cfg.token and cfg.project:
        try:
            ds = _api("GET", cfg.api_url, f"/datasets/{cfg.project}/", token=cfg.token)
            info["project_name"] = ds.get("name")
        except CLIError:
            pass

    if args.json:
        print(json.dumps(info, indent=2))
        return

    print(f"API:      {info['api_url']}")
    if info["logged_in"] and info["email"]:
        plan = info["plan"] or "?"
        sub = info["subscription_status"] or "?"
        print(f"Login:    {info['email']}  (plan: {plan}, {sub})")
    elif cfg.token:
        print("Login:    token present, but verification failed")
    else:
        print("Login:    not logged in  (run 'seedbase login')")
    if info["project"]:
        suffix = f"  ({info['project_name']})" if info["project_name"] else ""
        print(f"Project:  {info['project']}{suffix}")
    else:
        print("Project:  none  (run 'seedbase init')")
    print(f"Target:   {info['target'] or 'none'}")


def _cmd_projects(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    rows = _api_list(cfg.api_url, "/datasets/", cfg.token)

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        print("No projects found. Create one at https://seedba.se")
        return

    print(f"{'ID':<36}  {'Name':<28}  Source")
    print("-" * 84)
    for row in rows:
        pid = str(row.get("id", ""))[:36]
        name = str(row.get("name", ""))[:28]
        source = str(row.get("source_type", ""))
        print(f"{pid:<36}  {name:<28}  {source}")
    print(f"\n{len(rows)} project{'s' if len(rows) != 1 else ''}")


def _cmd_connections(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    rows = _api_list(cfg.api_url, "/db-connections/", cfg.token)

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        print("No database connections found. Create one at https://seedba.se")
        return

    print(f"{'ID':<36}  {'Name':<20}  {'Type':<10}  {'Host':<24}  Database")
    print("-" * 110)
    for row in rows:
        cid = str(row.get("id", ""))[:36]
        name = str(row.get("name", ""))[:20]
        db_type = str(row.get("db_type", ""))[:10]
        host = str(row.get("host", ""))[:24]
        database = str(row.get("database", ""))
        print(f"{cid:<36}  {name:<20}  {db_type:<10}  {host:<24}  {database}")
    print(f"\n{len(rows)} connection{'s' if len(rows) != 1 else ''}")


def _prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        raise CLIError(
            "stdin is closed — cannot prompt. Run non-interactively: "
            "seedbase init --project <id> [--target <uri>] --yes"
        ) from None


def _warn_if_target_has_credentials(target: str | None) -> None:
    if not target:
        return
    try:
        has_password = bool(urllib.parse.urlparse(target).password)
    except ValueError:
        has_password = False
    if has_password:
        print(f"Note: {PROJECT_CONFIG_PATH.name} contains database credentials — add it to your .gitignore.")


def _cmd_init(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())

    if args.project or args.yes:
        if not args.project:
            raise CLIError("--yes requires --project (and usually --target).")
        project_cfg = {"project": args.project, "target": args.target or None}
        PROJECT_CONFIG_PATH.write_text(json.dumps(project_cfg, indent=2) + "\n", encoding="utf-8")
        print(f"Created {PROJECT_CONFIG_PATH}")
        _warn_if_target_has_credentials(args.target)
        return

    rows = _api_list(cfg.api_url, "/datasets/", cfg.token)

    if not rows:
        raise CLIError("No projects found. Create one at https://seedba.se first.")

    print("Your projects:\n")
    for i, row in enumerate(rows, 1):
        print(f"  [{i}] {row.get('name', 'Unnamed')} ({row.get('id', '')})")

    print()
    choice = _prompt(f"Which project? [1-{len(rows)}]: ")
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(rows):
            raise ValueError
    except ValueError:
        raise CLIError("Invalid choice.")
    project_id = rows[idx].get("id")

    print()
    target = _prompt("Local database URI (e.g. postgresql://user:pass@localhost:5432/mydb): ")
    if not target:
        target = None

    project_cfg = {
        "project": project_id,
        "target": target,
    }
    PROJECT_CONFIG_PATH.write_text(json.dumps(project_cfg, indent=2) + "\n", encoding="utf-8")
    print(f"\nCreated {PROJECT_CONFIG_PATH}")
    _warn_if_target_has_credentials(target)
    if target:
        print("Run 'seedbase pull all' to create the tables and load a dataset into your DB.")
    else:
        print("Run 'seedbase pull data --to-file out.sql' to download a dataset.")


def _cmd_generations(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    project_id = _resolve_project(cfg, args.project)
    rows = _list_generations(cfg, project_id)

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        print("No generated datasets yet. Generate one at https://seedba.se")
        return

    print(f"{'ID':<36}  {'Name':<20}  {'Status':<10}  {'Rows':>8}  {'Format':<11}  Created")
    print("-" * 110)
    for g in rows:
        gid = str(g.get("id", ""))[:36]
        name = str(g.get("name") or "")[:20]
        st = str(g.get("status", ""))[:10]
        total = g.get("total_rows")
        total_s = f"{int(total):,}" if isinstance(total, int) else "-"
        fmt = str(g.get("export_format", ""))[:11]
        created = _short_date(g.get("created_at"))
        pin = "*" if g.get("is_pinned") else " "
        print(f"{gid:<36}  {pin}{name:<19}  {st:<10}  {total_s:>8}  {fmt:<11}  {created}")
    print(f"\n{len(rows)} dataset{'s' if len(rows) != 1 else ''}  (* = pinned)")


def _cmd_pull_schema(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    project_id = _resolve_project(cfg, args.project)
    dialect = _dialect_for(cfg, args)

    if args.dry_run:
        tables = _fetch_schema_tables(cfg, project_id)
        print(f"Dry run — target: {args.target or cfg.target or 'none'}")
        if args.drop:
            print(f"  would DROP {len(tables)} table(s): {', '.join(tables)}")
        print(f"  would CREATE {len(tables)} table(s) ({dialect})")
        return

    if args.to_file and not (args.target or cfg.target):
        _info("No target configured — defaulting to dialect 'postgresql' (pass --target to change).")

    sql = _build_schema_sql(cfg, project_id, dialect, drop=args.drop)

    if args.to_file:
        out = _prepare_out_file(args.to_file, args.force)
        out.write_text(sql, encoding="utf-8")
        print(f"Wrote schema to {out}")
        return

    target = _need_target(cfg, args)
    if int(args.wait_for_db or 0) > 0:
        _wait_for_db(target, int(args.wait_for_db))
    _info("Creating tables ...", flush=True)
    _apply_sql_text(target, sql)
    _info("Done.")


def _cmd_pull_data(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    project_id = _resolve_project(cfg, args.project)
    generation = _resolve_generation(cfg, project_id, args.generation)

    if args.to_file:
        out = _prepare_out_file(args.to_file, args.force)
        _download_generation(cfg, generation, out)
        print(f"Wrote {_gen_label(generation)} to {out}")
        return

    target = _need_target(cfg, args)
    dialect = _target_dialect(target)
    _check_generation_dialect(generation, dialect)
    rows = generation.get("total_rows")
    rows_label = f" ({int(rows):,} rows)" if isinstance(rows, int) else ""

    if args.dry_run:
        print(f"Dry run — target: {target}")
        if args.replace:
            tables = _fetch_schema_tables(cfg, project_id)
            print(f"  would EMPTY {len(tables)} table(s): {', '.join(tables)}")
        print(f"  would WRITE {_gen_label(generation)}{rows_label}")
        return

    if int(args.wait_for_db or 0) > 0:
        _wait_for_db(target, int(args.wait_for_db))

    if args.replace:
        tables = _fetch_schema_tables(cfg, project_id)
        _info("Emptying tables ...", flush=True)
        _apply_sql_text(target, _truncate_tables_sql(tables, dialect))

    with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as tmp:
        sql_file = Path(tmp.name)
    try:
        _info(f"Downloading {_gen_label(generation)} ...", flush=True)
        _download_generation(cfg, generation, sql_file, force_sql=True)
        _info("Writing data to database ...", flush=True)
        _apply_sql_to_target(target, sql_file)
    finally:
        sql_file.unlink(missing_ok=True)
    _info(f"Done.{f' {int(rows):,} rows.' if isinstance(rows, int) else ''}")


def _cmd_pull_all(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    project_id = _resolve_project(cfg, args.project)
    target = _need_target(cfg, args)
    dialect = _target_dialect(target)
    generation = _resolve_generation(cfg, project_id, args.generation)
    _check_generation_dialect(generation, dialect)
    rows = generation.get("total_rows")

    if args.dry_run:
        tables = _fetch_schema_tables(cfg, project_id)
        print(f"Dry run — target: {target}")
        if args.drop:
            print(f"  would DROP {len(tables)} table(s)")
        print(f"  would CREATE {len(tables)} table(s) ({dialect})")
        rows_label = f" ({int(rows):,} rows)" if isinstance(rows, int) else ""
        print(f"  would WRITE {_gen_label(generation)}{rows_label}")
        return

    if int(args.wait_for_db or 0) > 0:
        _wait_for_db(target, int(args.wait_for_db))

    schema_sql = _build_schema_sql(cfg, project_id, dialect, drop=args.drop)
    _info("Creating tables ...", flush=True)
    _apply_sql_text(target, schema_sql)

    with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as tmp:
        sql_file = Path(tmp.name)
    try:
        _info(f"Downloading {_gen_label(generation)} ...", flush=True)
        _download_generation(cfg, generation, sql_file, force_sql=True)
        _info("Writing data to database ...", flush=True)
        _apply_sql_to_target(target, sql_file)
    finally:
        sql_file.unlink(missing_ok=True)
    _info(f"Done.{f' {int(rows):,} rows.' if isinstance(rows, int) else ''}")


_JOB_POLL_MAX_ERRORS = 3


def _wait_for_tool_job(cfg, job_id: str, *, timeout_seconds: int = 1800, interval_seconds: int = 3) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    consecutive_errors = 0
    while time.monotonic() < deadline:
        try:
            job = _api("GET", cfg.api_url, f"/tool-jobs/{job_id}/", token=cfg.token)
            consecutive_errors = 0
        except CLIError:
            consecutive_errors += 1
            if consecutive_errors >= _JOB_POLL_MAX_ERRORS:
                raise
            time.sleep(interval_seconds)
            continue
        job_status = str(job.get("status") or "")
        if job_status == "completed":
            return job.get("result") or {}
        if job_status not in {"pending", "queued", "running"}:
            raise CLIError(f"Tool job failed: {job.get('error_message') or job_status or 'unknown error'}")
        time.sleep(interval_seconds)
    raise CLIError("Timed out waiting for the tool job.")


def _cmd_pull_subset(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())

    if (args.root_count is None) == (args.root_fraction is None):
        raise CLIError("Provide exactly one of --root-count or --root-fraction.")

    payload: dict[str, Any] = {
        "root_table": args.root_table,
        "include_children": bool(args.include_children),
        "format": args.format,
    }
    if args.root_count is not None:
        payload["root_count"] = int(args.root_count)
    if args.root_fraction is not None:
        payload["root_fraction"] = float(args.root_fraction)
    if args.max_rows is not None:
        payload["max_rows"] = int(args.max_rows)

    _info("Carving referential subset ...", flush=True)
    result = _api(
        "POST",
        cfg.api_url,
        f"/db-connections/{args.from_database}/subset/",
        token=cfg.token,
        payload=payload,
    )

    job_id = result.get("job_id")
    if job_id:
        result = _wait_for_tool_job(cfg, str(job_id))

    key = result.get("key")
    if not key:
        raise CLIError("Server did not return a subset.")
    rows_total = result.get("rows_total")
    rows_label = f" ({int(rows_total):,} rows)" if isinstance(rows_total, int) else ""

    download_path = f"/db-connections/{args.from_database}/subset-download/?key={urllib.parse.quote(str(key))}"

    if args.to_file:
        out = _prepare_out_file(args.to_file, args.force)
        _download_file(cfg.api_url, download_path, cfg.token, out)
        print(f"Wrote subset{rows_label} to {out}")
        return

    target = _need_target(cfg, args)
    if int(args.wait_for_db or 0) > 0:
        _wait_for_db(target, int(args.wait_for_db))

    with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as tmp:
        sql_file = Path(tmp.name)
    try:
        _info(f"Downloading subset{rows_label} ...", flush=True)
        _download_file(cfg.api_url, download_path, cfg.token, sql_file)
        _info("Writing data to database ...", flush=True)
        _apply_sql_to_target(target, sql_file)
    finally:
        sql_file.unlink(missing_ok=True)
    _info(f"Done.{rows_label}")


def _cmd_generate(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    project_id = _resolve_project(cfg, args.project)
    _trigger_generation(cfg, project_id, seed=args.seed, rows=args.rows, wait=args.wait, fmt=args.format)


def _cmd_mask(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    table = args.table.strip()
    if not table:
        raise CLIError("--table must not be empty.")
    columns = [c.strip() for c in str(args.columns).split(",") if c.strip()]
    if not columns:
        raise CLIError("--columns must list at least one column (e.g. --columns email,name).")
    qualified = [c if "." in c else f"{table}.{c}" for c in columns]

    payload = {"columns": qualified, "dry_run": bool(args.dry_run)}

    if args.dry_run:
        result = _api(
            "POST",
            cfg.api_url,
            f"/db-connections/{args.connection}/mask-in-place/",
            token=cfg.token,
            payload=payload,
        )
        masked_columns = result.get("columns") or []
        row_count = result.get("row_count") or 0
        print(f"Dry run — would mask {len(masked_columns)} column(s), {row_count} row(s):")
        for col in masked_columns:
            print(f"  {col}")
        preview = result.get("preview")
        if isinstance(preview, list) and preview:
            print("Preview:")
            for entry in preview[:5]:
                print(f"  {json.dumps(entry, ensure_ascii=False, default=str)}")
        return

    _info("Masking columns in-place ...", flush=True)
    result = _api(
        "POST",
        cfg.api_url,
        f"/db-connections/{args.connection}/mask-in-place/",
        token=cfg.token,
        payload=payload,
    )
    job_id = result.get("job_id")
    if job_id:
        result = _wait_for_tool_job(cfg, str(job_id))
    masked_columns = result.get("columns") or []
    row_count = result.get("row_count") or 0
    print(f"Masked {len(masked_columns)} column(s), {row_count} row(s).")


def _wait_for_push_job(cfg, job_id: str, *, timeout_seconds: int = 1800, interval_seconds: int = 3) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    consecutive_errors = 0
    while time.monotonic() < deadline:
        try:
            job = _api("GET", cfg.api_url, f"/push-jobs/{job_id}/", token=cfg.token)
            consecutive_errors = 0
        except CLIError:
            consecutive_errors += 1
            if consecutive_errors >= _JOB_POLL_MAX_ERRORS:
                raise
            time.sleep(interval_seconds)
            continue
        job_status = str(job.get("status") or "")
        if job_status == "completed":
            return job
        if job_status not in {"queued", "running"}:
            raise CLIError(f"Push job failed: {job.get('error_message') or job_status or 'unknown error'}")
        time.sleep(interval_seconds)
    raise CLIError("Timed out waiting for the push job.")


def _cmd_db_push(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    payload: dict[str, Any] = {}
    if args.generation:
        payload["generation_id"] = args.generation

    _info("Starting push to database ...", flush=True)
    result = _api(
        "POST",
        cfg.api_url,
        f"/db-connections/{args.connection}/push/",
        token=cfg.token,
        payload=payload,
    )
    job_id = result.get("job_id")
    if not job_id:
        raise CLIError("Server did not return a push job id.")
    job = _wait_for_push_job(cfg, str(job_id))
    rows_pushed = job.get("rows_pushed")
    tables = job.get("tables_pushed")
    rows_label = f"{int(rows_pushed):,} rows" if isinstance(rows_pushed, int) else "data"
    tables_label = f" across {len(tables)} table(s)" if isinstance(tables, list) and tables else ""
    print(f"Pushed {rows_label}{tables_label}.")


def _cmd_test(_args: argparse.Namespace) -> None:
    cfg = _load_config()
    target = _resolve_target(cfg)
    if not target:
        raise CLIError("No database configured. Run 'seedbase init' first.")
    _test_target(target)


def _cmd_diff(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    project_id = _resolve_project(cfg, args.project)

    remote = _api("GET", cfg.api_url, f"/datasets/{project_id}/", token=cfg.token)
    remote_schema = remote.get("schema") or {}
    remote_tables = remote_schema.get("tables") or {}

    local_cfg = _load_project_config()
    local_schema = local_cfg.get("last_schema") or {}
    local_tables = local_schema.get("tables") or {}

    if not local_tables:
        print("No local schema snapshot found. Run 'seedbase pull schema' first to create a baseline.")
        return

    has_changes = False
    all_table_names = sorted(set(list(remote_tables.keys()) + list(local_tables.keys())))

    for table_name in all_table_names:
        if table_name not in local_tables:
            print(f"+ {table_name} (new table)")
            has_changes = True
            continue
        if table_name not in remote_tables:
            print(f"- {table_name} (removed)")
            has_changes = True
            continue

        remote_cols = remote_tables[table_name].get("columns") or {}
        local_cols = local_tables[table_name].get("columns") or {}
        all_col_names = sorted(set(list(remote_cols.keys()) + list(local_cols.keys())))

        for col_name in all_col_names:
            if col_name not in local_cols:
                col_type = remote_cols[col_name].get("type", "")
                print(f"  + {table_name}.{col_name} ({col_type})")
                has_changes = True
            elif col_name not in remote_cols:
                print(f"  - {table_name}.{col_name} (removed)")
                has_changes = True
            else:
                remote_col = remote_cols[col_name]
                local_col = local_cols[col_name]
                diffs = []
                for key in ("type", "mode", "nullable", "primary_key", "unique", "default"):
                    rv = remote_col.get(key)
                    lv = local_col.get(key)
                    if rv != lv:
                        diffs.append(f"{key}: {lv} → {rv}")
                if diffs:
                    print(f"  ~ {table_name}.{col_name}: {', '.join(diffs)}")
                    has_changes = True

    if not has_changes:
        print("No schema changes detected.")


def _cmd_export_config(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    project_id = _resolve_project(cfg, args.project)
    result = _api("GET", cfg.api_url, f"/datasets/{project_id}/export-config/", token=cfg.token)
    engine_config = result.get("engine_config") or {}
    text = json.dumps(engine_config, indent=2) + "\n"
    if args.to_file:
        out = _prepare_out_file(args.to_file, args.force)
        out.write_text(text, encoding="utf-8")
        print(f"Wrote engine_config to {out}")
        return
    print(text, end="")


def _cmd_import_config(args: argparse.Namespace) -> None:
    cfg = _require_auth(_load_config())
    project_id = _resolve_project(cfg, args.project)
    src = Path(args.from_file)
    if not src.exists():
        raise CLIError(f"File not found: {src}")
    try:
        engine_config = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIError(f"Could not read {src}: {exc}") from exc
    if not isinstance(engine_config, dict):
        raise CLIError("engine_config file must contain a JSON object")
    _api(
        "POST",
        cfg.api_url,
        f"/datasets/{project_id}/import-config/",
        token=cfg.token,
        payload={"engine_config": engine_config},
    )
    print(f"Imported engine_config into project {project_id}")


# ── Shared logic ──────────────────────────────────────────────────────

def _target_dialect(target: str) -> str:
    scheme = urllib.parse.urlparse(target).scheme.lower()
    if scheme in {"mysql", "mariadb"}:
        return "mysql"
    if scheme in {"sqlite", "sqlite3"}:
        return "sqlite"
    return "postgresql"


def _dialect_for(cfg: Config, args: argparse.Namespace) -> str:
    target = args.target or cfg.target
    if target:
        return _target_dialect(target)
    return "postgresql"


def _need_target(cfg: Config, args: argparse.Namespace) -> str:
    target = args.target or _resolve_target(cfg)
    if not target:
        raise CLIError("No database configured. Run 'seedbase init' or pass --target, or use --to-file.")
    return target


def _api_root(api_url: str) -> str:
    return api_url[:-3] if api_url.endswith("/v1") else api_url


def _build_schema_sql(cfg: Config, project_id: str, dialect: str, *, drop: bool) -> str:
    result = _api(
        "GET",
        _api_root(cfg.api_url),
        f"/projects/{project_id}/ddl/?dialect={urllib.parse.quote(dialect)}",
        token=cfg.token,
    )
    ddl = result.get("ddl")
    if not ddl:
        raise CLIError("Server did not return a schema for this project.")
    if not drop:
        return ddl
    tables = _fetch_schema_tables(cfg, project_id)
    return _drop_tables_sql(tables, dialect) + "\n" + ddl


def _fetch_schema_tables(cfg: Config, project_id: str) -> list[str]:
    remote = _api("GET", cfg.api_url, f"/datasets/{project_id}/", token=cfg.token)
    schema = remote.get("schema") or {}
    tables = schema.get("tables") or {}
    return list(tables.keys())


def _drop_tables_sql(tables: list[str], dialect: str) -> str:
    if not tables:
        return ""
    if dialect == "mysql":
        body = "\n".join(f"DROP TABLE IF EXISTS `{t.replace('`', '``')}`;" for t in tables)
        return f"SET FOREIGN_KEY_CHECKS=0;\n{body}\nSET FOREIGN_KEY_CHECKS=1;\n"
    if dialect == "sqlite":
        body = "\n".join(f'DROP TABLE IF EXISTS "{t}";' for t in tables)
        return f"PRAGMA foreign_keys=OFF;\n{body}\nPRAGMA foreign_keys=ON;\n"
    body = "\n".join(f'DROP TABLE IF EXISTS "{t.replace(chr(34), chr(34) * 2)}" CASCADE;' for t in tables)
    return body + "\n"


def _truncate_tables_sql(tables: list[str], dialect: str) -> str:
    if not tables:
        return ""
    if dialect == "mysql":
        body = "\n".join(f"TRUNCATE TABLE `{t.replace('`', '``')}`;" for t in tables)
        return f"SET FOREIGN_KEY_CHECKS=0;\n{body}\nSET FOREIGN_KEY_CHECKS=1;\n"
    if dialect == "sqlite":
        body = "\n".join(f'DELETE FROM "{t}";' for t in tables)
        return f"PRAGMA foreign_keys=OFF;\n{body}\nPRAGMA foreign_keys=ON;\n"
    quoted = ", ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tables)
    return f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE;\n"


def _list_generations(cfg: Config, project_id: str) -> list[dict[str, Any]]:
    return _api_list(cfg.api_url, f"/generations/?dataset={urllib.parse.quote(project_id)}", cfg.token)


def _resolve_generation(cfg: Config, project_id: str, ref: str | None) -> dict[str, Any]:
    rows = _list_generations(cfg, project_id)
    completed = [g for g in rows if g.get("status") == "completed" and g.get("result_file")]

    if ref is None:
        if not completed:
            raise CLIError("No completed dataset found. Generate one at https://seedba.se first.")
        return completed[0]

    ref_l = ref.strip().lower()
    by_name = [g for g in rows if str(g.get("name") or "").lower() == ref_l]
    by_id = [g for g in rows if str(g.get("id", "")).lower() == ref_l]
    by_prefix = [g for g in rows if str(g.get("id", "")).lower().startswith(ref_l)] if len(ref_l) >= 4 else []

    for candidates in (by_id, by_name, by_prefix):
        if len(candidates) == 1:
            gen = candidates[0]
            if gen.get("status") != "completed" or not gen.get("result_file"):
                raise CLIError(f"Dataset '{ref}' is not a completed generation (status: {gen.get('status')}).")
            return gen
        if len(candidates) > 1:
            raise CLIError(f"'{ref}' is ambiguous — use the full ID. Run 'seedbase generations'.")

    raise CLIError(f"No dataset matches '{ref}'. Run 'seedbase generations' to see available datasets.")


def _check_generation_dialect(generation: dict[str, Any], dialect: str) -> None:
    fmt = str(generation.get("export_format") or "").lower()
    ok = {
        "mysql": {"mysql", "mariadb"},
        "postgresql": {"postgresql", "postgres"},
        "sqlite": {"sqlite", "sqlite3"},
    }.get(dialect, set())
    if fmt not in ok:
        raise CLIError(
            f"This dataset was generated as '{fmt or 'unknown'}', but your target is {dialect}. "
            f"Generate a {dialect} dataset on the platform first, or pull it with --to-file."
        )


def _download_generation(cfg: Config, generation: dict[str, Any], target: Path, *, force_sql: bool = False) -> None:
    fmt = str(generation.get("export_format") or "").lower()
    export_param = fmt if (not force_sql and fmt in {"csv", "json"}) else "sql"
    _download_file(
        cfg.api_url,
        f"/generations/{generation['id']}/download/?export_format={export_param}",
        cfg.token,
        target,
    )


def _gen_label(generation: dict[str, Any]) -> str:
    name = generation.get("name")
    if name:
        return f"dataset '{name}'"
    return f"dataset {str(generation.get('id', ''))[:8]}"


def _short_date(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:16]


def _trigger_generation(cfg: Config, project_id: str, *, seed: int | None, rows: int | None, wait: int, fmt: str | None = None) -> None:
    payload: dict[str, Any] = {}
    if seed is not None:
        payload["seed"] = seed
    if rows is not None:
        payload["rows_per_table"] = max(1, rows)
    if fmt:
        payload["format"] = fmt

    create = _api("POST", cfg.api_url, f"/datasets/{project_id}/generate/", token=cfg.token, payload=payload)
    generation_id = create.get("generation_id")
    if not generation_id:
        raise CLIError("Generation did not return an id")

    _info("Generating ...", flush=True)
    show_progress = sys.stdout.isatty()
    deadline = time.time() + max(5, wait)
    consecutive_errors = 0
    while True:
        status_data = None
        try:
            status_data = _api("GET", cfg.api_url, f"/generations/{generation_id}/", token=cfg.token)
            consecutive_errors = 0
        except CLIError:
            consecutive_errors += 1
            if consecutive_errors >= _JOB_POLL_MAX_ERRORS:
                raise
        if status_data is not None:
            status = status_data.get("status")
            percent = int(status_data.get("progress_percent") or 0)
            if show_progress:
                _info(f"  {status} {percent}%", end="\r", flush=True)
            if status in {"completed", "failed", "cancelled"}:
                if show_progress:
                    _info(" " * 40, end="\r")
                if status != "completed":
                    raise CLIError(f"Generation {status}")
                total = int(status_data.get("total_rows") or 0)
                _info(f"Generated {total:,} rows. (ID {str(generation_id)[:8]})")
                return
        if time.time() >= deadline:
            raise CLIError("Generation timed out")
        time.sleep(2)


# ── Config ────────────────────────────────────────────────────────────

_LOCAL_API_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validate_api_url(api_url: str) -> str:
    parsed = urllib.parse.urlparse(api_url)
    if parsed.scheme == "https":
        return api_url
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host in _LOCAL_API_HOSTS:
        return api_url
    raise CLIError(
        f"Insecure API URL '{api_url}' — only https:// is allowed "
        "(http:// is only accepted for localhost/127.0.0.1/::1)."
    )


def _load_config() -> Config:
    raw = _load_json(CONFIG_PATH)
    project_cfg = _load_project_config()
    if project_cfg.get("api_url"):
        # api_url aus der projektlokalen Datei wird ignoriert: sonst könnte ein
        # fremdes Repo den globalen Token an eine fremde URL umleiten.
        print(
            f"warning: ignoring 'api_url' from {PROJECT_CONFIG_PATH} — "
            "use SEEDBASE_API_URL or the global config instead.",
            file=sys.stderr,
        )
    api_url = str(os.getenv("SEEDBASE_API_URL") or raw.get("api_url") or API_URL).rstrip("/")
    _validate_api_url(api_url)
    token = os.getenv("SEEDBASE_TOKEN") or raw.get("token") or None
    project = project_cfg.get("project") or raw.get("default_project") or None
    target = project_cfg.get("target") or raw.get("default_target") or None
    return Config(token=token, api_url=api_url, project=project, target=target)


def _require_auth(cfg: Config) -> Config:
    if not cfg.token:
        raise CLIError("Not logged in. Run 'seedbase login' first.")
    return cfg


def _load_project_config() -> dict[str, Any]:
    if PROJECT_CONFIG_PATH.exists():
        return _load_json(PROJECT_CONFIG_PATH)
    return {}


def _save_project_config(data: dict[str, Any]) -> None:
    PROJECT_CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _resolve_project(cfg: Config, explicit: str | None) -> str:
    if explicit:
        return explicit
    if cfg.project:
        return cfg.project
    raise CLIError("No project configured. Use 'seedbase init' or pass --project.")


def _resolve_target(cfg: Config) -> str | None:
    return cfg.target


def _save_config(cfg: Config) -> Path:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Nur die wirklich globalen Schlüssel anfassen: projektlokale Werte
    # (project/target) und Env-Werte dürfen nicht in ~/.seedbase/config.json wandern.
    payload = _load_json(CONFIG_PATH)
    payload["token"] = cfg.token
    CONFIG_PATH.touch(mode=0o600, exist_ok=True)
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    CONFIG_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return CONFIG_PATH


# ── API helpers ───────────────────────────────────────────────────────

_SSL_CONTEXT = None


def _ssl_context():
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = create_ssl_context()
    return _SSL_CONTEXT


def _api(
    method: str,
    api_url: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    allow_non_2xx: set[int] | None = None,
) -> dict[str, Any]:
    rel = path if path.startswith("/") else f"/{path}"
    url = f"{api_url}{rel}"

    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if token:
        if token.startswith("dr_sk_"):
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers["Authorization"] = f"Token {token}"

    req = urllib.request.Request(url=url, method=method.upper(), headers=headers, data=data)
    allow_non_2xx = allow_non_2xx or set()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=_ssl_context()) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        parsed: Any = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
        if exc.code in allow_non_2xx:
            return parsed if isinstance(parsed, dict) else {}
        raise CLIError(_http_error_message(exc.code, parsed, raw)) from exc
    except urllib.error.URLError as exc:
        if is_cert_verify_error(exc):
            raise CLIError(CERTIFI_HINT) from exc
        raise CLIError(f"Network error: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise CLIError(f"Network error: {exc}") from exc

    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CLIError(f"Server returned an unexpected non-JSON response from {url}") from exc


def _http_error_message(code: int, parsed: Any, raw: str) -> str:
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    if detail:
        return str(detail)
    # DRF-Feldfehler ({"root_count": [...]}, non_field_errors) sichtbar machen.
    if isinstance(parsed, (dict, list)) and parsed:
        snippet = json.dumps(parsed, ensure_ascii=False)
    else:
        snippet = raw.strip()
    snippet = snippet[:300]
    if snippet:
        return f"API request failed ({code}): {snippet}"
    return f"API request failed ({code})"


def _download_file(api_url: str, path: str, token: str | None, target: Path) -> None:
    rel = path if path.startswith("/") else f"/{path}"
    url = f"{api_url}{rel}"
    headers = {}
    if token:
        if token.startswith("dr_sk_"):
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers["Authorization"] = f"Token {token}"
    req = urllib.request.Request(url=url, method="GET", headers=headers)

    part = target.with_name(target.name + ".part")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=_ssl_context()) as resp, part.open("wb") as out:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(part, target)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
        detail = parsed.get("detail") if isinstance(parsed, dict) else None
        raise CLIError(detail or f"Download failed ({exc.code})") from exc
    except urllib.error.URLError as exc:
        if is_cert_verify_error(exc):
            raise CLIError(CERTIFI_HINT) from exc
        raise CLIError(f"Download failed: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise CLIError(f"Download failed: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)


def _extract_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        rows = payload.get("results")
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    return []


_MAX_PAGES = 50


def _next_page_path(next_url: str, api_url: str) -> str:
    if next_url.startswith(api_url):
        return next_url[len(api_url):]
    parsed = urllib.parse.urlparse(next_url)
    rel = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    base_path = urllib.parse.urlparse(api_url).path
    if base_path and rel.startswith(base_path):
        rel = rel[len(base_path):]
    return rel


def _api_list(api_url: str, path: str, token: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    next_path: str | None = path
    for _ in range(_MAX_PAGES):
        if not next_path:
            break
        data = _api("GET", api_url, next_path, token=token)
        rows.extend(_extract_results(data))
        next_url = data.get("next") if isinstance(data, dict) else None
        next_path = _next_page_path(str(next_url), api_url) if next_url else None
    return rows


def _prepare_out_file(path_str: str, force: bool) -> Path:
    out = Path(path_str)
    if out.exists() and not force:
        raise CLIError(f"{out} already exists — pass --force to overwrite.")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


# ── DB operations ─────────────────────────────────────────────────────

def _sqlite_path(parsed: urllib.parse.ParseResult) -> str:
    # SQLAlchemy-Konvention: sqlite:///x.db ist relativ, sqlite:////abs/x.db absolut.
    path = f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path
    if path.startswith("/"):
        path = path[1:]
    if not path:
        raise CLIError("SQLite target requires a file path")
    return path


def _pg_command_target(target: str) -> tuple[str, dict[str, str]]:
    # Passwort nicht als argv übergeben (via ps sichtbar) — stattdessen PGPASSWORD.
    parsed = urllib.parse.urlparse(target)
    env = os.environ.copy()
    password = urllib.parse.unquote(parsed.password or "")
    if password:
        env["PGPASSWORD"] = password
    netloc = ""
    if parsed.username:
        netloc = f"{parsed.username}@"
    netloc += parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    sanitized = urllib.parse.urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )
    return sanitized, env


def _test_target(target: str) -> None:
    parsed = urllib.parse.urlparse(target)
    scheme = parsed.scheme.lower()

    if scheme in {"sqlite", "sqlite3"}:
        path = _sqlite_path(parsed)
        conn = sqlite3.connect(path)
        conn.execute("SELECT 1")
        conn.close()
        print(f"OK: SQLite ({path})")
        return

    if scheme in {"postgres", "postgresql"}:
        if shutil.which("psql") is None:
            raise CLIError("psql not found in PATH")
        uri, env = _pg_command_target(target)
        result = subprocess.run(
            ["psql", uri, "-c", "SELECT 1"],
            capture_output=True, text=True, env=env, check=False,
        )
        if result.returncode != 0:
            raise CLIError(f"Connection failed: {result.stderr.strip()}")
        print(f"OK: PostgreSQL ({parsed.hostname}:{parsed.port or 5432})")
        return

    if scheme in {"mysql", "mariadb"}:
        if shutil.which("mysql") is None:
            raise CLIError("mysql client not found in PATH")
        host = parsed.hostname or "localhost"
        port = str(parsed.port or 3306)
        user = urllib.parse.unquote(parsed.username or "root")
        password = urllib.parse.unquote(parsed.password or "")
        database = parsed.path.lstrip("/")
        cmd = ["mysql", "--host", host, "--port", port, "--user", user, database, "-e", "SELECT 1"]
        env = os.environ.copy()
        if password:
            env["MYSQL_PWD"] = password
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
        if result.returncode != 0:
            raise CLIError(f"Connection failed: {result.stderr.strip()}")
        print(f"OK: MySQL ({host}:{port})")
        return

    raise CLIError(f"Unsupported database scheme: {scheme}")


def _wait_for_db(target: str, timeout_seconds: int) -> None:
    deadline = time.time() + max(1, timeout_seconds)
    while time.time() < deadline:
        try:
            _test_target(target)
            return
        except CLIError:
            time.sleep(1)
    raise CLIError("Database did not become ready in time")


def _apply_sql_text(target: str, sql: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".sql", delete=False, mode="w", encoding="utf-8") as tmp:
        tmp.write(sql)
        sql_file = Path(tmp.name)
    try:
        _apply_sql_to_target(target, sql_file)
    finally:
        sql_file.unlink(missing_ok=True)


def _apply_sql_to_target(target: str, sql_file: Path) -> None:
    parsed = urllib.parse.urlparse(target)
    scheme = parsed.scheme.lower()

    if scheme in {"sqlite", "sqlite3"}:
        db_path = _sqlite_path(parsed)
        sql = sql_file.read_text(encoding="utf-8")
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(sql)
            conn.commit()
        finally:
            conn.close()
        return

    if scheme in {"postgres", "postgresql"}:
        if shutil.which("psql") is None:
            raise CLIError("psql not found in PATH")
        uri, env = _pg_command_target(target)
        cmd = ["psql", uri, "-v", "ON_ERROR_STOP=1", "-f", str(sql_file)]
        result = subprocess.run(cmd, env=env, check=False)
        if result.returncode != 0:
            raise CLIError("psql import failed")
        return

    if scheme in {"mysql", "mariadb"}:
        if shutil.which("mysql") is None:
            raise CLIError("mysql client not found in PATH")
        host = parsed.hostname or "localhost"
        port = str(parsed.port or 3306)
        user = urllib.parse.unquote(parsed.username or "root")
        password = urllib.parse.unquote(parsed.password or "")
        database = parsed.path.lstrip("/")
        if not database:
            raise CLIError("MySQL target must include database name")
        cmd = ["mysql", "--host", host, "--port", port, "--user", user, database]
        env = os.environ.copy()
        if password:
            env["MYSQL_PWD"] = password
        with sql_file.open("rb") as src:
            result = subprocess.run(cmd, stdin=src, env=env, check=False)
        if result.returncode != 0:
            raise CLIError("mysql import failed")
        return

    raise CLIError(f"Unsupported database scheme: {scheme}")


# ── Utilities ─────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


if __name__ == "__main__":
    main()
