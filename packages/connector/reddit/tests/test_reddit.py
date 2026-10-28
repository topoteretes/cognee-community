"""Unit and dlt-pipeline tests for the Reddit connector. No Reddit account needed.

* Client tests run ``RedditClient`` against ``httpx.MockTransport``.
* Rendering tests cover the post document and comment trees, on recorded answers.
* Sync tests drive ``_iter_rows`` with a fake API built from recorded answers and a
  plain dict as state, and run ``reddit_source`` through a dlt pipeline into a temp
  sqlite destination to prove the incremental sync and forget-on-delete.
"""

import base64
import json
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from conftest import FakeReddit, fixture

from cognee_community_connector_reddit import reddit as reddit_module
from cognee_community_connector_reddit.reddit import (
    RedditAPIError,
    RedditAuthError,
    RedditClient,
    RedditRateLimitedError,
    RedditSourceError,
    _iter_rows,
    _RedditConfig,
    _Thread,
    reddit_source,
    render_post,
)

CLIENT_ID = "app-id-123"
SECRET = "app-secret-456"
AGENT = "script:cognee-test:0.1 (by /u/tester)"
DAY = 86400


def _config(**overrides):
    values = {"subreddits": ("Python",)}
    values.update(overrides)
    return _RedditConfig(**values)


def _run(reddit, state, stats=None, check_active=None, **overrides):
    stats = stats if stats is not None else {}
    config = _config(**overrides)
    return list(_iter_rows(reddit, config, state, stats, check_active, now=reddit.now))


def _live(rows):
    return sorted(r["id"] for r in rows if not r["_deleted"])


def _gone(rows):
    return sorted(r["id"] for r in rows if r["_deleted"])


def _threads(reddit):
    return list(reddit.paths("/comments/"))


# ---------------------------------------------------------------------------
# RedditClient over httpx.MockTransport
# ---------------------------------------------------------------------------
def _token_response(lifetime=3600):
    return httpx.Response(
        200, json={**fixture("token"), "access_token": "tok-1", "expires_in": lifetime}
    )


def _client(handler, *, clock=None, **kwargs):
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    sleeps = []
    times = clock or [0.0]
    client = RedditClient(
        CLIENT_ID,
        SECRET,
        AGENT,
        http=httpx.Client(transport=httpx.MockTransport(record), follow_redirects=False),
        sleep=sleeps.append,
        clock=lambda: times[0],
        **kwargs,
    )
    return client, seen, sleeps


def _api(responses):
    """Answer the token request, then each API request from ``responses`` in turn."""
    queue = list(responses)

    def handler(request):
        if request.url.path == "/api/v1/access_token":
            return _token_response()
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler


def test_client_gets_an_app_only_token_and_sends_it():
    client, seen, _ = _client(_api([httpx.Response(200, json={"ok": True})]))
    assert client.get("/r/Python/new", {"limit": 100}) == {"ok": True}
    token, call = seen
    assert str(token.url) == "https://www.reddit.com/api/v1/access_token"
    basic = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    assert token.headers["Authorization"] == f"Basic {basic}"
    assert token.content == b"grant_type=client_credentials"
    assert token.headers["User-Agent"] == AGENT
    assert call.url.host == "oauth.reddit.com" and call.url.path == "/r/Python/new"
    assert call.url.params["raw_json"] == "1" and call.url.params["limit"] == "100"
    assert call.headers["Authorization"] == "bearer tok-1"
    assert call.headers["User-Agent"] == AGENT


def test_client_uses_the_password_grant_with_an_account():
    client, seen, _ = _client(
        _api([httpx.Response(200, json={})]), username="tester", password="pw-789"
    )
    client.get("/api/info")
    assert dict(httpx.QueryParams(seen[0].content.decode())) == {
        "grant_type": "password",
        "username": "tester",
        "password": "pw-789",
    }


def test_client_posts_a_form_body():
    client, seen, _ = _client(_api([httpx.Response(200, json={"json": {}})]))
    client.post("/api/morechildren", {"api_type": "json", "children": "a,b"})
    assert seen[1].method == "POST"
    assert dict(httpx.QueryParams(seen[1].content.decode())) == {
        "api_type": "json",
        "children": "a,b",
    }


def test_token_is_renewed_before_it_expires():
    clock = [0.0]
    client, seen, _ = _client(_api([httpx.Response(200, json={})]), clock=clock)
    client.get("/a")
    clock[0] = 3600 - 30
    client.get("/b")
    assert [r.url.path for r in seen] == [
        "/api/v1/access_token",
        "/a",
        "/api/v1/access_token",
        "/b",
    ]


def test_a_401_renews_the_token_once_then_raises():
    client, seen, _ = _client(_api([httpx.Response(401), httpx.Response(200, json={"ok": 1})]))
    assert client.get("/a") == {"ok": 1}
    assert [r.url.path for r in seen].count("/api/v1/access_token") == 2
    client, _, _ = _client(_api([httpx.Response(401)]))
    with pytest.raises(RedditAuthError, match="token"):
        client.get("/a")


