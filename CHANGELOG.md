# Changelog

## 0.1.2

Security:
- Project-local `.seedbase.json` can no longer override `api_url` (token exfiltration via cloned repos); `login`/`logout` only persist the token instead of the merged config.
- Non-https API URLs are rejected (localhost excepted); token file written with mode 0600; `psql` receives the password via `PGPASSWORD` instead of argv.

Reliability:
- Request timeouts on all HTTP calls; downloads go through a `.part` file with atomic rename; clean Ctrl-C handling; clear error messages for field validation errors, non-JSON responses and network failures.
- Paginated list endpoints are followed across pages; polling tolerates transient errors and unknown job states are treated as terminal.

Features:
- `seedbase generate --rows` now works end-to-end (server expands `rows_per_table` to all tables).
- New commands: `seedbase connections`, `seedbase mask` (dry-run preview + async execution) and `seedbase db-push`.
- `pull data --to-file` writes the generation's actual export format instead of always `.sql`; `--force` flag to overwrite existing output files.
- pytest plugin: generation timeout configurable via `SEEDBASE_GENERATION_TIMEOUT` / `seedbase_generation_timeout` ini option (default raised to 300s).

CI:
- Publish workflow actions pinned to commit SHAs.

## 0.1.1

- Author email -> contact@seedba.se (consistent with website).
- Logo shown on the PyPI project page (embedded in README).

## 0.1.0

- Initial public release: CLI, Python SDK (SeedbaseClient), pytest plugin.
