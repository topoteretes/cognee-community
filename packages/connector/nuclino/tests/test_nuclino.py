"""Unit tests for the Nuclino connector (Phase 1 & Phase 2).

Covers authentication, transient error handling, cursor pagination, workspace discovery,
item/collection metadata extraction, item detail fetching, and row transformation.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from cognee_community_connector_nuclino.nuclino import (
    _DEFAULT_LIMIT,
    _MAX_RETRIES,
    NUCLINO_API_BASE,
    _deleted_row,
    _fetch_item,
    _is_transient,
    _item_to_row,
    _iter_item_metadata,
    _make_session,
    _paginate,
    _request,
    _resolve_workspace_ids,
    _retry_after,
    nuclino_source,
    sync_items,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeNuclinoResponse:
    """Mock requests.Response for Nuclino tests."""

    def __init__(
        self,
        payload: Any = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = requests.Request("GET", "https://api.nuclino.com/v0/test").prepare()
            resp = requests.Response()
            resp.status_code = self.status_code
            resp.headers.update(self.headers)
            resp.request = req
            raise requests.exceptions.HTTPError(
                f"HTTP {self.status_code} Error",
                response=resp,
            )


class FakeNuclinoSession:
    """Mock requests.Session tracking calls and returning sequential/routed responses."""

    def __init__(
        self,
        responses: list[FakeNuclinoResponse | Exception] | None = None,
        routes: dict[str, Any] | None = None,
    ):
        self.responses = list(responses or [])
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any] | None = None) -> FakeNuclinoResponse:
        self.calls.append((url, dict(params or {})))

        if url in self.routes:
            handler = self.routes[url]
            if callable(handler):
                res = handler(url, params)
            elif isinstance(handler, list):
                res = handler.pop(0)
            else:
                res = handler
            if isinstance(res, Exception):
                raise res
            return res

        if not self.responses:
            return FakeNuclinoResponse({"status": "success", "data": {"results": []}})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------
# 1. Authentication & Header Tests
# ---------------------------------------------------------------------------


def test_make_session_raw_api_key_auth_no_bearer():
    """Nuclino REST API requires 'Authorization: <API_KEY>' without 'Bearer'."""
    session = _make_session(api_key="my-secret-key-123")
    auth_header = session.headers.get("Authorization")

    assert auth_header == "my-secret-key-123"
    assert "Bearer" not in auth_header


def test_make_session_accept_json_header():
    """Nuclino session must include 'Accept: application/json'."""
    session = _make_session(api_key="my-secret-key-123")
    assert session.headers.get("Accept") == "application/json"


def test_make_session_env_fallback(monkeypatch):
    """When api_key is omitted, _make_session falls back to NUCLINO_API_KEY."""
    monkeypatch.setenv("NUCLINO_API_KEY", "env-api-key-456")
    session = _make_session()
    assert session.headers.get("Authorization") == "env-api-key-456"


def test_make_session_missing_key_raises_value_error(monkeypatch):
    """When no api_key is provided or in env, _make_session raises ValueError."""
    monkeypatch.delenv("NUCLINO_API_KEY", raising=False)
    with pytest.raises(ValueError, match="NUCLINO_API_KEY"):
        _make_session()


# ---------------------------------------------------------------------------
# 2. Transient Error Classification & Retry-After
# ---------------------------------------------------------------------------


def test_is_transient_classification():
    """Verify transient error classification for 429, 5xx, timeouts, and permanent 4xx."""
    # 429 Rate limit
    resp_429 = FakeNuclinoResponse(status_code=429)
    with pytest.raises(requests.exceptions.HTTPError) as exc_429:
        resp_429.raise_for_status()
    assert _is_transient(exc_429.value) is True

    # 5xx Server errors
    for code in (500, 502, 503, 504):
        resp = FakeNuclinoResponse(status_code=code)
        with pytest.raises(requests.exceptions.HTTPError) as exc_5xx:
            resp.raise_for_status()
        assert _is_transient(exc_5xx.value) is True

    # Network / Timeout errors
    assert _is_transient(requests.exceptions.Timeout()) is True
    assert _is_transient(requests.exceptions.ConnectionError()) is True

    # Permanent client errors: 400, 401, 403, 404 must NOT be transient
    for code in (400, 401, 403, 404):
        resp = FakeNuclinoResponse(status_code=code)
        with pytest.raises(requests.exceptions.HTTPError) as exc_4xx:
            resp.raise_for_status()
        assert _is_transient(exc_4xx.value) is False

    # Other non-HTTP exceptions
    assert _is_transient(ValueError("misc")) is False


def test_retry_after_parses_header_and_falls_back():
    """Verify Retry-After header parsing and exponential backoff fallback."""
    # Case-insensitive header lookup
    assert _retry_after({"Retry-After": "3.5"}, attempt=0) == 3.5
    assert _retry_after({"retry-after": "10"}, attempt=1) == 10.0

    # Non-numeric or missing headers fall back to 2**attempt
    assert _retry_after({"Retry-After": "invalid"}, attempt=0) == 1.0
    assert _retry_after({}, attempt=2) == 4.0
    assert _retry_after(None, attempt=3) == 8.0


# ---------------------------------------------------------------------------
# 3. HTTP Request & Retry Behavior
# ---------------------------------------------------------------------------


def test_request_429_retries_with_retry_after(monkeypatch):
    """429 response retries and succeeds when subsequent request succeeds."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)

    session = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(status_code=429, headers={"Retry-After": "0.5"}),
            FakeNuclinoResponse({"status": "success", "data": {"ok": True}}),
        ]
    )

    resp = _request(session.get, f"{NUCLINO_API_BASE}/items")
    assert resp.json()["data"]["ok"] is True
    assert len(session.calls) == 2
    assert slept == [0.5]


