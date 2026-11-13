import base64

import httpx
import pytest

from cognee_community_connector_mixpanel.client import MixpanelClient, collection
from cognee_community_connector_mixpanel.errors import (
    MixpanelConnectorError,
    RequestBudgetExceeded,
    ResponseContractError,
)


@pytest.mark.parametrize(
    "region,host", [("us", "mixpanel.com"), ("eu", "eu.mixpanel.com"), ("in", "in.mixpanel.com")]
)
def test_installed_sdk_basic_auth_region_and_project_scope(tmp_path, region, host):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"status": "ok", "results": []})

    client = MixpanelClient(
        project_id="123",
        username="test",
        secret="synthetic",
        region=region,
        state_directory=tmp_path,
        transport=httpx.MockTransport(handler),
    )
    with client:
        assert client.inventory("report") == []
    req = seen[0]
    assert req.url.host == host and req.url.path == "/api/app/projects/123/bookmarks"
    assert req.url.params["v"] == "2" and req.method == "GET"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"test:synthetic").decode()


def test_workspace_scopes_reports_but_not_schemas(tmp_path):
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"results": []})

    client = MixpanelClient(
        project_id="123",
        username="test",
        secret="synthetic",
        workspace_id=99,
        state_directory=tmp_path,
        transport=httpx.MockTransport(handler),
    )
    with client:
        client.inventory("cohort")
        client.inventory("schema")
    assert seen == ["/api/app/workspaces/99/cohorts", "/api/app/projects/123/schemas"]


@pytest.mark.parametrize(
    "body",
    [
        {"results": [], "has_more": True},
        {"results": [], "next_cursor": "next"},
        {"results": [], "truncated": True},
        {"results": [], "total": 1},
        {"results": [], "unknown_paging": "x"},
        {"results": [None]},
        {"status": "error", "results": []},
        {"unexpected": []},
    ],
)
def test_incomplete_inventory_cannot_become_confirmed_empty(body):
    with pytest.raises(ResponseContractError):
        collection(body)


def test_nested_v2_envelope_and_confirmed_empty():
    assert collection({"status": "ok", "results": {"results": [{"id": 1}]}}) == [{"id": 1}]
    assert collection({"results": [], "total": 0}) == []


@pytest.mark.parametrize("status", [401, 403, 404, 500, 429])
def test_read_errors_do_not_return_empty_or_leak_payload(tmp_path, status):
    client = MixpanelClient(
        project_id="123",
        username="test",
        secret="synthetic",
        state_directory=tmp_path,
        max_retries=0,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(status, json={"error": "private-person@example.com"})
        ),
    )
    with client, pytest.raises(MixpanelConnectorError) as caught:
        client.inventory("cohort")
    assert "private-person" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_hourly_budget_survives_new_client_and_failed_read(tmp_path):
    calls = []

    def make():
        return MixpanelClient(
            project_id="123",
            username="test",
            secret="synthetic",
            state_directory=tmp_path,
            hourly_limit=1,
            max_retries=0,
            transport=httpx.MockTransport(
                lambda req: calls.append(req) or httpx.Response(500, json={})
            ),
        )

    with make() as first, pytest.raises(MixpanelConnectorError):
        first.inventory("report")
    with make() as second, pytest.raises(RequestBudgetExceeded):
        second.inventory("report")
    assert len(calls) == 1


def test_retry_counted_and_response_size_guarded(tmp_path):
    calls, delays = [], []

    def handler(req):
        calls.append(req)
        return (
            httpx.Response(429, headers={"Retry-After": "1"})
            if len(calls) == 1
            else httpx.Response(200, json={"results": []})
        )

    client = MixpanelClient(
        project_id="123",
        username="test",
        secret="synthetic",
        state_directory=tmp_path,
        transport=httpx.MockTransport(handler),
        sleep=delays.append,
    )
    with client:
        assert client.inventory("report") == []
    assert len(calls) == 2 and delays == [1]
    oversized = MixpanelClient(
        project_id="123",
        username="other",
        secret="synthetic",
        state_directory=tmp_path,
        max_response_bytes=5,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"results": []})),
    )
    with oversized, pytest.raises(ResponseContractError):
        oversized.inventory("schema")


def test_workspace_access_verification(tmp_path):
    body = {"results": {"123": {"role": "analyst", "workspaces": {"99": {"is_visible": True}}}}}
    client = MixpanelClient(
        project_id="123",
        username="test",
        secret="synthetic",
        workspace_id=99,
        state_directory=tmp_path,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
    )
    with client:
        assert "analyst" in client.access_fingerprint()


def test_transport_lifecycle_reuse_and_nested_context(tmp_path, monkeypatch):
    transports = []

    class TrackingTransport(httpx.MockTransport):
        def __init__(self):
            super().__init__(lambda req: httpx.Response(200, json={"results": []}))
            self.closed = False
            transports.append(self)

        def handle_request(self, request):
            assert not self.closed
            return super().handle_request(request)

        def close(self):
            self.closed = True

    monkeypatch.setattr(httpx, "HTTPTransport", TrackingTransport)
    client = MixpanelClient(
        project_id="00123", username="test", secret="synthetic", state_directory=tmp_path
    )
    assert client.project_id == "123"
    with pytest.raises(MixpanelConnectorError, match="context manager"):
        client.inventory("report")
    for _ in range(2):
        with client:
            assert client.inventory("report") == []
            with pytest.raises(MixpanelConnectorError, match="already in use"):
                client.__enter__()
        assert transports[-1].closed
    assert len(transports) == 2


def test_lexicon_budget_shared_across_client_runs(tmp_path):
    client = MixpanelClient(
        project_id="123",
        username="test",
        secret="synthetic",
        state_directory=tmp_path,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"results": []})),
    )
    for _ in range(5):
        with client:
            client.inventory("schema")
    with client, pytest.raises(RequestBudgetExceeded, match="five-per-minute"):
        client.inventory("schema")


def test_compressed_response_bounded_after_decoding(tmp_path):
    import gzip

    client = MixpanelClient(
        project_id="123",
        username="test",
        secret="synthetic",
        state_directory=tmp_path,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, content=gzip.compress(b'{"results": []}'), headers={"content-encoding": "gzip"}
            )
        ),
    )
    with client:
        assert client.inventory("report") == []
