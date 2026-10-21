"""The HTTP client: site routing, auth headers, error mapping, and keys kept out of errors."""

import time

import pytest
from fake_datadog import API_KEY, APP_KEY, FakeDatadog, client_for

from cognee_community_connector_datadog.datadog import (
    DatadogAccessError,
    DatadogAPIError,
    DatadogClient,
    DatadogNotFoundError,
    DatadogRateLimitedError,
)


def test_each_site_is_reached_on_its_own_api_host():
    eu = FakeDatadog(site="datadoghq.eu")
    eu.add_monitor(1, "EU monitor")

    pages = list(client_for(eu, site="datadoghq.eu").pages("monitors"))

    assert [m["name"] for page in pages for m in page] == ["EU monitor"]


def test_keys_from_another_site_are_refused():
    us = FakeDatadog(site="datadoghq.com")
    session_client = client_for(us, site="datadoghq.eu")

    with pytest.raises(DatadogAccessError):
        list(session_client.pages("monitors"))


@pytest.mark.parametrize("site", ["datadoghq.co", "evil.example.com", "", "app.datadoghq.com"])
def test_an_unknown_site_is_rejected_before_any_key_is_sent(site):
    with pytest.raises(ValueError, match="Unknown Datadog site"):
        DatadogClient(API_KEY, APP_KEY, site)


@pytest.mark.parametrize(
    ("api_key", "app_key"),
    [("", APP_KEY), (API_KEY, ""), ("bad key", APP_KEY), (API_KEY, "bad\nkey"), (API_KEY, None)],
)
def test_a_malformed_key_is_rejected_without_echoing_it(api_key, app_key):
    with pytest.raises(ValueError) as error:
        DatadogClient(api_key, app_key)
    assert "bad" not in str(error.value)


def test_the_client_repr_hides_the_keys():
    text = repr(client_for(FakeDatadog()))
    assert API_KEY not in text and APP_KEY not in text


def test_a_missing_item_raises_not_found():
    with pytest.raises(DatadogNotFoundError):
        client_for(FakeDatadog()).get("monitors", "404")


def test_an_item_id_is_quoted_into_the_path():
    fake = FakeDatadog()
    with pytest.raises(DatadogNotFoundError):
        client_for(fake).get("monitors", "1/../../v2/incidents")

    assert fake.calls == ["GET /api/v1/monitor/1%2F..%2F..%2Fv2%2Fincidents"]


def test_a_wrapped_item_is_unwrapped():
    fake = FakeDatadog()
    fake.add_slo("slo1", "Checkout availability")

    assert client_for(fake).get("slos", "slo1")["name"] == "Checkout availability"


@pytest.mark.parametrize(
    ("status", "error"),
    [(403, DatadogAccessError), (429, DatadogRateLimitedError)],
)
def test_error_statuses_map_to_their_errors_without_keys_in_the_message(status, error):
    fake = FakeDatadog()
    fake.add_monitor(1, "Checkout latency")
    fake.statuses["list:monitors"] = status
    fake.statuses["get:monitors"] = status
    client = client_for(fake)

    with pytest.raises(error) as listed:
        list(client.pages("monitors"))
    with pytest.raises(error) as fetched:
        client.get("monitors", "1")
    for raised in (listed, fetched):
        assert API_KEY not in str(raised.value) and APP_KEY not in str(raised.value)


def test_a_server_error_is_retried_twice_and_a_long_retry_after_is_capped(monkeypatch):
    fake = FakeDatadog()
    fake.statuses["list:monitors"] = 503
    fake.headers["list:monitors"] = {"Retry-After": "86400"}
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    # the default session carries dlt's retry; the fake replaces only its transport
    client = DatadogClient(API_KEY, APP_KEY)
    client._rest.session.mount("https://", fake)

    with pytest.raises(DatadogAPIError):
        list(client.pages("monitors"))

    assert len(fake.calls) == 3
    assert sleeps and max(sleeps) <= 60, "cognee's staging lock is held while it sleeps"


def test_a_429_is_not_retried(monkeypatch):
    fake = FakeDatadog()
    fake.statuses["list:monitors"] = 429
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    client = DatadogClient(API_KEY, APP_KEY)
    client._rest.session.mount("https://", fake)

    with pytest.raises(DatadogRateLimitedError):
        list(client.pages("monitors"))

    assert len(fake.calls) == 1, "a spent window stops the kind instead of waiting it out"


def test_the_rate_limit_window_is_read_per_endpoint():
    fake = FakeDatadog()
    fake.add_dashboard("abc-123", "Checkout")
    fake.remaining["list:dashboards"] = 42
    fake.remaining["get:dashboards"] = 7
    client = client_for(fake)

    list(client.pages("dashboards"))
    client.get("dashboards", "abc-123")

    assert client.rate_limit == {"list:dashboards": 42, "get:dashboards": 7}