def test_request_transient_5xx_retries_with_backoff(monkeypatch):
    """503 Service Unavailable triggers exponential backoff retry."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)

    session = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(status_code=503),
            FakeNuclinoResponse({"status": "success", "data": {"results": []}}),
        ]
    )

    resp = _request(session.get, f"{NUCLINO_API_BASE}/items")
    assert resp.status_code == 200
    assert len(session.calls) == 2
    assert slept == [1.0]  # 2**0


def test_request_permanent_401_and_403_do_not_retry():
    """Permanent errors (401 Unauthorized, 403 Forbidden) fail immediately without retrying."""
    # 401 Unauthorized
    session_401 = FakeNuclinoSession(responses=[FakeNuclinoResponse(status_code=401)])
    with pytest.raises(requests.exceptions.HTTPError) as exc:
        _request(session_401.get, f"{NUCLINO_API_BASE}/items")
    assert exc.value.response.status_code == 401
    assert len(session_401.calls) == 1

    # 403 Forbidden
    session_403 = FakeNuclinoSession(responses=[FakeNuclinoResponse(status_code=403)])
    with pytest.raises(requests.exceptions.HTTPError) as exc:
        _request(session_403.get, f"{NUCLINO_API_BASE}/items")
    assert exc.value.response.status_code == 403
    assert len(session_403.calls) == 1


def test_request_exhausted_retries_raises(monkeypatch):
    """When retries exceed _MAX_RETRIES on continuous transient errors, error propagates."""
    monkeypatch.setattr("time.sleep", lambda _: None)

    session = FakeNuclinoSession(responses=[FakeNuclinoResponse(status_code=500)] * _MAX_RETRIES)
    with pytest.raises(requests.exceptions.HTTPError) as exc:
        _request(session.get, f"{NUCLINO_API_BASE}/items")
    assert exc.value.response.status_code == 500
    assert len(session.calls) == _MAX_RETRIES


# ---------------------------------------------------------------------------
# 4. Cursor Pagination (_paginate)
# ---------------------------------------------------------------------------


def test_paginate_parses_data_results_across_multiple_pages():
    """_paginate parses response.json()['data']['results'] and follows 'after' cursor."""
    page1_items = [{"id": f"item_{i}", "title": f"Item {i}"} for i in range(1, 101)]
    page2_items = [{"id": f"item_{i}", "title": f"Item {i}"} for i in range(101, 201)]
    page3_items = [{"id": f"item_{i}", "title": f"Item {i}"} for i in range(201, 221)]  # 20 items

    session = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(
                {"status": "success", "data": {"results": page1_items}},
            ),
            FakeNuclinoResponse(
                {"status": "success", "data": {"results": page2_items}},
            ),
            FakeNuclinoResponse(
                {"status": "success", "data": {"results": page3_items}},
            ),
        ]
    )

    results = list(_paginate(session, f"{NUCLINO_API_BASE}/items", params={"workspaceId": "ws-1"}))

    # All 220 items yielded in order
    assert len(results) == 220
    assert results[0]["id"] == "item_1"
    assert results[-1]["id"] == "item_220"

    # Exactly 3 calls made
    assert len(session.calls) == 3

    # Call 1: initial limit, no 'after'
    assert session.calls[0][1] == {"workspaceId": "ws-1", "limit": _DEFAULT_LIMIT}

    # Call 2: after set to last item of page 1
    assert session.calls[1][1] == {
        "workspaceId": "ws-1",
        "limit": _DEFAULT_LIMIT,
        "after": "item_100",
    }

    # Call 3: after set to last item of page 2
    assert session.calls[2][1] == {
        "workspaceId": "ws-1",
        "limit": _DEFAULT_LIMIT,
        "after": "item_200",
    }


def test_paginate_empty_results_terminates_immediately():
    """An empty results array terminates pagination on the first request."""
    session = FakeNuclinoSession(
        responses=[FakeNuclinoResponse({"status": "success", "data": {"results": []}})]
    )

    results = list(_paginate(session, f"{NUCLINO_API_BASE}/items"))
    assert results == []
    assert len(session.calls) == 1


def test_paginate_final_page_fewer_than_limit_terminates():
    """When a page returns fewer items than limit, pagination stops without extra request."""
    items = [{"id": "item_1", "title": "Single Item"}]
    session = FakeNuclinoSession(
        responses=[FakeNuclinoResponse({"status": "success", "data": {"results": items}})]
    )

    results = list(_paginate(session, f"{NUCLINO_API_BASE}/items", limit=100))
    assert len(results) == 1
    assert results[0]["id"] == "item_1"
    assert len(session.calls) == 1  # No second request attempted


def test_paginate_malformed_response_raises_value_error():
    """Malformed payloads (non-dict payload/data or non-list results) raise ValueError."""
    # Payload is not a dict
    session1 = FakeNuclinoSession(responses=[FakeNuclinoResponse("not-a-dict")])
    with pytest.raises(ValueError, match="JSON object"):
        list(_paginate(session1, f"{NUCLINO_API_BASE}/items"))

    # Data is not a dict
    session2 = FakeNuclinoSession(
        responses=[FakeNuclinoResponse({"status": "error", "data": None})]
    )
    with pytest.raises(ValueError, match="'data' object"):
        list(_paginate(session2, f"{NUCLINO_API_BASE}/items"))

    # Results is not a list
    session3 = FakeNuclinoSession(
        responses=[FakeNuclinoResponse({"status": "success", "data": {"results": "bad"}})]
    )
    with pytest.raises(ValueError, match="'results' list"):
        list(_paginate(session3, f"{NUCLINO_API_BASE}/items"))


def test_paginate_stagnant_cursor_raises_runtime_error():
    """If upstream returns the exact same last ID across iterations, raise RuntimeError."""
    duplicate_items = [{"id": "stuck_id", "title": "Stuck"}] * 100

    session = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse({"status": "success", "data": {"results": duplicate_items}}),
            FakeNuclinoResponse({"status": "success", "data": {"results": duplicate_items}}),
        ]
    )

    with pytest.raises(RuntimeError, match="cursor stagnated"):
        list(_paginate(session, f"{NUCLINO_API_BASE}/items", limit=100))


def test_paginate_full_page_missing_last_id_raises_value_error():
    """A full page whose last item has no valid string id raises ValueError."""
    items = [{"id": f"item_{i}", "title": f"Item {i}"} for i in range(99)]
    items.append({"title": "No ID"})  # 100th item missing id

    session = FakeNuclinoSession(
        responses=[FakeNuclinoResponse({"status": "success", "data": {"results": items}})]
    )

    with pytest.raises(ValueError, match="missing valid string 'id'"):
        list(_paginate(session, f"{NUCLINO_API_BASE}/items", limit=100))


# ---------------------------------------------------------------------------
# 5. Workspace Discovery (_resolve_workspace_ids)
# ---------------------------------------------------------------------------


def test_resolve_workspace_ids_explicit_avoids_api_calls():
    """Explicit workspace IDs are normalized, deduplicated, and bypass API calls."""
    session = FakeNuclinoSession()
    ids = _resolve_workspace_ids(session, workspace_ids=["ws_1", "ws_2", "ws_1", " ws_3 "])

    assert ids == ["ws_1", "ws_2", "ws_3"]
    assert len(session.calls) == 0


def test_resolve_workspace_ids_discovery_returns_ids():
    """When workspace_ids is omitted, discovery lists workspaces via GET /v0/workspaces."""
    session = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(
                {
                    "status": "success",
                    "data": {
                        "results": [
                            {"id": "w1", "name": "Workspace 1"},
                            {"id": "w2", "name": "Workspace 2"},
                        ]
                    },
                }
            )
        ]
    )

    ids = _resolve_workspace_ids(session)
    assert ids == ["w1", "w2"]
    assert len(session.calls) == 1
    assert session.calls[0][0] == f"{NUCLINO_API_BASE}/workspaces"
    assert session.calls[0][1] == {"limit": _DEFAULT_LIMIT}


def test_resolve_workspace_ids_passes_team_id():
    """team_id parameter is forwarded as teamId query parameter."""
    session = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(
                {"status": "success", "data": {"results": [{"id": "w1", "name": "W1"}]}}
            )
        ]
    )

    ids = _resolve_workspace_ids(session, team_id="team_alpha")
    assert ids == ["w1"]
    assert session.calls[0][1] == {"teamId": "team_alpha", "limit": _DEFAULT_LIMIT}


def test_resolve_workspace_ids_pagination_works():
    """Workspace discovery paginates over multiple pages."""
    p1 = [{"id": f"w_{i}", "name": f"WS {i}"} for i in range(1, 101)]
    p2 = [{"id": "w_101", "name": "WS 101"}]

    session = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse({"status": "success", "data": {"results": p1}}),
            FakeNuclinoResponse({"status": "success", "data": {"results": p2}}),
        ]
    )

    ids = _resolve_workspace_ids(session)
    assert len(ids) == 101
    assert ids[0] == "w_1"
    assert ids[-1] == "w_101"
    assert len(session.calls) == 2
    assert session.calls[1][1]["after"] == "w_100"


def test_resolve_workspace_ids_malformed_missing_id_raises():
    """Workspace object missing string id raises ValueError."""
    session = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(
                {"status": "success", "data": {"results": [{"name": "Missing ID"}]}}
            )
        ]
    )

    with pytest.raises(ValueError, match="missing valid string 'id'"):
        _resolve_workspace_ids(session)


# ---------------------------------------------------------------------------
# 6. Item/Collection Metadata Iteration (_iter_item_metadata)
# ---------------------------------------------------------------------------


def test_iter_item_metadata_returns_both_items_and_collections():
    """_iter_item_metadata yields both 'item' and 'collection' objects."""
    metadata = [
        {"id": "item-1", "workspaceId": "ws-1", "object": "item", "title": "A"},
        {"id": "col-1", "workspaceId": "ws-1", "object": "collection", "title": "B"},
    ]
    session = FakeNuclinoSession(
        responses=[FakeNuclinoResponse({"status": "success", "data": {"results": metadata}})]
    )

    results = list(_iter_item_metadata(session, workspace_ids=["ws-1"]))
    assert len(results) == 2
    assert results[0]["object"] == "item"
    assert results[1]["object"] == "collection"


def test_iter_item_metadata_multiple_workspaces_queried_independently():
    """Iterates through all provided workspaces sequentially."""
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": [
                FakeNuclinoResponse(
                    {
                        "status": "success",
                        "data": {"results": [{"id": "i1", "workspaceId": "w1", "object": "item"}]},
                    }
                ),
                FakeNuclinoResponse(
                    {
                        "status": "success",
                        "data": {
                            "results": [{"id": "c2", "workspaceId": "w2", "object": "collection"}]
                        },
                    }
                ),
            ]
        }
    )

    results = list(_iter_item_metadata(session, workspace_ids=["w1", "w2"]))
    assert len(results) == 2
    assert results[0]["id"] == "i1"
    assert results[1]["id"] == "c2"
    assert len(session.calls) == 2
    assert session.calls[0][1]["workspaceId"] == "w1"
    assert session.calls[1][1]["workspaceId"] == "w2"


def test_iter_item_metadata_sends_workspace_id_param():
    """workspaceId parameter is always present in items listing queries."""
    session = FakeNuclinoSession(
        responses=[FakeNuclinoResponse({"status": "success", "data": {"results": []}})]
    )

    list(_iter_item_metadata(session, workspace_ids=["ws-target"]))
    assert session.calls[0][1]["workspaceId"] == "ws-target"


def test_iter_item_metadata_unexpected_object_type_raises():
    """Objects with type other than 'item' or 'collection' raise ValueError."""
    bad_item = [{"id": "x1", "workspaceId": "ws-1", "object": "tag"}]
    session = FakeNuclinoSession(
        responses=[FakeNuclinoResponse({"status": "success", "data": {"results": bad_item}})]
    )

    with pytest.raises(ValueError, match="unexpected type 'tag'"):
        list(_iter_item_metadata(session, workspace_ids=["ws-1"]))


def test_iter_item_metadata_malformed_missing_id_or_workspace_raises():
    """Objects missing id or workspaceId raise ValueError."""
    # Missing workspaceId
    session1 = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(
                {"status": "success", "data": {"results": [{"id": "i1", "object": "item"}]}}
            )
        ]
    )
    with pytest.raises(ValueError, match="missing valid string 'workspaceId'"):
        list(_iter_item_metadata(session1, workspace_ids=["ws-1"]))

    # Missing id
    session2 = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(
                {
                    "status": "success",
                    "data": {"results": [{"workspaceId": "ws-1", "object": "collection"}]},
                }
            )
        ]
    )
    with pytest.raises(ValueError, match="missing valid string 'id'"):
        list(_iter_item_metadata(session2, workspace_ids=["ws-1"]))


# ---------------------------------------------------------------------------
# 7. Item Detail Fetching (_fetch_item)
# ---------------------------------------------------------------------------


def test_fetch_item_full_item_content_parsed_correctly():
    """Item detail returns the data payload with markdown content."""
    item_payload = {
        "status": "success",
        "data": {
            "id": "item-abc",
            "workspaceId": "ws-1",
            "object": "item",
            "title": "Welcome",
            "content": "# Welcome\n\nThis is Nuclino.",
            "url": "https://app.nuclino.com/t/b/item-abc",
        },
    }
    session = FakeNuclinoSession(responses=[FakeNuclinoResponse(item_payload)])

    data = _fetch_item(session, "item-abc")
    assert data is not None
    assert data["id"] == "item-abc"
    assert data["object"] == "item"
    assert data["content"] == "# Welcome\n\nThis is Nuclino."
    assert session.calls[0][0] == f"{NUCLINO_API_BASE}/items/item-abc"


def test_fetch_item_full_collection_content_parsed_correctly():
    """Collection detail returns the data payload with markdown content."""
    col_payload = {
        "status": "success",
        "data": {
            "id": "col-xyz",
            "workspaceId": "ws-1",
            "object": "collection",
            "title": "Engineering Wiki",
            "content": "# Engineering Wiki\n\nCollection overview.",
            "url": "https://app.nuclino.com/t/b/col-xyz",
        },
    }
    session = FakeNuclinoSession(responses=[FakeNuclinoResponse(col_payload)])

    data = _fetch_item(session, "col-xyz")
    assert data is not None
    assert data["id"] == "col-xyz"
    assert data["object"] == "collection"
    assert data["content"] == "# Engineering Wiki\n\nCollection overview."


def test_fetch_item_404_returns_none():
    """HTTP 404 indicates item vanished/deleted and returns None."""
    session = FakeNuclinoSession(responses=[FakeNuclinoResponse(status_code=404)])
    result = _fetch_item(session, "item-deleted")
    assert result is None


def test_fetch_item_403_propagates_and_not_treated_as_deletion():
    """HTTP 403 Forbidden is a permanent authorization error and must propagate."""
    session = FakeNuclinoSession(responses=[FakeNuclinoResponse(status_code=403)])
    with pytest.raises(requests.exceptions.HTTPError) as exc:
        _fetch_item(session, "item-locked")
    assert exc.value.response.status_code == 403


def test_fetch_item_malformed_success_payload_raises():
    """Malformed item detail payloads raise ValueError."""
    # Data is not a dict
    session1 = FakeNuclinoSession(
        responses=[FakeNuclinoResponse({"status": "success", "data": "bad"})]
    )
    with pytest.raises(ValueError, match="'data' object"):
        _fetch_item(session1, "item-1")

    # Missing object type or unknown object type
    session2 = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(
                {"status": "success", "data": {"id": "item-1", "object": "unknown"}}
            )
        ]
    )
    with pytest.raises(ValueError, match="unexpected object type 'unknown'"):
        _fetch_item(session2, "item-1")


def test_fetch_item_returned_id_mismatch_raises():
    """Mismatch between requested ID and returned data['id'] raises ValueError."""
    session = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse({"status": "success", "data": {"id": "wrong-id", "object": "item"}})
        ]
    )
    with pytest.raises(ValueError, match="ID mismatch"):
        _fetch_item(session, "expected-id")


def test_fetch_item_malformed_content_raises():
    """Non-string or missing content raises ValueError; empty string succeeds."""
    # 1. Non-string content raises
    session1 = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(
                {
                    "status": "success",
                    "data": {"id": "item-1", "object": "item", "content": ["list-content"]},
                }
            )
        ]
    )
    with pytest.raises(ValueError, match="content"):
        _fetch_item(session1, "item-1")

    # 2. Missing content key raises
    session2 = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(
                {
                    "status": "success",
                    "data": {"id": "item-2", "object": "item"},
                }
            )
        ]
    )
    with pytest.raises(ValueError, match="content"):
        _fetch_item(session2, "item-2")

    # 3. None content raises
    session3 = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(
                {
                    "status": "success",
                    "data": {"id": "item-3", "object": "item", "content": None},
                }
            )
        ]
    )
    with pytest.raises(ValueError, match="content"):
        _fetch_item(session3, "item-3")

    # 4. Empty string content succeeds
    session4 = FakeNuclinoSession(
        responses=[
            FakeNuclinoResponse(
                {
                    "status": "success",
                    "data": {"id": "item-4", "object": "item", "content": ""},
                }
            )
        ]
    )
    res = _fetch_item(session4, "item-4")
    assert res is not None
    assert res["content"] == ""


# ---------------------------------------------------------------------------
# 8. Row Transformation (_item_to_row)
# ---------------------------------------------------------------------------


def test_item_to_row_converts_item_to_expected_row():
    """_item_to_row transforms an item into a Cognee document-mode row."""
    item = {
        "id": "item-1",
        "workspaceId": "ws-100",
        "object": "item",
        "title": "Onboarding",
        "content": "# Welcome to the team",
        "url": "https://app.nuclino.com/t/b/item-1",
    }
    row = _item_to_row(item)

    assert row == {
        "id": "item-1",
        "title": "Onboarding",
        "content": "# Welcome to the team",
        "url": "https://app.nuclino.com/t/b/item-1",
        "workspace_id": "ws-100",
        "_deleted": False,
    }


def test_item_to_row_converts_collection_to_expected_row():
    """_item_to_row treats collections identically to items."""
    collection = {
        "id": "col-5",
        "workspaceId": "ws-200",
        "object": "collection",
        "title": "Handbooks",
        "content": "# Handbooks overview",
        "url": "https://app.nuclino.com/t/b/col-5",
    }
    row = _item_to_row(collection)

    assert row == {
        "id": "col-5",
        "title": "Handbooks",
        "content": "# Handbooks overview",
        "url": "https://app.nuclino.com/t/b/col-5",
        "workspace_id": "ws-200",
        "_deleted": False,
    }


def test_item_to_row_omits_timestamps_and_volatile_metadata():
    """Volatile API metadata is strictly omitted from the document row."""
    item_with_metadata = {
        "id": "item-99",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Doc",
        "content": "Body text",
        "url": "https://app.nuclino.com/t/b/item-99",
        # Volatile API fields:
        "createdAt": "2026-01-01T00:00:00.000Z",
        "lastUpdatedAt": "2026-02-01T12:00:00.000Z",
        "createdUserId": "usr-1",
        "lastUpdatedUserId": "usr-2",
        "contentVersion": 14,
        "fields": {"custom_field": "val"},
        "childIds": ["child-1", "child-2"],
    }
    row = _item_to_row(item_with_metadata)

    assert "createdAt" not in row
    assert "lastUpdatedAt" not in row
    assert "createdUserId" not in row
    assert "lastUpdatedUserId" not in row
    assert "contentVersion" not in row
    assert "fields" not in row
    assert "childIds" not in row
    assert set(row.keys()) == {"id", "title", "content", "url", "workspace_id", "_deleted"}


def test_item_to_row_malformed_input_raises():
    """Invalid item inputs raise ValueError."""
    # Not a dict
    with pytest.raises(ValueError, match="dictionary"):
        _item_to_row("bad")  # type: ignore[arg-type]

    # Missing id
    with pytest.raises(ValueError, match="missing valid string 'id'"):
        _item_to_row({"workspaceId": "ws-1"})

    # Missing workspaceId
    with pytest.raises(ValueError, match="missing valid string 'workspaceId'"):
        _item_to_row({"id": "item-1"})


# ---------------------------------------------------------------------------
# 9. Deletion Tombstones (_deleted_row)
# ---------------------------------------------------------------------------


def test_deleted_row_validation_and_output():
    """_deleted_row produces valid tombstones and validates input."""
    # Valid output
    row = _deleted_row("item-deleted-123")
    assert row == {
        "id": "item-deleted-123",
        "_deleted": True,
    }

    # Strips whitespace
    row2 = _deleted_row("  item-trimmed  ")
    assert row2 == {
        "id": "item-trimmed",
        "_deleted": True,
    }

    # Invalid inputs
    for bad in ("", "   ", None, 123, []):
        with pytest.raises(ValueError, match="Invalid item_id"):
            _deleted_row(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 10. Incremental Synchronization (sync_items)
# ---------------------------------------------------------------------------


def test_sync_items_initial_sync_fetches_every_item_and_records_versions():
    """Initial sync with empty state fetches every item and populates item_versions."""
    metadata = [
        {
            "id": "i1",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-01-01T00:00:00Z",
        },
        {
            "id": "i2",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-01-02T00:00:00Z",
        },
    ]
    detail1 = {
        "id": "i1",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "T1",
        "content": "# C1",
    }
    detail2 = {
        "id": "i2",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "T2",
        "content": "# C2",
    }

    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
            f"{NUCLINO_API_BASE}/items/i1": FakeNuclinoResponse(
                {"status": "success", "data": detail1}
            ),
            f"{NUCLINO_API_BASE}/items/i2": FakeNuclinoResponse(
                {"status": "success", "data": detail2}
            ),
        }
    )

    state: dict[str, Any] = {}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert len(rows) == 2
    assert rows[0]["id"] == "i1"
    assert rows[0]["content"] == "# C1"
    assert rows[0]["_deleted"] is False
    assert rows[1]["id"] == "i2"
    assert rows[1]["content"] == "# C2"
    assert rows[1]["_deleted"] is False

    assert state["item_versions"] == {
        "i1": "2026-01-01T00:00:00Z",
        "i2": "2026-01-02T00:00:00Z",
    }


def test_sync_items_collection_versions_tracked_same_as_items():
    """Collection versions are tracked in state and emitted identically to items."""
    metadata = [
        {
            "id": "col-1",
            "workspaceId": "ws-1",
            "object": "collection",
            "lastUpdatedAt": "2026-02-01T00:00:00Z",
        },
        {
            "id": "item-1",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-02-02T00:00:00Z",
        },
    ]
    detail_col = {
        "id": "col-1",
        "workspaceId": "ws-1",
        "object": "collection",
        "title": "Col",
        "content": "Col text",
    }
    detail_item = {
        "id": "item-1",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Item",
        "content": "Item text",
    }

    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
            f"{NUCLINO_API_BASE}/items/col-1": FakeNuclinoResponse(
                {"status": "success", "data": detail_col}
            ),
            f"{NUCLINO_API_BASE}/items/item-1": FakeNuclinoResponse(
                {"status": "success", "data": detail_item}
            ),
        }
    )

    state: dict[str, Any] = {}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert len(rows) == 2
    assert state["item_versions"] == {
        "col-1": "2026-02-01T00:00:00Z",
        "item-1": "2026-02-02T00:00:00Z",
    }


def test_sync_items_unchanged_known_item_does_not_perform_detail_get():
    """When an item's version in metadata matches state, detail GET is skipped."""
    metadata = [
        {
            "id": "i1",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-01-01T00:00:00Z",
        },
    ]
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
        }
    )

    state = {"item_versions": {"i1": "2026-01-01T00:00:00Z"}}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert rows == []
    # Only the items listing endpoint was called; no detail GET was performed
    assert len(session.calls) == 1
    assert session.calls[0][0] == f"{NUCLINO_API_BASE}/items"
    assert state["item_versions"] == {"i1": "2026-01-01T00:00:00Z"}


