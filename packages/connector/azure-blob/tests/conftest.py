"""Isolate Azure configuration for the offline helper and sync tests."""

import pytest


@pytest.fixture(autouse=True)
def clear_azure_credentials(monkeypatch):
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")
    for name in (
        "AZURE_STORAGE_ACCOUNT_URL",
        "AZURE_STORAGE_SAS_TOKEN",
        "AZURE_STORAGE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
