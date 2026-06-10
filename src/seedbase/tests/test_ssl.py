from __future__ import annotations

import ssl
import urllib.error

import pytest

from seedbase import _ssl, cli, sdk
from seedbase.cli import CLIError
from seedbase.sdk import SeedbaseClient, SeedbaseError


def test_create_ssl_context_returns_verifying_context():
    ctx = _ssl.create_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_create_ssl_context_uses_certifi_when_available(monkeypatch):
    import types

    sentinel = "/fake/path/to/cacert.pem"
    captured = {}

    fake_certifi = types.SimpleNamespace(where=lambda: sentinel)
    monkeypatch.setitem(__import__("sys").modules, "certifi", fake_certifi)

    def fake_create_default_context(*, cafile=None):
        captured["cafile"] = cafile
        return ssl.create_default_context()

    monkeypatch.setattr(_ssl.ssl, "create_default_context", fake_create_default_context)

    _ssl.create_ssl_context()
    assert captured["cafile"] == sentinel


def test_create_ssl_context_without_certifi(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "certifi":
            raise ImportError("no certifi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    captured = {}

    def fake_create_default_context(*, cafile=None):
        captured["cafile"] = cafile
        return ssl.create_default_context()

    monkeypatch.setattr(_ssl.ssl, "create_default_context", fake_create_default_context)

    _ssl.create_ssl_context()
    assert captured["cafile"] is None


def test_is_cert_verify_error_detects_wrapped_ssl_error():
    inner = ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")
    wrapped = urllib.error.URLError(inner)
    assert _ssl.is_cert_verify_error(wrapped)


def test_is_cert_verify_error_ignores_plain_network_error():
    assert not _ssl.is_cert_verify_error(urllib.error.URLError("Connection refused"))


def test_sdk_ssl_error_gives_certifi_hint(monkeypatch):
    client = SeedbaseClient(token="tok", api_url="https://example.test/api/v1")

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))

    monkeypatch.setattr(sdk.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SeedbaseError) as exc:
        client.list_projects()
    assert "certifi" in str(exc.value)
    assert "CERTIFICATE_VERIFY_FAILED" not in str(exc.value)


def test_sdk_passes_ssl_context_to_urlopen(monkeypatch):
    client = SeedbaseClient(token="tok", api_url="https://example.test/api/v1")
    seen = {}

    class _Resp:
        def read(self, *a):
            return b"{\"results\": []}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["context"] = context
        return _Resp()

    monkeypatch.setattr(sdk.urllib.request, "urlopen", fake_urlopen)
    client.list_projects()
    assert isinstance(seen["context"], ssl.SSLContext)


def test_cli_ssl_error_gives_certifi_hint(monkeypatch):
    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(CLIError) as exc:
        cli._api("GET", "https://example.test/api/v1", "/datasets/", token="tok")
    assert "certifi" in str(exc.value)