def test_sync_items_changed_known_item_performs_detail_get_and_yields_live_row():
    """When an item's version changed, fetches detail and yields live row."""
    metadata = [
        {
            "id": "i1",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-01-05T00:00:00Z",
        },
    ]
    detail = {
        "id": "i1",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Updated",
        "content": "Updated content",
    }

    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
            f"{NUCLINO_API_BASE}/items/i1": FakeNuclinoResponse(
                {"status": "success", "data": detail}
            ),
        }
    )

    state = {"item_versions": {"i1": "2026-01-01T00:00:00Z"}}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert len(rows) == 1
    assert rows[0]["id"] == "i1"
    assert rows[0]["content"] == "Updated content"
    assert rows[0]["_deleted"] is False
    assert state["item_versions"] == {"i1": "2026-01-05T00:00:00Z"}


def test_sync_items_new_item_fetched_even_when_timestamp_is_older():
    """A new item with an older timestamp than existing state is still fetched."""
    metadata = [
        {
            "id": "i_recent",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-05-01T00:00:00Z",
        },
        {
            "id": "i_old",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2020-01-01T00:00:00Z",
        },
    ]
    detail_old = {
        "id": "i_old",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Old",
        "content": "Archived text",
    }

    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
            f"{NUCLINO_API_BASE}/items/i_old": FakeNuclinoResponse(
                {"status": "success", "data": detail_old}
            ),
        }
    )

    state = {"item_versions": {"i_recent": "2026-05-01T00:00:00Z"}}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert len(rows) == 1
    assert rows[0]["id"] == "i_old"
    assert state["item_versions"] == {
        "i_old": "2020-01-01T00:00:00Z",
        "i_recent": "2026-05-01T00:00:00Z",
    }


