"""Unit tests for the Zoho Desk connector, against ``FakeZohoDesk`` (see fake_zoho_desk.py).

Coverage: ticket and article rendering (customer, agent, internal thread and comment
labels; names, never email addresses or phone numbers; time order; HTML), the first full
read and the Search API cursor, replies and comments added and edited, repeats in the
overlap window, a search index that lags, the Recycle Bin and a just-deleted ticket the
search still returns, trashed and purged articles, departments (selected, moved
out), articles published, edited, unpublished and deleted, include_comments and
include_articles, the reconcile after a long gap, archived tickets missing from the
listing, listings that count from 1, the credit budget, rate limits, organizations, the
HTTP client (token refresh, a 401 mid-run, 204, redirects, retries, the credentials),
config validation, check_active, the sync stats, and real dlt merges into SQLite.
"""

import io
import json
import urllib.error
import urllib.parse

import pytest
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from dlt.extract.exceptions import ResourceExtractionError
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_zoho_desk import BILLING, ORG, SUPPORT, FakeZohoDesk

from cognee_community_connector_zoho_desk import zoho_desk_source
from cognee_community_connector_zoho_desk.zoho_desk import (
    ZohoDeskAPIError,
    ZohoDeskAuthError,
    ZohoDeskClient,
    ZohoDeskRateLimitError,
    article_to_row,
    sync_desk,
    ticket_to_row,
)

DAY = 24 * 3600 * 1000


def _cause(error, kind):
    while error is not None:
        if isinstance(error, kind):
            return error
        error = error.__cause__ or error.__context__
    return None


def sync(fake, state, stats=None, **kw):
    stats = {} if stats is None else stats
    return list(sync_desk(fake, state, stats, now_ms=lambda: fake.now, **kw))


def ids(rows):
    return sorted((r["id"], r["_deleted"]) for r in rows)


def ticket_reads(fake, since=0):
    return sorted(
        p.split("/")[2]
        for p in fake.calls[since:]
        if p.count("/") == 2 and p.startswith("/tickets/") and "search" not in p
    )


# --------------------------------------------------------------------- rendering
def test_a_ticket_is_one_document_with_labels_and_no_addresses():
    fake = FakeZohoDesk()
    tid = fake.ticket("Invoice shows the wrong amount", text="I was charged twice.")
    fake.thread(tid, "Sorry, refunding the second charge now.")
    fake.thread(tid, "Forwarding to finance for approval.", private=True)
    fake.comment(tid, "Refund approved by finance.")
    fake.comment(tid, "We have refunded you.", public=True)
    (row,) = sync(fake, {}, include_articles=False)
    content = row["content"]
    assert row["id"] == f"ticket:{tid}"
    assert row["title"].endswith("Invoice shows the wrong amount")
    assert row["url"].startswith("https://desk.zoho.in/") and row["cognee_node_set"] == ["Support"]
    assert content.startswith("Ticket #")
    assert "Department: Support" in content and "Status: Open | Priority: High" in content
    assert "Assignee: Alice Smith" in content and "Customer: Jane Doe" in content
    assert "--- Customer (Jane Doe)," in content and "I was charged twice." in content
    assert "--- Agent (Alice Smith)," in content
    assert "--- Internal thread (Alice Smith)," in content
    assert "--- Internal comment (Alice Smith)," in content
    assert "--- Comment (Alice Smith)," in content
    assert content.count("I was charged twice.") == 1, "the description thread is not repeated"
    for secret in ("jane@example.com", "alice@acme.test", "someone@example.com", "+15550100"):
        assert secret not in content
    order = [
        content.index(x)
        for x in ("charged twice", "refunding", "Forwarding", "approved", "We have")
    ]
    assert order == sorted(order)


def test_a_ticket_renders_the_same_whatever_order_zoho_sends():
    fake = FakeZohoDesk()
    tid = fake.ticket("A")
    fake.thread(tid, "one")
    fake.comment(tid, "two")
    ticket = {**fake.tickets[tid]}
    threads, comments = fake.threads[tid], fake.comments[tid]
    a = ticket_to_row(ticket, threads, comments, "Support")
    b = ticket_to_row(ticket, list(reversed(threads)), list(reversed(comments)), "Support")
    assert a == b


