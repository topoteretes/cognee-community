"""Unit tests for the Zendesk connector, against ``FakeZendesk`` (see fake_zendesk.py).

Coverage: rendering (thread, internal notes, HTML articles, requester and assignee
names), first export and cursor resume, comment-only change, ticket deletion, a ticket
listed several times in one window (the newest version wins, so delete-then-restore
works), turning internal notes off, article edit / archive / unpublish / publish again /
no-op, failed or short listings never deleting, rendering independent of input order,
export page cap, a sync through the real HTTP client, rate-limit retry, record ids kept
out of errors, a 200 without JSON, auth/config errors, per-account tables, check_active,
tickets and articles failing separately (with the ticket cursor put back), a large
backfill saving progress every run, the sync stats, dlt wiring, the row-to-document
mapping in cognee, and a real dlt merge into SQLite. The review round added: one ticket
whose comments fail (404/410 forgotten, 403/422/5xx set aside and retried), a failing
user lookup, two translations of one article, cursor pagination that does not move,
dropped connections, a long Retry-After, redirects, plain http, the subdomain and
resource_name checks, emails kept out by default, a part that keeps failing, a
set-aside ticket answered without the ticket, an export page without a cursor, a
source object run twice, and the cognee version check.
"""

import http.client
import io
import json
import urllib.error

import pytest
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from dlt.extract.exceptions import ResourceExtractionError
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_zendesk import AGENT, ALICE, SUB, FakeZendesk, article, comment, ticket

from cognee_community_connector_zendesk.zendesk import (
    ZendeskAPIError,
    ZendeskAuthError,
    ZendeskClient,
    ZendeskRateLimitError,
    article_to_row,
    html_to_text,
    sync_articles,
    sync_tickets,
    ticket_to_row,
    zendesk_source,
)


def _cause(error, kind):
    """The first exception of ``kind`` in an error's chain (dlt wraps what a source raises)."""
    while error is not None:
        if isinstance(error, kind):
            return error
        error = error.__cause__ or error.__context__
    return None


def tickets(fake, state, **kw):
    return list(sync_tickets(fake, state, SUB, **kw))


# --------------------------------------------------------------------- rendering
def test_ticket_row_renders_thread_and_hides_internal_notes():
    comments = [
        comment(1, "I cannot log in."),
        comment(2, "Checking SSO logs", 22, public=False),
        comment(3, "Fixed, please retry.", 22),
    ]
    row = ticket_to_row(ticket(7), comments, {11: ALICE, 22: AGENT}, SUB, include_internal=False)
    assert row["id"] == "7"
    assert set(row) == {"id", "title", "content", "url", "_deleted"}, "no extra columns"
    assert row["title"] == "Ticket #7: Login fails"
    assert "Requester: Alice Customer\n" in row["content"]
    assert "alice@example.com" not in row["content"], "emails stay out by default"
    assert "Tags: login, sso" in row["content"]
    assert "I cannot log in." in row["content"] and "Fixed, please retry." in row["content"]
    assert "Checking SSO logs" not in row["content"]
    assert row["url"] == "https://acme.zendesk.com/agent/tickets/7"


def test_internal_notes_included_when_asked_and_labelled():
    row = ticket_to_row(
        ticket(7),
        [comment(2, "secret", 22, public=False)],
        {22: AGENT},
        SUB,
        include_internal=True,
    )
    assert "[internal note]" in row["content"] and "secret" in row["content"]


def test_emails_are_opt_in():
    row = ticket_to_row(ticket(7), [comment(1, "hi")], {11: ALICE, 22: AGENT}, SUB, False, True)
    assert "Requester: Alice Customer <alice@example.com>" in row["content"]
    assert "--- Alice Customer <alice@example.com>," in row["content"]


def test_ticket_row_ignores_updated_at_so_metadata_bumps_do_not_churn():
    a = ticket_to_row(ticket(7), [comment(1, "x")], {}, SUB, False)
    b = ticket_to_row(
        ticket(7, updated_at="2026-10-09T00:00:00Z"), [comment(1, "x")], {}, SUB, False
    )
    assert a == b


def test_html_article_is_flattened():
    assert (
        html_to_text("<h2>Steps</h2><ul><li>One</li><li>Two</li></ul><p>Done &amp; dusted</p>")
        == "Steps\n\n- One\n\n- Two\n\nDone & dusted"
    )
    row = article_to_row(article(5))
    assert row["content"].startswith("Go to Settings.") and "Labels: account" in row["content"]


# --------------------------------------------------------------------- tickets
def test_first_export_then_cursor_returns_only_changes():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.touch(ticket(2, "Billing"), [comment(2, "b")])
    state = {}
    assert [r["id"] for r in tickets(fake, state)] == ["1", "2"]
    assert fake.calls[0][1] == {"start_time": 0}
    assert state["after_cursor"] == "2"

    assert tickets(fake, state) == []  # nothing changed
    fake.touch(ticket(2, "Billing (edited)"))
    rows = tickets(fake, state)
    assert [r["id"] for r in rows] == ["2"] and "Billing (edited)" in rows[0]["content"]


def test_comment_only_change_brings_the_ticket_back_with_new_comment():
    fake = FakeZendesk()
    fake.touch(ticket(3), [comment(1, "first")])
    state = {}
    tickets(fake, state)
    fake.touch(ticket(3), [comment(1, "first"), comment(2, "a new reply", 22)])
    rows = tickets(fake, state)
    assert len(rows) == 1 and "a new reply" in rows[0]["content"]


