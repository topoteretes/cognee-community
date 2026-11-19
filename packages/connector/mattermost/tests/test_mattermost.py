"""Unit tests for the Mattermost connector, against ``FakeMattermost`` (see fake_mattermost.py).

Coverage: thread rendering (names not emails, reply order, titles, permalinks, node
sets, input order), the first full read and the since cursor, new replies, edits and
their hidden copies, deleted replies and roots, system posts, the same-millisecond edge,
a full since page (in no order) falling back to a full read, posts deleted while a
channel is read, a channel that keeps changing while it is read, roots missing from a
listing, archived channels (kept or forgotten), lost access, a channel that keeps
failing, dropped channels, rate limits, the HTTP client (retries, dropped connections,
long waits, redirects, a 200 without JSON, ids kept out of errors, the token), config
validation, check_active, the sync stats, and real dlt merges into SQLite.
"""

import http.client
import io
import json
import random
import urllib.error

import pytest
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from dlt.extract.exceptions import ResourceExtractionError
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_mattermost import ALICE, BOB, SERVER, FakeMattermost, channel_id

from cognee_community_connector_mattermost import mattermost_source
from cognee_community_connector_mattermost.mattermost import (
    MattermostAPIError,
    MattermostAuthError,
    MattermostClient,
    MattermostRateLimitError,
    sync_channels,
    thread_to_row,
)

C1, C2 = channel_id(1), channel_id(2)


def _cause(error, kind):
    """The first exception of ``kind`` in an error's chain (dlt wraps what a source raises)."""
    while error is not None:
        if isinstance(error, kind):
            return error
        error = error.__cause__ or error.__context__
    return None


def sync(fake, state, channels=(C1,), stats=None, **kw):
    stats = {} if stats is None else stats
    return list(sync_channels(fake, state, SERVER, list(channels), stats, **kw))


def ids(rows):
    return [(r["id"], r["_deleted"]) for r in rows]


def thread_calls(fake, since=0):
    return [path for _, path, _ in fake.calls[since:] if path.endswith("/thread")]


# --------------------------------------------------------------------- rendering
def test_a_thread_is_one_document_with_names_and_no_emails():
    fake = FakeMattermost()
    root = fake.post("Why did we roll back payments?\nDetails below.")
    fake.post("Latency doubled after the deploy.", user=BOB, root=root)
    rows = sync(fake, {})
    assert ids(rows) == [(root, False)]
    row = rows[0]
    assert row["title"] == "Why did we roll back payments?"
    assert row["url"] == f"{SERVER}/_redirect/pl/{root}"
    assert row["cognee_node_set"] == ["eng/town-square"]
    assert "Channel: Town Square" in row["content"]
    assert "--- Alice Smith (@alice)," in row["content"]
    assert "--- @bob," in row["content"]
    assert row["content"].index("roll back") < row["content"].index("Latency")
    assert "@example.com" not in row["content"], "email addresses never reach a document"


def test_a_thread_renders_the_same_whatever_order_the_server_sends():
    root = {"id": "r" * 26, "user_id": ALICE["id"], "message": "root", "create_at": 1}
    replies = [
        {"id": f"{i:026d}", "user_id": BOB["id"], "message": f"reply {i}", "create_at": 10 + i % 3}
        for i in range(6)
    ]
    users = {ALICE["id"]: ALICE, BOB["id"]: BOB}
    channel = {"display_name": "Town Square", "node_set": "eng/town-square"}
    expected = thread_to_row(root, replies, users, channel, SERVER)
    rng = random.Random(3)
    for _ in range(5):
        shuffled = rng.sample(replies, len(replies))
        shuffled_users = dict(rng.sample(list(users.items()), len(users)))
        assert thread_to_row(root, shuffled, shuffled_users, channel, SERVER) == expected


def test_long_first_lines_are_shortened_and_empty_posts_get_a_placeholder():
    fake = FakeMattermost()
    fake.post("x" * 200)
    attachment = fake.post("")
    fake.posts[attachment]["file_ids"] = ["f1"]
    rows = {r["id"]: r for r in sync(fake, {})}
    long_row = next(r for r in rows.values() if r["id"] != attachment)
    assert len(long_row["title"]) == 80 and long_row["title"].endswith("...")
    assert rows[attachment]["title"] == "(attachment)"