@pytest.mark.parametrize(
    "answer",
    [httpx.Response(401), httpx.Response(200, json={"error": "invalid_grant"})],
)
def test_rejected_credentials_raise_without_the_secret(answer):
    def handler(request):
        return answer

    client, _, _ = _client(handler)
    with pytest.raises(RedditAuthError, match="credentials") as caught:
        client.get("/a")
    assert SECRET not in str(caught.value) and CLIENT_ID not in str(caught.value)


def test_rate_limit_waits_for_retry_after_then_gives_up_on_a_long_one():
    client, _, sleeps = _client(
        _api([httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json={})])
    )
    client.get("/a")
    assert sleeps == [3.0]
    client, _, sleeps = _client(_api([httpx.Response(429, headers={"Retry-After": "120"})]))
    with pytest.raises(RedditRateLimitedError):
        client.get("/a")
    assert sleeps == []


def test_a_spent_rate_limit_window_is_waited_out_or_stops():
    used_up = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "5", "x-ratelimit-used": "100"}
    client, _, sleeps = _client(_api([httpx.Response(200, json={}, headers=used_up)]))
    client.get("/a")
    client.get("/b")
    assert sleeps == [5.0]
    long_reset = {**used_up, "x-ratelimit-reset": "400"}
    client, _, _ = _client(_api([httpx.Response(200, json={}, headers=long_reset)]))
    client.get("/a")
    with pytest.raises(RedditRateLimitedError):
        client.get("/b")


def test_server_and_transport_errors_are_retried():
    client, _, sleeps = _client(_api([httpx.Response(503), httpx.Response(200, json={"ok": 1})]))
    assert client.get("/a") == {"ok": 1} and sleeps == [1.0]

    def broken(request):
        if request.url.path == "/api/v1/access_token":
            return _token_response()
        raise httpx.ConnectError("down")

    client, _, sleeps = _client(broken)
    with pytest.raises(RedditAPIError, match="ConnectError"):
        client.get("/a")
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_the_wait_budget_stops_retries():
    client, _, _ = _client(_api([httpx.Response(503)]), max_sleep=2.0)
    with pytest.raises(RedditRateLimitedError):
        client.get("/a")


def test_check_active_runs_after_every_wait():
    checks = []
    client, _, _ = _client(
        _api([httpx.Response(503), httpx.Response(200, json={})]),
        check_active=lambda: checks.append(1),
    )
    client.get("/a")
    assert checks == [1]


def test_a_redirect_is_not_followed_and_errors_carry_no_body():
    client, _, _ = _client(
        _api([httpx.Response(302, headers={"Location": "https://www.reddit.com/search"})])
    )
    with pytest.raises(RedditAPIError) as caught:
        client.get("/r/nosuchsub/new")
    assert caught.value.status == 302
    client, _, _ = _client(_api([httpx.Response(403, json=fixture("forbidden"))]))
    with pytest.raises(RedditAPIError) as caught:
        client.get("/r/q/new")
    assert caught.value.status == 403 and "Forbidden" not in str(caught.value)


def test_a_200_that_is_not_json_raises():
    client, _, _ = _client(_api([httpx.Response(200, text="<html>")]))
    with pytest.raises(RedditAPIError, match="not JSON"):
        client.get("/a")


def test_bad_credentials_are_refused_without_echoing_them():
    for bad in ["", "  ", "has\nnewline", "nön-ascii"]:
        with pytest.raises(ValueError, match="invalid format") as caught:
            RedditClient(bad, SECRET, AGENT)
        assert bad.strip() == "" or bad not in str(caught.value)
    assert SECRET not in repr(RedditClient(CLIENT_ID, SECRET, AGENT))


def test_an_owned_http_client_does_not_follow_redirects():
    client = RedditClient(CLIENT_ID, SECRET, AGENT)
    assert client._http.follow_redirects is False
    client.close()


def test_close_only_closes_its_own_http_client():
    http = httpx.Client()
    RedditClient(CLIENT_ID, SECRET, AGENT, http=http).close()
    assert not http.is_closed
    own = RedditClient(CLIENT_ID, SECRET, AGENT)
    own.close()
    assert own._http.is_closed


# ---------------------------------------------------------------------------
# Rendering, on recorded answers
# ---------------------------------------------------------------------------
def test_the_recorded_thread_is_read_from_the_second_listing():
    recorded = fixture("comments")
    thread = _Thread("t3_2gmzqe", 8)
    thread.add(recorded[1]["data"]["children"], thread.top, 0)
    assert [c["author"] for c in thread.top] == ["paneer_burrito"]
    assert [r["author"] for r in thread.top[0]["replies"]] == ["bboe"]
    # The first listing holds only the post.
    assert [c["kind"] for c in recorded[0]["data"]["children"]] == ["t3"]