def test_sync_items_deletion_emits_deleted_true():
    """Items present in state but absent in current sweep emit deletion tombstones."""
    metadata = [
        {"id": "i1", "workspaceId": "ws-1", "object": "item", "lastUpdatedAt": "ts1"},
    ]
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
        }
    )

    state = {"item_versions": {"i1": "ts1", "i2": "ts2", "i3": "ts3"}}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert len(rows) == 2
    assert rows == [
        {"id": "i2", "_deleted": True},
        {"id": "i3", "_deleted": True},
    ]
    assert state["item_versions"] == {"i1": "ts1"}


def test_sync_items_listed_item_404_treated_as_vanished():
    """A previously known item that 404s during detail fetch is emitted as a tombstone."""
    metadata = [
        {"id": "i_vanished", "workspaceId": "ws-1", "object": "item", "lastUpdatedAt": "ts2"},
    ]
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
            f"{NUCLINO_API_BASE}/items/i_vanished": FakeNuclinoResponse(status_code=404),
        }
    )

    state = {"item_versions": {"i_vanished": "ts1"}}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert len(rows) == 1
    assert rows[0] == {"id": "i_vanished", "_deleted": True}
    assert "i_vanished" not in state["item_versions"]


def test_sync_items_brand_new_item_404_produces_no_unnecessary_tombstone():
    """A brand-new item that 404s during detail fetch produces no tombstone and is omitted."""
    metadata = [
        {
            "id": "i_new_vanished",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "ts1",
        },
    ]
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
            f"{NUCLINO_API_BASE}/items/i_new_vanished": FakeNuclinoResponse(status_code=404),
        }
    )

    state: dict[str, Any] = {"item_versions": {}}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert rows == []
    assert state["item_versions"] == {}


