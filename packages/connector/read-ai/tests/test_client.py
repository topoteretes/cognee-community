import json
import time

import httpx
import pytest

from cognee_community_connector_read_ai import client as client_module
from cognee_community_connector_read_ai.auth import OAuthIdentity, TokenBundle
from cognee_community_connector_read_ai.client import ReadAIClient
from cognee_community_connector_read_ai.models import (
    AuthenticationError,
    BudgetExceededError,
    IncompleteSyncError,
    Selection,
    SyncLimits,
)

MID = "01HFYH0A6JM4R7MZ2E6X5T9BNP"
OTHER = "01HFYH0A6JM4R7MZ2E6X5T9BNQ"
START = 1733800000000


class Provider:
    def __init__(self):
        self.refreshes = 0

    def get_tokens(self, force_refresh=False):
        if force_refresh:
            self.refreshes += 1
        return TokenBundle(access_token="token", client_id="client", id_token="proof")


def raw(mid=MID, **overrides):
    return {
        "id": mid,
        "start_time_ms": START,
        "end_time_ms": START + 1000,
        "title": "Meeting",
        "summary": "Useful summary",
        "transcript": {"text": "Speaker: useful transcript"},
        "metrics": {"read_score": 0},
        **overrides,
    }


def page(meetings=(), more=False):
    return {"object": "list", "data": list(meetings), "has_more": more}


@pytest.fixture
def factory(tmp_path, monkeypatch):
    identity = OAuthIdentity(issuer="https://authn.read.ai/", subject="synthetic-subject")
    monkeypatch.setattr(client_module, "verify_identity", lambda *args, **kwargs: identity)
    clients = []

    def make(handler, limits=None, rate=True):
        transport = httpx.Client(transport=httpx.MockTransport(handler))
        connector = ReadAIClient(
            Provider(), client=transport, limits=limits or SyncLimits(), rate_dir=tmp_path
        )
        if not rate:
            monkeypatch.setattr(connector, "_reserve_request", lambda: None)
        clients.append(transport)
        return connector

    yield make
    for transport in clients:
        transport.close()


def test_pagination_uses_last_id_equal_times_and_selected_expansions(factory):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/v1/meetings":
            return httpx.Response(
                200,
                json=page([raw(OTHER)])
                if request.url.params.get("cursor")
                else page([raw()], True),
            )
        return httpx.Response(200, json=raw(request.url.path.rsplit("/", 1)[1]))

    selection = Selection(start_time_ms=START, end_time_ms=START + 1, include_metrics=False)
    snapshot = factory(handler).reconcile(selection, set())
    assert set(snapshot.rows) == {MID, OTHER}
    assert seen[0].url.params["start_time_ms.gte"] == str(START)
    assert seen[1].url.params["cursor"] == MID
    assert seen[2].url.params.get_list("expand[]") == ["summary", "transcript"]
    assert snapshot.pending == []


def test_removal_confirms_404_and_second_complete_inventory_in_same_attempt(factory):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/meetings":
            return httpx.Response(200, json=page())
        return httpx.Response(404)

    snapshot = factory(handler).reconcile(Selection(), {MID})
    assert snapshot.rows == {} and snapshot.meetings == {}
    assert calls == ["/v1/meetings", f"/v1/meetings/{MID}", "/v1/meetings"]


def test_explicit_absence_requires_second_detail_request(factory):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(404)

    assert factory(handler).reconcile(Selection(meeting_ids=(MID,)), {MID}).rows == {}
    assert len(calls) == 2


def test_retrievable_missing_report_is_not_deletion(factory):
    def handler(request):
        return httpx.Response(200, json=page() if request.url.path == "/v1/meetings" else raw())

    with pytest.raises(IncompleteSyncError, match="still retrievable"):
        factory(handler).reconcile(Selection(), {MID})


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_cannot_authorize_removal(factory, status):
    connector = factory(lambda _: httpx.Response(status))
    with pytest.raises(AuthenticationError):
        connector.reconcile(Selection(), {MID})
    assert connector.token_provider.refreshes == (1 if status == 401 else 0)


def test_one_401_refresh_then_success(factory):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        assert request.headers["Authorization"] == "Bearer token"
        return httpx.Response(401) if attempts == 1 else httpx.Response(200, json=page())

    connector = factory(handler)
    assert connector.reconcile(Selection(), set()).rows == {}
    assert connector.token_provider.refreshes == 1


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"object": "list", "data": [], "has_more": "false"},
        page([], True),
        page([raw(), raw()], True),
        page([raw(start_time_ms=START + 10)]),
    ],
)
def test_malformed_or_inconsistent_page_fails_closed(factory, invalid):
    connector = factory(lambda _: httpx.Response(200, json=invalid))
    with pytest.raises(IncompleteSyncError):
        connector.reconcile(Selection(end_time_ms=START + 1), {MID})