def test_recorded_morechildren_answers_attach_under_their_parents():
    things = fixture("morechildren")["json"]["data"]["things"]
    comments = [t["data"] for t in things if t["kind"] == "t1"]
    thread = _Thread("t3_3hahrw", 50)
    # The comments the stub hung under, as the first page of the thread held them.
    own = {f"t1_{c['id']}" for c in comments}
    for parent in {c["parent_id"] for c in comments} - own - {"t3_3hahrw"}:
        thread.nodes[parent] = ({"author": "x", "body": "x", "replies": []}, 0)
    thread.attach(things)
    assert own <= set(thread.nodes)
    assert not thread.truncated


def test_a_continue_this_thread_stub_marks_the_thread_truncated():
    thread = _Thread("t3_x", 20)
    thread.add([fixture("continue_thread")], thread.top, 10)
    assert thread.truncated and not thread.more


def test_render_post_has_the_header_body_and_comment_tree():
    post = {
        "id": "abc",
        "title": "  Release notes  ",
        "selftext": "Version 2 is out.",
        "author": "alice",
        "subreddit": "Python",
        "link_flair_text": "News",
        "created_utc": 1_793_000_000.0,
        "is_self": True,
        "url": "https://www.reddit.com/r/Python/comments/abc/release_notes/",
        "permalink": "/r/Python/comments/abc/release_notes/",
        "score": 999,
        "ups": 999,
    }
    comments = [
        {
            "author": "bob",
            "body": "Great\nwork",
            "replies": [
                {"author": "alice", "body": "Thanks", "replies": []},
            ],
        },
        {"author": "carol", "body": "[deleted]", "replies": []},
        {
            "author": "dave",
            "body": "[removed]",
            "replies": [
                {"author": "erin", "body": "Why?", "replies": []},
            ],
        },
    ]
    row = render_post(post, comments, truncated=True)
    assert row["id"] == "t3_abc" and row["title"] == "Release notes"
    assert row["url"] == "https://www.reddit.com/r/Python/comments/abc/release_notes/"
    assert row["cognee_node_set"] == ["r/Python"]
    assert row["content"] == (
        "Subreddit: r/Python\nAuthor: u/alice\nFlair: News\nPosted: 2026-10-26\n\n"
        "Version 2 is out.\n\n"
        "Comments:\n- u/bob: Great\n  work\n  - u/alice: Thanks\n- [deleted]\n  - u/erin: Why?\n\n"
        "(Some comments of this thread were not loaded.)"
    )
    assert "999" not in row["content"]


def test_a_link_post_without_comments_shows_its_link():
    post = {**fixture("comments")[0]["data"]["children"][0]["data"]}
    post.update(is_self=False, url="https://example.com/x", selftext="")
    row = render_post(post, None, truncated=False)
    assert "Link: https://example.com/x" in row["content"]
    assert "Comments:" not in row["content"]


def test_row_becomes_a_reddit_document_in_its_subreddit_node_set():
    post = fixture("comments")[0]["data"]["children"][0]["data"]
    result = render_post(post, [], truncated=False)
    dlt_row = SimpleNamespace(
        table_name="reddit_documents",
        primary_key_value=result["id"],
        row_data=result,
        content_hash="h",
    )
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, result["id"]), "reddit")
    assert item.data.startswith("# [PRAW] HTTPS enabled PRAW testing needed")
    assert item.system_metadata["source"] == "reddit"
    assert item.node_set == ["reddit:r/redditdev"]


# ---------------------------------------------------------------------------
# Sync: first run, new posts, changes
# ---------------------------------------------------------------------------
def test_first_sync_reads_new_posts_with_their_threads(reddit):
    first = reddit.add_post("Python", "Async tips", "Use gather.")
    second = reddit.add_post("Python", "Packaging", "Use uv.", flair="Help")
    reply = reddit.comment(first, "Nice")
    reddit.comment(first, "Agreed", parent=reply)
    stats = {}
    rows = _run(reddit, {}, stats)
    by_id = {r["id"]: r for r in rows}
    assert sorted(by_id) == [f"t3_{first['id']}", f"t3_{second['id']}"]
    assert "- u/bob: Nice\n  - u/bob: Agreed" in by_id[f"t3_{first['id']}"]["content"]
    assert reddit.paths("/r/") == ["/r/Python/new"]
    assert sorted(_threads(reddit)) == sorted(
        [f"/comments/{first['id']}", f"/comments/{second['id']}"]
    )
    assert stats["emitted"] == 2 and stats["scanned"] == 2