def test_a_ticket_without_threads_uses_its_description():
    ticket = {
        "id": "1",
        "subject": "Hi",
        "description": "<p>Help&nbsp;me</p>",
        "createdTime": "2026-10-01T10:00:00.000Z",
    }
    row = ticket_to_row(ticket, [], [], "")
    assert "--- Customer, 2026-10-01 10:00 UTC\nHelp me" in row["content"]
    assert "cognee_node_set" not in row and row["url"] is None


def test_an_article_renders_its_title_category_and_body():
    row = article_to_row(
        {
            "id": "9",
            "title": "Refunds",
            "answer": "<p>We refund within 5 days.</p>",
            "category": {"name": "Billing FAQ"},
        },
        "Support",
    )
    assert row["id"] == "article:9"
    assert (
        row["content"] == "Article: Refunds\nDepartment: Support\nCategory: Billing FAQ\n\n"
        "We refund within 5 days."
    )
    assert article_to_row({"id": "9", "title": "Empty", "answer": ""}, "S") is None


# --------------------------------------------------------------------- incremental
def test_first_run_reads_everything_then_only_what_changed():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    b = fake.ticket("B")
    state, stats = {}, {}
    assert ids(sync(fake, state, stats=stats)) == sorted(
        [(f"ticket:{a}", False), (f"ticket:{b}", False)]
    )
    assert stats == {"scanned": 2, "deleted": 0, "skipped": 0, "failed": 0}

    fake.tick()
    mark = len(fake.calls)
    assert sync(fake, state) == []
    assert ticket_reads(fake, mark) == [], "nothing changed: no ticket is read again"
    assert "/tickets/search" in fake.calls[mark:]

    fake.thread(a, "Here is the fix.")
    mark = len(fake.calls)
    (row,) = sync(fake, state)
    assert row["id"] == f"ticket:{a}" and "Here is the fix." in row["content"]
    assert ticket_reads(fake, mark) == [a]


def test_comments_added_and_edited_rerender_the_ticket():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    state = {}
    sync(fake, state)
    cid = fake.comment(a, "Check the courier first.")
    assert "Check the courier first." in sync(fake, state)[0]["content"]
    fake.edit_comment(a, cid, "Refund now.")
    content = sync(fake, state)[0]["content"]
    assert "Refund now." in content and "courier" not in content


def test_repeats_in_the_overlap_window_are_not_read_again():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    state = {}
    sync(fake, state)
    fake.thread(a, "reply")
    sync(fake, state)
    fake.tick()
    mark = len(fake.calls)
    assert sync(fake, state) == []  # the window still covers A's change
    assert ticket_reads(fake, mark) == []


def test_a_change_the_search_index_shows_late_is_still_caught():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    state = {}
    sync(fake, state)
    fake.thread(a, "late reply")
    fake.search_lag.add(a)
    assert sync(fake, state) == []
    fake.search_lag.clear()
    fake.now += 5 * 60 * 1000  # five minutes later, still inside the overlap
    (row,) = sync(fake, state)
    assert "late reply" in row["content"]


def test_a_change_that_keeps_the_text_is_skipped():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    state, stats = {}, {}
    sync(fake, state)
    fake._touch(a)
    assert sync(fake, state, stats=stats) == []
    assert stats["skipped"] == 1


def test_a_deleted_ticket_is_forgotten_through_the_recycle_bin():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    b = fake.ticket("B")
    state = {}
    sync(fake, state)
    fake.delete_ticket(a)
    assert ids(sync(fake, state)) == [(f"ticket:{a}", True)]
    assert sync(fake, state) == [], "forgotten once, though it stays in the bin"
    assert f"ticket:{b}" in state["tickets"]


def test_a_just_deleted_ticket_the_search_still_returns_is_not_read():
    """Seen live: right after a ticket goes to the Recycle Bin, the Search API can still
    return it, and its conversation answers 403."""
    fake = FakeZohoDesk()
    fake.ticket("A")
    b = fake.ticket("B")
    state = {}
    sync(fake, state)
    ghost = dict(fake.tickets[b])
    fake.delete_ticket(b)
    real_get = fake.get

    def get(path, params=None):
        if path == "/tickets/search":
            payload, now = real_get(path, params)
            stale = {k: ghost[k] for k in ("id", "departmentId", "modifiedTime")}
            return {"data": [*payload.get("data", []), stale]}, now
        if path == f"/tickets/{b}":
            return dict(ghost), fake.now
        if path == f"/tickets/{b}/conversations":
            raise ZohoDeskAPIError("HTTP 403", 403)
        return real_get(path, params)

    fake.get = get
    assert ids(sync(fake, state)) == [(f"ticket:{b}", True)]
    assert sync(fake, state) == [], "a later run neither fails nor reads it"


