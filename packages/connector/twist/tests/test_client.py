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
    TwistRateLimitError,
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


def test_long_retry_after_stops_instead_of_sleeping():
    # cognee holds its dlt staging lock while a source runs.
    client, seen, sleeps = _scripted(httpx.Response(429, headers={"retry-after": "3600"}))
    with pytest.raises(TwistRateLimitError, match="asked to wait 3600s"):
        client.request("v3/channels/get")
    assert sleeps == []
    assert len(seen) == 1


def test_429_that_never_clears_is_a_rate_limit_error():
    client, _, sleeps = _scripted(*[httpx.Response(429, headers={"retry-after": "1"})] * 5)
    with pytest.raises(TwistRateLimitError, match="after 5 tries"):
        client.request("v3/channels/get")
    assert sleeps == [1.0] * 4


def test_non_finite_retry_after_falls_back_to_backoff():
    # float("nan") parses, and max(nan, 0.0) is nan, which time.sleep rejects.
    for value in ("nan", "inf", "-inf"):
        client, _, sleeps = _scripted(
            httpx.Response(429, headers={"retry-after": value}),
            httpx.Response(200, json={"ok": True}),
        )
        assert client.request("v3/channels/get") == {"ok": True}
        assert sleeps == [2.0]


def test_long_wait_on_a_5xx_is_a_rate_limit_too():
    client, _, sleeps = _scripted(httpx.Response(503, headers={"retry-after": "600"}))
    with pytest.raises(TwistRateLimitError, match="asked to wait 600s"):
        client.request("v3/channels/get")
    assert sleeps == []


def test_documented_error_shape_is_read_too():
    # Twist's docs show {"error": [code, string]}; live answers use error_code.
    client, _, _ = _scripted(
        httpx.Response(403, json={"error": [200, "Invalid token"]}),
        httpx.Response(403, json={"error": [109, "Forbidden"]}),
    )
    with pytest.raises(TwistAuthError, match="error 200: Invalid token"):
        client.request("v3/channels/get")
    with pytest.raises(TwistForbiddenError, match="error 109"):
        client.request("v3/channels/get")


def test_odd_200_bodies_stop_instead_of_reading_as_empty():
    for body in (
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"threads": []}),
    ):
        client, _, _ = _scripted(body)
        with pytest.raises(TwistError, match=r"unexpected body|not JSON"):
            list(client.iter_threads(1))
    client, _, _ = _scripted(httpx.Response(200, json=[]))
    with pytest.raises(TwistError, match="unexpected body"):
        client.get_workspace(1)


def test_channels_include_archived_ones_either_way(seeded):
    # Live: the plain call already returned the archived channel; the docs say
    # only archived=true does. The client asks for both and merges them.
    seeded.channels[852086]["archived"] = True
    channels, complete = _client(seeded).list_channels(seeded.workspace_id)
    assert sorted(c["id"] for c in channels) == [852086, 852087]
    assert complete
    calls = [r for r in seeded.requests if r.url.path.endswith("channels/get")]
    assert [r.url.params.get("archived") for r in calls] == [None, "true"]
    # The documented behaviour (plain call without archived ones) works too.
    seeded.plain_hides_archived = True
    channels, _ = _client(seeded).list_channels(seeded.workspace_id)
    assert sorted(c["id"] for c in channels) == [852086, 852087]


def test_a_failing_archived_listing_is_reported_not_raised(seeded):
    seeded.fail_archived_listing = 400
    channels, complete = _client(seeded).list_channels(seeded.workspace_id)
    assert sorted(c["id"] for c in channels) == [852086, 852087]
    assert not complete
    # A rejected token on the archived call itself is never swallowed.
    client, _, _ = _scripted(
        httpx.Response(200, json=[{"id": 1, "name": "general"}]),
        httpx.Response(403, json={"error_code": 200, "error_string": "Invalid token"}),
    )
    with pytest.raises(TwistAuthError):
        client.list_channels(1)


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
    comments = list(_client(seeded).iter_comments(thread["id"], thread["comment_count"]))
    assert [c["content"] for c in comments] == [f"reply {i}" for i in range(5)]


def _windows(fake):
    return [int(r.url.params["from_obj_index"]) for r in fake.requests]


def test_comment_windows_cover_a_low_count_and_stop_at_an_empty_one(seeded, monkeypatch):
    monkeypatch.setattr(twist_module, "PAGE_LIMIT", 2)
    thread = seeded.add_thread(852087, "Long discussion")
    for i in range(5):
        seeded.add_comment(thread["id"], f"reply {i}")
    client = _client(seeded)
    for count in (None, 1, 5):  # unknown, too low, right
        seeded.requests.clear()
        assert len(list(client.iter_comments(thread["id"], count))) == 5
        assert _windows(seeded) == [0, 2, 4]


def test_an_inflated_comment_count_costs_one_extra_request(seeded, monkeypatch):
    monkeypatch.setattr(twist_module, "PAGE_LIMIT", 2)
    thread = seeded.add_thread(852087, "Short")
    for i in range(4):
        seeded.add_comment(thread["id"], f"reply {i}")
    seeded.requests.clear()
    original = seeded.handler

    def budget(request):
        # A regression here would mean millions of requests; fail fast instead.
        assert len(seeded.requests) < 20, "request storm on an inflated comment_count"
        return original(request)

    seeded.handler = budget
    assert len(list(_client(seeded).iter_comments(thread["id"], 10**9))) == 4
    assert _windows(seeded) == [0, 2, 4]  # the empty window ends it


def test_a_stale_zero_count_still_reads_the_first_window(seeded):
    checkout = next(t for t in seeded.threads.values() if t["title"] == "Checkout redesign")
    seeded.requests.clear()
    assert len(list(_client(seeded).iter_comments(checkout["id"], 0))) == 2
    assert _windows(seeded) == [0]


def test_requests_never_ask_for_more_than_500(seeded):
    client = _client(seeded)
    for channel_id in (852087, 852086):
        for thread in client.iter_threads(channel_id):
            list(client.iter_comments(thread["id"], thread["comment_count"]))
    limits = [int(r.url.params["limit"]) for r in seeded.requests if "limit" in r.url.params]
    assert limits
    assert max(limits) == 500


def test_paging_that_does_not_advance_stops_instead_of_looping(monkeypatch):
    monkeypatch.setattr(twist_module, "PAGE_LIMIT", 2)
    same_page = [{"id": 9}, {"id": 8}]
    client, _, _ = _scripted(*[httpx.Response(200, json=same_page) for _ in range(3)])
    with pytest.raises(TwistError, match="did not advance"):
        list(client.iter_threads(1))