def test_membership_change_on_confirmation_aborts(factory):
    inventories = 0

    def handler(request):
        nonlocal inventories
        if request.url.path != "/v1/meetings":
            return httpx.Response(404)
        inventories += 1
        return httpx.Response(200, json=page() if inventories == 1 else page([raw()]))

    with pytest.raises(IncompleteSyncError, match="changed during removal"):
        factory(handler).reconcile(Selection(), {MID})


def test_active_meetings_not_fetched_and_unavailable_meetings_pending(factory):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=page([raw(end_time_ms=None)]))

    snapshot = factory(handler).reconcile(Selection(), set())
    assert snapshot.pending == [MID] and snapshot.rows == {}
    assert len(calls) == 1


@pytest.mark.parametrize(
    "limit",
    [
        SyncLimits(max_meetings=1),
        SyncLimits(max_pages=1),
        SyncLimits(max_response_bytes=10),
        SyncLimits(max_total_response_bytes=10),
    ],
)
def test_configured_limits_never_truncate_and_report_success(factory, limit):
    connector = factory(lambda _: httpx.Response(200, json=page([raw(), raw(OTHER)], True)), limit)
    with pytest.raises(BudgetExceededError):
        connector.reconcile(Selection(), set())


def test_exhausted_transient_retries_fail(factory, monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"Retry-After": "0"})

    connector = factory(handler)
    monkeypatch.setattr(connector, "_sleep", lambda seconds: None)
    with pytest.raises(IncompleteSyncError, match="retries were exhausted"):
        connector.reconcile(Selection(), set())
    assert calls == 5


def test_retry_after_validation_and_deadline(factory):
    connector = factory(lambda _: httpx.Response(200, json=page()))
    assert connector._retry_after("NaN", 2) == 4
    assert connector._retry_after("-10", 1) == 2
    assert connector._retry_after("Wed, 01 Jan 2020 00:00:00 GMT", 0) == 1
    with pytest.raises(IncompleteSyncError):
        connector._retry_after("301", 0)
    connector.deadline = time.monotonic() - 1
    with pytest.raises(BudgetExceededError):
        connector.check_budget()


def test_identity_callback_uses_bounded_transport(factory, monkeypatch):
    def verify(bundle, *, request_json, **kwargs):
        request_json("https://authn.read.ai/userinfo", headers={"Authorization": "Bearer token"})
        return OAuthIdentity("https://authn.read.ai/", "subject")

    monkeypatch.setattr(client_module, "verify_identity", verify)
    connector = factory(
        lambda _: httpx.Response(200, json={"sub": "too big"}), SyncLimits(max_response_bytes=1)
    )
    with pytest.raises(BudgetExceededError):
        connector.reconcile(Selection(), set())


def test_rate_window_is_account_shared_and_persisted(factory, tmp_path):
    connector = factory(lambda _: httpx.Response(200, json=page()))
    connector.reconcile(Selection(), set())
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert len(json.loads(files[0].read_text())) == 1
    connector.reconcile(Selection(), set())
    assert len(json.loads(files[0].read_text())) == 2


def test_redirect_is_not_followed(factory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://unsafe.example/token"})

    with pytest.raises(IncompleteSyncError, match="HTTP 302"):
        factory(handler).reconcile(Selection(), set())
    assert len(calls) == 1


def test_compressed_bodies_are_rejected_before_unbounded_decoder(factory):
    def handler(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"bad")
        )

    with pytest.raises(IncompleteSyncError, match="compressed"):
        factory(handler).reconcile(Selection(), set())


def test_remote_protocol_failures_are_bounded_retries(factory, monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.RemoteProtocolError("closed")

    connector = factory(handler)
    monkeypatch.setattr(connector, "_sleep", lambda seconds: None)
    with pytest.raises(IncompleteSyncError, match="transport retries"):
        connector.reconcile(Selection(), set())
    assert calls == 5


def test_changed_historical_content_is_rendered_in_current_snapshot(factory):
    current = "original"

    def handler(request):
        return httpx.Response(
            200,
            json=page([raw()])
            if request.url.path == "/v1/meetings"
            else raw(summary=current, metrics={"read_score": 0}),
        )

    connector = factory(handler)
    before = connector.reconcile(Selection(), set())
    current = "historical edit"
    after = connector.reconcile(Selection(), {MID})
    assert before.rows[MID]["content"] != after.rows[MID]["content"]
    assert "historical edit" in after.rows[MID]["content"]


def test_available_then_all_sections_unavailable_withdraws_row(factory):
    current = raw()

    def handler(request):
        return httpx.Response(
            200, json=page([raw()]) if request.url.path == "/v1/meetings" else current
        )

    connector = factory(handler)
    assert MID in connector.reconcile(Selection(), set()).rows
    current = raw(summary=None, transcript={}, metrics={})
    snapshot = connector.reconcile(Selection(), {MID})
    assert snapshot.rows == {}
    assert MID in snapshot.meetings and snapshot.pending == [MID]


def test_outer_lock_wait_deadline_cannot_be_reset_by_reconciliation(factory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=page())

    connector = factory(handler)
    with pytest.raises(BudgetExceededError):
        connector.reconcile(Selection(), set(), deadline=time.monotonic() - 1)
    assert calls == []