def test_system_posts_and_edit_copies_never_become_documents():
    fake = FakeMattermost()
    fake.post("alice joined the channel", type_="system_join_channel")
    root = fake.post("first version")
    fake.edit(root, "second version")
    rows = sync(fake, {})
    assert ids(rows) == [(root, False)]
    assert "second version" in rows[0]["content"] and "first version" not in rows[0]["content"]


# --------------------------------------------------------------------- the cursor
def test_first_run_reads_the_channel_then_only_changes():
    fake = FakeMattermost()
    a = fake.post("thread a")
    b = fake.post("thread b")
    state = {}
    assert sorted(ids(sync(fake, state))) == sorted([(a, False), (b, False)])
    channel_state = state["channels"][C1]
    assert channel_state["cursor"] == fake.now - 1000, "server time before the read, minus 1 s"
    assert set(channel_state["threads"]) == {a, b}

    fake.post("reply to a", user=BOB, root=a)
    rows = sync(fake, state)
    assert ids(rows) == [(a, False)] and "reply to a" in rows[0]["content"]


def test_nothing_changed_means_no_rows_and_no_thread_reads():
    fake = FakeMattermost()
    fake.post("thread a")
    state = {}
    sync(fake, state)
    sync(fake, state)  # the window still covers the first read's last second
    before = len(fake.calls)
    stats = {}
    assert sync(fake, state, stats=stats) == []
    assert thread_calls(fake, before) == []
    assert stats["scanned"] == 0


def test_a_change_that_keeps_the_text_emits_nothing():
    fake = FakeMattermost()
    root = fake.post("pinned")
    state = {}
    sync(fake, state)
    fake.touch(root)  # a reaction or a pin bumps update_at
    stats = {}
    assert sync(fake, state, stats=stats) == []
    assert stats["skipped"] == 1


def test_listing_pages_that_do_not_move_raise():
    fake = FakeMattermost()
    for i in range(200):
        fake.post(f"t{i}")
    fake.stall_listing = True
    with pytest.raises(MattermostAPIError, match="Every selected"):
        sync(fake, {})
    assert len(fake.calls) < 10


def test_an_edited_reply_rerenders_its_thread_and_its_hidden_copy_is_not_a_delete():
    fake = FakeMattermost()
    root = fake.post("deploy?")
    reply = fake.post("not yet", user=BOB, root=root)
    state = {}
    sync(fake, state)
    fake.edit(reply, "ship it")
    rows = sync(fake, state)
    assert ids(rows) == [(root, False)]
    assert "ship it" in rows[0]["content"] and "not yet" not in rows[0]["content"]


def test_a_deleted_reply_leaves_the_thread_and_a_deleted_root_forgets_it():
    fake = FakeMattermost()
    keep = fake.post("keep me")
    root = fake.post("incident")
    reply = fake.post("secret detail", user=BOB, root=root)
    state = {}
    sync(fake, state)

    fake.delete(reply)
    rows = sync(fake, state)
    assert ids(rows) == [(root, False)] and "secret detail" not in rows[0]["content"]

    fake.delete(root)
    stats = {}
    before = len(fake.calls)
    assert ids(sync(fake, state, stats=stats)) == [(root, True)]
    assert thread_calls(fake, before) == [], "the feed already says the root is gone"
    assert set(state["channels"][C1]["threads"]) == {keep}
    assert stats["deleted"] == 1


def test_a_changed_root_whose_thread_is_gone_is_forgotten():
    fake = FakeMattermost()
    root = fake.post("moved away")
    state = {}
    sync(fake, state)
    fake.post("reply", root=root)
    fake.fail[f"/posts/{root}/thread"] = 404  # moved to another channel, say
    assert ids(sync(fake, state)) == [(root, True)]