def test_deleted_ticket_becomes_tombstone():
    fake = FakeZendesk()
    fake.touch(ticket(4), [comment(1, "x")])
    state = {}
    tickets(fake, state)
    fake.touch(ticket(4, status="deleted"))
    assert tickets(fake, state) == [{"id": "4", "_deleted": True}]


def test_assignee_who_never_commented_is_still_named():
    fake = FakeZendesk()
    fake.touch(ticket(7), [comment(1, "I cannot log in.", author=11)])
    row = tickets(fake, {})[0]
    assert "Requester: Alice Customer" in row["content"]
    assert "Assignee: Sam Agent" in row["content"]
    lookups = [p for p, _ in fake.calls if p == "/api/v2/users/show_many.json"]
    assert lookups == ["/api/v2/users/show_many.json"]  # one batched lookup, not one per user


def test_ticket_listed_several_times_in_one_window_keeps_its_newest_version():
    fake = FakeZendesk()
    fake.touch(ticket(5), [comment(1, "x")])
    fake.touch(ticket(5, "Edited"))
    fake.touch(ticket(5, status="deleted"))
    assert tickets(fake, {}) == [{"id": "5", "_deleted": True}]
    assert not any(path.endswith("/comments.json") for path, _ in fake.calls)

    # deleted and then restored between two syncs: the restore wins
    fake = FakeZendesk()
    fake.touch(ticket(6), [comment(1, "x")])
    state = {}
    tickets(fake, state)
    fake.touch(ticket(6, status="deleted"))
    fake.touch(ticket(6, "Restored"))
    rows = tickets(fake, state)
    assert len(rows) == 1 and rows[0]["_deleted"] is False and "Restored" in rows[0]["title"]
    assert sum(1 for path, _ in fake.calls if path.endswith("/comments.json")) == 2


def test_turning_internal_notes_off_re_renders_every_ticket_without_them():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "hello"), comment(2, "SSO token expired", 22, public=False)])
    fake.touch(ticket(2, "Billing"), [comment(3, "invoice please")])
    state = {}
    rows = tickets(fake, state, include_internal=True)
    assert "SSO token expired" in rows[0]["content"]
    assert tickets(fake, state, include_internal=True) == []  # nothing changed

    rows = tickets(fake, state, include_internal=False)  # setting changed: start over
    assert [r["id"] for r in rows] == ["1", "2"]
    assert not any("SSO token expired" in r["content"] for r in rows)
    assert tickets(fake, state, include_internal=False) == []


def test_export_page_cap_resumes_next_run():
    fake = FakeZendesk()
    fake.page_size = 2
    for i in range(1, 6):
        fake.touch(ticket(i), [comment(i, "x")])
    state = {}
    assert [r["id"] for r in tickets(fake, state, max_pages=2)] == ["1", "2", "3", "4"]
    assert [r["id"] for r in tickets(fake, state, max_pages=2)] == ["5"]


# --------------------------------------------------------------------- articles
def test_articles_first_run_edit_noop_archive_and_unpublish():
    fake = FakeZendesk()
    fake.articles = [article(5), article(6, "VPN setup"), article(9, "WIP", draft=True)]
    state = {}
    assert sorted(r["id"] for r in sync_articles(fake, state, SUB)) == [
        "5:en-us",
        "6:en-us",
    ]  # draft skipped
    assert list(sync_articles(fake, state, SUB)) == []  # no-op

    fake.articles[0] = article(5, body="<p>New steps</p>", updated="2026-10-06T09:00:00Z")
    rows = list(sync_articles(fake, state, SUB))
    assert [r["id"] for r in rows] == ["5:en-us"] and rows[0]["content"].startswith("New steps")

    fake.articles = [article(5, updated="2026-10-06T09:00:00Z")]  # 6 archived
    assert list(sync_articles(fake, state, SUB)) == [{"id": "6:en-us", "_deleted": True}]

    fake.articles = [article(5, updated="2026-10-07T09:00:00Z", draft=True)]  # unpublished
    assert list(sync_articles(fake, state, SUB)) == [{"id": "5:en-us", "_deleted": True}]
    assert state["versions"] == {}


def test_archived_article_that_is_published_again_comes_back():
    fake = FakeZendesk()
    fake.articles = [article(5), article(6, "VPN setup")]
    state = {}
    list(sync_articles(fake, state, SUB))
    fake.articles = [article(5)]  # 6 archived
    assert list(sync_articles(fake, state, SUB)) == [{"id": "6:en-us", "_deleted": True}]

    # Restoring an archived article brings it back as a draft: still skipped.
    fake.articles = [article(5), article(6, "VPN setup", draft=True)]
    assert list(sync_articles(fake, state, SUB)) == []
    # Published again: it is stored again.
    fake.articles = [article(5), article(6, "VPN setup", updated="2026-10-08T09:00:00Z")]
    rows = list(sync_articles(fake, state, SUB))
    assert [r["id"] for r in rows] == ["6:en-us"] and rows[0]["_deleted"] is False


def test_failed_article_listing_never_deletes():
    fake = FakeZendesk()
    fake.articles = [article(5)]
    state = {}
    list(sync_articles(fake, state, SUB))
    fake.fail_articles = True
    with pytest.raises(ZendeskAPIError):
        list(sync_articles(fake, state, SUB))
    assert state["versions"] == {"5:en-us": "2026-10-05T12:00:00Z"}


class _Listing:
    """Serves a count probe and one cursor page as given, to check the count guard."""

    def __init__(self, page):
        self.page = page

    def get(self, path, **params):
        if "per_page" in params:
            return {k: v for k, v in self.page.items() if k == "count"}
        return {"articles": self.page["articles"], "meta": {"has_more": False}}


