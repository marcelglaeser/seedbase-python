from __future__ import annotations

import io
import json
import urllib.error

import pytest

from seedbase import cli


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._done = False

    def read(self, size=-1):
        if self._done:
            return b""
        self._done = True
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_response(payload):
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


def _install_urlopen(monkeypatch, handler):
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append(req)
        return handler(req, len(calls) - 1)

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    return calls


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    config_path = tmp_path / "global" / "config.json"
    project_path = tmp_path / "project" / ".seedbase.json"
    config_path.parent.mkdir(parents=True)
    project_path.parent.mkdir(parents=True)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "PROJECT_CONFIG_PATH", project_path)
    monkeypatch.delenv("SEEDBASE_TOKEN", raising=False)
    monkeypatch.delenv("SEEDBASE_API_URL", raising=False)
    return config_path, project_path


# ── _save_config ──────────────────────────────────────────────────────

def test_save_config_creates_file_with_0600(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)

    cli._save_config(cli.Config(token="tok", api_url="https://example.test", project=None, target=None))

    assert (config_path.stat().st_mode & 0o777) == 0o600
    assert json.loads(config_path.read_text())["token"] == "tok"


def test_save_config_tightens_existing_permissions(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    config_path.chmod(0o644)
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)

    cli._save_config(cli.Config(token="tok", api_url="https://example.test", project=None, target=None))

    assert (config_path.stat().st_mode & 0o777) == 0o600


def test_save_config_does_not_leak_merged_values(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"token": "old", "api_url": "https://global.test", "default_project": "global-proj"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)

    merged = cli.Config(
        token="new-tok",
        api_url="https://from-env.test",
        project="project-from-local-file",
        target="postgresql://u:secret@db/local",
    )
    cli._save_config(merged)

    stored = json.loads(config_path.read_text())
    assert stored["token"] == "new-tok"
    assert stored["api_url"] == "https://global.test"
    assert stored["default_project"] == "global-proj"
    assert "default_target" not in stored
    assert "secret" not in config_path.read_text()


# ── _load_config ──────────────────────────────────────────────────────

def test_load_config_ignores_project_api_url(isolated_config, capsys):
    config_path, project_path = isolated_config
    config_path.write_text(json.dumps({"token": "tok"}), encoding="utf-8")
    project_path.write_text(
        json.dumps({"api_url": "https://evil.example/api/v1", "project": "p1"}),
        encoding="utf-8",
    )

    cfg = cli._load_config()

    assert cfg.api_url == cli.API_URL
    assert cfg.project == "p1"
    assert "ignoring 'api_url'" in capsys.readouterr().err


def test_load_config_rejects_http_api_url(isolated_config, monkeypatch):
    monkeypatch.setenv("SEEDBASE_API_URL", "http://evil.example/api/v1")
    with pytest.raises(cli.CLIError, match="Insecure API URL"):
        cli._load_config()


def test_load_config_allows_http_localhost(isolated_config, monkeypatch):
    monkeypatch.setenv("SEEDBASE_API_URL", "http://localhost:8000/api/v1")
    cfg = cli._load_config()
    assert cfg.api_url == "http://localhost:8000/api/v1"


def test_load_config_allows_http_loopback_ip(isolated_config, monkeypatch):
    monkeypatch.setenv("SEEDBASE_API_URL", "http://127.0.0.1:8000/api/v1")
    cfg = cli._load_config()
    assert cfg.api_url == "http://127.0.0.1:8000/api/v1"


# ── _api error messages ───────────────────────────────────────────────

def test_api_error_includes_field_errors(monkeypatch):
    body = json.dumps({"root_count": ["A valid integer is required."]}).encode("utf-8")

    def handler(req, i):
        raise urllib.error.HTTPError(url=req.full_url, code=400, msg="Bad Request", hdrs=None, fp=io.BytesIO(body))

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(cli.CLIError) as exc:
        cli._api("GET", "https://example.test/api/v1", "/x/", token="tok")
    assert "root_count" in str(exc.value)
    assert "A valid integer is required." in str(exc.value)


def test_api_error_prefers_detail(monkeypatch):
    body = json.dumps({"detail": "boom"}).encode("utf-8")

    def handler(req, i):
        raise urllib.error.HTTPError(url=req.full_url, code=400, msg="Bad Request", hdrs=None, fp=io.BytesIO(body))

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(cli.CLIError, match="^boom$"):
        cli._api("GET", "https://example.test/api/v1", "/x/", token="tok")


def test_api_error_message_truncated():
    huge = {"field": ["x" * 1000]}
    msg = cli._http_error_message(400, huge, json.dumps(huge))
    assert len(msg) < 400


def test_api_non_json_2xx_raises_clean_error(monkeypatch):
    _install_urlopen(monkeypatch, lambda req, i: _FakeResponse(b"<html>gateway</html>"))
    with pytest.raises(cli.CLIError, match="non-JSON"):
        cli._api("GET", "https://example.test/api/v1", "/x/", token="tok")