def test_two_posts_in_the_same_millisecond_at_the_cursor_are_both_read_once():
    fake = FakeMattermost()
    fake.post("first")
    state = {}
    sync(fake, state)
    sync(fake, state)  # leaves the window of the first, full read
    t = fake.tick()
    a = fake.post("a", at=t)
    rows = sync(fake, state)
    assert ids(rows) == [(a, False)]
    assert state["channels"][C1]["cursor"] == t

    b = fake.post("b", at=t)  # written in the same millisecond, after the run read a
    before = len(fake.calls)
    rows = sync(fake, state)
    assert ids(rows) == [(b, False)], "b is found, and a is not read again"
    assert thread_calls(fake, before) == [f"/posts/{b}/thread"]


def test_a_full_since_page_in_no_order_falls_back_to_a_full_read():
    fake = FakeMattermost()
    gone = fake.post("will be deleted")
    state = {}
    sync(fake, state)
    fake.delete(gone)
    fake.since_order = "shuffled"
    for i in range(1100):
        fake.post(f"burst {i}")
    stats = {}
    rows = sync(fake, state, stats=stats)
    assert stats["scanned"] == 1100, "the posts beyond the first 1000 are not lost"
    assert (gone, True) in ids(rows)
    assert state["channels"][C1]["cursor"] == fake.now - 1000


def test_a_post_deleted_while_the_channel_is_read_makes_the_read_start_over():
    fake = FakeMattermost()
    posts = [fake.post(f"thread {i}") for i in range(450)]
    pages = []

    def delete_once(page):
        pages.append(page)
        if page == 1 and len(pages) == 2:
            fake.delete(posts[-1])  # the newest post: every later page shifts by one

    fake.on_page = delete_once
    state = {}
    rows = sync(fake, state)
    assert pages.count(0) == 2, "the read started over"
    assert set(state["channels"][C1]["threads"]) == set(posts[:-1])
    assert len(rows) == 449


def test_posts_that_keep_being_deleted_fail_the_channel_without_forgetting():
    fake = FakeMattermost()
    fake.add_channel(2, "incidents", "Incidents")
    other = fake.post("incident", channel=2)
    posts = [fake.post(f"t{i}") for i in range(250)]
    # every read deletes a post it already returned, so a later page always shifts
    fake.on_page = lambda page: fake.delete(posts.pop()) if page == 1 else None
    state, stats = {}, {}
    rows = sync(fake, state, channels=(C1, C2), stats=stats)
    assert ids(rows) == [(other, False)]
    assert stats["failed_channels"] == 1 and "threads" not in state["channels"][C1]


def test_a_channel_that_changes_over_1000_times_during_every_read_is_still_synced():
    fake = FakeMattermost()
    root = fake.post("before the burst")

    def burst(page):
        if page == 0:
            for i in range(1001):
                fake.post(f"busy {i}")

    fake.on_page = burst
    stats = {}
    state = {}
    rows = sync(fake, state, stats=stats)
    assert stats["failed"] == 0 and root in state["channels"][C1]["threads"]
    assert len(rows) > 1


def test_a_stored_root_missing_from_a_listing_is_checked_before_it_is_forgotten():
    fake = FakeMattermost()
    a = fake.post("a")
    b = fake.post("b")
    state = {}
    sync(fake, state)
    fake.delete(b)
    fake.since_order = "shuffled"
    for i in range(1000):
        fake.post(f"burst {i}")
    fake.hide_from_listing = {a}  # a page shift skips a, which is still live
    rows = sync(fake, state)
    assert (b, True) in ids(rows) and (a, True) not in ids(rows)
    assert a in state["channels"][C1]["threads"]


def test_a_reply_whose_root_was_skipped_by_the_listing_still_brings_its_thread():
    fake = FakeMattermost()
    root = fake.post("root")
    fake.post("reply", root=root)
    fake.hide_from_listing = {root}
    rows = sync(fake, {})
    assert ids(rows) == [(root, False)] and "reply" in rows[0]["content"]


def test_a_full_read_needs_the_servers_clock():
    fake = FakeMattermost()
    fake.post("a")
    fake.date_header = False
    with pytest.raises(MattermostAPIError, match="Date header"):
        sync(fake, {})