def test_a_ticket_whose_conversation_answers_403_is_skipped_not_forgotten():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    state = {}
    sync(fake, state)
    fake.thread(a, "new reply")
    fake.fail[f"/tickets/{a}/conversations"] = 403
    assert sync(fake, state) == []
    assert f"ticket:{a}" in state["tickets"]
    fake.fail = {}
    fake.thread(a, "another reply")
    assert "another reply" in sync(fake, state)[0]["content"]


def test_only_selected_departments_and_a_move_out_forgets():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    b = fake.ticket("B", department=BILLING)
    state = {}
    assert ids(sync(fake, state, departments=[SUPPORT])) == [(f"ticket:{a}", False)]
    fake.move(a, BILLING)
    assert ids(sync(fake, state, departments=[SUPPORT])) == [(f"ticket:{a}", True)]
    assert b not in json.dumps(state)


def test_changing_the_departments_rereads_and_forgets_the_rest():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    b = fake.ticket("B", department=BILLING)
    state = {}
    sync(fake, state)
    rows = sync(fake, state, departments=[BILLING])
    assert ids(rows) == [(f"ticket:{a}", True)]
    assert f"ticket:{b}" in state["tickets"]


def test_articles_published_edited_unpublished_and_deleted():
    fake = FakeZohoDesk()
    pub = fake.article("Refunds", "We refund within 5 days.")
    draft = fake.article("Draft", "Not ready", status="Draft")
    gone = fake.article("Old policy", "Refunds take 30 days.")
    state = {}
    rows = sync(fake, state)
    assert ids(rows) == sorted([(f"article:{gone}", False), (f"article:{pub}", False)])
    assert draft not in json.dumps(rows)

    fake.edit_article(pub, answer="<p>We refund within 3 days.</p>")
    fake.delete_article(gone)
    rows = sync(fake, state)
    assert ids(rows) == sorted([(f"article:{gone}", True), (f"article:{pub}", False)])
    assert "3 days" in next(r for r in rows if not r["_deleted"])["content"]

    fake.edit_article(pub, status="Unpublished")
    assert ids(sync(fake, state)) == [(f"article:{pub}", True)]
    fake.edit_article(draft, status="Published")
    assert ids(sync(fake, state)) == [(f"article:{draft}", False)]


def test_a_trashed_article_is_forgotten_although_it_is_not_in_the_recycle_bin():
    """Seen live: the article drops out of the listing and answers isTrashed: true."""
    fake = FakeZohoDesk()
    aid = fake.article("Refunds", "We refund within 5 days.")
    keep = fake.article("Shipping", "We ship in 2 days.")
    state = {}
    sync(fake, state)
    fake.delete_article(aid)
    assert not fake.bin
    assert ids(sync(fake, state)) == [(f"article:{aid}", True)]
    assert f"article:{keep}" in state["articles"]


def test_an_article_deleted_for_good_is_forgotten():
    fake = FakeZohoDesk()
    aid = fake.article("Refunds", "We refund within 5 days.")
    state = {}
    sync(fake, state)
    fake.purge_article(aid)
    assert ids(sync(fake, state)) == [(f"article:{aid}", True)]


def test_unchanged_articles_are_not_read_again():
    fake = FakeZohoDesk()
    fake.article("Refunds", "We refund within 5 days.")
    state = {}
    sync(fake, state)
    fake.tick()
    mark = len(fake.calls)
    sync(fake, state)
    assert not [p for p in fake.calls[mark:] if p.startswith("/articles/")]


def test_include_comments_off_and_switching_rereads():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    fake.comment(a, "internal: VIP customer")
    state = {}
    (row,) = sync(fake, state, include_comments=False, include_articles=False)
    assert "VIP" not in row["content"]
    (row,) = sync(fake, state, include_comments=True, include_articles=False)
    assert "VIP" in row["content"]


def test_include_articles_off_forgets_stored_articles():
    fake = FakeZohoDesk()
    aid = fake.article("Refunds", "We refund within 5 days.")
    state = {}
    sync(fake, state)
    mark = len(fake.calls)
    assert ids(sync(fake, state, include_articles=False)) == [(f"article:{aid}", True)]
    assert not [p for p in fake.calls[mark:] if p.startswith("/articles")]


