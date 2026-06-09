from __future__ import annotations

import io
import json
import urllib.error

import pytest

from seedbase import sdk
from seedbase.sdk import SeedbaseClient, SeedbaseError


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, *args):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_response(payload):
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


def _install_urlopen(monkeypatch, handler):
    calls = []

    def fake_urlopen(req):
        calls.append(req)
        return handler(req, len(calls) - 1)

    monkeypatch.setattr(sdk.urllib.request, "urlopen", fake_urlopen)
    return calls


@pytest.fixture
def client():
    return SeedbaseClient(token="test-token", api_url="https://example.test/api/v1")


def test_init_requires_token(tmp_path):
    with pytest.raises(SeedbaseError):
        SeedbaseClient(config_path=tmp_path / "missing.json")


def test_init_loads_token_from_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"token": "cfg-token"}), encoding="utf-8")
    c = SeedbaseClient(config_path=cfg)
    assert c.token == "cfg-token"


def test_list_projects(monkeypatch, client):
    rows = [{"id": "p1", "name": "Alpha"}, {"id": "p2", "name": "Beta"}]
    calls = _install_urlopen(monkeypatch, lambda req, i: _json_response({"results": rows}))
    result = client.list_projects()
    assert result == rows
    assert calls[0].full_url == "https://example.test/api/v1/datasets/"
    assert calls[0].headers["Authorization"] == "Token test-token"


def test_get_generation(monkeypatch, client):
    gen = {"id": "g1", "status": "completed", "total_rows": 42}
    _install_urlopen(monkeypatch, lambda req, i: _json_response(gen))
    assert client.get_generation("g1") == gen


def test_bearer_token_for_api_key():
    c = SeedbaseClient(token="dr_sk_abc", api_url="https://example.test/api/v1")
    assert sdk._auth_header(c.token) == {"Authorization": "Bearer dr_sk_abc"}


def test_generate_no_wait(monkeypatch, client):
    created = {"generation_id": "g9"}
    _install_urlopen(monkeypatch, lambda req, i: _json_response(created))
    result = client.generate("p1", seed=7, rows=10)
    assert result == created


def test_generate_wait_polls_until_completed(monkeypatch, client):
    monkeypatch.setattr(sdk.time, "sleep", lambda *_: None)
    responses = [
        {"generation_id": "g9"},
        {"id": "g9", "status": "running", "progress_percent": 40},
        {"id": "g9", "status": "completed", "total_rows": 100},
    ]
    _install_urlopen(monkeypatch, lambda req, i: _json_response(responses[i]))
    result = client.generate("p1", wait=True, timeout=10)
    assert result["status"] == "completed"
    assert result["total_rows"] == 100


def test_generate_wait_raises_on_failed(monkeypatch, client):
    monkeypatch.setattr(sdk.time, "sleep", lambda *_: None)
    responses = [
        {"generation_id": "g9"},
        {"id": "g9", "status": "failed"},
    ]
    _install_urlopen(monkeypatch, lambda req, i: _json_response(responses[i]))
    with pytest.raises(SeedbaseError, match="failed"):
        client.generate("p1", wait=True, timeout=10)


def test_download_returns_bytes(monkeypatch, client):
    _install_urlopen(monkeypatch, lambda req, i: _FakeResponse(b"-- sql dump"))
    data = client.download("g1")
    assert data == b"-- sql dump"


def test_export_config(monkeypatch, client):
    cfg = {"tables": [{"name": "users", "columns": [{"name": "id", "generator": "auto_increment"}]}]}
    calls = _install_urlopen(monkeypatch, lambda req, i: _json_response({"engine_config": cfg}))
    result = client.export_config("p1")
    assert result == cfg
    assert calls[0].full_url == "https://example.test/api/v1/datasets/p1/export-config/"
    assert calls[0].method == "GET"


def test_export_config_empty(monkeypatch, client):
    _install_urlopen(monkeypatch, lambda req, i: _json_response({"engine_config": {}}))
    assert client.export_config("p1") == {}


def test_import_config(monkeypatch, client):
    cfg = {"tables": [{"name": "users", "columns": [{"name": "id", "generator": "auto_increment"}]}]}
    calls = _install_urlopen(monkeypatch, lambda req, i: _json_response({"engine_config": cfg}))
    result = client.import_config("p1", cfg)
    assert result == cfg
    assert calls[0].full_url == "https://example.test/api/v1/datasets/p1/import-config/"
    assert calls[0].method == "POST"
    assert json.loads(calls[0].data.decode("utf-8")) == {"engine_config": cfg}


def test_import_config_invalid_raises(monkeypatch, client):
    def handler(req, i):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"detail": "engine_config.tables must be a non-empty list"}).encode("utf-8")),
        )

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(SeedbaseError) as exc:
        client.import_config("p1", {"tables": []})
    assert exc.value.status_code == 400


def test_http_error_raises_seedbase_error(monkeypatch, client):
    def handler(req, i):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"detail": "bad seed"}).encode("utf-8")),
        )

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(SeedbaseError) as exc:
        client.list_projects()
    assert exc.value.status_code == 400
    assert "bad seed" in str(exc.value)
