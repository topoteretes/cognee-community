"""Keep the connector acceptance suite offline, including library telemetry."""

import os

import pytest

os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"
os.environ["TELEMETRY_DISABLED"] = "true"


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    import httpx
    import requests

    def blocked(*args, **kwargs):
        raise AssertionError("Live HTTP is disabled in Airtable connector tests.")

    async def blocked_async(*args, **kwargs):
        raise AssertionError("Live HTTP is disabled in Airtable connector tests.")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(httpx.Client, "send", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", blocked_async)