# ── Pagination ────────────────────────────────────────────────────────

def test_api_list_follows_next_links(monkeypatch):
    pages = {
        "https://example.test/api/v1/datasets/": {
            "results": [{"id": "a"}],
            "next": "https://example.test/api/v1/datasets/?page=2",
        },
        "https://example.test/api/v1/datasets/?page=2": {
            "results": [{"id": "b"}],
            "next": None,
        },
    }
    _install_urlopen(monkeypatch, lambda req, i: _json_response(pages[req.full_url]))
    rows = cli._api_list("https://example.test/api/v1", "/datasets/", "tok")
    assert [r["id"] for r in rows] == ["a", "b"]


def test_api_list_stops_after_max_pages(monkeypatch):
    _install_urlopen(
        monkeypatch,
        lambda req, i: _json_response(
            {"results": [{"id": str(i)}], "next": "https://example.test/api/v1/datasets/?page=next"}
        ),
    )
    rows = cli._api_list("https://example.test/api/v1", "/datasets/", "tok")
    assert len(rows) == cli._MAX_PAGES


# ── _download_file ────────────────────────────────────────────────────

def test_download_file_uses_part_and_replaces(monkeypatch, tmp_path):
    _install_urlopen(monkeypatch, lambda req, i: _FakeResponse(b"-- sql"))
    target = tmp_path / "out.sql"
    cli._download_file("https://example.test/api/v1", "/dl/", "tok", target)
    assert target.read_bytes() == b"-- sql"
    assert not (tmp_path / "out.sql.part").exists()


def test_download_file_removes_part_on_failure(monkeypatch, tmp_path):
    class _BrokenResponse:
        def __init__(self):
            self._calls = 0

        def read(self, size=-1):
            if self._calls == 0:
                self._calls += 1
                return b"partial"
            raise TimeoutError("read timed out")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    _install_urlopen(monkeypatch, lambda req, i: _BrokenResponse())
    target = tmp_path / "out.sql"
    with pytest.raises(cli.CLIError, match="Download failed"):
        cli._download_file("https://example.test/api/v1", "/dl/", "tok", target)
    assert not target.exists()
    assert not (tmp_path / "out.sql.part").exists()


def test_download_file_network_error_raises_cli_error(monkeypatch, tmp_path):
    def handler(req, i):
        raise urllib.error.URLError("connection refused")

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(cli.CLIError, match="Download failed"):
        cli._download_file("https://example.test/api/v1", "/dl/", "tok", tmp_path / "out.sql")


# ── --to-file overwrite protection ────────────────────────────────────

def test_prepare_out_file_refuses_existing(tmp_path):
    out = tmp_path / "out.sql"
    out.write_text("old", encoding="utf-8")
    with pytest.raises(cli.CLIError, match="--force"):
        cli._prepare_out_file(str(out), force=False)


def test_prepare_out_file_force_allows_overwrite(tmp_path):
    out = tmp_path / "out.sql"
    out.write_text("old", encoding="utf-8")
    assert cli._prepare_out_file(str(out), force=True) == out


# ── sqlite path semantics ─────────────────────────────────────────────

def test_sqlite_three_slashes_is_relative():
    import urllib.parse

    parsed = urllib.parse.urlparse("sqlite:///rel.db")
    assert cli._sqlite_path(parsed) == "rel.db"


def test_sqlite_four_slashes_is_absolute():
    import urllib.parse

    parsed = urllib.parse.urlparse("sqlite:////abs/path.db")
    assert cli._sqlite_path(parsed) == "/abs/path.db"


# ── psql credential handling ──────────────────────────────────────────

def test_pg_command_target_moves_password_to_env():
    uri, env = cli._pg_command_target("postgresql://alice:s3cret@db.example:5432/mydb?sslmode=require")
    assert "s3cret" not in uri
    assert env["PGPASSWORD"] == "s3cret"
    assert uri == "postgresql://alice@db.example:5432/mydb?sslmode=require"


def test_pg_command_target_without_password():
    uri, env = cli._pg_command_target("postgresql://db.example/mydb")
    assert "PGPASSWORD" not in env or env.get("PGPASSWORD") == ""
    assert uri == "postgresql://db.example/mydb"


# ── Commands: connections / mask / db-push ────────────────────────────

@pytest.fixture
def logged_in(isolated_config):
    config_path, _project_path = isolated_config
    config_path.write_text(json.dumps({"token": "tok"}), encoding="utf-8")
    return config_path


def test_cmd_connections_lists(monkeypatch, logged_in, capsys):
    rows = [{"id": "c1", "name": "Staging", "db_type": "postgresql", "host": "db.example", "database": "app"}]

    def handler(req, i):
        assert req.full_url.endswith("/db-connections/")
        return _json_response({"results": rows, "next": None})

    _install_urlopen(monkeypatch, handler)
    cli.main(["connections"])
    out = capsys.readouterr().out
    assert "c1" in out
    assert "Staging" in out