def test_after_a_long_gap_a_full_read_catches_deletes_the_bin_no_longer_holds():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    b = fake.ticket("B")
    state = {}
    sync(fake, state)
    fake.delete_ticket(a)
    fake.purge_bin()  # sixty days later the bin has dropped it
    fake.now += 55 * DAY
    assert ids(sync(fake, state)) == [(f"ticket:{a}", True)]
    assert f"ticket:{b}" in state["tickets"]


def test_a_ticket_missing_from_the_full_listing_but_still_readable_is_kept():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    state = {}
    sync(fake, state)
    real_get = fake.get

    def get(path, params=None):
        payload, now = real_get(path, params)
        if path == "/tickets":
            payload = {"data": [t for t in payload.get("data", []) if t["id"] != a]}
        return payload, now

    fake.get = get  # archived tickets drop out of the plain listing
    fake.now += 55 * DAY
    assert sync(fake, state) == []
    assert f"ticket:{a}" in state["tickets"]


def test_listings_are_read_across_pages_and_from_one_when_zoho_counts_from_one(monkeypatch):
    from cognee_community_connector_zoho_desk import zoho_desk

    monkeypatch.setattr(zoho_desk, "_PAGE_SIZE", 2)
    fake = FakeZohoDesk(first_index=1)
    made = [fake.ticket(f"T{i}") for i in range(5)]
    for i in range(3):
        fake.comment(made[0], f"note {i}")
    rows = sync(fake, {}, include_articles=False)
    assert sorted(r["id"] for r in rows) == sorted(f"ticket:{t}" for t in made)
    first = next(r for r in rows if r["id"] == f"ticket:{made[0]}")
    assert all(f"note {i}" in first["content"] for i in range(3))


def test_running_low_on_credits_stops_cleanly_and_the_next_run_resumes():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    b = fake.ticket("B")
    state, stats = {}, {}
    real_get = fake.get

    def get(path, params=None):
        answer = real_get(path, params)
        if path == f"/tickets/{a}/conversations":
            fake.credits_left = 10  # the day's allowance is nearly gone
        return answer

    fake.get = get
    rows = sync(fake, state, stats=stats)
    assert ids(rows) == [], "a stopped run emits what it finished; A was interrupted"
    assert stats["failed_rate_limit"] == 1 and "cursor" not in state
    fake.get = real_get
    fake.credits_left = 5000
    assert ids(sync(fake, state)) == sorted([(f"ticket:{a}", False), (f"ticket:{b}", False)])


def test_a_rate_limit_keeps_what_was_read_and_the_cursor_stays():
    fake = FakeZohoDesk()
    a = fake.ticket("A")
    state = {}
    sync(fake, state)
    cursor = state["cursor"]
    fake.thread(a, "reply")
    b = fake.ticket("B")  # changed after A, so read after it
    fake.fail[f"/tickets/{b}/conversations"] = 429
    stats = {}
    rows = sync(fake, state, stats=stats)
    assert ids(rows) == [(f"ticket:{a}", False)]
    assert state["cursor"] == cursor and stats["failed_rate_limit"] == 1
    fake.fail = {}
    assert ids(sync(fake, state)) == [(f"ticket:{b}", False)]


def test_an_account_in_several_organizations_needs_org_id():
    fake = FakeZohoDesk()
    fake.orgs.append({"id": "60009999999"})
    with pytest.raises(ValueError, match="org_id"):
        sync(fake, {})
    fake.org_id = ORG
    sync(fake, {})


def test_a_state_of_another_organization_is_refused():
    fake = FakeZohoDesk()
    fake.ticket("A")
    state = {}
    sync(fake, state)
    other = FakeZohoDesk()
    other.orgs = [{"id": "60009999999"}]
    other.get = lambda path, params=None: ({"data": other.orgs}, other.now)
    with pytest.raises(ValueError, match="another Zoho Desk organization"):
        sync(other, state)


def test_an_api_error_fails_the_run_and_forgets_nothing():
    fake = FakeZohoDesk()
    fake.ticket("A")
    state = {}
    sync(fake, state)
    before = json.dumps(state, sort_keys=True)
    fake.fail["/recycleBin"] = 500
    with pytest.raises(ZohoDeskAPIError):
        sync(fake, state)
    assert json.dumps(state, sort_keys=True) == before