def test_article_listing_that_does_not_add_up_never_deletes():
    fake = FakeZendesk()
    fake.articles = [article(5), article(6, "VPN setup")]
    state = {}
    list(sync_articles(fake, state, SUB))
    short = {"articles": [article(5)], "count": 2}
    with pytest.raises(ZendeskAPIError, match="1 of 2"):
        list(sync_articles(_Listing(short), state, SUB))
    no_count = {"articles": [article(5)]}
    with pytest.raises(ZendeskAPIError, match="no count"):
        list(sync_articles(_Listing(no_count), state, SUB))
    assert set(state["versions"]) == {"5:en-us", "6:en-us"}  # nothing was forgotten


def test_ticket_renders_the_same_whatever_order_zendesk_sends():
    import random

    comments = [comment(1, "I cannot log in."), comment(2, "Fixed, please retry.", 22)]
    users = {11: ALICE, 22: AGENT}
    base = ticket(7, tags=["sso", "login", "urgent", "enterprise"])
    expected = ticket_to_row(base, comments, users, SUB, False)
    for seed in range(20):
        rng = random.Random(seed)
        shuffled = dict(base, tags=rng.sample(base["tags"], len(base["tags"])))
        shuffled_users = dict(rng.sample(list(users.items()), len(users)))
        assert ticket_to_row(shuffled, comments, shuffled_users, SUB, False) == expected

    labelled = article(5) | {"label_names": ["account", "billing", "sso"]}
    expected = article_to_row(labelled)
    for seed in range(20):
        rng = random.Random(seed)
        labels = rng.sample(labelled["label_names"], 3)
        assert article_to_row(labelled | {"label_names": labels}) == expected


# --------------------------------------------------------------------- client
def _http_error(status, headers=None):
    return urllib.error.HTTPError("https://acme.zendesk.com/x", status, "err", headers or {}, None)


