"""The real TriggerDevClient against the HTTP-level fake: URLs, headers, errors."""

import logging

import pytest
import requests
from fake_triggerdev import KEY, FakeResponse, FakeTriggerDev

from cognee_community_connector_triggerdev import triggerdev
from cognee_community_connector_triggerdev.triggerdev import (
    TriggerDevAccessError,
    TriggerDevAPIError,
    TriggerDevAuthError,
    TriggerDevClient,
    TriggerDevNotFoundError,
    TriggerDevRateLimitedError,
    _iter_rows,
)


def _client(sleeps: list | None = None) -> TriggerDevClient:
    return TriggerDevClient(KEY, sleep=(sleeps.append if sleeps is not None else lambda _: None))


def _answer(monkeypatch, *responses):
    """Make the connector's requests.get answer with these responses in turn."""
    queue = list(responses)
    calls = []

    class _Stub:
        RequestException = requests.RequestException

        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            calls.append(url)
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr(triggerdev, "requests", _Stub)
    return calls


def test_a_full_sync_only_calls_documented_paths_and_parameters(monkeypatch):
    fake = FakeTriggerDev()
    fake.add_schedule("sched_1", "nightly-backup")
    fake.add_error("error_1", "send-invoice")
    fake.add_run("run_1", "send-invoice")
    fake.add_deployment("deployment_1", "20261028.1")
    fake.install(monkeypatch)

    rows = list(_iter_rows(_client(), {}, {}))

    assert {row["id"] for row in rows} == {
        "schedule:sched_1",
        "error:error_1",
        "run:run_1",
        "deployment:deployment_1",
    }
    assert fake.paths() == [
        "/api/v1/schedules",
        "/api/v1/errors",
        "/api/v1/runs",
        "/api/v3/runs/run_1",
        "/api/v1/deployments",
    ]
    params = dict(fake.calls)
    assert params["/api/v1/schedules"] == {"page": 1, "perPage": 100}
    assert params["/api/v1/runs"]["page[size]"] == 100
    assert params["/api/v1/runs"]["filter[status]"] == "FAILED,CRASHED,SYSTEM_FAILURE,INTERRUPTED"
    assert params["/api/v1/runs"]["filter[createdAt][from]"].endswith("Z")
    assert set(params["/api/v1/errors"]) == {"page[size]", "filter[from]"}
    assert set(params["/api/v1/deployments"]) == {"page[size]", "from"}


@pytest.mark.parametrize(
    "api_url",
    [
        "https://api.trigger.dev/api/v1",  # a path would be prefixed to every request path
        "http://api.trigger.dev",
        "https://user:secret@api.trigger.dev",
        "https://api.trigger.dev?x=1",
        "api.trigger.dev",
        "",
    ],
)
def test_api_url_must_be_an_https_origin(api_url):
    with pytest.raises(ValueError, match="api_url"):
        TriggerDevClient(KEY, api_url=api_url)


def test_a_self_hosted_origin_and_local_http_are_accepted():
    assert TriggerDevClient(KEY, api_url="https://jobs.acme.dev/").api_url == (
        "https://jobs.acme.dev"
    )
    assert TriggerDevClient(KEY, api_url="http://localhost:3030").api_url == (
        "http://localhost:3030"
    )


@pytest.mark.parametrize("key", ["", "   ", "tr_prod\nsk_abc", "tr prod", "tr_prod_ключ"])
def test_a_malformed_key_is_refused_without_being_echoed(key):
    with pytest.raises(ValueError) as caught:
        TriggerDevClient(key)
    assert key.strip() == "" or key.strip() not in str(caught.value)


def test_a_pasted_key_with_a_trailing_newline_is_trimmed(monkeypatch):
    fake = FakeTriggerDev()
    fake.install(monkeypatch)
    TriggerDevClient(KEY + "\n").request("/api/v1/schedules", params={"page": 1})
    assert fake.paths() == ["/api/v1/schedules"]