# --------------------------------------------------------------------- client
class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = {"Date": "Wed, 21 Oct 2026 10:00:00 GMT", **(headers or {})}


def _http_error(url, status, headers=None):
    return urllib.error.HTTPError(url, status, "err", headers or {}, None)


TOKEN_OK = {"access_token": "acc-1", "expires_in": 3600}


class _Opener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, _Response):
            return outcome
        body = outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode()
        return _Response(body)


def _client(*outcomes, **kw):
    sleeps = []
    client = ZohoDeskClient(
        data_center="in",
        client_id="1000.CLIENT",
        client_secret="client-secret-xyz",
        refresh_token="1000.refresh-secret",
        org_id=ORG,
        sleep=sleeps.append,
        **kw,
    )
    client._open = _Opener(*outcomes)
    return client, sleeps


def test_the_client_refreshes_a_token_and_sends_it_with_the_org_header():
    client, _ = _client(
        TOKEN_OK, _Response(b'{"data": []}', headers={"X-Rate-Limit-Remaining-v3": "4990"})
    )
    payload, server_ms = client.get("/tickets", {"from": 0, "limit": 50})
    token_request, api_request = client._open.requests
    assert token_request.full_url == "https://accounts.zoho.in/oauth/v2/token"
    form = urllib.parse.parse_qs(token_request.data.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == [
        "1000.refresh-secret"
    ]
    assert api_request.full_url == "https://desk.zoho.in/api/v1/tickets?from=0&limit=50"
    assert api_request.get_header("Authorization") == "Zoho-oauthtoken acc-1"
    assert api_request.get_header("Orgid") == ORG
    assert payload == {"data": []} and server_ms == 1792576800000
    assert client.credits_left == 4990


def test_a_204_is_an_empty_listing():
    client, _ = _client(TOKEN_OK, _Response(b"", status=204))
    assert client.get("/recycleBin")[0] == {}


def test_a_401_mid_run_refreshes_the_token_once():
    url = "https://desk.zoho.in/api/v1/x"
    client, _ = _client(TOKEN_OK, _http_error(url, 401), {"access_token": "acc-2"}, {"ok": 1})
    assert client.get("/x")[0] == {"ok": 1}
    assert client._open.requests[-1].get_header("Authorization") == "Zoho-oauthtoken acc-2"


def test_a_refused_refresh_token_is_an_auth_error_without_the_secrets():
    client, _ = _client({"error": "invalid_code"})
    with pytest.raises(ZohoDeskAuthError, match="invalid_code") as caught:
        client.get("/x")
    error = caught.value
    while error is not None:
        for secret in ("refresh-secret", "client-secret-xyz"):
            assert secret not in str(error)
        error = error.__cause__ or error.__context__


def test_a_second_401_ends_the_run():
    url = "https://desk.zoho.in/api/v1/x"
    client, _ = _client(TOKEN_OK, _http_error(url, 401), TOKEN_OK, _http_error(url, 401))
    with pytest.raises(ZohoDeskAuthError, match="HTTP 401"):
        client.get("/x")


def test_the_client_retries_5xx_and_waits_out_a_short_rate_limit():
    url = "https://desk.zoho.in/api/v1/x"
    client, sleeps = _client(
        TOKEN_OK, _http_error(url, 503), _http_error(url, 429, {"Retry-After": "3"}), {"ok": 1}
    )
    assert client.get("/x")[0] == {"ok": 1}
    assert sleeps == [1.0, 3.0]


def test_a_long_wait_is_not_slept_through():
    client, sleeps = _client(TOKEN_OK, _http_error("u", 429, {"Retry-After": "3600"}))
    with pytest.raises(ZohoDeskRateLimitError):
        client.get("/x")
    assert sleeps == []


def test_a_redirect_is_not_followed_and_errors_hide_ids():
    from cognee_community_connector_zoho_desk.zoho_desk import _NoRedirect

    client, _ = _client(TOKEN_OK, _http_error("u", 302, {"Location": "https://evil.example/"}))
    with pytest.raises(ZohoDeskAPIError, match="not followed") as caught:
        client.get("/tickets/2000000123")
    assert "2000000123" not in str(caught.value) and "/tickets/{id}" in str(caught.value)
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://e/") is None


@pytest.mark.parametrize("body", [b"<html>login</html>", b"[1]", b"null"])
def test_a_200_without_a_json_object_fails(body):
    client, _ = _client(TOKEN_OK, body)
    with pytest.raises(ZohoDeskAPIError, match="without a JSON object"):
        client.get("/x")


def test_the_client_repr_hides_the_credentials():
    client, _ = _client()
    assert "secret" not in repr(client)


@pytest.mark.parametrize("dc", ["", "us", "zoho.com", None])
def test_an_unknown_data_center_is_refused(dc):
    with pytest.raises(ValueError, match="data_center"):
        ZohoDeskClient(data_center=dc, client_id="a", client_secret="b", refresh_token="c")


@pytest.mark.parametrize("field", ["client_id", "client_secret", "refresh_token"])
def test_credentials_with_spaces_are_refused_without_echoing_them(field):
    kw = {"client_id": "a", "client_secret": "b", "refresh_token": "c", field: "xq7 zv9"}
    with pytest.raises(ValueError, match=field) as caught:
        ZohoDeskClient(data_center="com", **kw)
    assert "xq7" not in str(caught.value) and "zv9" not in str(caught.value)


def test_sync_through_the_real_client_sends_the_token_but_never_stores_it():
    fake = FakeZohoDesk()
    fake.ticket("A", text="Hello")
    client = ZohoDeskClient(
        data_center="in", client_id="id", client_secret="sec", refresh_token="refresh-xyz"
    )
    seen = []

    def opener(request):
        url = urllib.parse.urlparse(request.full_url)
        if url.netloc == "accounts.zoho.in":
            return _Response(json.dumps({"access_token": "acc-abc"}).encode())
        assert url.netloc == "desk.zoho.in" and url.path.startswith("/api/v1/")
        seen.append(request.get_header("Authorization"))
        fake.org_id = request.get_header("Orgid")
        params = dict(urllib.parse.parse_qsl(url.query))
        try:
            payload, _ = fake.get(url.path.removeprefix("/api/v1"), params)
        except ZohoDeskAPIError as exc:
            raise _http_error(request.full_url, exc.status or 500) from None
        return _Response(
            json.dumps(payload).encode() if payload else b"", status=200 if payload else 204
        )

    client._open = opener
    rows = sync(client, {})
    assert ids(rows)[0][0].startswith("ticket:") and "Hello" in rows[0]["content"]
    assert seen and all(h == "Zoho-oauthtoken acc-abc" for h in seen)
    assert "refresh-xyz" not in json.dumps(rows) and "acc-abc" not in json.dumps(rows)


# --------------------------------------------------------------------- factory
def test_source_validates_config():
    fake = FakeZohoDesk()
    with pytest.raises(ValueError, match="departments"):
        zoho_desk_source(departments=SUPPORT, client=fake)
    with pytest.raises(ValueError, match="departments"):
        zoho_desk_source(departments=["Support"], client=fake)
    with pytest.raises(ValueError, match="departments"):
        zoho_desk_source(departments=[], client=fake)
    with pytest.raises(ValueError, match="client_id"):
        zoho_desk_source()
    with pytest.raises(ValueError, match="not both"):
        zoho_desk_source(client=fake, service=FakeZohoDesk())


def test_credentials_are_arguments_only(monkeypatch):
    for name in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN"):
        monkeypatch.setenv(name, "from-env")
    with pytest.raises(ValueError, match="client_id"):
        zoho_desk_source()


@pytest.mark.parametrize(
    "name", ["Zoho-Desk", "zoho desk", "zohoDesk", "1zoho", "zoho__desk", "zoho_", "a" * 52]
)
def test_a_resource_name_dlt_would_rename_is_refused(name):
    with pytest.raises(ValueError, match="resource_name"):
        zoho_desk_source(resource_name=name, client=FakeZohoDesk())


@pytest.mark.parametrize("name", ["zoho_desk", "zoho_desk_acme", "desk2"])
def test_accepted_names_are_the_table_names_dlt_writes(name):
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    source = zoho_desk_source(resource_name=name, client=FakeZohoDesk())
    assert NamingConvention().normalize_table_identifier(source.name) == name


def test_a_cognee_without_table_scoped_cleanup_is_refused(monkeypatch):
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)
    with pytest.raises(RuntimeError, match="cognee 1"):
        zoho_desk_source(client=FakeZohoDesk())


