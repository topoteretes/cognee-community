"""The HTTP client: auth header, pagination, rate limits, retries and error hygiene."""

import pytest
import responses
from fake_fathom import FakeFathom

from cognee_community_connector_fathom.fathom import (
    BASE_URL,
    FathomAPIError,
    FathomAuthError,
    FathomClient,
    FathomRateLimitedError,
)

KEY = "SECRET-fathom-key-123"


def _client(sleeps: list[float] | None = None) -> FathomClient:
    return FathomClient(KEY, sleep=(sleeps if sleeps is not None else []).append)


@pytest.fixture
def fake():
    api = FakeFathom()
    with responses.RequestsMock() as rsps:
        api.install(rsps)
        yield api


def test_every_request_carries_the_api_key_header(fake):
    fake.add(1)
    list(_client().list_meetings())

    assert [r["key"] for r in fake.requests] == [KEY]


def test_follows_next_cursor_to_the_last_page(fake):
    for recording_id in range(1, 26):
        fake.add(recording_id, days_ago=recording_id)

    ids = [m["recording_id"] for m in _client().list_meetings()]

    assert ids == list(range(1, 26))  # newest first, 10 per page
    assert len(fake.requests) == 3
    assert "cursor" not in fake.requests[0]["params"]
    assert "cursor" in fake.requests[1]["params"]


def test_list_filters_use_the_bracket_names_and_repeat(fake):
    fake.add(1)
    list(_client().list_meetings({"teams[]": ["Sales", "Eng"], "include_action_items": "true"}))

    params = fake.requests[0]["params"]
    assert params["teams[]"] == ["Sales", "Eng"]
    assert params["include_action_items"] == "true"


def test_a_repeating_cursor_is_an_error_not_an_endless_loop():
    page = {"limit": 10, "next_cursor": "same", "items": [{"recording_id": 1}]}
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{BASE_URL}/meetings", json=page)
        with pytest.raises(FathomAPIError, match="repeats"):
            list(_client().list_meetings())


def test_429_waits_for_retry_after_then_succeeds(fake):
    fake.add(1)
    fake.script = [429, 429]
    fake.retry_after = "7"
    sleeps: list[float] = []

    assert [m["recording_id"] for m in _client(sleeps).list_meetings()] == [1]
    assert sleeps == [7.0, 7.0]


def test_429_without_a_header_backs_off_and_a_huge_header_is_clamped(fake):
    fake.add(1)
    fake.script = [429, 429]
    fake.retry_after = None
    sleeps: list[float] = []
    list(_client(sleeps).list_meetings())
    assert sleeps == [2.0, 4.0]

    fake.script = [429]
    fake.retry_after = "86400"
    sleeps.clear()
    list(_client(sleeps).list_meetings())
    assert sleeps == [120.0]


def test_a_429_that_never_clears_raises_after_the_retry_budget(fake):
    fake.script = [429] * 50
    sleeps: list[float] = []

    with pytest.raises(FathomRateLimitedError):
        list(_client(sleeps).list_meetings())
    assert len(sleeps) == 6


def test_5xx_is_retried_with_backoff_then_raises(fake):
    fake.add(1)
    fake.script = [503, 500]
    sleeps: list[float] = []
    assert len(list(_client(sleeps).list_meetings())) == 1
    assert sleeps == [1.0, 2.0]

    fake.script = [502] * 10
    with pytest.raises(FathomAPIError, match="HTTP 502"):
        list(_client().list_meetings())


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_not_retried_and_never_echoed(fake, status):
    fake.script = [status]
    sleeps: list[float] = []

    with pytest.raises(FathomAuthError) as raised:
        list(_client(sleeps).list_meetings())

    assert sleeps == []
    assert len(fake.requests) == 1
    assert KEY not in str(raised.value)


def test_other_4xx_raises_without_retry(fake):
    fake.script = [400]
    with pytest.raises(FathomAPIError, match="HTTP 400"):
        list(_client().list_meetings())
    assert len(fake.requests) == 1


def test_network_errors_are_retried_and_do_not_leak_the_request():
    import requests

    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{BASE_URL}/meetings", body=requests.ConnectionError(KEY))
        sleeps: list[float] = []
        with pytest.raises(FathomAPIError) as raised:
            list(_client(sleeps).list_meetings())
    assert sleeps == [1.0, 2.0, 4.0]
    assert KEY not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("body", ["not json", "[]", '{"limit": 10}'])
def test_malformed_payloads_raise(body):
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{BASE_URL}/meetings", body=body)
        with pytest.raises(FathomAPIError):
            list(_client().list_meetings())


@pytest.mark.parametrize("key", ["", "   ", "bad key", "k\u00e9y", None])
def test_a_malformed_key_is_refused_without_echoing_it(key):
    with pytest.raises(ValueError) as raised:
        FathomClient(key)
    assert "FATHOM_API_KEY" in str(raised.value)


def test_repr_does_not_contain_the_key():
    assert KEY not in repr(_client())