def test_an_unchanged_run_reads_no_thread(reddit):
    reddit.add_post("Python", "A", "a")
    reddit.add_post("Python", "B", "b")
    state = {}
    _run(reddit, state)
    reddit.calls.clear()
    stats = {}
    assert _run(reddit, state, stats) == []
    # The "before" page, the newest post to tell "nothing new" from a lost anchor,
    # and the deletion check.
    assert [path for _m, path, _p in reddit.calls] == ["/r/Python/new"] * 2 + ["/api/info"]
    assert reddit.calls[1][2]["limit"] == 1
    assert reddit.calls[0][2]["before"] == state["subs"]["python"]["anchor"]
    assert stats["unchanged"] == 2


def test_new_posts_come_through_the_before_anchor(reddit):
    reddit.add_post("Python", "Old", "o")
    state = {}
    _run(reddit, state)
    new = reddit.add_post("Python", "New", "n")
    reddit.calls.clear()
    assert _live(_run(reddit, state)) == [f"t3_{new['id']}"]
    assert state["subs"]["python"]["anchor"] == new["name"]


def test_more_new_posts_than_a_page_are_all_read(reddit):
    reddit.add_post("Python", "Seed", "s")
    state = {}
    _run(reddit, state)
    new = [reddit.add_post("Python", f"Post {i}", "x") for i in range(130)]
    rows = _run(reddit, state, max_requests=1000)
    assert _live(rows) == sorted(f"t3_{p['id']}" for p in new)
    assert state["subs"]["python"]["anchor"] == new[-1]["name"]


def test_a_removed_anchor_falls_back_to_the_anchor_time(reddit):
    reddit.add_post("Python", "Older", "o")
    anchor = reddit.add_post("Python", "Anchor", "a")
    state = {}
    _run(reddit, state)
    reddit.remove(anchor)
    new = reddit.add_post("Python", "After the anchor", "n")
    rows = _run(reddit, state)
    assert _live(rows) == [f"t3_{new['id']}"]
    assert _gone(rows) == [f"t3_{anchor['id']}"]


def test_an_empty_before_page_with_nothing_newer_reads_nothing(reddit):
    reddit.add_post("Python", "Only", "o")
    state = {}
    _run(reddit, state)
    reddit.calls.clear()
    assert _run(reddit, state) == []
    assert reddit.paths("/r/") == ["/r/Python/new", "/r/Python/new"]


def test_the_first_sync_reads_at_most_first_sync_posts(reddit):
    posts = [reddit.add_post("Python", f"P{i}", "x") for i in range(5)]
    rows = _run(reddit, {}, first_sync_posts=3)
    assert _live(rows) == sorted(f"t3_{p['id']}" for p in posts[-3:])


def test_an_edited_post_and_a_new_comment_are_read_again(reddit):
    post = reddit.add_post("Python", "Q", "first")
    other = reddit.add_post("Python", "Other", "o")
    state = {}
    _run(reddit, state)
    reddit.edit(post, selftext="second")
    reddit.comment(other, "A reply")
    reddit.calls.clear()
    rows = _run(reddit, state)
    contents = {r["id"]: r["content"] for r in rows}
    assert "second" in contents[f"t3_{post['id']}"]
    assert "- u/bob: A reply" in contents[f"t3_{other['id']}"]


def test_votes_alone_do_not_read_a_thread_again(reddit):
    post = reddit.add_post("Python", "Q", "body")
    state = {}
    _run(reddit, state)
    post.update(score=5000, ups=5000, upvote_ratio=0.5)
    reddit.calls.clear()
    assert _run(reddit, state) == [] and _threads(reddit) == []


def test_a_thread_whose_text_came_out_the_same_is_not_emitted(reddit):
    post = reddit.add_post("Python", "Q", "body")
    state = {}
    _run(reddit, state)
    reddit.edit(post)
    reddit.calls.clear()
    stats = {}
    assert _run(reddit, state, stats) == []
    assert stats["unchanged"] == 1 and _threads(reddit) == [f"/comments/{post['id']}"]


def test_an_old_post_is_only_checked_for_deletion(reddit):
    post = reddit.add_post("Python", "Old", "body")
    state = {}
    _run(reddit, state)
    reddit.now += 30 * DAY
    reddit.edit(post, selftext="changed much later")
    reddit.calls.clear()
    assert _run(reddit, state) == [] and _threads(reddit) == []
    reddit.remove(post)
    assert _gone(_run(reddit, state)) == [f"t3_{post['id']}"]


# ---------------------------------------------------------------------------
# Sync: forget-on-delete
# ---------------------------------------------------------------------------
def test_a_removed_link_post_is_forgotten(reddit):
    reddit.add_post("Python", "Keep", "k")
    post = reddit.add_post("Python", "Link", link="https://example.com/a")
    state = {}
    _run(reddit, state)
    reddit.remove(post)
    assert post["selftext"] == ""
    assert _gone(_run(reddit, state)) == [f"t3_{post['id']}"]


