from __future__ import annotations

import os
import socket

import pytest

import seedbase
from seedbase import pytest_plugin
from seedbase.sdk import SeedbaseClient, SeedbaseError

pytest_plugins = ["pytester"]

_SRC_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(seedbase.__file__)))


@pytest.fixture(autouse=True)
def _seedbase_on_subprocess_path(monkeypatch):
    existing = os.environ.get("PYTHONPATH", "")
    parts = [_SRC_ROOT] + ([existing] if existing else [])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(parts))


def test_import_has_no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("network access during plugin import")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    import importlib

    importlib.reload(pytest_plugin)


class _StubConfig:
    def __init__(self, options=None, ini=None):
        self._options = options or {}
        self._ini = ini or {}

    def getoption(self, name, default=None):
        return self._options.get(name, default)

    def getini(self, name):
        return self._ini.get(name, "")


def test_resolve_project_prefers_option(monkeypatch):
    monkeypatch.delenv("SEEDBASE_PROJECT", raising=False)
    cfg = _StubConfig(options={"seedbase_project": "from-option"}, ini={"seedbase_project": "from-ini"})
    assert pytest_plugin._resolve_project(cfg) == "from-option"


def test_resolve_project_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("SEEDBASE_PROJECT", "from-env")
    cfg = _StubConfig()
    assert pytest_plugin._resolve_project(cfg) == "from-env"


def test_resolve_seed_parses_int(monkeypatch):
    monkeypatch.delenv("SEEDBASE_SEED", raising=False)
    cfg = _StubConfig(ini={"seedbase_seed": "42"})
    assert pytest_plugin._resolve_seed(cfg) == 42


def test_resolve_seed_invalid_raises(monkeypatch):
    monkeypatch.delenv("SEEDBASE_SEED", raising=False)
    cfg = _StubConfig(options={"seedbase_seed": "abc"})
    with pytest.raises(SeedbaseError):
        pytest_plugin._resolve_seed(cfg)


def test_resolve_api_url_default(monkeypatch):
    monkeypatch.delenv("SEEDBASE_API_URL", raising=False)
    cfg = _StubConfig()
    assert pytest_plugin._resolve_api_url(cfg) == pytest_plugin.API_URL


def test_resolve_api_url_rejects_http(monkeypatch):
    monkeypatch.setenv("SEEDBASE_API_URL", "http://evil.example/api/v1")
    cfg = _StubConfig()
    with pytest.raises(SeedbaseError, match="Insecure API URL"):
        pytest_plugin._resolve_api_url(cfg)


def test_resolve_api_url_allows_http_localhost(monkeypatch):
    monkeypatch.setenv("SEEDBASE_API_URL", "http://localhost:8000/api/v1")
    cfg = _StubConfig()
    assert pytest_plugin._resolve_api_url(cfg) == "http://localhost:8000/api/v1"


def test_resolve_generation_timeout_default(monkeypatch):
    monkeypatch.delenv("SEEDBASE_GENERATION_TIMEOUT", raising=False)
    cfg = _StubConfig()
    assert pytest_plugin._resolve_generation_timeout(cfg) == pytest_plugin.DEFAULT_GENERATION_TIMEOUT
    assert pytest_plugin.DEFAULT_GENERATION_TIMEOUT == 300


def test_resolve_generation_timeout_from_ini(monkeypatch):
    monkeypatch.delenv("SEEDBASE_GENERATION_TIMEOUT", raising=False)
    cfg = _StubConfig(ini={"seedbase_generation_timeout": "600"})
    assert pytest_plugin._resolve_generation_timeout(cfg) == 600


def test_resolve_generation_timeout_from_env(monkeypatch):
    monkeypatch.setenv("SEEDBASE_GENERATION_TIMEOUT", "450")
    cfg = _StubConfig()
    assert pytest_plugin._resolve_generation_timeout(cfg) == 450


def test_resolve_generation_timeout_invalid_raises(monkeypatch):
    monkeypatch.setenv("SEEDBASE_GENERATION_TIMEOUT", "soon")
    cfg = _StubConfig()
    with pytest.raises(SeedbaseError):
        pytest_plugin._resolve_generation_timeout(cfg)


def test_client_fixture_skips_without_credentials(pytester, monkeypatch):
    monkeypatch.delenv("SEEDBASE_TOKEN", raising=False)
    pytester.makepyfile(
        """
        def test_uses_client(seedbase_client):
            assert seedbase_client is not None
        """
    )
    result = pytester.runpytest_subprocess("-rs", "-p", "seedbase.pytest_plugin")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*credentials missing*"])


def test_project_fixture_skips_without_project(pytester, monkeypatch):
    monkeypatch.setenv("SEEDBASE_TOKEN", "tok")
    monkeypatch.delenv("SEEDBASE_PROJECT", raising=False)
    pytester.makepyfile(
        """
        def test_uses_project(seedbase_project):
            assert seedbase_project
        """
    )
    result = pytester.runpytest_subprocess("-p", "seedbase.pytest_plugin")
    result.assert_outcomes(skipped=1)


def test_seeded_data_fixture_with_mocked_client(pytester, monkeypatch):
    monkeypatch.setenv("SEEDBASE_TOKEN", "tok")
    monkeypatch.setenv("SEEDBASE_PROJECT", "proj-1")
    monkeypatch.setenv("SEEDBASE_SEED", "7")
    pytester.makeconftest(
        """
        import pytest
        from seedbase import pytest_plugin
        from seedbase.sdk import SeedbaseClient

        class FakeClient:
            def __init__(self, *a, **k):
                self.generate_calls = []
                self.download_calls = []

            def generate(self, project, *, seed=None, rows=None, fmt=None, wait=False, timeout=None):
                self.generate_calls.append((project, seed, rows, fmt, wait, timeout))
                return {"generation_id": "gen-9", "status": "completed"}

            def download(self, generation_id, fmt=None):
                self.download_calls.append((generation_id, fmt))
                return b"-- seeded sql dump"

        @pytest.fixture(scope="session")
        def seedbase_client(pytestconfig):
            return FakeClient()
        """
    )
    pytester.makepyfile(
        """
        def test_generation_is_completed(seedbase_generation):
            assert seedbase_generation["status"] == "completed"
            assert seedbase_generation["generation_id"] == "gen-9"

        def test_seeded_data_bytes(seeded_data):
            assert seeded_data == b"-- seeded sql dump"
        """
    )
    result = pytester.runpytest_subprocess("-p", "seedbase.pytest_plugin")
    result.assert_outcomes(passed=2)