def test_sync_items_empty_sweep_with_previous_state_yields_no_tombstones_and_preserves_state():
    """Empty-sweep safety guard: 0 discovered items when previous state existed preserves state."""
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": []}}
            ),
        }
    )

    state = {"item_versions": {"i1": "ts1", "i2": "ts2"}}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert rows == []
    assert state["item_versions"] == {"i1": "ts1", "i2": "ts2"}


def test_sync_items_malformed_missing_last_updated_at_raises_and_preserves_state():
    """Missing or non-string lastUpdatedAt raises ValueError and preserves previous state."""
    metadata = [
        {
            "id": "i_bad",
            "workspaceId": "ws-1",
            "object": "item",
            "createdAt": "2026-01-01T00:00:00Z",
        },
    ]
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
        }
    )

    state = {"item_versions": {"i1": "ts1"}}
    with pytest.raises(ValueError, match="lastUpdatedAt"):
        list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert state["item_versions"] == {"i1": "ts1"}


def test_sync_items_api_failure_halfway_through_sync_preserves_previous_state():
    """API exception during detail fetching propagates and preserves previous state."""
    metadata = [
        {"id": "i1", "workspaceId": "ws-1", "object": "item", "lastUpdatedAt": "new_ts1"},
        {"id": "i2", "workspaceId": "ws-1", "object": "item", "lastUpdatedAt": "new_ts2"},
    ]
    detail1 = {"id": "i1", "workspaceId": "ws-1", "object": "item", "title": "T1", "content": "C1"}

    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
            f"{NUCLINO_API_BASE}/items/i1": FakeNuclinoResponse(
                {"status": "success", "data": detail1}
            ),
            f"{NUCLINO_API_BASE}/items/i2": FakeNuclinoResponse(status_code=500),
        }
    )

    state = {"item_versions": {"i1": "old_ts1"}}
    with pytest.raises(requests.exceptions.HTTPError):
        list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert state["item_versions"] == {"i1": "old_ts1"}