class _Opener:
    """Answers each request with the next outcome: an exception, or a JSON-able body."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.urls = []

    def __call__(self, request):
        self.urls.append(request.full_url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        body = outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode()
        return io.BytesIO(body)


def _client(*outcomes, **kw):
    sleeps = []
    client = ZendeskClient(SUB, oauth_token="zd-secret", sleep=sleeps.append, **kw)
    client._open = _Opener(*outcomes)
    return client, sleeps


def test_client_auth_headers():
    assert ZendeskClient(SUB, oauth_token="t")._auth == "Bearer t"
    assert ZendeskClient(SUB, email="a@b.c", api_token="k")._auth.startswith("Basic ")
    with pytest.raises(ValueError):
        ZendeskClient(SUB)


@pytest.mark.parametrize("bad", ["evil.com#", "acme.zendesk.com", "a/b", "", "-acme", "ACME!"])
def test_client_refuses_a_subdomain_that_is_not_one(bad):
    with pytest.raises(ValueError, match="subdomain"):
        ZendeskClient(bad, oauth_token="t")


def test_client_waits_out_a_short_rate_limit_and_stops_on_bad_credentials():
    client, sleeps = _client(_http_error(429, {"Retry-After": "7"}), {"ok": True})
    assert client.get("/api/v2/x.json") == {"ok": True}
    assert sleeps == [7.0]

    client, sleeps = _client(_http_error(401))
    with pytest.raises(ZendeskAuthError, match="expire"):
        client.get("/api/v2/x.json")
    assert sleeps == [], "no retries on a 401"


def test_a_long_retry_after_is_not_slept_through():
    client, sleeps = _client(_http_error(429, {"Retry-After": "3600"}))
    with pytest.raises(ZendeskRateLimitError) as caught:
        client.get("/api/v2/x.json")
    assert sleeps == [] and caught.value.retry_after == 3600


def test_a_403_on_one_record_is_not_called_a_credentials_problem():
    client, _ = _client(_http_error(403))
    with pytest.raises(ZendeskAPIError) as caught:
        client.get("/api/v2/tickets/48213/comments.json")
    assert not isinstance(caught.value, ZendeskAuthError) and not caught.value.ends_run
    assert "credentials" not in str(caught.value) and caught.value.status == 403


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
    assert client.get("/api/v2/x.json") == {"ok": True}
    assert len(sleeps) == 1


@pytest.mark.parametrize("body", [b"", b"<html>maintenance</html>", b"[]", b"null"])
def test_a_200_without_a_json_object_raises_a_clean_error(body):
    client, _ = _client(body)
    with pytest.raises(ZendeskAPIError, match="without a JSON object") as caught:
        client.get("/api/v2/help_center/articles.json")
    assert caught.value.status == 200 and caught.value.ends_run, "a login page ends the run"


def test_404_names_the_endpoint_but_not_the_record():
    client, _ = _client(_http_error(404))
    with pytest.raises(ZendeskAPIError) as caught:
        client.get("/api/v2/tickets/48213/comments.json")
    assert "/api/v2/tickets/{id}/comments.json" in str(caught.value)
    assert "48213" not in str(caught.value)


def test_a_redirect_is_not_followed():
    client, _ = _client(_http_error(302, {"Location": "https://evil.example/"}))
    with pytest.raises(ZendeskAPIError, match="not followed"):
        client.get("/api/v2/x.json")
    assert client._open.urls == ["https://acme.zendesk.com/api/v2/x.json"]


def test_the_redirect_handler_never_builds_a_follow_up_request():
    from cognee_community_connector_zendesk.zendesk import _NoRedirect

    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://e.x/") is None
    client = ZendeskClient(SUB, oauth_token="t")
    assert any(isinstance(h, _NoRedirect) for h in client._opener.handlers)


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/next",
        "http://acme.zendesk.com/api/v2/x.json",  # cleartext
        "httpfoo://acme.zendesk.com/x",
        "https://acme.zendesk.com.evil.example/x",
    ],
)
def test_client_refuses_pagination_links_off_the_account_host(link):
    with pytest.raises(ZendeskAPIError, match="another host"):
        ZendeskClient(SUB, oauth_token="t")._url(link, {})


@pytest.mark.parametrize("token", ["zd-secret\n", " zd-secret ", "zd-se cret", "zd-\x00secret"])
def test_a_token_with_whitespace_is_stripped_or_refused_without_echoing_it(token):
    if token.strip() == "zd-secret":
        assert ZendeskClient(SUB, oauth_token=token)._auth == "Bearer zd-secret"
        return
    with pytest.raises(ValueError) as caught:
        ZendeskClient(SUB, oauth_token=token)
    assert "secret" not in str(caught.value) and "cret" not in str(caught.value)


def test_check_active_is_asked_before_every_retry_wait():
    checks = []
    client, sleeps = _client(
        _http_error(503), _http_error(503), {"ok": True}, check_active=lambda: checks.append(1)
    )
    assert client.get("/api/v2/x.json") == {"ok": True}
    assert len(checks) == len(sleeps) == 2


def test_the_client_repr_hides_the_credentials():
    assert "zd-secret" not in repr(ZendeskClient(SUB, oauth_token="zd-secret"))


def test_sync_through_the_real_client_sends_the_token_but_never_stores_it():
    """The real ZendeskClient against FakeZendesk: URLs, query parameters and the
    auth header take the same path as against Zendesk, and the token never ends up
    in a stored row."""
    import urllib.parse

    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "I cannot log in.")])
    fake.articles = [article(5), article(6, "VPN setup")]
    fake.article_page_size = 1  # follow a cursor link through the real client
    token, headers = "zd-secret-token-123", []
    client = ZendeskClient(SUB, oauth_token=token)

    def opener(request):
        url = urllib.parse.urlparse(request.full_url)
        assert url.scheme == "https" and url.netloc == "acme.zendesk.com"
        headers.append(request.get_header("Authorization"))
        params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        return io.BytesIO(json.dumps(fake.get(url.path, **params)).encode())

    client._open = opener
    rows = list(sync_tickets(client, {}, SUB)) + list(sync_articles(client, {}, SUB))

    assert [r["id"] for r in rows] == ["1", "5:en-us", "6:en-us"]
    assert "I cannot log in." in rows[0]["content"]
    assert headers and all(h == f"Bearer {token}" for h in headers)
    assert token not in json.dumps(rows)


# --------------------------------------------------------------------- one bad item
@pytest.mark.parametrize("status", [404, 410])
def test_a_ticket_whose_comments_are_gone_is_forgotten_and_the_rest_sync(status):
    fake = FakeZendesk()
    fake.page_size = 2
    for i in (1, 2, 3):
        fake.touch(ticket(i), [comment(i, f"m{i}")])
    fake.fail_comments = {1: status}  # deleted after the export listed it
    state = {}
    rows = tickets(fake, state)
    assert rows[0] == {"id": "1", "_deleted": True}
    assert [r["id"] for r in rows[1:]] == ["2"], "ticket 2 on the same page still syncs"
    assert state["after_cursor"] == "2" and state["pending"] == {}
    assert [r["id"] for r in tickets(fake, state)] == ["3"], "the cursor moved on"


@pytest.mark.parametrize("status", [403, 422, 500])
def test_a_ticket_whose_comments_fail_is_set_aside_and_retried(status):
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.touch(ticket(2), [comment(2, "b")])
    fake.fail_comments = {1: status}
    state, stats = {}, {}
    assert [r["id"] for r in tickets(fake, state, stats=stats)] == ["2"]
    assert state["pending"] == {"1": 1} and stats["kept"] == 1
    assert state["after_cursor"] == "2", "the cursor moves on"

    fake.fail_comments = {}  # access restored: the next run fetches it again
    rows = tickets(fake, state)
    assert [r["id"] for r in rows] == ["1"] and "a" in rows[0]["content"]
    assert state["pending"] == {}


def test_a_ticket_that_keeps_failing_is_given_up_after_three_runs():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.fail_comments = {1: 403}
    state, stats = {}, {}
    for _ in range(3):
        assert tickets(fake, state, stats=stats) == []
    assert state["pending"] == {} and stats == {"kept": 2, "given_up": 1}
    before = len(fake.calls)
    tickets(fake, state)
    assert not any("/tickets/1" in path for path, _ in fake.calls[before:]), "not asked again"


def test_a_set_aside_ticket_deleted_meanwhile_is_forgotten():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.fail_comments = {1: 403}
    state = {}
    tickets(fake, state)
    fake.log.clear()  # the ticket is gone: GET /tickets/1 answers 404
    fake.log.append({"id": 9, "status": "deleted"})
    assert {"id": "1", "_deleted": True} in tickets(fake, state)
    assert state["pending"] == {}


def test_three_fresh_5xx_in_a_row_stop_the_run_cleanly_and_the_rest_waits():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "ok")])
    for i in (2, 3, 4, 5):
        fake.touch(ticket(i), [comment(i, "x")])
    fake.fail_comments = {2: 503, 3: 502, 4: 500}
    state, stats = {}, {}
    rows = tickets(fake, state, stats=stats)
    assert [r["id"] for r in rows] == ["1"], "what was read is kept"
    assert state["pending"] == {"2": 1, "3": 1, "4": 1, "5": 0}, "the rest waits"
    assert state["after_cursor"] == "5" and stats["outage"] == 1
    assert not any("/tickets/5/comments" in path for path, _ in fake.calls), "stopped asking"


def test_tickets_that_always_fail_never_stop_every_run():
    """goran's Asana case: tickets that stay broken next to each other must not stop
    every run forever. Set aside once, they are retries and no longer count."""
    fake = FakeZendesk()
    for i in (10, 11, 12, 13, 14):
        fake.touch(ticket(i), [comment(i, "x")])
    fake.fail_comments = {11: 500, 12: 500, 13: 500}
    state = {}
    tickets(fake, state)  # run 1: 11-13 fail fresh, the outage stop sets 14 aside
    rows = tickets(fake, state, stats=(stats := {}))  # run 2: all four are retries
    assert "14" in [r["id"] for r in rows] and "outage" not in stats
    fake.touch(ticket(15), [comment(15, "new")])
    rows = tickets(fake, state)  # run 3: a new ticket still arrives
    assert "15" in [r["id"] for r in rows]
    assert state["pending"] == {}, "11-13 given up after three runs"


def test_records_without_an_id_are_skipped():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "x")])
    fake.log.append({"subject": "no id"})
    fake.articles = [article(5), {"title": "no id"}]
    assert [r["id"] for r in tickets(fake, {})] == ["1"]
    fake.articles = [article(5)]
    assert [r["id"] for r in sync_articles(fake, {}, SUB)] == ["5:en-us"]


def test_a_200_without_json_on_one_ticket_ends_the_run():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "x")])
    fake.fail_comments = {1: 200}
    original = fake.get

    def login_page(path, **params):
        if path.endswith("/comments.json"):
            raise ZendeskAPIError("HTTP 200 without a JSON object", 200, ends_run=True)
        return original(path, **params)

    fake.get = login_page
    with pytest.raises(ZendeskAPIError, match="JSON"):
        tickets(fake, {})


def test_a_failing_user_lookup_falls_back_to_user_ids():
    fake = FakeZendesk()
    fake.touch(ticket(7), [comment(1, "I cannot log in.", author=11)])
    fake.fail_users = 403
    row = tickets(fake, {})[0]
    assert "Requester: Alice Customer" in row["content"], "sideloaded with the comment"
    assert "Assignee: user 22" in row["content"]


def test_a_set_aside_ticket_answered_without_the_ticket_stays_set_aside():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.fail_comments = {1: 403}
    state = {}
    tickets(fake, state)
    fake.log.append({"id": 9, "status": "deleted"})  # the cursor moves past ticket 1
    fake.blank_tickets = {1}
    assert tickets(fake, state) == [{"id": "9", "_deleted": True}], "not forgotten"
    assert state["pending"] == {"1": 2}


def test_an_export_page_without_a_cursor_raises():
    fake = FakeZendesk()
    fake.page_size = 1
    fake.touch(ticket(1), [comment(1, "x")])
    fake.touch(ticket(2), [comment(2, "y")])
    fake.export_without_cursor = True
    state = {}
    with pytest.raises(ZendeskAPIError, match="no cursor"):
        tickets(fake, state, max_pages=None)
    assert "after_cursor" not in state


def test_an_export_403_ends_the_run_as_a_permissions_problem():
    fake = FakeZendesk()
    fake.fail_export = 403
    with pytest.raises(ZendeskAuthError, match="admin"):
        tickets(fake, {})


def test_an_export_cursor_that_does_not_move_raises():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "x")])
    state = {}
    tickets(fake, state)
    fake.stall_export = True
    with pytest.raises(ZendeskAPIError, match="did not move"):
        tickets(fake, state, max_pages=None)
    assert len(fake.calls) < 10


def test_article_pagination_that_does_not_move_raises():
    fake = FakeZendesk()
    fake.articles = [article(i) for i in range(1, 6)]
    fake.article_page_size = 2
    fake.stall_articles = True
    with pytest.raises(ZendeskAPIError, match="did not move"):
        list(sync_articles(fake, {}, SUB))
    assert len(fake.calls) < 10


def test_comment_pagination_that_does_not_move_raises():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "x")])
    fake.stall_comments = True
    state = {}
    assert tickets(fake, state) == [], "the loop stops instead of spinning"
    assert len(fake.calls) < 10
    assert state["pending"] == {"1": 1}, "that ticket is set aside like any failing one"


def test_articles_are_read_with_cursor_pagination():
    fake = FakeZendesk()
    fake.articles = [article(i, f"A{i}") for i in range(1, 6)]
    fake.article_page_size = 2
    rows = list(sync_articles(fake, {}, SUB))
    assert len(rows) == 5
    cursor_calls = [p for _, p in fake.calls if "page[size]" in p or "page[after]" in p]
    assert len(cursor_calls) == 3, "three cursor pages of two"


def test_two_translations_of_one_article_are_two_documents():
    fake = FakeZendesk()
    fake.articles = [article(5, "Reset", locale="en-us"), article(5, "Zurücksetzen", locale="de")]
    state = {}
    rows = list(sync_articles(fake, state, SUB))
    assert sorted(r["id"] for r in rows) == ["5:de", "5:en-us"]
    assert list(sync_articles(fake, state, SUB)) == [], "the count check passes"
    fake.articles = [article(5, "Reset", locale="en-us")]  # the German one removed
    assert list(sync_articles(fake, state, SUB)) == [{"id": "5:de", "_deleted": True}]


def test_a_sync_state_from_another_account_is_refused():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "x")])
    state = {}
    tickets(fake, state)
    with pytest.raises(ValueError, match="belongs to Zendesk 'acme'"):
        list(sync_tickets(fake, state, "globex"))


# --------------------------------------------------------------------- config
def test_source_validates_config():
    with pytest.raises(ValueError, match="subdomain"):
        zendesk_source(client=FakeZendesk())
    with pytest.raises(ValueError, match="subdomain"):
        zendesk_source("evil.com#", client=FakeZendesk())
    with pytest.raises(ValueError, match="Unknown"):
        zendesk_source(SUB, resources=["users"], client=FakeZendesk())
    with pytest.raises(ValueError, match="locale"):
        zendesk_source(SUB, locale="../x", client=FakeZendesk())
    with pytest.raises(ValueError, match="max_export_pages_per_run"):
        zendesk_source(SUB, max_export_pages_per_run=0, client=FakeZendesk())
    with pytest.raises(ValueError, match="not both"):
        zendesk_source(SUB, client=FakeZendesk(), service=FakeZendesk())


def test_credentials_are_arguments_only(monkeypatch):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESK_OAUTH_TOKEN", "from-env")
    with pytest.raises(ValueError, match="subdomain"):
        zendesk_source()
    with pytest.raises(ValueError, match="credentials"):
        zendesk_source(SUB)


@pytest.mark.parametrize(
    "name", ["Acme-EU", "acme eu", "acmeEU", "1acme", "acme__eu", "acme_", "a" * 52]
)
def test_a_resource_name_dlt_would_rename_is_refused(name):
    with pytest.raises(ValueError, match="resource_name"):
        zendesk_source(SUB, resource_name=name, client=FakeZendesk())


def test_a_cognee_without_table_scoped_cleanup_is_refused(monkeypatch):
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)
    with pytest.raises(RuntimeError, match="cognee 1"):
        zendesk_source(SUB, client=FakeZendesk())


def test_service_is_an_alias_for_client():
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "x")])
    source = zendesk_source(SUB, resources=["tickets"], service=fake)
    assert [r["id"] for r in source.resources["zendesk_acme_tickets"]] == ["1"]


def test_resources_are_merge_with_hard_delete_and_document_mode():
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    source = zendesk_source(SUB, client=FakeZendesk())
    assert set(source.resources) == {"zendesk_acme_tickets", "zendesk_acme_articles"}
    for resource in source.resources.values():
        assert resource.write_disposition == "merge"
        assert resource.compute_table_schema()["columns"]["_deleted"]["hard_delete"] is True
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "zendesk"
    assert set(zendesk_source(SUB, resources=["articles"], client=FakeZendesk()).resources) == {
        "zendesk_acme_articles"
    }
    dashed = zendesk_source("acme-eu", client=FakeZendesk())
    assert set(dashed.resources) == {"zendesk_acme_eu_tickets", "zendesk_acme_eu_articles"}


def test_check_active_runs_before_every_request():
    """A connection revoked mid-run stops the sync before the next API call, not
    only between emitted rows (every request happens before the first row)."""

    class AccessRevokedError(Exception):
        pass

    fake = FakeZendesk()
    for i in range(1, 4):
        fake.touch(ticket(i), [comment(i, "x")])

    def revoked_after_first_request():
        if fake.calls:
            raise AccessRevokedError

    source = zendesk_source(
        SUB, resources=["tickets"], check_active=revoked_after_first_request, client=fake
    )
    with pytest.raises(ResourceExtractionError) as caught:
        list(source.resources["zendesk_acme_tickets"])
    assert _cause(caught.value, AccessRevokedError)
    assert len(fake.calls) == 1  # the export page; no comments were fetched


def test_ticket_row_becomes_a_zendesk_document_in_cognee():
    # The row -> document mapping is cognee's (the same check the Notion tests do).
    from types import SimpleNamespace
    from uuid import NAMESPACE_OID, uuid5

    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = ticket_to_row(ticket(7), [comment(1, "I cannot log in.")], {11: ALICE}, SUB, False)
    item = _build_document_data_item(
        SimpleNamespace(row_data=row, table_name="zendesk_tickets"),
        uuid5(NAMESPACE_OID, "7"),
        "zendesk",
    )
    # source="zendesk" (not "dlt") routes the ticket through normal cognify.
    assert item.system_metadata["source"] == "zendesk"
    assert item.system_metadata["external_id"] == "7"
    assert item.system_metadata["url"] == "https://acme.zendesk.com/agent/tickets/7"
    assert item.data.startswith("# Ticket #7: Login fails")
    assert item.data.endswith("I cannot log in.")


# --------------------------------------------------------------------- dlt runs
def _pipeline(tmp_path, name="zendesk_partial"):
    import dlt

    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'zendesk.db'}"),
        dataset_name="zendesk_partial",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _ids(pipeline, table):
    with pipeline.sql_client() as sql:
        try:
            return [r[0] for r in sql.execute_sql(f"SELECT id FROM {table} ORDER BY id")]
        except DatabaseUndefinedRelation:  # nothing loaded into this table yet
            return []


def _state(pipeline, resource):
    return pipeline.state["sources"]["zendesk"]["resources"][resource]


def test_e2e_dlt_merge_keeps_unchanged_and_removes_deleted(tmp_path):
    """Real dlt merge into SQLite: an incremental run keeps rows it did not
    re-emit, an edit replaces a row, and tombstones remove rows."""
    pipeline, fake = _pipeline(tmp_path, "zendesk_e2e"), FakeZendesk()

    def sync():
        pipeline.run(zendesk_source(SUB, client=fake))
        with pipeline.sql_client() as client:
            t = client.execute_sql("SELECT id, title FROM zendesk_acme_tickets ORDER BY id")
            a = client.execute_sql("SELECT id FROM zendesk_acme_articles ORDER BY id")
        return [tuple(r) for r in t], [r[0] for r in a]

    fake.touch(ticket(1, "Login fails"), [comment(1, "a")])
    fake.touch(ticket(2, "Billing"), [comment(2, "b")])
    fake.articles = [article(5), article(6, "VPN setup")]
    t, a = sync()
    assert [x[0] for x in t] == ["1", "2"] and a == ["5:en-us", "6:en-us"]

    fake.touch(ticket(2, "Billing question"))  # edit one ticket only
    t, a = sync()
    assert t == [("1", "Ticket #1: Login fails"), ("2", "Ticket #2: Billing question")]

    fake.touch(ticket(1, status="deleted"))
    fake.articles = [article(5)]
    t, a = sync()
    assert [x[0] for x in t] == ["2"] and a == ["5:en-us"]


def test_failing_articles_do_not_block_tickets(tmp_path):
    """Help Center switched off, say: tickets still sync and keep their progress."""
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.articles = [article(5)]
    fake.fail_articles = 403
    source = zendesk_source(SUB, client=fake)
    pipeline.run(source)  # does not raise
    assert _ids(pipeline, "zendesk_acme_tickets") == ["1"]
    assert _ids(pipeline, "zendesk_acme_articles") == []
    assert source.cognee_sync_stats == {
        "scanned": 1,
        "deleted": 0,
        "kept": 0,
        "given_up": 0,
        "failed": 1,
        "failed_articles": 1,
    }
    assert _state(pipeline, "zendesk_acme_articles")["failed_runs"] == 1

    fake.fail_articles = False  # fixed: the articles are picked up on the next run
    source = zendesk_source(SUB, client=fake)
    pipeline.run(source)
    assert _ids(pipeline, "zendesk_acme_articles") == ["5:en-us"]
    assert source.cognee_sync_stats["failed"] == 0
    assert "failed_runs" not in _state(pipeline, "zendesk_acme_articles")


def test_a_part_that_keeps_failing_raises_on_its_third_run(tmp_path):
    """Raising in the first failed run would fail pipeline.run, and dlt would keep
    neither part's progress, so the failure is counted and the third one raises."""
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.fail_articles = 403
    pipeline.run(zendesk_source(SUB, client=fake))
    pipeline.run(zendesk_source(SUB, client=fake))
    assert _ids(pipeline, "zendesk_acme_tickets") == ["1"], "tickets kept syncing"
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(zendesk_source(SUB, client=fake))
    assert "articles failed 3 runs in a row" in str(_cause(caught.value, ZendeskAPIError))


