from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ._ssl import CERTIFI_HINT, create_ssl_context, is_cert_verify_error

API_URL = "https://seedba.se/api/v1"
CONFIG_PATH = Path.home() / ".seedbase" / "config.json"
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_GENERATION_TIMEOUT = 300
_MAX_PAGES = 50
_LOCAL_API_HOSTS = {"localhost", "127.0.0.1", "::1"}


class SeedbaseError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _validate_api_url(api_url: str) -> str:
    parsed = urllib.parse.urlparse(api_url)
    if parsed.scheme == "https":
        return api_url
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host in _LOCAL_API_HOSTS:
        return api_url
    raise SeedbaseError(
        f"Insecure API URL '{api_url}' — only https:// is allowed "
        "(http:// is only accepted for localhost/127.0.0.1/::1)."
    )


def _load_token_from_config(config_path: Path) -> str | None:
    if not config_path.exists():
        return None
    try:
        content = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("token")
    return str(token) if token else None


def _auth_header(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    if token.startswith("dr_sk_"):
        return {"Authorization": f"Bearer {token}"}
    return {"Authorization": f"Token {token}"}


def _extract_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        rows = payload.get("results")
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    return []


def _next_page_path(next_url: str, api_url: str) -> str:
    if next_url.startswith(api_url):
        return next_url[len(api_url):]
    parsed = urllib.parse.urlparse(next_url)
    rel = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    base_path = urllib.parse.urlparse(api_url).path
    if base_path and rel.startswith(base_path):
        rel = rel[len(base_path):]
    return rel


def _http_error_message(code: int, text: str) -> str:
    parsed: Any = None
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    if detail:
        return str(detail)
    if isinstance(parsed, (dict, list)) and parsed:
        snippet = json.dumps(parsed, ensure_ascii=False)
    else:
        snippet = text.strip()
    snippet = snippet[:300]
    if snippet:
        return f"API request failed ({code}): {snippet}"
    return f"API request failed ({code})"


class SeedbaseClient:
    def __init__(
        self,
        token: str | None = None,
        api_url: str = API_URL,
        config_path: Path | str | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        path = Path(config_path) if config_path is not None else CONFIG_PATH
        resolved = (
            token
            or os.getenv("SEEDBASE_TOKEN")
            or _load_token_from_config(path)
        )
        if not resolved:
            raise SeedbaseError(
                "No token provided. Pass token=..., set SEEDBASE_TOKEN, or run 'seedbase login'."
            )
        self.token = resolved
        self.api_url = _validate_api_url(str(api_url).rstrip("/"))
        self.request_timeout = float(request_timeout)
        self._ssl_context = create_ssl_context()

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        rel = path if path.startswith("/") else f"/{path}"
        url = f"{self.api_url}{rel}"

        headers = dict(_auth_header(self.token))
        data = None
        if not raw:
            headers["Accept"] = "application/json"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url=url, method=method.upper(), headers=headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout, context=self._ssl_context) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            text = body.decode("utf-8", errors="replace") if body else ""
            raise SeedbaseError(
                _http_error_message(exc.code, text),
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            if is_cert_verify_error(exc):
                raise SeedbaseError(CERTIFI_HINT) from exc
            raise SeedbaseError(f"Network error: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise SeedbaseError(f"Network error: {exc}") from exc

        if raw:
            return body
        text = body.decode("utf-8", errors="replace")
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SeedbaseError(f"Server returned an unexpected non-JSON response from {url}") from exc

    def _request_list(self, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        next_path: str | None = path
        for _ in range(_MAX_PAGES):
            if not next_path:
                break
            data = self._request("GET", next_path)
            rows.extend(_extract_results(data))
            next_url = data.get("next") if isinstance(data, dict) else None
            next_path = _next_page_path(str(next_url), self.api_url) if next_url else None
        return rows

    def list_projects(self) -> list[dict[str, Any]]:
        return self._request_list("/datasets/")

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", f"/datasets/{project_id}/")

    def list_generations(self, project_id: str) -> list[dict[str, Any]]:
        return self._request_list(f"/generations/?dataset={urllib.parse.quote(project_id)}")

    def get_generation(self, generation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/generations/{generation_id}/")

    def generate(
        self,
        project_id: str,
        *,
        seed: int | None = None,
        rows: int | None = None,
        fmt: str | None = None,
        rebase_to: str | None = None,
        wait: bool = False,
        timeout: int = DEFAULT_GENERATION_TIMEOUT,
        poll_interval: float = 2.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if seed is not None:
            payload["seed"] = seed
        if rows is not None:
            payload["rows_per_table"] = max(1, rows)
        if fmt:
            payload["format"] = fmt
        if rebase_to is not None:
            payload["rebase_to"] = rebase_to

        create = self._request("POST", f"/datasets/{project_id}/generate/", payload=payload)
        generation_id = create.get("generation_id")
        if not generation_id:
            raise SeedbaseError("Generation did not return an id")

        if not wait:
            return create

        deadline = time.time() + max(1, timeout)
        consecutive_errors = 0
        while True:
            status_data = None
            try:
                status_data = self.get_generation(generation_id)
                consecutive_errors = 0
            except SeedbaseError:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    raise
            if status_data is not None:
                status = status_data.get("status")
                if status in {"completed", "failed", "cancelled"}:
                    if status != "completed":
                        raise SeedbaseError(f"Generation {status}")
                    return status_data
            if time.time() >= deadline:
                raise SeedbaseError("Generation timed out")
            time.sleep(poll_interval)

    def export_config(self, project_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/datasets/{project_id}/export-config/")
        config = data.get("engine_config") if isinstance(data, dict) else None
        return config if isinstance(config, dict) else {}

    def import_config(self, project_id: str, config: dict[str, Any]) -> dict[str, Any]:
        data = self._request(
            "POST",
            f"/datasets/{project_id}/import-config/",
            payload={"engine_config": config},
        )
        result = data.get("engine_config") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}

    def download(self, generation_id: str, fmt: str | None = None) -> bytes:
        export_format = fmt or "sql"
        path = f"/generations/{generation_id}/download/?export_format={urllib.parse.quote(export_format)}"
        return self._request("GET", path, raw=True)