def test_the_resource_is_merge_with_hard_delete_and_document_mode():
    from cognee.tasks.ingestion.dlt_utils import (
        DOCUMENT_SOURCE_ATTR,
        NODE_SET_COLUMN,
        PIPELINE_SCOPE_ATTR,
    )

    source = zoho_desk_source(client=FakeZohoDesk())
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "zoho_desk"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "zoho_desk"
    assert source.write_disposition == "merge"
    assert source.columns["_deleted"]["hard_delete"] is True
    assert source.columns[NODE_SET_COLUMN]["data_type"] == "json"


def test_check_active_runs_before_every_request_and_can_stop_the_sync():
    fake = FakeZohoDesk()
    fake.ticket("A")
    checks = []
    list(zoho_desk_source(client=fake, check_active=lambda: checks.append(len(fake.calls))))
    request_checks = [n for n in checks if n < len(fake.calls)]
    assert len(set(request_checks)) == len(fake.calls)

    fresh = FakeZohoDesk()

    def revoked():
        raise PermissionError("access revoked")

    with pytest.raises(ResourceExtractionError) as caught:
        list(zoho_desk_source(client=fresh, check_active=revoked))
    assert _cause(caught.value, PermissionError) is not None and fresh.calls == []


# --------------------------------------------------------------------- dlt runs
def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="zoho_desk_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'zd.db'}"),
        dataset_name="zoho_desk_test",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _ids(pipeline, table="zoho_desk"):
    with pipeline.sql_client() as sql:
        try:
            return sorted(r[0] for r in sql.execute_sql(f"SELECT id FROM {table}"))
        except DatabaseUndefinedRelation:
            return []


