import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cognee_community_connector_twitch import TwitchAPIError, TwitchAuthError, TwitchRateLimitError
from cognee_community_connector_twitch.twitch import _API, _clips, _duration, _known, _sync

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


class Helix:
    """Mutable provider fixture; every request goes through HTTPX MockTransport."""

    def __init__(self):
        self.requests = []
        self.follows = ["10"]
        self.login = "coder"
        self.title = "Learning Rust"
        self.videos = {
            "v1": {
                "id": "v1",
                "title": "Rust ownership",
                "description": "Borrowing",
                "created_at": "2026-10-30T12:00:00Z",
                "duration": "1h2m3s",
                "url": "https://www.twitch.tv/videos/v1",
                "language": "en",
            }
        }
        self.clips = {
            "c1": {
                "id": "c1",
                "title": "Borrow checker",
                "duration": 12.5,
                "created_at": "2026-10-30T12:00:00Z",
                "game_id": "509658",
                "url": "https://clips.twitch.tv/c1",
            }
        }
        self.failure = None
        self.viewer = "viewer"

    def handler(self, request):
        self.requests.append(request)
        assert request.headers["Client-Id"] == "app"
        assert request.headers["Authorization"] == "Bearer secret"
        endpoint = request.url.path.removeprefix("/helix/")
        query = request.url.params
        if self.failure:
            result = self.failure(request)
            if result is not None:
                return result
        if endpoint == "users":
            data = (
                [
                    {
                        "id": query["id"],
                        "login": self.login,
                        "display_name": "Coder",
                        "description": "Programming tutorials",
                    }
                ]
                if "id" in query
                else [{"id": self.viewer}]
            )
        elif endpoint == "channels/followed":
            data = [{"broadcaster_id": value} for value in self.follows]
        elif endpoint == "channels":
            data = [
                {
                    "title": self.title,
                    "game_name": "Software and Game Development",
                    "tags": ["Python"],
                    "broadcaster_language": "en",
                }
            ]
        elif endpoint in ("videos", "clips"):
            items = self.videos if endpoint == "videos" else self.clips
            if "id" in query:
                data = [items[key] for key in query.get_list("id") if key in items]
                if endpoint == "videos" and not data:
                    return httpx.Response(404)
            else:
                data = sorted(items.values(), key=lambda row: row["created_at"], reverse=True)
                if endpoint == "clips":
                    data = [
                        row
                        for row in data
                        if query["started_at"] <= row["created_at"] <= query["ended_at"]
                    ]
        else:
            raise AssertionError(f"Unexpected endpoint: {endpoint}")
        return httpx.Response(200, json={"data": data, "pagination": {}})

    def api(self):
        return _API(httpx.Client(transport=httpx.MockTransport(self.handler)), "app", "secret")

    def sync(self, state, now=NOW):
        api = self.api()
        try:
            return list(_sync(api, state, now, 30))
        finally:
            api.client.close()


def test_initial_documents_and_node_sets():
    fake, state = Helix(), {}
    rows = fake.sync(state)
    assert {row["id"] for row in rows} == {"channel:10", "video:v1", "clip:c1"}
    assert all(row["cognee_node_set"] == ["channel:coder"] for row in rows)
    content = {row["id"]: json.loads(row["content"]) for row in rows}
    assert content["video:v1"]["duration_seconds"] == 3723
    assert content["video:v1"]["description"] == "Borrowing"
    assert content["clip:c1"]["category"] == "509658"
    assert content["clip:c1"]["duration_seconds"] == 12.5
    assert content["channel:10"]["tags"] == ["Python"]
    assert content["channel:10"]["description"] == "Programming tutorials"
    assert "secret" not in json.dumps(state)
    assert fake.requests[0].url.path == "/helix/users"


def test_incremental_noop_new_content_and_known_id_checks():
    fake, state = Helix(), {}
    fake.sync(state)
    fake.requests.clear()
    assert fake.sync(state, NOW + timedelta(hours=1)) == []
    assert (
        sum(
            request.url.path == "/helix/users" and not request.url.query
            for request in fake.requests
        )
        == 1
    )
    clip_query = next(
        request.url.params
        for request in fake.requests
        if request.url.path.endswith("/clips") and "started_at" in request.url.params
    )
    assert clip_query["started_at"] == "2026-10-31T11:55:00Z"
    assert clip_query["ended_at"] == "2026-10-31T13:00:00Z"
    assert any(request.url.params.get("id") == "v1" for request in fake.requests)
    fake.videos["v2"] = {**fake.videos["v1"], "id": "v2", "created_at": "2026-10-31T13:30:00Z"}
    fake.clips["c2"] = {**fake.clips["c1"], "id": "c2", "created_at": "2026-10-31T13:30:00Z"}
    assert {row["id"] for row in fake.sync(state, NOW + timedelta(hours=2))} == {
        "video:v2",
        "clip:c2",
    }


def test_expired_video_clip_and_final_unfollow():
    fake, state = Helix(), {}
    fake.sync(state)
    fake.videos.clear()  # Explicit id lookup now returns Helix's all-missing 404.
    rows = fake.sync(state, NOW + timedelta(days=1))
    assert rows == [
        {
            "id": "video:v1",
            "title": "",
            "content": "",
            "url": None,
            "cognee_node_set": [],
            "_deleted": True,
        }
    ]
    fake.clips.clear()
    assert fake.sync(state, NOW + timedelta(days=2))[0]["id"] == "clip:c1"
    fake.follows.clear()
    rows = fake.sync(state, NOW + timedelta(days=3))
    assert len(rows) == 1 and rows[0]["id"] == "channel:10" and rows[0]["_deleted"]
    assert state["hashes"] == {} and state["channels"] == {}
    assert fake.sync(state, NOW + timedelta(days=4)) == []