def test_sync_items_two_changed_items_with_identical_timestamps_handled_correctly():
    """Two items sharing the identical changed timestamp are both fetched and recorded."""
    metadata = [
        {
            "id": "i1",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-06-01T12:00:00Z",
        },
        {
            "id": "i2",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-06-01T12:00:00Z",
        },
    ]
    detail1 = {"id": "i1", "workspaceId": "ws-1", "object": "item", "title": "T1", "content": "C1"}
    detail2 = {"id": "i2", "workspaceId": "ws-1", "object": "item", "title": "T2", "content": "C2"}

    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
            f"{NUCLINO_API_BASE}/items/i1": FakeNuclinoResponse(
                {"status": "success", "data": detail1}
            ),
            f"{NUCLINO_API_BASE}/items/i2": FakeNuclinoResponse(
                {"status": "success", "data": detail2}
            ),
        }
    )

    state = {"item_versions": {"i1": "old_ts", "i2": "old_ts"}}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert len(rows) == 2
    assert state["item_versions"] == {
        "i1": "2026-06-01T12:00:00Z",
        "i2": "2026-06-01T12:00:00Z",
    }


def test_sync_items_version_change_detection_uses_inequality():
    """Version comparison uses inequality (!=), capturing restored/earlier timestamps."""
    metadata = [
        {
            "id": "i_reverted",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-01-01T00:00:00Z",
        },
    ]
    detail = {
        "id": "i_reverted",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Reverted",
        "content": "Reverted text",
    }

    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
            f"{NUCLINO_API_BASE}/items/i_reverted": FakeNuclinoResponse(
                {"status": "success", "data": detail}
            ),
        }
    )

    state = {"item_versions": {"i_reverted": "2026-02-01T00:00:00Z"}}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert len(rows) == 1
    assert rows[0]["id"] == "i_reverted"
    assert rows[0]["content"] == "Reverted text"
    assert state["item_versions"] == {"i_reverted": "2026-01-01T00:00:00Z"}


