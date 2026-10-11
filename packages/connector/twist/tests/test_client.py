"""TwistClient: auth errors, retries, and paging, against the in-memory fake."""

import httpx
import pytest

from cognee_community_connector_twist import twist as twist_module
from cognee_community_connector_twist.twist import (
    TwistAuthError,
    TwistClient,
    TwistError,
    TwistForbiddenError,
    TwistNotFoundError,
)

TOKEN = "oauth2:test-token"


def _client(fake, token=None, sleeps=None):
    return TwistClient(
        token or fake.token, http_client=fake.http_client(), sleep=(sleeps or []).append
    )


def _scripted(*responses):
    """A client whose transport answers with the given responses in order."""
    queue = list(responses)
    seen = []

    def handler(request):
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    sleeps = []
    client = TwistClient(
        TOKEN, http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append
    )
    return client, seen, sleeps


def test_rejected_token_is_an_auth_error_without_the_token(fake):
    # Live: a bad token gives HTTP 403 with error code 200, not a 401.
    with pytest.raises(TwistAuthError) as err:
        _client(fake, token="oauth2:wrong-secret").get_workspace(fake.workspace_id)
    assert "error 200" in str(err.value)
    assert "wrong-secret" not in str(err.value)


def test_plain_401_is_also_an_auth_error():
    client, _, _ = _scripted(httpx.Response(401, json={}))
    with pytest.raises(TwistAuthError):
        client.request("v3/workspaces/getone", {"id": 1})


def test_403_and_404_map_to_their_own_errors_and_are_not_retried():
    client, seen, _ = _scripted(
        httpx.Response(403, json={"error_code": 109, "error_string": "Forbidden"}),
        httpx.Response(404, json={"error_code": 110, "error_string": "Resource not found"}),
    )
    with pytest.raises(TwistForbiddenError):
        client.request("v3/threads/get", {"channel_id": 1})
    with pytest.raises(TwistNotFoundError):
        client.request("v3/threads/getone", {"id": 1})
    assert len(seen) == 2


def test_429_waits_for_retry_after_then_succeeds():
    client, seen, sleeps = _scripted(
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(200, json={"ok": True}),
    )
    assert client.request("v3/channels/get") == {"ok": True}
    assert sleeps == [7.0]
    assert len(seen) == 2


def test_5xx_and_network_errors_back_off_then_give_up():
    client, seen, sleeps = _scripted(
        httpx.ConnectError("down"),
        httpx.Response(502),
        httpx.Response(503),
        httpx.Response(500),
        httpx.Response(500),
    )
    with pytest.raises(TwistError, match="500"):
        client.request("v3/channels/get")
    assert len(seen) == 5
    assert sleeps == [2.0, 4.0, 8.0, 16.0]


def test_threads_are_paged_by_id_without_gaps(seeded, monkeypatch):
    monkeypatch.setattr(twist_module, "PAGE_LIMIT", 2)
    for i in range(5):
        seeded.add_thread(852087, f"Extra {i}")
    ids = [t["id"] for t in _client(seeded).iter_threads(852087)]
    expected = sorted(t["id"] for t in seeded.threads.values() if t["channel_id"] == 852087)
    assert ids == sorted(ids, reverse=True)
    assert sorted(ids) == expected
    # Every page sent before_id, so the order never switched to last_updated_ts.
    pages = [r for r in seeded.requests if r.url.path.endswith("threads/get")]
    assert len(pages) == 4
    assert all("before_id" in r.url.params for r in pages)


def test_comments_are_read_in_order_across_windows(seeded, monkeypatch):
    monkeypatch.setattr(twist_module, "PAGE_LIMIT", 2)
    thread = seeded.add_thread(852087, "Long discussion")
    for i in range(5):
        seeded.add_comment(thread["id"], f"reply {i}")
    thread = seeded.threads[thread["id"]]
    comments = list(_client(seeded).iter_comments(thread["id"], thread["last_obj_index"]))
    assert [c["content"] for c in comments] == [f"reply {i}" for i in range(5)]


def test_thread_without_comments_makes_no_comment_request(seeded):
    client = _client(seeded)
    assert list(client.iter_comments(1, -1)) == []
    assert not seeded.requests


def test_requests_never_ask_for_more_than_500(seeded):
    client = _client(seeded)
    for channel_id in (852087, 852086):
        for thread in client.iter_threads(channel_id):
            list(client.iter_comments(thread["id"], thread["last_obj_index"]))
    limits = [int(r.url.params["limit"]) for r in seeded.requests if "limit" in r.url.params]
    assert limits
    assert max(limits) == 500


def test_paging_that_does_not_advance_stops_instead_of_looping(monkeypatch):
    monkeypatch.setattr(twist_module, "PAGE_LIMIT", 2)
    same_page = [{"id": 9}, {"id": 8}]
    client, _, _ = _scripted(*[httpx.Response(200, json=same_page) for _ in range(3)])
    with pytest.raises(TwistError, match="did not advance"):
        list(client.iter_threads(1))