def test_unfollow_deletes_channel_and_all_children():
    fake, state = Helix(), {}
    fake.sync(state)
    fake.follows = []
    rows = fake.sync(state)
    assert {row["id"] for row in rows} == {"channel:10", "video:v1", "clip:c1"}
    assert all(row["_deleted"] for row in rows)


def test_metadata_edit_and_login_change_keep_ids():
    fake, state = Helix(), {}
    original = fake.sync(state)
    fake.login = "renamed"
    fake.title = "Python asyncio"
    rows = fake.sync(state, NOW + timedelta(days=1))
    assert {row["id"] for row in rows} == {row["id"] for row in original}
    assert all(row["cognee_node_set"] == ["channel:renamed"] for row in rows)
    assert not any(row["_deleted"] for row in rows)


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_failed_request_never_yields_deletes_or_advances_state(status, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    fake, state = Helix(), {}
    fake.sync(state)
    before = copy.deepcopy(state)
    fake.failure = lambda req: httpx.Response(status) if req.url.path.endswith("/clips") else None
    iterator = _sync(fake.api(), state, NOW + timedelta(days=1), 30)
    with pytest.raises(TwitchAPIError):
        next(iterator)
    assert state == before


def test_account_switch_and_app_token_refused():
    fake, state = Helix(), {}
    fake.sync(state)
    before = copy.deepcopy(state)
    fake.viewer = "someone-else"
    with pytest.raises(TwitchAuthError, match="another Twitch user"):
        fake.sync(state)
    assert state == before
    fake.failure = lambda _: httpx.Response(200, json={"data": []})
    with pytest.raises(TwitchAuthError, match="user access token"):
        fake.sync({})


def test_generator_interruption_does_not_commit_state():
    fake, state = Helix(), {}
    iterator = _sync(fake.api(), state, NOW, 30)
    next(iterator)
    iterator.close()
    assert state == {}
    assert len(fake.sync(state)) == 3


@pytest.mark.parametrize("endpoint", ["users", "channels/followed", "videos", "clips"])
def test_pagination(endpoint):
    requests = []

    def handler(request):
        requests.append(request)
        after = request.url.params.get("after")
        return httpx.Response(
            200,
            json={
                "data": [{"id": "two" if after else "one"}],
                "pagination": {} if after else {"cursor": "next"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        api = _API(client, "app", "secret")
        assert [row["id"] for row in api.pages(endpoint, user_id="x")] == ["one", "two"]
    assert requests[1].url.params["after"] == "next"
    assert requests[1].url.params["user_id"] == "x"


def test_repeated_pagination_cursor_is_error():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [], "pagination": {"cursor": "same"}})
        )
    ) as client:
        with pytest.raises(TwitchAPIError, match="pagination cursor"):
            list(_API(client, "app", "secret").pages("clips"))


@pytest.mark.parametrize("status", [429, 503])
def test_retry_backoff_and_exhaustion(status, monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr("time.sleep", sleeps.append)

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "1"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TwitchAPIError, match="retries exhausted"):
            _API(client, "app", "secret").get("users")
    assert len(calls) == 4 and sum(sleeps) == 3


def test_rate_limit_recovery_and_remaining_header(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setattr("time.time", lambda: 1000)

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Ratelimit-Reset": "1002"})
        return httpx.Response(
            200, json={"data": []}, headers={"Ratelimit-Remaining": "0", "Ratelimit-Reset": "1003"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        api = _API(client, "app", "secret")
        assert api.get("users") == []
        assert sum(sleeps) == 2
        assert api.get("users") == []
        assert sum(sleeps) == 5


def test_excessive_wait_fails_and_check_active_cancels(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "61"}))
    ) as client:
        with pytest.raises(TwitchRateLimitError, match="wait exceeded"):
            _API(client, "app", "secret").get("users")

        def cancelled():
            raise PermissionError("revoked")

        with pytest.raises(PermissionError, match="revoked"):
            _API(client, "app", "secret", cancelled).get("users")


def test_known_ids_batched_and_all_missing_videos_are_empty():
    queries = []

    def handler(request):
        queries.append(request.url.params.get_list("id"))
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        api = _API(client, "app", "secret")
        assert _known(api, "videos", [str(i) for i in range(205)]) == []
        assert list(map(len, queries)) == [100, 100, 5]
        with pytest.raises(TwitchAPIError):
            api.get("videos", {"user_id": "10"})


def test_saturated_clip_window_splits_and_deduplicates():
    windows = []

    def handler(request):
        params = request.url.params
        windows.append((params["started_at"], params["ended_at"]))
        if len(windows) == 1:
            data = [{"id": str(i)} for i in range(900)]
        else:
            data = [{"id": params["started_at"]}]
        return httpx.Response(200, json={"data": data})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = _clips(_API(client, "app", "secret"), "10", NOW, NOW + timedelta(hours=2))
    assert len(rows) == 2 and len(windows) == 3
    assert windows[1][1] == windows[2][0] == "2026-10-31T13:00:00Z"


@pytest.mark.parametrize(
    "value,expected",
    [("1h2m3s", 3723), ("PT2H3M", 7380), ("0s", 0), ("3m21s", 201), ("12h", 43200)],
)
def test_duration(value, expected):
    assert _duration(value) == expected


@pytest.mark.parametrize("value", ["", "PT", "nonsense", "1d", "2mgarbage"])
def test_invalid_duration(value):
    with pytest.raises(TwitchAPIError):
        _duration(value)


def test_token_not_in_errors(caplog):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"message": "secret"}))
    ) as client:
        with pytest.raises(TwitchAuthError) as error:
            _API(client, "app", "secret").get("users")
    assert "secret" not in str(error.value)
    assert "secret" not in caplog.text
