from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_URL = "https://seedba.se/api/v1"
CONFIG_PATH = Path.home() / ".seedbase" / "config.json"


class SeedbaseError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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


class SeedbaseClient:
    def __init__(
        self,
        token: str | None = None,
        api_url: str = API_URL,
        config_path: Path | str | None = None,
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
        self.api_url = str(api_url).rstrip("/")

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
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                if raw:
                    return body
                text = body.decode("utf-8")
                if not text.strip():
                    return {}
                return json.loads(text)
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            text = body.decode("utf-8", errors="replace") if body else ""
            detail = None
            if text:
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        detail = parsed.get("detail")
                except json.JSONDecodeError:
                    detail = text.strip() or None
            raise SeedbaseError(
                detail or f"API request failed ({exc.code})",
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise SeedbaseError(f"Network error: {exc.reason}") from exc

    def list_projects(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/datasets/")
        return _extract_results(data)

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", f"/datasets/{project_id}/")

    def list_generations(self, project_id: str) -> list[dict[str, Any]]:
        path = f"/generations/?dataset={urllib.parse.quote(project_id)}"
        data = self._request("GET", path)
        return _extract_results(data)

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
        timeout: int = 120,
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
        while time.time() < deadline:
            time.sleep(poll_interval)
            status_data = self.get_generation(generation_id)
            status = status_data.get("status")
            if status in {"completed", "failed", "cancelled"}:
                if status != "completed":
                    raise SeedbaseError(f"Generation {status}")
                return status_data
        raise SeedbaseError("Generation timed out")

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