# --------------------------------------------------------------------- channels
def test_an_archived_channel_is_kept_and_no_longer_synced_by_default():
    fake = FakeMattermost()
    root = fake.post("kept")
    state = {}
    sync(fake, state)
    fake.archive()
    stats = {}
    before = len(fake.calls)
    assert sync(fake, state, stats=stats) == []
    assert stats["archived"] == 1
    assert [path for _, path, _ in fake.calls[before:]] == [f"/channels/{C1}"]
    assert root in state["channels"][C1]["threads"]


def test_an_archived_channel_is_forgotten_when_asked_and_read_again_if_restored():
    fake = FakeMattermost()
    root = fake.post("archived soon")
    state = {}
    sync(fake, state)
    fake.archive()
    assert ids(sync(fake, state, forget_archived=True)) == [(root, True)]
    assert C1 not in state["channels"]
    fake.channels[C1]["delete_at"] = 0  # unarchived
    assert ids(sync(fake, state, forget_archived=True)) == [(root, False)]


@pytest.mark.parametrize("status", [403, 404])
def test_lost_access_to_a_channel_forgets_nothing_and_the_others_still_sync(status):
    fake = FakeMattermost()
    fake.add_channel(2, "incidents", "Incidents")
    a = fake.post("in town square")
    b = fake.post("in incidents", channel=2)
    state = {}
    sync(fake, state, channels=(C1, C2))
    fake.delete(a)
    fake.post("new incident", channel=2)
    fake.fail[f"/channels/{C1}"] = status
    stats = {}
    rows = sync(fake, state, channels=(C1, C2), stats=stats)
    assert all(not r["_deleted"] for r in rows), "an error never forgets"
    assert len(rows) == 1 and b in state["channels"][C2]["threads"]
    assert a in state["channels"][C1]["threads"]
    assert stats["failed"] == 1 and stats["failed_channels"] == 1
    assert state["channels"][C1]["failed_runs"] == 1


def test_a_channel_that_keeps_failing_raises_on_its_third_run():
    fake = FakeMattermost()
    fake.add_channel(2, "incidents", "Incidents")
    state = {}
    fake.fail[f"/channels/{C1}"] = 500
    sync(fake, state, channels=(C1, C2))
    sync(fake, state, channels=(C1, C2))
    with pytest.raises(MattermostAPIError, match="3 runs in a row"):
        sync(fake, state, channels=(C1, C2))


def test_a_channel_that_recovers_starts_counting_again():
    fake = FakeMattermost()
    fake.add_channel(2, "incidents", "Incidents")
    state = {}
    fake.fail[f"/channels/{C1}"] = 500
    sync(fake, state, channels=(C1, C2))
    fake.fail = {}
    sync(fake, state, channels=(C1, C2))
    assert "failed_runs" not in state["channels"][C1]


def test_a_dropped_channel_is_forgotten():
    fake = FakeMattermost()
    fake.add_channel(2, "incidents", "Incidents")
    a = fake.post("town square")
    b = fake.post("incident", channel=2)
    state = {}
    sync(fake, state, channels=(C1, C2))
    rows = sync(fake, state, channels=(C2,))
    assert ids(rows) == [(a, True)]
    assert set(state["channels"]) == {C2} and b in state["channels"][C2]["threads"]


def test_a_rate_limit_stops_the_rest_of_the_run_and_keeps_what_was_planned():
    fake = FakeMattermost()
    fake.add_channel(2, "incidents", "Incidents")
    a = fake.post("town square")
    fake.post("incident", channel=2)
    state, stats = {}, {}
    fake.fail[f"/channels/{C2}"] = 429
    rows = sync(fake, state, channels=(C1, C2), stats=stats)
    assert ids(rows) == [(a, False)]
    assert stats["failed_rate_limit"] == 1 and C2 not in state["channels"]


@pytest.mark.parametrize("status", [401, 200])
def test_a_rejected_token_or_a_login_page_ends_the_run(status):
    fake = FakeMattermost()
    fake.fail[f"/channels/{C1}"] = status
    with pytest.raises(MattermostAPIError) as caught:
        sync(fake, {})
    assert caught.value.ends_run