def test_e2e_dlt_merge_keeps_unchanged_and_removes_deleted(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeZohoDesk()
    a = fake.ticket("keep")
    b = fake.ticket("delete me")
    art = fake.article("Refunds", "We refund within 5 days.")
    pipeline.run(zoho_desk_source(client=fake))
    assert _ids(pipeline) == sorted([f"ticket:{a}", f"ticket:{b}", f"article:{art}"])

    fake.comment(a, "note")
    fake.delete_ticket(b)
    fake.delete_article(art)
    source = zoho_desk_source(client=fake)
    pipeline.run(source)
    assert _ids(pipeline) == [f"ticket:{a}"]
    assert source.cognee_sync_stats == {"scanned": 1, "deleted": 2, "skipped": 0, "failed": 0}
    assert all(type(v) is int for v in source.cognee_sync_stats.values())


def test_a_failed_load_keeps_the_cursor_so_nothing_is_lost(tmp_path, monkeypatch):
    from dlt.pipeline.pipeline import Pipeline

    pipeline, fake = _pipeline(tmp_path), FakeZohoDesk()
    a = fake.ticket("first")
    pipeline.run(zoho_desk_source(client=fake))
    b = fake.ticket("second")

    def boom(*_args, **_kwargs):
        raise RuntimeError("load failed")

    monkeypatch.setattr(Pipeline, "load", boom)
    with pytest.raises(RuntimeError):
        pipeline.run(zoho_desk_source(client=fake))
    monkeypatch.undo()
    pipeline.run(zoho_desk_source(client=fake))
    assert _ids(pipeline) == sorted([f"ticket:{a}", f"ticket:{b}"])


def test_a_source_object_run_again_starts_with_fresh_stats(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeZohoDesk()
    fake.ticket("A")
    fake.credits_left = 10
    source = zoho_desk_source(client=fake)
    pipeline.run(source)
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
    fake.credits_left = 5000
    pipeline.run(source)
    assert source.cognee_sync_stats["failed"] == 0
    assert "failed_rate_limit" not in source.cognee_sync_stats


def test_two_organizations_cannot_share_one_table(tmp_path):
    pipeline = _pipeline(tmp_path)
    one, two = FakeZohoDesk(), FakeZohoDesk()
    one.ticket("one")
    pipeline.run(zoho_desk_source(client=one))
    two.orgs = [{"id": "60009999999"}]
    real = two.get

    def get(path, params=None):
        two.org_id = ORG  # the fake checks its own org; the state check is what matters
        return real(path, params)

    two.get = get
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(zoho_desk_source(client=two))
    assert "another Zoho Desk organization" in str(_cause(caught.value, ValueError))