def test_sync_items_unchanged_sync_is_noop():
    """When nothing has changed, sync yields 0 rows and leaves state identical."""
    metadata = [
        {"id": "i1", "workspaceId": "ws-1", "object": "item", "lastUpdatedAt": "ts1"},
        {"id": "c1", "workspaceId": "ws-1", "object": "collection", "lastUpdatedAt": "ts2"},
    ]
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
        }
    )

    state = {"item_versions": {"c1": "ts2", "i1": "ts1"}}
    rows = list(sync_items(session, state, workspace_ids=["ws-1"]))

    assert rows == []
    assert state["item_versions"] == {"c1": "ts2", "i1": "ts1"}


def test_sync_items_multiple_workspaces_participate_in_coherent_version_map():
    """Items across multiple workspaces are synchronized in a single unified state map."""
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": [
                FakeNuclinoResponse(
                    {
                        "status": "success",
                        "data": {
                            "results": [
                                {
                                    "id": "w1_i1",
                                    "workspaceId": "w1",
                                    "object": "item",
                                    "lastUpdatedAt": "t1",
                                }
                            ]
                        },
                    }
                ),
                FakeNuclinoResponse(
                    {
                        "status": "success",
                        "data": {
                            "results": [
                                {
                                    "id": "w2_i2",
                                    "workspaceId": "w2",
                                    "object": "item",
                                    "lastUpdatedAt": "t2",
                                }
                            ]
                        },
                    }
                ),
            ],
            f"{NUCLINO_API_BASE}/items/w1_i1": FakeNuclinoResponse(
                {
                    "status": "success",
                    "data": {
                        "id": "w1_i1",
                        "workspaceId": "w1",
                        "object": "item",
                        "content": "W1",
                    },
                }
            ),
            f"{NUCLINO_API_BASE}/items/w2_i2": FakeNuclinoResponse(
                {
                    "status": "success",
                    "data": {
                        "id": "w2_i2",
                        "workspaceId": "w2",
                        "object": "item",
                        "content": "W2",
                    },
                }
            ),
        }
    )

    state: dict[str, Any] = {}
    rows = list(sync_items(session, state, workspace_ids=["w1", "w2"]))

    assert len(rows) == 2
    assert state["item_versions"] == {"w1_i1": "t1", "w2_i2": "t2"}


# ---------------------------------------------------------------------------
# 11. DLT Resource & Factory Wiring (nuclino_source)
# ---------------------------------------------------------------------------


def test_nuclino_source_created_without_api_key_when_session_supplied(monkeypatch):
    """nuclino_source(session=fake_session) succeeds without an API key or env var."""
    monkeypatch.delenv("NUCLINO_API_KEY", raising=False)
    session = FakeNuclinoSession()
    res = nuclino_source(session=session)
    assert res is not None
    assert res.name == "nuclino_items"


def test_nuclino_source_importable_from_package_root():
    """nuclino_source is exported from the package root __init__.py."""
    from cognee_community_connector_nuclino import nuclino_source as exported_source

    assert exported_source is nuclino_source


def test_nuclino_source_resource_configuration():
    """Returned resource has name, primary key, merge disposition, and _deleted column."""
    session = FakeNuclinoSession()
    res = nuclino_source(session=session)

    assert res.name == "nuclino_items"

    schema = res.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema.get("columns") or {}
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True
    assert columns["_deleted"].get("data_type") == "bool"


def test_nuclino_source_document_source_tag():
    """Resource is tagged with DOCUMENT_SOURCE_ATTR == 'nuclino'."""
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    session = FakeNuclinoSession()
    res = nuclino_source(session=session)
    assert getattr(res, DOCUMENT_SOURCE_ATTR) == "nuclino"


def test_nuclino_source_execution_uses_dlt_resource_state(tmp_path):
    """Resource execution retrieves and updates dlt.current.resource_state()."""
    import dlt

    metadata = [
        {
            "id": "i1",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-01-01T00:00:00Z",
        },
    ]
    detail = {"id": "i1", "workspaceId": "ws-1", "object": "item", "title": "T1", "content": "C1"}
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": metadata}}
            ),
            f"{NUCLINO_API_BASE}/items/i1": FakeNuclinoResponse(
                {"status": "success", "data": detail}
            ),
        }
    )

    db_path = (tmp_path / "test_exec.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_state_exec",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="nuclino_state_test",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(nuclino_source(session=session, workspace_ids=["ws-1"]))

    state = pipeline.state
    sources_state = state.get("sources", {})
    found_versions = False
    for src in sources_state.values():
        res_state = src.get("resources", {}).get("nuclino_items", {})
        if "item_versions" in res_state:
            assert res_state["item_versions"] == {"i1": "2026-01-01T00:00:00Z"}
            found_versions = True
    assert found_versions


def test_nuclino_source_injected_session_passed_through():
    """Injected session is used directly during execution without building a new session."""
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": []}}
            ),
        }
    )
    res = nuclino_source(session=session, workspace_ids=["ws-1"])
    list(res)

    assert len(session.calls) >= 1
    assert session.calls[0][0] == f"{NUCLINO_API_BASE}/items"