@pytest.mark.parametrize("how", ["moderator", "reddit", "deleted", "copyright_takedown"])
def test_a_removed_or_deleted_post_is_forgotten(reddit, how):
    keep = reddit.add_post("Python", "Keep", "k")
    post = reddit.add_post("Python", "Gone", "g")
    state = {}
    _run(reddit, state)
    reddit.remove(post, how)
    stats = {}
    rows = _run(reddit, state, stats)
    assert _gone(rows) == [f"t3_{post['id']}"] and _live(rows) == []
    assert stats["deleted"] == 1 and f"t3_{keep['id']}" not in _gone(rows)


def test_a_post_reddit_no_longer_returns_is_forgotten(reddit):
    reddit.add_post("Python", "Keep", "k")
    post = reddit.add_post("Python", "Purged", "p")
    state = {}
    _run(reddit, state)
    reddit.purge(post)
    assert _gone(_run(reddit, state)) == [f"t3_{post['id']}"]


def test_the_recorded_removed_post_counts_as_gone():
    posts = [c["data"] for c in fixture("info")["data"]["children"]]
    assert [p["id"] for p in posts if reddit_module._gone(p)] == ["5dec21"]
    assert posts[0]["selftext"] == "[removed]"


def test_a_deleted_account_alone_keeps_the_post(reddit):
    post = reddit.add_post("Python", "Q", "Still here")
    state = {}
    _run(reddit, state)
    post["author"] = "[deleted]"
    assert _gone(_run(reddit, state)) == []


def test_a_post_that_drops_off_the_listing_stays(reddit):
    old = reddit.add_post("Python", "Old", "o")
    state = {}
    _run(reddit, state)
    for i in range(120):
        reddit.add_post("Python", f"Newer {i}", "n")
    rows = _run(reddit, state, max_requests=1000)
    assert f"t3_{old['id']}" not in _gone(rows)
    assert old["id"] in state["subs"]["python"]["posts"]


def test_an_info_answer_without_any_post_is_confirmed_by_the_next_run(reddit):
    post = reddit.add_post("Python", "Q", "q")
    state = {}
    _run(reddit, state)
    reddit.info_drops.add(post["name"])
    assert _run(reddit, state) == []
    assert _gone(_run(reddit, state)) == [f"t3_{post['id']}"]


def test_a_deselected_subreddit_is_forgotten(reddit):
    reddit.add_sub("rust")
    py = reddit.add_post("Python", "P", "p")
    rs = reddit.add_post("rust", "R", "r")
    state = {}
    _run(reddit, state, subreddits=("Python", "rust"))
    rows = _run(reddit, state, subreddits=("Python",))
    assert _gone(rows) == [f"t3_{rs['id']}"]
    assert f"t3_{py['id']}" not in _gone(rows) and "rust" not in state["subs"]


def test_a_subreddit_that_cannot_be_read_on_its_first_sync_raises(reddit):
    with pytest.raises(RedditSourceError, match=r"r/nosuchsub cannot be read \(HTTP 302\)"):
        _run(reddit, {}, subreddits=("nosuchsub",))
    reddit.errors["/r/Python/new"] = 403
    with pytest.raises(RedditSourceError, match="HTTP 403"):
        _run(reddit, {})


@pytest.mark.parametrize("status", [403, 404])
def test_a_known_subreddit_that_becomes_unreadable_is_kept(reddit, status):
    post = reddit.add_post("Python", "Q", "q")
    state = {}
    _run(reddit, state)
    reddit.errors["/r/Python/new"] = status
    reddit.purge(post)
    reddit.calls.clear()
    stats = {}
    assert _run(reddit, state, stats) == []
    assert stats["unreadable"] == 1 and reddit.paths("/api/info") == []
    assert post["id"] in state["subs"]["python"]["posts"]


# ---------------------------------------------------------------------------
# Sync: comment trees
# ---------------------------------------------------------------------------
def test_hidden_comments_beyond_the_first_page_are_loaded_from_morechildren(reddit, monkeypatch):
    monkeypatch.setattr(reddit_module, "_COMMENT_LIMIT", 3)
    post = reddit.add_post("Python", "Busy", "b")
    for i in range(8):
        reddit.comment(post, f"comment {i}")
    stats = {}
    rows = _run(reddit, {}, stats)
    content = rows[0]["content"]
    assert all(f"comment {i}" in content for i in range(8))
    assert reddit.paths("/api/morechildren") == ["/api/morechildren"]
    assert stats["truncated"] == 0 and "not loaded" not in content
    assert reddit.calls[-1][2]["link_id"] == f"t3_{post['id']}"


def test_a_thread_beyond_the_more_limit_says_it_was_cut(reddit, monkeypatch):
    monkeypatch.setattr(reddit_module, "_COMMENT_LIMIT", 2)
    post = reddit.add_post("Python", "Busy", "b")
    for i in range(6):
        reddit.comment(post, f"comment {i}")
    stats = {}
    rows = _run(reddit, {}, stats, max_more_requests=0)
    assert "(Some comments of this thread were not loaded.)" in rows[0]["content"]
    assert stats["truncated"] == 1 and reddit.paths("/api/morechildren") == []


