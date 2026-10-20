"""Synthetic documented envelopes; never a guessed deletion/session contract."""

import asyncio
import json
import traceback
from unittest.mock import AsyncMock

import httpx2
import pytest

from cognee_community_connector_logrocket.rest import (
    LogRocketRESTClient,
    LogRocketRESTError,
    parse_audit_page,
)


def row(**extra):
    return {
        "created_date": "2026-10-01T00:00:00Z",
        "user": "employee@example.invalid",
        "action": "Viewed session",
        "description": "private-session-id",
        **extra,
    }


def page(*records, cursor="opaque:next", has_next=False):
    return {"logs": list(records), "cursor": cursor, "has_next": has_next}


def client(handler, **kwargs):
    return LogRocketRESTClient(
        "org", "app", api_key="synthetic-secret", transport=httpx2.MockTransport(handler), **kwargs
    )


def run(awaitable):
    return asyncio.run(awaitable)


def test_audit_pagination_uses_opaque_cursors_and_terminal_signal():
    requests = []
    responses = [page(row(), cursor="1700:same-ms", has_next=True), page(cursor="1700:same-ms")]

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, json=responses.pop(0))

    batch = run(client(handler).read_audit_logs(cursor="1700", limit=2))
    assert batch.pages == 2
    assert batch.cursor == "1700:same-ms"
    assert batch.records == (row(),)
    assert [r.url.params["cursor"] for r in requests] == ["1700", "1700:same-ms"]
    assert all(r.method == "GET" for r in requests)
    assert all(r.headers["Authorization"] == "token synthetic-secret" for r in requests)
    assert requests[0].url.path == "/v1/orgs/org/apps/app/audit/logs/"
    assert "private-session-id" not in repr(batch)
    assert "1700:same-ms" not in repr(batch)


@pytest.mark.parametrize(
    ("responses", "budget", "code"),
    [
        ([page(row(), cursor="start", has_next=True)], 2, "audit_cursor_not_progressing"),
        ([page(cursor="next", has_next=True)], 2, "audit_cursor_not_progressing"),
        ([page(row(), cursor="next", has_next=True)], 1, "audit_page_budget_exceeded"),
        (
            [page(row(), cursor="next", has_next=True), page(row(), cursor="start", has_next=True)],
            3,
            "audit_cursor_not_progressing",
        ),
    ],
)
def test_incomplete_audit_traversal_never_returns_partial_batch(responses, budget, code):
    def handler(request):
        return httpx2.Response(200, json=responses.pop(0))

    with pytest.raises(LogRocketRESTError, match=code):
        run(client(handler).read_audit_logs(cursor="start", max_pages=budget))


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        page(cursor=None),
        page(cursor=True),
        page(cursor=1.2),
        page(has_next="false"),
        page(None),
        page({}),
        page(row(user=123)),
        {"logs": {}, "cursor": "next", "has_next": False},
        page(row(), row()),
    ],
)
def test_audit_envelope_fails_closed(payload):
    with pytest.raises(LogRocketRESTError):
        parse_audit_page(payload, limit=1)


def test_numeric_cursor_and_extra_fields_are_not_assumed_to_be_deletion_contract():
    parsed = parse_audit_page(page(row(deleted_ids=["x"]), cursor=1700), limit=1)
    assert parsed.cursor == "1700"
    assert "private-session-id" not in repr(parsed)
    report = run(
        client(
            lambda _: httpx2.Response(
                200,
                json=page(
                    row(deleted_ids=["x"], **{"private-dynamic-email@example.invalid": "value"})
                ),
            )
        ).probe("audit", since_ms=0)
    )
    rendered = json.dumps(report)
    for value in (
        "private-session-id",
        "employee@example.invalid",
        "private-dynamic",
        "deleted_ids",
    ):
        assert value not in rendered
    assert report["automatic_deletion_supported"] is False
    assert report["session_inventory_complete"] is False
    assert report["issue_inventory_complete"] is False
    assert report["audit_traversal_complete"] is True
    assert report["additional_record_fields_present"] is True


@pytest.mark.parametrize("status", [301, 401, 403, 404])
def test_permanent_failures_and_redirects_are_not_retried_or_followed(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(
            status,
            headers={"location": "https://elsewhere.invalid/"},
            text="synthetic-secret private-response",
        )

    with pytest.raises(LogRocketRESTError) as error:
        run(client(handler).audit_page(cursor="private-cursor"))
    assert error.value.status == status
    assert len(requests) == 1
    assert "synthetic-secret" not in str(error.value)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx2.Response(200, text="<html>private</html>"), "invalid_json"),
        (httpx2.Response(200, json=[]), "invalid_json_object"),
    ],
)
def test_http_success_does_not_imply_usable_data(response, code):
    report = run(client(lambda _: response).probe("session-exports"))
    assert report["status"] == "error"
    assert report["error_code"] == code
    assert report["http_status"] == 200


