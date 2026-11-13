import pytest

from cognee_community_connector_airtable import airtable as connector


@pytest.fixture(autouse=True)
def fast_http(monkeypatch):
    """Exercise real retries without waiting for simulated network requests."""
    waits = []
    monkeypatch.setattr(connector, "_REQUEST_INTERVAL", 0)
    monkeypatch.setattr(connector.time, "sleep", waits.append)
    return waits
