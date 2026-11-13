"""Provider resolution is offline and errors must never expose secret values."""

import asyncio
import json
import os
import subprocess
import sys
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import dlt
import httpx2
import pytest

from cognee_community_connector_logrocket.logrocket import (
    LogRocketMCPError,
    _CredentialError,
    _LogRocketMCPClient,
    _resolve_api_key,
)
from examples.diagnostics import LogRocketRESTClient


def test_explicit_key_wins_without_reading_other_providers(monkeypatch):
    monkeypatch.setenv("LOGROCKET_API_KEY", "environment-fixture")
    getter = Mock(side_effect=AssertionError("DLT should not be consulted"))
    monkeypatch.setattr(dlt, "secrets", SimpleNamespace(get=getter))
    assert _resolve_api_key("explicit-fixture") == "explicit-fixture"
    getter.assert_not_called()


@pytest.mark.parametrize("env_name", ["LOGROCKET_API_KEY", "CUSTOM_LOGROCKET_KEY"])
def test_selected_environment_wins_over_dlt(monkeypatch, env_name):
    monkeypatch.setenv(env_name, "environment-fixture")
    getter = Mock(side_effect=AssertionError("DLT should not be consulted"))
    monkeypatch.setattr(dlt, "secrets", SimpleNamespace(get=getter))
    assert _resolve_api_key(None, env_name) == "environment-fixture"
    getter.assert_not_called()


def test_fallback_uses_namespaced_dlt_secret(monkeypatch):
    monkeypatch.delenv("LOGROCKET_API_KEY", raising=False)
    getter = Mock(return_value="provider-fixture")
    monkeypatch.setattr(dlt, "secrets", SimpleNamespace(get=getter))
    assert _resolve_api_key(None) == "provider-fixture"
    getter.assert_called_once_with("sources.logrocket.api_key")


@pytest.mark.parametrize(
    "value", ["", " ", " leading", "trailing ", "line\nbreak", "bad\x7f", 123, True]
)
def test_invalid_explicit_values_fail_without_fallback(monkeypatch, value):
    monkeypatch.setenv("LOGROCKET_API_KEY", "otherwise-valid")
    with pytest.raises(_CredentialError, match="invalid_api_key"):
        _resolve_api_key(value)


@pytest.mark.parametrize("value", [" ", 123, True])
def test_invalid_dlt_values_are_rejected(monkeypatch, value):
    monkeypatch.delenv("LOGROCKET_API_KEY", raising=False)
    monkeypatch.setattr(dlt, "secrets", SimpleNamespace(get=lambda _: value))
    with pytest.raises(_CredentialError, match="invalid_api_key"):
        _resolve_api_key(None)


def test_provider_errors_are_sanitized_for_both_clients(monkeypatch):
    monkeypatch.delenv("LOGROCKET_API_KEY", raising=False)
    private_detail = "synthetic-provider-secret"
    getter = Mock(side_effect=ValueError(private_detail))
    monkeypatch.setattr(dlt, "secrets", SimpleNamespace(get=getter))
    with pytest.raises(LogRocketMCPError, match="credential_provider_failure") as error:
        _LogRocketMCPClient("org", "project")._resolve_api_key()
    assert private_detail not in "".join(traceback.format_exception(error.value))
    report = asyncio.run(LogRocketRESTClient("org", "project").probe("audit", since_ms=0))
    assert report["error_code"] == "credential_provider_failure"
    assert private_detail not in json.dumps(report)


def test_rest_client_uses_dlt_key_with_rest_auth_scheme(monkeypatch):
    monkeypatch.delenv("LOGROCKET_API_KEY", raising=False)
    monkeypatch.setattr(dlt, "secrets", SimpleNamespace(get=lambda _: "provider-fixture"))

    def handler(request):
        assert request.headers["Authorization"] == "token provider-fixture"
        return httpx2.Response(200, json={"logs": [], "cursor": "", "has_next": False})

    client = LogRocketRESTClient("org", "project", transport=httpx2.MockTransport(handler))
    assert asyncio.run(client.probe("audit", since_ms=0))["status"] == "ok"


@pytest.mark.parametrize("provider", ["environment", "toml"])
def test_actual_dlt_providers_in_isolated_process(tmp_path, provider):
    env = dict(os.environ)
    for name in tuple(env):
        if "API_KEY" in name or name.startswith("DLT_"):
            env.pop(name)
    fixture_key = "synthetic-dlt-provider-fixture"
    if provider == "environment":
        env["SOURCES__LOGROCKET__API_KEY"] = fixture_key
    else:
        config_dir = tmp_path / ".dlt"
        config_dir.mkdir()
        (config_dir / "secrets.toml").write_text(
            f'[sources.logrocket]\napi_key = "{fixture_key}"\n', encoding="utf-8"
        )
    env["EXPECTED_FIXTURE_KEY"] = fixture_key
    env["PYTHON_DOTENV_DISABLED"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "from cognee_community_connector_logrocket.logrocket import _LogRocketMCPClient; "
                "assert _LogRocketMCPClient('org', 'project')._resolve_api_key() "
                "== os.environ['EXPECTED_FIXTURE_KEY']; print('PROVIDER_OK')"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, "Isolated DLT provider resolution failed; details withheld."
    assert result.stdout.strip() == "PROVIDER_OK"