def test_transient_status_retries_are_bounded(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("cognee_community_connector_logrocket.rest.asyncio.sleep", sleep)
    statuses = [429, 503, 200]

    def handler(request):
        return httpx2.Response(statuses.pop(0), json=page())

    assert run(client(handler).audit_page(cursor="0")).records == ()
    assert sleep.await_count == 2
    with pytest.raises(LogRocketRESTError, match="transient_http_failure"):
        run(client(lambda _: httpx2.Response(503)).audit_page(cursor="0"))
    assert sleep.await_count == 4


def test_transport_failures_are_retried_and_sanitized_in_traceback(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("cognee_community_connector_logrocket.rest.asyncio.sleep", sleep)
    private_cursor = "-".join(["private", "cursor"])

    def handler(request):
        raise httpx2.ConnectError("synthetic-secret private-cursor", request=request)

    with pytest.raises(LogRocketRESTError, match="transport_failure") as error:
        run(client(handler).audit_page(cursor=private_cursor))
    assert sleep.await_count == 2
    rendered = "".join(traceback.format_exception(error.value))
    assert "synthetic-secret" not in rendered
    assert private_cursor not in rendered


def test_unexpected_transport_exception_is_sanitized():
    def handler(request):
        raise ValueError("synthetic-secret private-response")

    with pytest.raises(LogRocketRESTError, match="request_failure") as error:
        run(client(handler).audit_page(cursor="0"))
    assert "synthetic-secret" not in "".join(traceback.format_exception(error.value))


def test_second_page_failure_returns_no_partial_success():
    responses = [httpx2.Response(200, json=page(row(), has_next=True)), httpx2.Response(403)]
    report = run(client(lambda _: responses.pop(0)).probe("audit", since_ms=0, max_pages=2))
    assert report["status"] == "error"
    assert "record_count" not in report
    assert "audit_traversal_complete" not in report


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, {"limit": "5"}),
        ({"since_ms": 1700}, {"limit": "5", "date": "1700"}),
        ({"cursor": "opaque:2"}, {"limit": "5", "cursor": "opaque:2"}),
    ],
)
def test_export_listing_parameters_and_no_file_downloads(kwargs, expected):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "cursor": "opaque:next",
                "sessions": [{"url": "http://169.254.169.254/private?token=synthetic-secret"}],
            },
        )

    assert run(client(handler).export_page(**kwargs))["cursor"] == "opaque:next"
    assert len(requests) == 1
    assert dict(requests[0].url.params) == expected
    assert requests[0].url.path.endswith("/data-export/")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"sessions": [], "cursor": None},
        {"sessions": [None], "cursor": "next"},
        {"sessions": [{"url": 123}], "cursor": "next"},
    ],
)
def test_invalid_export_listing(payload):
    with pytest.raises(LogRocketRESTError):
        run(client(lambda _: httpx2.Response(200, json=payload)).export_page())


def test_empty_export_is_not_an_authoritative_inventory():
    report = run(
        client(lambda _: httpx2.Response(200, json={"cursor": "next", "sessions": []})).probe(
            "session-exports"
        )
    )
    assert report["record_count"] == 0
    assert report["session_inventory_complete"] is False
    assert report["automatic_deletion_supported"] is False
    assert report["export_completion_contract"] == "unverified"


def test_live_observed_empty_terminal_audit_cursor_preserves_start_position():
    batch = run(
        client(lambda _: httpx2.Response(200, json=page(cursor=""))).read_audit_logs(
            cursor="starting-position"
        )
    )
    assert batch.records == ()
    assert batch.cursor == "starting-position"
    assert batch.pages == 1


@pytest.mark.parametrize("payload", [page(row(), cursor=""), page(cursor="", has_next=True)])
def test_empty_cursor_is_not_valid_for_continuation_or_nonempty_page(payload):
    with pytest.raises(LogRocketRESTError, match="invalid_cursor"):
        parse_audit_page(payload, limit=2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 6},
        {"limit": True},
        {"max_pages": 6},
        {"since_ms": -1},
        {"since_ms": True},
        {"since_ms": 0, "cursor": "next"},
    ],
)
def test_invalid_probe_inputs_do_not_make_requests(kwargs):
    def handler(request):
        pytest.fail("Invalid inputs must not cause network access")

    with pytest.raises(ValueError):
        run(client(handler).probe("audit", **kwargs))


def test_missing_key_is_safe(monkeypatch):
    from types import SimpleNamespace

    import dlt

    monkeypatch.delenv("LOGROCKET_API_KEY", raising=False)
    monkeypatch.setattr(dlt, "secrets", SimpleNamespace(get=lambda _: None))
    report = run(LogRocketRESTClient("org", "app").probe("audit", since_ms=0))
    assert report["error_code"] == "missing_api_key"


def test_nested_time_cannot_leak_dynamic_identifiers_in_probe():
    report = run(
        client(
            lambda _: httpx2.Response(
                200, json=page(row(time={"private-email@example.invalid": "secret"}))
            )
        ).probe("audit", since_ms=0)
    )
    assert report["error_code"] == "invalid_audit_record"
    assert "private-email" not in json.dumps(report)


@pytest.mark.parametrize("project", [".", ".."])
def test_project_cannot_escape_scope(project):
    with pytest.raises(ValueError, match="path traversal"):
        LogRocketRESTClient("org", project)


def test_transport_timeouts_are_bounded(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("cognee_community_connector_logrocket.rest.asyncio.sleep", sleep)

    def handler(request):
        raise httpx2.ReadTimeout("synthetic-secret", request=request)

    report = run(client(handler).probe("audit", since_ms=0))
    assert report["error_code"] == "transport_failure"
    assert sleep.await_count == 2


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.logrocket.com",
        "https://key@api.logrocket.com",
        "https://api.logrocket.com/path",
        "https://api.logrocket.com/?key=value",
        "https://api.logrocket.com/#key",
    ],
)
def test_custom_origin_rejects_credentials_and_unexpected_paths(base_url):
    with pytest.raises(ValueError):
        LogRocketRESTClient("org", "app", base_url=base_url)