def test_a_failing_export_keeps_its_cursor_and_does_not_block_articles(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    pipeline.run(zendesk_source(SUB, resources=["tickets"], client=fake))
    cursor = _state(pipeline, "zendesk_acme_tickets")["after_cursor"]

    fake.touch(ticket(2, "Billing"), [comment(2, "b")])
    fake.articles = [article(5)]
    fake.fail_export = 500
    source = zendesk_source(SUB, client=fake)
    pipeline.run(source)  # does not raise
    assert _state(pipeline, "zendesk_acme_tickets")["after_cursor"] == cursor
    assert _ids(pipeline, "zendesk_acme_tickets") == ["1"]
    assert _ids(pipeline, "zendesk_acme_articles") == ["5:en-us"]
    assert source.cognee_sync_stats["failed_tickets"] == 1

    fake.fail_export = None  # the next run reads the same window again
    pipeline.run(zendesk_source(SUB, client=fake))
    assert _ids(pipeline, "zendesk_acme_tickets") == ["1", "2"]


def test_a_run_where_everything_fails_raises(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.fail_export = 500
    fake.fail_articles = 500
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(zendesk_source(SUB, client=fake))
    assert _cause(caught.value, ZendeskAPIError).status == 500
    with pytest.raises(PipelineStepFailed) as caught:  # also with only one resource selected
        pipeline.run(zendesk_source(SUB, resources=["articles"], client=fake))
    assert _cause(caught.value, ZendeskAPIError).status == 500


@pytest.mark.parametrize("status", [401, "transport"])
def test_bad_credentials_or_no_connection_end_the_run(tmp_path, status):
    """These would fail the other resource the same way, so they are not isolated."""
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.articles = [article(5)]
    fake.fail_articles = status
    source = zendesk_source(SUB, client=fake)
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(source)
    error = _cause(caught.value, ZendeskAPIError)
    assert error.ends_run and (error.status == 401 or error.transport)
    assert _ids(pipeline, "zendesk_acme_tickets") == []
    if status == 401:
        assert source.cognee_sync_stats["failed_auth"] == 1


def test_a_rate_limit_stops_the_run_cleanly(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.articles = [article(5)]
    fake.fail_export = 429
    source = zendesk_source(SUB, client=fake)
    pipeline.run(source)  # does not raise
    assert _ids(pipeline, "zendesk_acme_tickets") == []
    assert _ids(pipeline, "zendesk_acme_articles") == [], "the rest of the run stops too"
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
    assert all(type(v) is int for v in source.cognee_sync_stats.values())

    fake.fail_export = None
    pipeline.run(zendesk_source(SUB, client=fake))
    assert _ids(pipeline, "zendesk_acme_tickets") == ["1"]


def test_a_source_object_run_again_starts_with_fresh_counters(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.articles = [article(5)]
    fake.fail_export = 429
    source = zendesk_source(SUB, client=fake)
    pipeline.run(source)
    assert source.cognee_sync_stats["failed_rate_limit"] == 1

    fake.fail_export = None
    pipeline.run(source)  # the same object: last run's rate limit must not stop this one
    assert _ids(pipeline, "zendesk_acme_tickets") == ["1"]
    assert _ids(pipeline, "zendesk_acme_articles") == ["5:en-us"]
    assert source.cognee_sync_stats["failed"] == 0
    assert "failed_rate_limit" not in source.cognee_sync_stats


def test_an_outage_stops_the_rest_of_the_run_and_keeps_what_was_read(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "ok")])
    for i in (2, 3, 4):
        fake.touch(ticket(i), [comment(i, "x")])
    fake.articles = [article(5)]
    fake.fail_comments = {2: 503, 3: 503, 4: 503}
    source = zendesk_source(SUB, client=fake)
    pipeline.run(source)  # does not raise
    assert _ids(pipeline, "zendesk_acme_tickets") == ["1"]
    assert _ids(pipeline, "zendesk_acme_articles") == [], "articles wait for the next run"
    assert source.cognee_sync_stats["failed_outage"] == 1

    fake.fail_comments = {}  # Zendesk is back
    pipeline.run(zendesk_source(SUB, client=fake))
    assert _ids(pipeline, "zendesk_acme_tickets") == ["1", "2", "3", "4"]
    assert _ids(pipeline, "zendesk_acme_articles") == ["5:en-us"]


def test_a_large_first_backfill_saves_progress_every_run(tmp_path):
    """By default one export page is read per run, so a backfill that cannot finish
    in one run (an OAuth token expiring, a run failing late) still moves forward."""
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.page_size = 2
    for i in range(1, 6):
        fake.touch(ticket(i), [comment(i, "x")])
    loaded = []
    for _ in range(3):
        pipeline.run(zendesk_source(SUB, resources=["tickets"], client=fake))
        loaded.append(_ids(pipeline, "zendesk_acme_tickets"))
    assert loaded == [["1", "2"], ["1", "2", "3", "4"], ["1", "2", "3", "4", "5"]]


def test_the_page_cap_warns_how_far_behind_the_export_is(monkeypatch):
    from cognee_community_connector_zendesk import zendesk as module

    warnings = []

    class RecordingLogger:
        def info(self, *a):
            pass

        def warning(self, fmt, *args):
            warnings.append(fmt % args)

    monkeypatch.setattr(module, "logger", RecordingLogger())
    fake = FakeZendesk()
    fake.page_size = 1
    fake.touch(ticket(1), [comment(1, "x")])
    fake.touch(ticket(2), [comment(2, "y")])
    tickets(fake, {}, max_pages=1)
    assert any("more ticket changes waiting" in w and "hours behind" in w for w in warnings)


def test_turning_internal_notes_off_reads_the_whole_export_at_once():
    """With one page per run, the notes would otherwise stay in later pages' tickets."""
    fake = FakeZendesk()
    fake.page_size = 2
    for i in range(1, 6):
        fake.touch(ticket(i), [comment(i, "hi"), comment(100 + i, "SECRET", 22, public=False)])
    state = {}
    for _ in range(3):
        tickets(fake, state, include_internal=True, max_pages=1)
    rows = tickets(fake, state, include_internal=False, max_pages=1)
    assert [r["id"] for r in rows] == ["1", "2", "3", "4", "5"]
    assert not any("SECRET" in r["content"] for r in rows)


def test_sync_stats_count_rows_and_tombstones(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.touch(ticket(2), [comment(2, "b")])
    fake.articles = [article(5)]
    pipeline.run(zendesk_source(SUB, client=fake))
    fake.touch(ticket(2, status="deleted"))
    fake.articles = []
    source = zendesk_source(SUB, client=fake)
    pipeline.run(source)
    assert source.cognee_sync_stats == {
        "scanned": 0,
        "deleted": 2,
        "kept": 0,
        "given_up": 0,
        "failed": 0,
    }


def test_accepted_names_are_the_table_names_dlt_writes():
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    naming = NamingConvention()
    for source in (
        zendesk_source("a--b", client=FakeZendesk()),
        zendesk_source(SUB, resource_name="support_eu2", client=FakeZendesk()),
    ):
        for table in source.resources:
            assert naming.normalize_table_identifier(table) == table


def test_two_subdomains_with_the_same_default_name_are_refused(tmp_path):
    pipeline = _pipeline(tmp_path)
    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "x")])
    pipeline.run(zendesk_source("a-b", client=fake, resources=["tickets"]))
    with pytest.raises(PipelineStepFailed) as failed:
        pipeline.run(zendesk_source("a--b", client=fake, resources=["tickets"]))
    assert "belongs to Zendesk 'a-b'" in str(_cause(failed.value, ValueError))


def test_each_account_gets_its_own_tables_and_sync_state():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR

    source = zendesk_source(SUB, client=FakeZendesk())
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "zendesk_acme"
    other = zendesk_source("globex", client=FakeZendesk())
    assert set(other.resources) == {"zendesk_globex_tickets", "zendesk_globex_articles"}
    named = zendesk_source(SUB, resource_name="support_eu", client=FakeZendesk())
    assert set(named.resources) == {"support_eu_tickets", "support_eu_articles"}
    assert getattr(named, PIPELINE_SCOPE_ATTR) == "support_eu"


@pytest.mark.parametrize("resource", ["tickets", "articles"])
def test_check_active_can_stop_a_sync_before_anything_is_emitted(tmp_path, resource):
    import dlt

    class AccessRevokedError(Exception):
        pass

    def revoked():
        raise AccessRevokedError

    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.articles = [article(5)]
    source = zendesk_source(SUB, resources=[resource], check_active=revoked, client=fake)
    pipeline = dlt.pipeline(pipeline_name="zendesk_guard", pipelines_dir=str(tmp_path))
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.extract(source)
    assert _cause(caught.value, AccessRevokedError)
    assert fake.calls == []  # stopped before the first request