def test_nuclino_source_forwards_workspace_ids_and_team_id():
    """Workspace IDs and team ID are forwarded through to the discovery engine."""
    session = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/workspaces": FakeNuclinoResponse(
                {"status": "success", "data": {"results": []}}
            ),
        }
    )
    res = nuclino_source(session=session, team_id="team-99")
    list(res)

    assert len(session.calls) >= 1
    assert session.calls[0][0] == f"{NUCLINO_API_BASE}/workspaces"
    assert session.calls[0][1]["teamId"] == "team-99"


def test_nuclino_source_missing_api_key_raises_on_execution_without_session(monkeypatch):
    """When no session or API key is provided, execution raises ValueError."""
    monkeypatch.delenv("NUCLINO_API_KEY", raising=False)
    res = nuclino_source(workspace_ids=["ws-1"])
    with pytest.raises(Exception, match="Nuclino API key required") as exc_info:
        list(res)
    assert isinstance(exc_info.value.__cause__, ValueError) or isinstance(
        exc_info.value, ValueError
    )


def test_nuclino_source_requires_dlt_raises_import_error(monkeypatch):
    """Simulating dlt missing raises an informative ImportError."""
    import builtins

    orig_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("No module named 'dlt'")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match='requires "dlt"'):
        nuclino_source()


def test_nuclino_source_dlt_pipeline_merge_hard_delete(tmp_path):
    """End-to-end DLT merge test: surviving row remains, deleted row is physically removed."""
    import dlt

    db_path = (tmp_path / "nuclino_merge.db").as_posix()
    pipelines_dir = str(tmp_path / "dlt_pipelines")

    # Run 1: Load item-1 and item-2
    meta_run1 = [
        {
            "id": "item-1",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-01-01T00:00:00Z",
        },
        {
            "id": "item-2",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-01-01T00:00:00Z",
        },
    ]
    detail1 = {
        "id": "item-1",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Doc 1",
        "content": "Content 1",
    }
    detail2 = {
        "id": "item-2",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "Doc 2",
        "content": "Content 2",
    }

    session1 = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": meta_run1}}
            ),
            f"{NUCLINO_API_BASE}/items/item-1": FakeNuclinoResponse(
                {"status": "success", "data": detail1}
            ),
            f"{NUCLINO_API_BASE}/items/item-2": FakeNuclinoResponse(
                {"status": "success", "data": detail2}
            ),
        }
    )

    pipeline1 = dlt.pipeline(
        pipeline_name="nuclino_merge_e2e",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="nuclino_data",
        pipelines_dir=pipelines_dir,
    )
    pipeline1.run(nuclino_source(session=session1, workspace_ids=["ws-1"]))

    with pipeline1.sql_client() as client:
        rows1 = client.execute_sql("SELECT id FROM nuclino_items ORDER BY id")
    assert [row[0] for row in rows1] == ["item-1", "item-2"]

    # Run 2: item-1 is unchanged; item-2 is deleted from Nuclino.
    # The connector yields a tombstone for item-2 with _deleted=True.
    meta_run2 = [
        {
            "id": "item-1",
            "workspaceId": "ws-1",
            "object": "item",
            "lastUpdatedAt": "2026-01-01T00:00:00Z",
        },
    ]
    session2 = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": meta_run2}}
            ),
        }
    )

    pipeline2 = dlt.pipeline(
        pipeline_name="nuclino_merge_e2e",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="nuclino_data",
        pipelines_dir=pipelines_dir,
    )
    pipeline2.run(nuclino_source(session=session2, workspace_ids=["ws-1"]))

    with pipeline2.sql_client() as client:
        rows2 = client.execute_sql("SELECT id FROM nuclino_items ORDER BY id")
    # Verify: item-2 was physically hard-deleted from the SQLite table; item-1 remains
    assert [row[0] for row in rows2] == ["item-1"]


def test_nuclino_source_dlt_pipeline_state_persistence(tmp_path):
    """DLT pipeline persists item_versions across runs and avoids re-fetching unchanged items."""
    import dlt

    db_path = (tmp_path / "nuclino_persist.db").as_posix()
    pipelines_dir = str(tmp_path / "dlt_pipelines")

    # Run 1: backfill
    meta = [
        {"id": "i1", "workspaceId": "ws-1", "object": "item", "lastUpdatedAt": "ts1"},
    ]
    detail = {"id": "i1", "workspaceId": "ws-1", "object": "item", "title": "T1", "content": "C1"}
    session1 = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": meta}}
            ),
            f"{NUCLINO_API_BASE}/items/i1": FakeNuclinoResponse(
                {"status": "success", "data": detail}
            ),
        }
    )

    pipeline = dlt.pipeline(
        pipeline_name="nuclino_persist_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="nuclino_ds",
        pipelines_dir=pipelines_dir,
    )
    pipeline.run(nuclino_source(session=session1, workspace_ids=["ws-1"]))
    assert len(session1.calls) == 2  # items listing + items/i1 detail

    # Run 2: same timestamp -> detail GET is skipped because state was restored
    session2 = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": meta}}
            ),
        }
    )
    pipeline.run(nuclino_source(session=session2, workspace_ids=["ws-1"]))
    assert len(session2.calls) == 1  # Only items listing; no items/i1 call

    # Run 3: changed timestamp -> detail GET is executed
    meta_changed = [
        {"id": "i1", "workspaceId": "ws-1", "object": "item", "lastUpdatedAt": "ts2"},
    ]
    detail_v2 = {
        "id": "i1",
        "workspaceId": "ws-1",
        "object": "item",
        "title": "T1",
        "content": "C1 v2",
    }
    session3 = FakeNuclinoSession(
        routes={
            f"{NUCLINO_API_BASE}/items": FakeNuclinoResponse(
                {"status": "success", "data": {"results": meta_changed}}
            ),
            f"{NUCLINO_API_BASE}/items/i1": FakeNuclinoResponse(
                {"status": "success", "data": detail_v2}
            ),
        }
    )
    pipeline.run(nuclino_source(session=session3, workspace_ids=["ws-1"]))
    assert len(session3.calls) == 2  # items listing + items/i1 detail


def test_nuclino_source_intended_for_caller_merge_disposition():
    """Verify resource declares write_disposition='merge' for caller compatibility."""
    res = nuclino_source(session=FakeNuclinoSession())
    schema = res.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"