def test_the_key_is_never_in_repr_or_errors(monkeypatch):
    client = _client()
    assert KEY not in repr(client)
    _answer(monkeypatch, FakeResponse(500, "<html>key=" + KEY + "</html>"))
    with pytest.raises(TriggerDevAPIError) as caught:
        client.request("/api/v1/errors")
    assert KEY not in str(caught.value)
    assert "html" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, TriggerDevAuthError),
        (403, TriggerDevAccessError),
        (404, TriggerDevNotFoundError),
        (429, TriggerDevRateLimitedError),
        (400, TriggerDevAPIError),
    ],
)
def test_statuses_map_to_errors(monkeypatch, status, error):
    _answer(monkeypatch, FakeResponse(status, {"error": "nope"}))
    with pytest.raises(error):
        _client().request("/api/v1/runs")


def test_a_record_id_never_reaches_an_error_message(monkeypatch):
    _answer(monkeypatch, FakeResponse(403, {"error": "forbidden"}))
    with pytest.raises(TriggerDevAccessError) as caught:
        _client().request("/api/v3/runs/run_secret_customer_77")
    assert "run_secret_customer_77" not in str(caught.value)
    assert "/api/v3/runs/{id}" in str(caught.value)


def test_a_200_that_is_not_a_json_object_is_refused(monkeypatch):
    _answer(monkeypatch, FakeResponse(200, "<html>login</html>"))
    with pytest.raises(TriggerDevAPIError, match="unreadable"):
        _client().request("/api/v1/runs")
    _answer(monkeypatch, FakeResponse(200, [1, 2]))
    with pytest.raises(TriggerDevAPIError, match="unreadable"):
        _client().request("/api/v1/runs")


def test_server_errors_are_retried_with_backoff_then_raised(monkeypatch):
    sleeps: list[float] = []
    calls = _answer(monkeypatch, FakeResponse(502, "bad gateway"))
    with pytest.raises(TriggerDevAPIError, match="HTTP 502"):
        _client(sleeps).request("/api/v1/runs")
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_a_network_error_is_retried_and_reported_by_type_only(monkeypatch):
    sleeps: list[float] = []
    _answer(
        monkeypatch,
        requests.ConnectionError("https://api.trigger.dev " + KEY),
        FakeResponse(200, {"data": [], "pagination": {}}),
    )
    assert _client(sleeps).request("/api/v1/runs") == {"data": [], "pagination": {}}
    assert sleeps == [1]

    _answer(monkeypatch, requests.ConnectionError("down " + KEY))
    with pytest.raises(TriggerDevAPIError) as caught:
        _client().request("/api/v1/runs")
    assert str(caught.value) == "Trigger.dev request failed: ConnectionError"


def test_a_429_is_not_slept_through(monkeypatch):
    sleeps: list[float] = []
    calls = _answer(
        monkeypatch, FakeResponse(429, {"error": "slow down"}, {"retry-after": "86400"})
    )
    with pytest.raises(TriggerDevRateLimitedError):
        _client(sleeps).request("/api/v1/runs")
    assert sleeps == [] and len(calls) == 1


def test_the_remaining_quota_header_is_recorded(monkeypatch):
    client = _client()
    _answer(
        monkeypatch,
        FakeResponse(200, {"data": [], "pagination": {}}, {"x-ratelimit-remaining": "1200"}),
    )
    client.request("/api/v1/runs")
    assert client.rate_limit == {"remaining": 1200}


def test_warnings_carry_no_key(monkeypatch, caplog):
    fake = FakeTriggerDev()
    fake.add_run("run_1", "send-invoice")
    fake.fail["/api/v3/runs/run_1"] = 500
    fake.install(monkeypatch)
    with caplog.at_level(logging.WARNING):
        list(_iter_rows(_client(), {}, {}, include=("runs",)))
    assert KEY not in caplog.text
    assert "run_1" not in caplog.text
