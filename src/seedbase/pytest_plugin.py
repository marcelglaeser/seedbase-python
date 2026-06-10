from __future__ import annotations

import os
from typing import Any

import pytest

from .sdk import (
    API_URL,
    DEFAULT_GENERATION_TIMEOUT,
    DEFAULT_REQUEST_TIMEOUT,
    SeedbaseClient,
    SeedbaseError,
    _validate_api_url,
)

_ENV_TOKEN = "SEEDBASE_TOKEN"
_ENV_API_URL = "SEEDBASE_API_URL"
_ENV_TIMEOUT = "SEEDBASE_TIMEOUT"
_ENV_PROJECT = "SEEDBASE_PROJECT"
_ENV_SEED = "SEEDBASE_SEED"
_ENV_ROWS = "SEEDBASE_ROWS"
_ENV_FORMAT = "SEEDBASE_FORMAT"
_ENV_GENERATION_TIMEOUT = "SEEDBASE_GENERATION_TIMEOUT"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("seedbase", "seeded test data from SeedBase")
    group.addoption(
        "--seedbase-project",
        action="store",
        dest="seedbase_project",
        default=None,
        help="SeedBase project/dataset id to pull seeded data from.",
    )
    group.addoption(
        "--seedbase-seed",
        action="store",
        dest="seedbase_seed",
        default=None,
        help="Deterministic seed for the generation.",
    )
    parser.addini("seedbase_api_url", "SeedBase API base URL.", default="")
    parser.addini("seedbase_project", "SeedBase project/dataset id.", default="")
    parser.addini("seedbase_seed", "Deterministic generation seed.", default="")
    parser.addini("seedbase_rows", "Rows per table for the generation.", default="")
    parser.addini("seedbase_format", "Export format for downloads.", default="")
    parser.addini(
        "seedbase_generation_timeout",
        "Seconds to wait for a generation to complete.",
        default="",
    )


def _resolve(config: pytest.Config, option: str, ini: str, env: str) -> str | None:
    value = config.getoption(option, default=None) if option else None
    if value:
        return str(value)
    ini_value = config.getini(ini) if ini else ""
    if ini_value:
        return str(ini_value)
    env_value = os.getenv(env)
    if env_value:
        return env_value
    return None


def _resolve_api_url(config: pytest.Config) -> str:
    api_url = (
        os.getenv(_ENV_API_URL)
        or (config.getini("seedbase_api_url") or "")
        or API_URL
    )
    return _validate_api_url(api_url)


def _resolve_project(config: pytest.Config) -> str | None:
    return _resolve(config, "seedbase_project", "seedbase_project", _ENV_PROJECT)


def _resolve_seed(config: pytest.Config) -> int | None:
    raw = _resolve(config, "seedbase_seed", "seedbase_seed", _ENV_SEED)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise SeedbaseError(f"Invalid seedbase seed: {raw!r}")


def _resolve_rows(config: pytest.Config) -> int | None:
    raw = (config.getini("seedbase_rows") or "") or os.getenv(_ENV_ROWS)
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise SeedbaseError(f"Invalid seedbase rows: {raw!r}")


def _resolve_format(config: pytest.Config) -> str | None:
    return (config.getini("seedbase_format") or "") or os.getenv(_ENV_FORMAT) or None


def _resolve_generation_timeout(config: pytest.Config) -> int:
    raw = (config.getini("seedbase_generation_timeout") or "") or os.getenv(_ENV_GENERATION_TIMEOUT)
    if not raw:
        return DEFAULT_GENERATION_TIMEOUT
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise SeedbaseError(f"Invalid seedbase generation timeout: {raw!r}")


def _resolve_timeout() -> float:
    raw = os.getenv(_ENV_TIMEOUT)
    if not raw:
        return DEFAULT_REQUEST_TIMEOUT
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise SeedbaseError(f"Invalid seedbase timeout: {raw!r}")


@pytest.fixture(scope="session")
def seedbase_client(pytestconfig: pytest.Config) -> SeedbaseClient:
    token = os.getenv(_ENV_TOKEN)
    api_url = _resolve_api_url(pytestconfig)
    try:
        return SeedbaseClient(token=token, api_url=api_url, request_timeout=_resolve_timeout())
    except SeedbaseError as exc:
        pytest.skip(
            f"SeedBase credentials missing: {exc}. "
            f"Set {_ENV_TOKEN} (and optionally {_ENV_API_URL}) to enable seeded-data fixtures."
        )


@pytest.fixture(scope="session")
def seedbase_project(pytestconfig: pytest.Config) -> str:
    project = _resolve_project(pytestconfig)
    if not project:
        pytest.skip(
            f"No SeedBase project configured. Set {_ENV_PROJECT}, "
            "ini 'seedbase_project', or --seedbase-project."
        )
    return project


@pytest.fixture(scope="session")
def seedbase_generation(
    pytestconfig: pytest.Config,
    seedbase_client: SeedbaseClient,
    seedbase_project: str,
) -> dict[str, Any]:
    seed = _resolve_seed(pytestconfig)
    rows = _resolve_rows(pytestconfig)
    fmt = _resolve_format(pytestconfig)
    timeout = _resolve_generation_timeout(pytestconfig)
    try:
        return seedbase_client.generate(
            seedbase_project,
            seed=seed,
            rows=rows,
            fmt=fmt,
            wait=True,
            timeout=timeout,
        )
    except SeedbaseError as exc:
        pytest.fail(f"SeedBase generation failed: {exc}", pytrace=False)


@pytest.fixture(scope="session")
def seeded_data(
    pytestconfig: pytest.Config,
    seedbase_client: SeedbaseClient,
    seedbase_generation: dict[str, Any],
) -> bytes:
    generation_id = (
        seedbase_generation.get("generation_id")
        or seedbase_generation.get("id")
    )
    if not generation_id:
        pytest.fail("SeedBase generation has no id to download.", pytrace=False)
    fmt = _resolve_format(pytestconfig)
    try:
        return seedbase_client.download(str(generation_id), fmt=fmt)
    except SeedbaseError as exc:
        pytest.fail(f"SeedBase download failed: {exc}", pytrace=False)