def test_a_failing_user_lookup_falls_back_to_user_ids():
    fake = FakeMattermost()
    fake.post("hello")
    fake.fail["/users/ids"] = 403
    rows = sync(fake, {})
    assert f"--- user {ALICE['id']}," in rows[0]["content"]


def test_a_sync_state_from_another_server_is_refused():
    fake = FakeMattermost()
    state = {}
    sync(fake, state)
    with pytest.raises(ValueError, match="belongs to"):
        list(sync_channels(fake, state, "https://other.example.com", [C1], {}))


def test_a_channel_answer_of_the_wrong_shape_fails_the_channel():
    fake = FakeMattermost()
    fake.add_channel(2, "incidents", "Incidents")
    fake.channels[C1] = {"oops": True}
    stats = {}
    sync(fake, {}, channels=(C1, C2), stats=stats)
    assert stats["failed_channels"] == 1


# --------------------------------------------------------------------- client
class _Response(io.BytesIO):
    def __init__(self, body: bytes, date: str | None = "Wed, 21 Oct 2026 10:00:00 GMT"):
        super().__init__(body)
        self.headers = {"Date": date} if date else {}


def _http_error(status, headers=None):
    return urllib.error.HTTPError(f"{SERVER}/x", status, "err", headers or {}, None)


class _Opener:
    """Answers each request with the next outcome: an exception, or a JSON-able body."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        body = outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode()
        return _Response(body)


def _client(*outcomes, **kw):
    sleeps = []
    client = MattermostClient(SERVER, "mm-secret", sleep=sleeps.append, **kw)
    client._open = _Opener(*outcomes)
    return client, sleeps


def test_the_client_sends_the_token_and_reads_the_servers_clock():
    client, _ = _client({"ok": True})
    payload, server_ms = client.request("GET", "/channels/x/posts", params={"since": 5})
    request = client._open.requests[0]
    assert payload == {"ok": True}
    assert request.full_url == f"{SERVER}/api/v4/channels/x/posts?since=5"
    assert request.get_header("Authorization") == "Bearer mm-secret"
    assert server_ms == 1792576800000


def test_the_client_retries_5xx_and_waits_out_a_short_rate_limit():
    client, sleeps = _client(_http_error(503), _http_error(429, {"Retry-After": "3"}), {"ok": 1})
    assert client.request("GET", "/x")[0] == {"ok": 1}
    assert sleeps == [1.0, 3.0]


def test_a_long_wait_is_not_slept_through():
    client, sleeps = _client(_http_error(429, {"X-Ratelimit-Reset": "3600"}))
    with pytest.raises(MattermostRateLimitError):
        client.request("GET", "/x")
    assert sleeps == []


@pytest.mark.parametrize(
    "blip",
    [
        ConnectionResetError(104, "reset"),
        http.client.RemoteDisconnected("closed"),
        http.client.IncompleteRead(b"{"),
        TimeoutError(),
        urllib.error.URLError("down"),
    ],
)
def test_a_dropped_connection_is_retried(blip):
    client, sleeps = _client(blip, {"ok": True})
    assert client.request("GET", "/x")[0] == {"ok": True}
    assert len(sleeps) == 1


def test_a_server_that_stays_unreachable_ends_the_run():
    client, _ = _client(*[TimeoutError()] * 5)
    with pytest.raises(MattermostAPIError, match="could not be reached") as caught:
        client.request("GET", "/x")
    assert caught.value.ends_run


def test_a_401_ends_the_run_without_the_token_in_the_error():
    client, sleeps = _client(_http_error(401))
    with pytest.raises(MattermostAuthError) as caught:
        client.request("GET", "/x")
    assert sleeps == [] and caught.value.ends_run
    error = caught.value
    while error is not None:
        assert "mm-secret" not in str(error)
        error = error.__cause__ or error.__context__


def test_errors_name_the_endpoint_but_not_the_ids():
    client, _ = _client(_http_error(404))
    post = "p" + "1" * 25
    with pytest.raises(MattermostAPIError) as caught:
        client.request("GET", f"/posts/{post}/thread")
    assert "/posts/{id}/thread" in str(caught.value) and post not in str(caught.value)
    assert caught.value.status == 404 and not caught.value.ends_run


def test_a_redirect_is_not_followed():
    from cognee_community_connector_mattermost.mattermost import _NoRedirect

    client, _ = _client(_http_error(302, {"Location": "https://evil.example/"}))
    with pytest.raises(MattermostAPIError, match="not followed") as caught:
        client.request("GET", "/x")
    assert caught.value.ends_run and len(client._open.requests) == 1
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://e.x/") is None
    assert any(isinstance(h, _NoRedirect) for h in MattermostClient(SERVER, "t")._opener.handlers)


@pytest.mark.parametrize("body", [b"", b"<html>login</html>", b"null", b"42"])
def test_a_200_without_json_ends_the_run(body):
    client, _ = _client(body)
    with pytest.raises(MattermostAPIError, match="without JSON") as caught:
        client.request("GET", "/x")
    assert caught.value.ends_run


def test_check_active_is_asked_before_every_retry_wait():
    checks = []
    client, sleeps = _client(
        _http_error(503), _http_error(503), {"ok": 1}, check_active=lambda: checks.append(1)
    )
    client.request("GET", "/x")
    assert len(checks) == len(sleeps) == 2


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://chat.example.com/", "https://chat.example.com"),
        ("https://Chat.Example.com:8443/mattermost/", "https://chat.example.com:8443/mattermost"),
        ("http://localhost:8065", "http://localhost:8065"),
        ("http://127.0.0.1:8065/", "http://127.0.0.1:8065"),
    ],
)
def test_server_urls_are_normalized(url, expected):
    assert MattermostClient(url, "t").server_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://chat.example.com",  # the token would travel in clear text
        "ftp://chat.example.com",
        "https://chat.example.com/?x=1",
        "https://user:pw@chat.example.com",
        "chat.example.com",
        "",
        None,
    ],
)
def test_server_urls_that_could_leak_the_token_are_refused(url):
    with pytest.raises(ValueError, match="server_url"):
        MattermostClient(url, "t")


@pytest.mark.parametrize("token", ["mm-secret\n", " mm-secret ", "mm-se cret", "mm-\x00secret", ""])
def test_a_token_with_whitespace_is_stripped_or_refused_without_echoing_it(token):
    if token.strip() == "mm-secret":
        assert MattermostClient(SERVER, token)._auth == "Bearer mm-secret"
        return
    with pytest.raises(ValueError) as caught:
        MattermostClient(SERVER, token)
    assert "secret" not in str(caught.value) and "cret" not in str(caught.value)


def test_the_client_repr_hides_the_token():
    assert "mm-secret" not in repr(MattermostClient(SERVER, "mm-secret"))


def test_sync_through_the_real_client_sends_the_token_but_never_stores_it():
    import urllib.parse

    fake = FakeMattermost()
    root = fake.post("We roll back payments.")
    fake.post("Agreed.", user=BOB, root=root)
    token, headers = "mm-secret-token-123", []
    client = MattermostClient(SERVER, token)

    def opener(request):
        url = urllib.parse.urlparse(request.full_url)
        assert f"{url.scheme}://{url.netloc}" == SERVER and url.path.startswith("/api/v4/")
        headers.append(request.get_header("Authorization"))
        params = {k: int(v[0]) for k, v in urllib.parse.parse_qs(url.query).items()}
        body = json.loads(request.data) if request.data else None
        payload, _ = fake.request(
            request.get_method(), url.path.removeprefix("/api/v4"), params=params, body=body
        )
        return _Response(json.dumps(payload).encode())

    client._open = opener
    rows = sync(client, {})
    assert ids(rows) == [(root, False)] and "Agreed." in rows[0]["content"]
    assert headers and all(h == f"Bearer {token}" for h in headers)
    assert token not in json.dumps(rows)


# --------------------------------------------------------------------- factory
def test_source_validates_config():
    fake = FakeMattermost()
    with pytest.raises(ValueError, match="channels"):
        mattermost_source(SERVER, channels=[], client=fake)
    with pytest.raises(ValueError, match="channels"):
        mattermost_source(SERVER, channels=C1, client=fake)
    with pytest.raises(ValueError, match="Info panel"):
        mattermost_source(SERVER, channels=["town-square"], client=fake)
    with pytest.raises(ValueError, match="server_url"):
        mattermost_source("http://chat.example.com", channels=[C1], client=fake)
    with pytest.raises(ValueError, match="token"):
        mattermost_source(SERVER, channels=[C1])
    with pytest.raises(ValueError, match="not both"):
        mattermost_source(SERVER, channels=[C1], client=fake, service=FakeMattermost())


def test_credentials_are_arguments_only(monkeypatch):
    monkeypatch.setenv("MATTERMOST_TOKEN", "from-env")
    monkeypatch.setenv("MATTERMOST_URL", SERVER)
    with pytest.raises(ValueError, match="token"):
        mattermost_source(SERVER, channels=[C1])


@pytest.mark.parametrize(
    "name", ["Team-Chat", "team chat", "teamChat", "1team", "team__chat", "team_", "a" * 52]
)
def test_a_resource_name_dlt_would_rename_is_refused(name):
    with pytest.raises(ValueError, match="resource_name"):
        mattermost_source(SERVER, channels=[C1], resource_name=name, client=FakeMattermost())


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SERVER, "mattermost_chat_example_com"),
        ("http://localhost:8065", "mattermost_localhost_8065"),
        ("https://example.com/team-chat/", "mattermost_example_com_team_chat"),
    ],
)
def test_the_default_name_comes_from_the_server_and_is_what_dlt_writes(url, expected):
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    source = mattermost_source(url, channels=[C1], client=FakeMattermost())
    assert source.name == expected
    assert NamingConvention().normalize_table_identifier(source.name) == source.name


def test_a_very_long_server_name_still_gives_a_valid_name():
    long_url = "https://" + "chat-" * 20 + "example.com"
    name = mattermost_source(long_url, channels=[C1], client=FakeMattermost()).name
    assert len(name) <= 51 and name.startswith("mattermost_chat_chat")


def test_a_cognee_without_table_scoped_cleanup_is_refused(monkeypatch):
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)
    with pytest.raises(RuntimeError, match="cognee 1"):
        mattermost_source(SERVER, channels=[C1], client=FakeMattermost())


def test_service_is_an_alias_for_client(tmp_path):
    fake = FakeMattermost()
    fake.post("hello")
    pipeline = _pipeline(tmp_path)
    pipeline.run(mattermost_source(SERVER, channels=[C1], service=fake))
    assert len(_ids(pipeline, "mattermost_chat_example_com")) == 1


def test_the_resource_is_merge_with_hard_delete_and_document_mode():
    from cognee.tasks.ingestion.dlt_utils import (
        DOCUMENT_SOURCE_ATTR,
        NODE_SET_COLUMN,
        PIPELINE_SCOPE_ATTR,
    )

    source = mattermost_source(SERVER, channels=[C1], client=FakeMattermost())
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "mattermost"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "mattermost_chat_example_com"
    assert source.write_disposition == "merge"
    columns = source.columns
    assert columns["_deleted"]["hard_delete"] is True
    assert columns[NODE_SET_COLUMN]["data_type"] == "json"


def test_check_active_runs_before_every_request():
    fake = FakeMattermost()
    fake.post("hello")
    checks = []

    def check_active():
        checks.append(len(fake.calls))

    source = mattermost_source(SERVER, channels=[C1], client=fake, check_active=check_active)
    list(source)
    request_checks = [n for n in checks if n < len(fake.calls)]
    assert len(set(request_checks)) == len(fake.calls), "one check before each request"


def test_check_active_can_stop_a_sync_before_anything_is_emitted():
    fake = FakeMattermost()
    fake.post("hello")

    def revoked():
        raise PermissionError("access revoked")

    source = mattermost_source(SERVER, channels=[C1], client=fake, check_active=revoked)
    with pytest.raises(ResourceExtractionError) as caught:
        list(source)
    assert _cause(caught.value, PermissionError) is not None
    assert fake.calls == []


# --------------------------------------------------------------------- dlt runs
def _pipeline(tmp_path, name="mattermost_test"):
    import dlt

    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'mm.db'}"),
        dataset_name="mattermost_test",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _ids(pipeline, table):
    with pipeline.sql_client() as sql:
        try:
            return sorted(r[0] for r in sql.execute_sql(f"SELECT id FROM {table}"))
        except DatabaseUndefinedRelation:
            return []


TABLE = "mattermost_chat_example_com"


def test_e2e_dlt_merge_keeps_unchanged_and_removes_deleted(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeMattermost()
    a = fake.post("keep")
    b = fake.post("delete me")
    pipeline.run(mattermost_source(SERVER, channels=[C1], client=fake))
    assert _ids(pipeline, TABLE) == sorted([a, b])

    fake.post("reply", root=a)
    fake.delete(b)
    source = mattermost_source(SERVER, channels=[C1], client=fake)
    pipeline.run(source)
    assert _ids(pipeline, TABLE) == [a]
    assert source.cognee_sync_stats == {
        "scanned": 1,
        "deleted": 1,
        "skipped": 0,
        "archived": 0,
        "failed": 0,
    }
    assert all(type(v) is int for v in source.cognee_sync_stats.values())


def test_a_failed_load_keeps_the_cursor_so_nothing_is_lost(tmp_path, monkeypatch):
    from dlt.pipeline.pipeline import Pipeline

    pipeline, fake = _pipeline(tmp_path), FakeMattermost()
    a = fake.post("first")
    pipeline.run(mattermost_source(SERVER, channels=[C1], client=fake))
    b = fake.post("second")

    def boom(*_args, **_kwargs):
        raise RuntimeError("load failed")

    monkeypatch.setattr(Pipeline, "load", boom)
    with pytest.raises(RuntimeError):
        pipeline.run(mattermost_source(SERVER, channels=[C1], client=fake))
    monkeypatch.undo()
    pipeline.run(mattermost_source(SERVER, channels=[C1], client=fake))
    assert _ids(pipeline, TABLE) == sorted([a, b])


def test_a_source_object_run_again_starts_with_fresh_stats(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeMattermost()
    fake.add_channel(2, "incidents", "Incidents")
    fake.post("hello")
    fake.fail[f"/channels/{C2}"] = 500
    source = mattermost_source(SERVER, channels=[C1, C2], client=fake)
    pipeline.run(source)
    assert source.cognee_sync_stats["failed_channels"] == 1
    fake.fail = {}
    pipeline.run(source)
    assert source.cognee_sync_stats["failed"] == 0
    assert "failed_channels" not in source.cognee_sync_stats


def test_a_channel_that_keeps_failing_fails_the_pipeline_run(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeMattermost()
    fake.add_channel(2, "incidents", "Incidents")
    fake.fail[f"/channels/{C1}"] = 500
    for _ in range(2):
        pipeline.run(mattermost_source(SERVER, channels=[C1, C2], client=fake))
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(mattermost_source(SERVER, channels=[C1, C2], client=fake))
    assert "3 runs in a row" in str(_cause(caught.value, MattermostAPIError))


def test_each_server_gets_its_own_table_and_two_servers_cannot_share_one(tmp_path):
    pipeline = _pipeline(tmp_path)
    one, two = FakeMattermost(), FakeMattermost()
    one.post("one")
    two.post("two")
    pipeline.run(mattermost_source(SERVER, channels=[C1], client=one))
    pipeline.run(mattermost_source("https://chat.other.org", channels=[C1], client=two))
    assert len(_ids(pipeline, TABLE)) == 1 and len(_ids(pipeline, "mattermost_chat_other_org")) == 1
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(
            mattermost_source(
                "https://chat.other.org", channels=[C1], client=two, resource_name=TABLE
            )
        )
    assert "belongs to" in str(_cause(caught.value, ValueError))