def test_hidden_replies_deeper_than_the_limit_are_cut(reddit, monkeypatch):
    monkeypatch.setattr(reddit_module, "_COMMENT_LIMIT", 1)
    post = reddit.add_post("Python", "Deep", "d")
    reddit.comment(post, "first")
    parent = None
    for depth in range(5):
        parent = reddit.comment(post, f"hidden level {depth}", parent=parent)
    stats = {}
    rows = _run(reddit, {}, stats, max_comment_depth=2)
    content = rows[0]["content"]
    # /api/morechildren hands the whole hidden branch back flat.
    assert reddit.paths("/api/morechildren")
    assert "hidden level 2" in content and "hidden level 3" not in content
    assert stats["truncated"] == 1


def test_replies_deeper_than_the_limit_are_cut(reddit):
    post = reddit.add_post("Python", "Deep", "d")
    parent = None
    for depth in range(6):
        parent = reddit.comment(post, f"level {depth}", parent=parent)
    rows = _run(reddit, {}, max_comment_depth=2)
    content = rows[0]["content"]
    assert "level 2" in content and "level 3" not in content
    assert "not loaded" in content
    assert reddit.calls[-1][2]["depth"] == 3


def test_without_comments_no_thread_is_read(reddit):
    post = reddit.add_post("Python", "Q", "body")
    reddit.comment(post, "ignored")
    rows = _run(reddit, {}, include_comments=False)
    assert _live(rows) == [f"t3_{post['id']}"] and _threads(reddit) == []
    assert "Comments:" not in rows[0]["content"]


def test_a_thread_that_answers_404_is_forgotten_and_403_kept(reddit):
    first = reddit.add_post("Python", "A", "a")
    second = reddit.add_post("Python", "B", "b")
    state = {}
    _run(reddit, state)
    reddit.edit(first, selftext="a2")
    reddit.edit(second, selftext="b2")
    reddit.errors[f"/comments/{first['id']}"] = 404
    reddit.errors[f"/comments/{second['id']}"] = 403
    stats = {}
    rows = _run(reddit, state, stats)
    assert _gone(rows) == [f"t3_{first['id']}"]
    assert stats["unreadable"] == 1 and second["id"] in state["subs"]["python"]["posts"]


def test_a_thread_that_is_not_two_listings_raises(reddit):
    reddit.add_post("Python", "Q", "q")
    original = reddit.get

    def broken(path, params=None):
        answer = original(path, params)
        return answer[:1] if path.startswith("/comments/") else answer

    reddit.get = broken
    with pytest.raises(RedditAPIError, match="two listings"):
        _run(reddit, {})


def test_a_listing_in_another_shape_raises_and_malformed_posts_are_skipped(reddit):
    reddit.add_post("Python", "Q", "q")
    original = reddit.get

    def odd(path, params=None):
        answer = original(path, params)
        if path.endswith("/new"):
            answer["data"]["children"].append({"kind": "t3", "data": {"id": 5}})
            answer["data"]["children"].append({"kind": "t5", "data": {"id": "x"}})
        return answer

    reddit.get = odd
    stats = {}
    assert len(_live(_run(reddit, {}, stats))) == 1 and stats["skipped"] == 2
    reddit.get = lambda path, params=None: {"error": 500}
    with pytest.raises(RedditAPIError, match="unexpected shape"):
        _run(reddit, {"subs": {}})


# ---------------------------------------------------------------------------
# Sync: bounded runs
# ---------------------------------------------------------------------------
def test_budget_pauses_without_failing_and_resumes(reddit):
    posts = [reddit.add_post("Python", f"P{i}", "x") for i in range(6)]
    state = {}
    seen = set()
    runs = []
    for _ in range(10):
        stats = {}
        seen.update(_live(_run(reddit, state, stats, max_requests=3)))
        runs.append(stats["failed_budget"])
        if not stats["failed_budget"]:
            break
        assert stats["failed"] == 0
    assert runs[0] == 1 and len(runs) > 1
    assert seen == {f"t3_{p['id']}" for p in posts} and state["pending"] == []


def test_budget_never_stops_a_run_before_any_progress(reddit):
    post = reddit.add_post("Python", "Q", "q")
    state = {}
    rows = []
    for _ in range(5):
        rows += _run(reddit, state, max_requests=1)
    assert _live(rows) == [f"t3_{post['id']}"]


def test_a_request_budget_of_one_still_finds_posts_after_a_lost_anchor(reddit):
    reddit.add_post("Python", "Older", "o")
    anchor = reddit.add_post("Python", "Anchor", "a")
    state = {}
    _run(reddit, state)
    reddit.remove(anchor)
    new = reddit.add_post("Python", "New", "n")
    rows = []
    for _ in range(6):
        rows += _run(reddit, state, max_requests=1)
    assert f"t3_{new['id']}" in _live(rows)