def test_cmd_mask_dry_run(monkeypatch, logged_in, capsys):
    calls = []

    def handler(req, i):
        calls.append(req)
        assert req.full_url.endswith("/db-connections/c1/mask-in-place/")
        payload = json.loads(req.data.decode("utf-8"))
        assert payload == {"columns": ["users.email", "users.name"], "dry_run": True}
        return _json_response({"dry_run": True, "columns": ["users.email", "users.name"], "row_count": 12, "preview": []})

    _install_urlopen(monkeypatch, handler)
    cli.main(["mask", "c1", "--table", "users", "--columns", "email,name", "--dry-run"])
    out = capsys.readouterr().out
    assert "Dry run" in out
    assert "users.email" in out
    assert len(calls) == 1


def test_cmd_mask_real_run_waits_for_tool_job(monkeypatch, logged_in, capsys):
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    responses = []

    def handler(req, i):
        responses.append(req.full_url)
        if req.full_url.endswith("/mask-in-place/"):
            payload = json.loads(req.data.decode("utf-8"))
            assert payload["dry_run"] is False
            return _json_response({"job_id": "j1", "status": "pending"})
        assert req.full_url.endswith("/tool-jobs/j1/")
        if i == 1:
            return _json_response({"status": "running"})
        return _json_response({"status": "completed", "result": {"columns": ["users.email"], "row_count": 7}})

    _install_urlopen(monkeypatch, handler)
    cli.main(["mask", "c1", "--table", "users", "--columns", "email"])
    out = capsys.readouterr().out
    assert "Masked 1 column(s), 7 row(s)." in out


def test_cmd_mask_cancelled_job_is_terminal(monkeypatch, logged_in):
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)

    def handler(req, i):
        if req.full_url.endswith("/mask-in-place/"):
            return _json_response({"job_id": "j1", "status": "pending"})
        return _json_response({"status": "cancelled"})

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(SystemExit):
        cli.main(["mask", "c1", "--table", "users", "--columns", "email"])


def test_cmd_db_push(monkeypatch, logged_in, capsys):
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)

    def handler(req, i):
        if req.full_url.endswith("/db-connections/c1/push/"):
            payload = json.loads(req.data.decode("utf-8"))
            assert payload == {"generation_id": "g1"}
            return _json_response({"job_id": "p1", "status": "queued"})
        assert req.full_url.endswith("/push-jobs/p1/")
        if i == 1:
            return _json_response({"status": "running"})
        return _json_response({"status": "completed", "rows_pushed": 1234, "tables_pushed": ["users", "orders"]})

    _install_urlopen(monkeypatch, handler)
    cli.main(["db-push", "c1", "--generation", "g1"])
    out = capsys.readouterr().out
    assert "Pushed 1,234 rows across 2 table(s)." in out


def test_cmd_db_push_failed_job(monkeypatch, logged_in):
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)

    def handler(req, i):
        if req.full_url.endswith("/push/"):
            return _json_response({"job_id": "p1", "status": "queued"})
        return _json_response({"status": "failed", "error_message": "connection refused"})

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(SystemExit):
        cli.main(["db-push", "c1"])


# ── Tool-job poll robustness ──────────────────────────────────────────

def test_wait_for_tool_job_tolerates_transient_errors(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    cfg = cli.Config(token="tok", api_url="https://example.test/api/v1", project=None, target=None)
    attempts = []

    def fake_api(method, api_url, path, **kwargs):
        attempts.append(path)
        if len(attempts) <= 2:
            raise cli.CLIError("Network error: flaky")
        return {"status": "completed", "result": {"key": "k"}}

    monkeypatch.setattr(cli, "_api", fake_api)
    result = cli._wait_for_tool_job(cfg, "j1")
    assert result == {"key": "k"}
    assert len(attempts) == 3


def test_wait_for_tool_job_gives_up_after_three_errors(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    cfg = cli.Config(token="tok", api_url="https://example.test/api/v1", project=None, target=None)

    def fake_api(*args, **kwargs):
        raise cli.CLIError("Network error: down")

    monkeypatch.setattr(cli, "_api", fake_api)
    with pytest.raises(cli.CLIError, match="down"):
        cli._wait_for_tool_job(cfg, "j1")


# ── generate --rows payload ───────────────────────────────────────────

def test_trigger_generation_sends_rows_per_table(monkeypatch, capsys):
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    cfg = cli.Config(token="tok", api_url="https://example.test/api/v1", project=None, target=None)
    sent = {}

    def fake_api(method, api_url, path, token=None, payload=None, **kwargs):
        if method == "POST":
            sent.update(payload or {})
            return {"generation_id": "g1"}
        return {"status": "completed", "total_rows": 50, "progress_percent": 100}

    monkeypatch.setattr(cli, "_api", fake_api)
    cli._trigger_generation(cfg, "p1", seed=None, rows=25, wait=30, fmt=None)
    assert sent == {"rows_per_table": 25}