def test_every_synced_post_gets_its_deletion_check_in_turn(reddit):
    posts = [reddit.add_post("Python", f"P{i}", "x") for i in range(230)]
    state = {}
    _run(reddit, state, max_requests=1000, first_sync_posts=300)
    reddit.now += 30 * DAY
    # Only posts of the last batch go, so only a rotation reaches them.
    removed = sorted(posts, key=lambda p: p["id"])[-10:]
    for post in removed:
        reddit.remove(post)
    gone = set()
    for _ in range(4):
        gone.update(_gone(_run(reddit, state, max_requests=3)))
    assert gone == {f"t3_{p['id']}" for p in removed}


def test_a_rate_limit_stops_cleanly_and_keeps_progress(reddit):
    posts = [reddit.add_post("Python", f"P{i}", "x") for i in range(3)]
    state = {}
    original = reddit.get
    calls = []

    def limited(path, params=None):
        calls.append(path)
        if len(calls) == 3:
            raise RedditRateLimitedError("Reddit rate limit exceeded (HTTP 429)")
        return original(path, params)

    reddit.get = limited
    stats = {}
    rows = _run(reddit, state, stats)
    assert stats["failed"] == 1 and stats["failed_rate_limit"] == 1
    assert len(_live(rows)) == 1 and len(state["pending"]) == 2
    reddit.get = original
    assert len(_live(_run(reddit, state))) == 2
    assert len(posts) == 3


def test_a_wrong_secret_raises_on_the_first_request(reddit):
    reddit.bad_secret = True
    with pytest.raises(RedditAuthError):
        _run(reddit, {})


def test_a_token_revoked_during_the_run_keeps_what_is_done(reddit):
    for i in range(3):
        reddit.add_post("Python", f"P{i}", "x")
    reddit.revoke_after = 2
    stats = {}
    rows = _run(reddit, {}, stats)
    assert stats["failed_auth"] == 1 and stats["failed"] == 1 and len(_live(rows)) == 1


def test_check_active_runs_before_every_request(reddit):
    reddit.add_post("Python", "Q", "q")
    checks = []
    _run(reddit, {}, check_active=lambda: checks.append(len(reddit.calls)))
    assert checks == list(range(len(reddit.calls)))


def test_stats_always_carry_every_key(reddit):
    stats = {}
    _run(reddit, {}, stats)
    assert set(stats) == set(reddit_module._STATS)
    assert all(value == 0 for key, value in stats.items() if key.startswith("failed"))


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------
def _name(**kwargs):
    values = {"subreddits": ["Python"], "service": FakeReddit()}
    values.update(kwargs)
    return getattr(reddit_source(**values), dlt_utils.PIPELINE_SCOPE_ATTR)


def test_default_resource_names_follow_the_subreddits():
    base = _name()
    assert base.startswith("reddit_python_") and len(base) <= 64
    assert _name(subreddits=["python"]) == base == _name(subreddits=["r/Python"])
    assert _name(subreddits=["ab_cd", "ef"]) != _name(subreddits=["ab", "cd_ef"])
    assert _name(subreddits=["rust", "Python"]) == _name(subreddits=["Python", "rust"])
    assert _name(subreddits=["Python", "python", "r/PYTHON"]) == base
    assert len(_name(subreddits=[f"sub{i:02d}" for i in range(30)])) <= 64


def test_resource_name_is_validated():
    for bad in ["", "  ", "Reddit", "red-dit", "a" * 65]:
        with pytest.raises(ValueError, match="resource_name"):
            _name(resource_name=bad)
    assert _name(resource_name="reddit_py") == "reddit_py"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"subreddits": []}, "at least one subreddit"),
        ({"subreddits": ["https://reddit.com/r/python"]}, "not a subreddit name"),
        ({"subreddits": ["has space"]}, "not a subreddit name"),
        ({"subreddits": ["x"]}, "not a subreddit name"),
        ({"max_requests": 0}, "max_requests"),
        ({"first_sync_posts": 0}, "first_sync_posts"),
        ({"max_comment_depth": -1}, "max_comment_depth"),
        ({"max_more_requests": True}, "max_more_requests"),
        ({"refresh_days": -1}, "refresh_days"),
    ],
)
def test_settings_are_validated(kwargs, message):
    with pytest.raises(ValueError, match=message):
        _name(**kwargs)


def test_credentials_are_required_and_checked_up_front(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "from-env")
    base = {"subreddits": ["Python"]}
    with pytest.raises(ValueError, match="client id"):
        reddit_source(**base)
    with pytest.raises(ValueError, match="user agent"):
        reddit_source(**base, client_id=CLIENT_ID, client_secret=SECRET)
    with pytest.raises(ValueError, match="together"):
        reddit_source(
            **base, client_id=CLIENT_ID, client_secret=SECRET, user_agent=AGENT, username="tester"
        )
    reddit_source(**base, client_id=CLIENT_ID, client_secret=SECRET, user_agent=AGENT)


def test_resource_carries_the_document_contract():
    resource = reddit_source(subreddits=["Python"], service=FakeReddit())
    assert getattr(resource, dlt_utils.DOCUMENT_SOURCE_ATTR) == "reddit"
    assert getattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR) == resource.name
    assert resource.columns[dlt_utils.NODE_SET_COLUMN]["data_type"] == "json"
    assert resource.cognee_sync_stats == {}


def test_client_is_an_alias_of_service(reddit):
    reddit.add_post("Python", "Q", "q")
    rows = list(reddit_source(subreddits=["Python"], client=reddit))
    assert [r["title"] for r in rows] == ["Q"]


def test_an_owned_client_is_closed_after_the_run(monkeypatch):
    built = []

    class Recording(FakeReddit):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.add_sub("Python")
            self.kwargs = kwargs
            self.closed = False
            built.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(reddit_module, "RedditClient", Recording)
    common = {
        "subreddits": ["Python"],
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "user_agent": AGENT,
    }
    list(reddit_source(**common))
    list(reddit_source(**common, username="tester", password="pw"))
    assert len(built) == 2 and all(c.closed for c in built)
    assert built[1].kwargs["username"] == "tester"


# ---------------------------------------------------------------------------
# dlt pipeline: incremental sync + forget-on-delete
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


PipelineStepFailed = pytest.importorskip("dlt.pipeline.exceptions").PipelineStepFailed


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "reddit.db").as_posix()
    return dlt.pipeline(
        pipeline_name="reddit_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="reddit_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _staged(pipeline, table="reddit_documents", columns="id"):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT {columns} FROM {table}") as cursor,
    ):
        return {r[0] if len(r) == 1 else tuple(r) for r in cursor.fetchall()}


def _source(reddit, name="reddit_documents", subreddits=("Python",), **kwargs):
    return reddit_source(subreddits=list(subreddits), service=reddit, resource_name=name, **kwargs)


def test_pipeline_resyncs_only_changes_and_forgets_deletions(dlt_mod, tmp_path, reddit):
    a = reddit.add_post("Python", "A", "a")
    b = reddit.add_post("Python", "B", "b")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(_source(reddit))
    assert _staged(pipeline) == {f"t3_{a['id']}", f"t3_{b['id']}"}

    # The state lives in dlt, so a fresh source object resumes from it.
    reddit.calls.clear()
    source = _source(reddit)
    pipeline.run(source)
    assert _threads(reddit) == [] and source.cognee_sync_stats["emitted"] == 0

    reddit.remove(b)
    pipeline.run(_source(reddit))
    assert _staged(pipeline) == {f"t3_{a['id']}"}


def test_the_node_set_column_is_stored_as_json(dlt_mod, tmp_path, reddit):
    reddit.add_post("Python", "A", "a")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(_source(reddit))
    (value,) = _staged(pipeline, columns="cognee_node_set")
    assert (json.loads(value) if isinstance(value, str) else value) == ["r/Python"]


def test_two_sources_in_one_dataset_leave_each_other_alone(dlt_mod, tmp_path, reddit):
    reddit.add_sub("rust")
    py = reddit.add_post("Python", "P", "p")
    rs = reddit.add_post("rust", "R", "r")
    pipeline = _pipeline(dlt_mod, tmp_path)
    one = reddit_source(subreddits=["Python"], service=reddit)
    two = reddit_source(subreddits=["rust"], service=reddit)
    assert one.name != two.name
    pipeline.run(one)
    pipeline.run(two)

    reddit.remove(py)
    pipeline.run(reddit_source(subreddits=["Python"], service=reddit))
    pipeline.run(reddit_source(subreddits=["rust"], service=reddit))
    assert _staged(pipeline, one.name) == set()
    assert _staged(pipeline, two.name) == {f"t3_{rs['id']}"}


def test_failed_run_keeps_staging_and_state(dlt_mod, tmp_path, reddit):
    a = reddit.add_post("Python", "A", "a")
    b = reddit.add_post("Python", "B", "b")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(_source(reddit))

    reddit.remove(b)
    reddit.errors["/api/info"] = 500
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(_source(reddit))
    assert "HTTP 500" in str(caught.value)
    assert _staged(pipeline) == {f"t3_{a['id']}", f"t3_{b['id']}"}

    reddit.errors.clear()
    pipeline.run(_source(reddit))
    assert _staged(pipeline) == {f"t3_{a['id']}"}


def test_stats_are_plain_json(reddit):
    reddit.add_post("Python", "A", "a")
    resource = reddit_source(subreddits=["Python"], service=reddit)
    list(resource)
    assert json.loads(json.dumps(resource.cognee_sync_stats))["emitted"] == 1
