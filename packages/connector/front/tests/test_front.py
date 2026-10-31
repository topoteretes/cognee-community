"""Unit tests for the Front connector, against ``FakeFront`` (see fake_front.py).

Coverage: conversation rendering (customer, teammate and internal comment labels,
names not addresses, drafts left out, HTML and entities, time order, tags, node sets),
the first sync and later runs that only re-read changed conversations, a tag-only
change, comments added, edited and deleted, a deleted message, trash and restore,
permanent delete (404), merge (301, never followed), a move out of and between the
selected inboxes, archived conversations kept or left out, include_comments switched,
a dropped inbox, paging, an inbox that fails or keeps failing, rate limits, the HTTP
client (retries, dropped connections, long waits, redirects, a 200 without JSON, ids
kept out of errors, the token, pacing from the rate-limit headers), config validation,
check_active, the sync stats, and real dlt merges into SQLite.
"""

import http.client
import io
import json
import urllib.error
import urllib.parse

import pytest
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from dlt.extract.exceptions import ResourceExtractionError
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_front import API, BOB, FakeFront

from cognee_community_connector_front import front_source
from cognee_community_connector_front.front import (
    FrontAPIError,
    FrontAuthError,
    FrontClient,
    FrontRateLimitError,
    conversation_to_row,
    sync_inboxes,
)

INBOX = "inb_support"


def _cause(error, kind):
    """The first exception of ``kind`` in an error's chain (dlt wraps what a source raises)."""
    while error is not None:
        if isinstance(error, kind):
            return error
        error = error.__cause__ or error.__context__
    return None


def sync(fake, state, inboxes=(INBOX,), stats=None, **kw):
    stats = {} if stats is None else stats
    return list(sync_inboxes(fake, state, list(inboxes), stats, **kw))


def ids(rows):
    return [(r["id"], r["_deleted"]) for r in rows]


def reads(fake, since=0):
    """Conversations whose messages were read."""
    return sorted(p.split("/")[2] for p in fake.calls[since:] if p.endswith("/messages"))


# --------------------------------------------------------------------- rendering
def test_a_conversation_is_one_document_with_labels_and_no_addresses():
    fake = FakeFront()
    cid = fake.conversation("Order #1042 never arrived", tags=("shipping",))
    fake.reply(cid, "Sorry! I've sent a replacement.")
    fake.comment(cid, "Courier lost it, refund the postage&#x20;too.")
    fake.reply(cid, "Unsent draft", draft=True)
    (row,) = sync(fake, {})
    content = row["content"]
    assert row["id"] == cid and row["title"] == "Order #1042 never arrived"
    assert row["url"] == f"https://app.frontapp.com/open/{cid}"
    assert row["cognee_node_set"] == ["Support"]
    assert content.startswith("Subject: Order #1042 never arrived\nInbox: Support\n")
    assert "Status: unassigned | Tags: shipping" in content
    assert "--- Customer (Jane Doe)," in content
    assert "--- Teammate (Alice Smith)," in content
    assert "--- Internal comment (Alice Smith)," in content
    assert "refund the postage too." in content
    assert "Unsent draft" not in content
    assert row["visibility"] == "customer_and_internal"
    for address in ("jane@example.com", "alice@acme.test", "support@acme.test"):
        assert address not in content
    # time order: customer, reply, comment
    assert content.index("Customer") < content.index("Teammate") < content.index("Internal")


def test_a_conversation_renders_the_same_whatever_order_front_sends():
    fake = FakeFront()
    cid = fake.conversation("Hi")
    fake.reply(cid, "One")
    fake.reply(cid, "Two", author=BOB)
    conversation = fake.conversations[cid]
    messages = fake.messages[cid]
    a = conversation_to_row(conversation, messages, [], "Support")
    b = conversation_to_row(conversation, list(reversed(messages)), [], "Support")
    assert a == b and "Teammate (@bob)" in a["content"]
    assert a["visibility"] == "customer", "no internal comments: customer-facing only"


def test_html_bodies_become_text_and_a_customer_address_name_is_dropped():
    msg = {
        "id": "msg_1",
        "is_inbound": True,
        "created_at": 1,
        "recipients": [{"role": "from", "name": "jane@example.com", "handle": "jane@example.com"}],
        "text": "",
        "body": "<p>Line one<br>Line&nbsp;two &amp; more</p>",
    }
    row = conversation_to_row({"id": "cnv_1", "subject": ""}, [msg], [], "Support")
    assert "--- Customer, " in row["content"]
    assert "Line one\nLine two & more" in row["content"]
    assert "jane@example.com" not in row["content"]
    assert row["title"] == "(no subject)"


def test_attachments_only_and_drafts_only():
    files = {
        "id": "msg_1",
        "is_inbound": True,
        "created_at": 1,
        "text": "",
        "body": "",
        "attachments": [{"filename": "invoice.pdf"}],
    }
    row = conversation_to_row({"id": "cnv_1", "subject": "x"}, [files], [], "Support")
    assert "(attachments: invoice.pdf)" in row["content"]
    draft = {**files, "is_draft": True}
    assert conversation_to_row({"id": "cnv_1", "subject": "x"}, [draft], [], "S") is None


def test_long_subjects_are_shortened_in_the_title():
    row = conversation_to_row(
        {"id": "cnv_1", "subject": "x" * 200}, [{"id": "m", "text": "t"}], [], "S"
    )
    assert len(row["title"]) == 80 and row["title"].endswith("...")


# --------------------------------------------------------------------- incremental
def test_first_run_reads_everything_then_only_changes():
    fake = FakeFront()
    a = fake.conversation("A")
    b = fake.conversation("B")
    state, stats = {}, {}
    assert sorted(ids(sync(fake, state, stats=stats))) == sorted([(a, False), (b, False)])
    assert stats == {"scanned": 2, "deleted": 0, "skipped": 0, "failed": 0}

    mark = len(fake.calls)
    assert sync(fake, state) == []
    assert reads(fake, mark) == [], "nothing changed: no conversation is read"

    fake.reply(a, "Here you go.")
    mark = len(fake.calls)
    assert ids(sync(fake, state)) == [(a, False)]
    assert reads(fake, mark) == [a]


def test_a_tag_change_is_seen_although_updated_at_does_not_move():
    fake = FakeFront()
    a = fake.conversation("A", tags=("vip",))
    state = {}
    sync(fake, state)
    fake.tag(a, "refund")
    (row,) = sync(fake, state)
    assert "Tags: refund, vip" in row["content"]
    fake.rename_tag("vip", "priority")
    (row,) = sync(fake, state)
    assert "Tags: priority, refund" in row["content"]


def test_comments_added_edited_and_deleted_rerender_the_conversation():
    fake = FakeFront()
    a = fake.conversation("A")
    state = {}
    sync(fake, state)
    com = fake.comment(a, "Check the courier first.")
    assert "Check the courier first." in sync(fake, state)[0]["content"]
    fake.edit_comment(a, com, "Refund now.")
    content = sync(fake, state)[0]["content"]
    assert "Refund now." in content and "courier" not in content
    fake.delete_comment(a, com)
    assert "Refund now." not in sync(fake, state)[0]["content"]


def test_a_deleted_message_leaves_the_document():
    fake = FakeFront()
    a = fake.conversation("A")
    mid = fake.reply(a, "Here is my card number 4242")
    state = {}
    sync(fake, state)
    fake.delete_message(a, mid)
    (row,) = sync(fake, state)
    assert "4242" not in row["content"]


def test_a_change_that_keeps_the_text_emits_nothing():
    fake = FakeFront()
    a = fake.conversation("A")
    state, stats = {}, {}
    sync(fake, state)
    fake._touch(a)  # updated_at moves, nothing readable changes
    assert sync(fake, state, stats=stats) == []
    assert stats["skipped"] == 1


def test_trash_forgets_and_restore_brings_it_back():
    fake = FakeFront()
    a = fake.conversation("A")
    b = fake.conversation("B")
    state = {}
    sync(fake, state)
    fake.set_status(a, "deleted")
    assert ids(sync(fake, state)) == [(a, True)]
    assert sync(fake, state) == [], "a trashed conversation is only forgotten once"
    fake.set_status(a, "unassigned")
    assert ids(sync(fake, state)) == [(a, False)]
    assert b in state["conversations"]


def test_a_permanent_delete_is_forgotten():
    fake = FakeFront()
    a = fake.conversation("A")
    state = {}
    sync(fake, state)
    fake.purge(a)
    assert ids(sync(fake, state)) == [(a, True)]
    assert state["conversations"] == {}


def test_a_merged_away_conversation_is_forgotten_and_the_redirect_is_not_followed():
    fake = FakeFront()
    gone = fake.conversation("Duplicate", text="Same problem from my phone.")
    keep = fake.conversation("Original")
    state = {}
    sync(fake, state)
    fake.merge(gone, keep)
    rows = sync(fake, state)
    assert sorted(ids(rows)) == sorted([(keep, False), (gone, True)])
    survivor = next(r for r in rows if r["id"] == keep)
    assert "Same problem from my phone." in survivor["content"]
    # The 301 was read as "gone", not followed to the survivor.
    assert fake.calls.count(f"/conversations/{gone}") == 1
    assert f"/conversations/{gone}/inboxes" not in fake.calls


def test_a_move_out_of_the_selection_forgets_and_a_move_between_selected_inboxes_keeps():
    fake = FakeFront()
    fake.add_inbox("inb_billing", "Billing")
    fake.add_inbox("inb_sales", "Sales")
    a = fake.conversation("A")
    b = fake.conversation("B")
    state = {}
    sync(fake, state, inboxes=(INBOX, "inb_billing"))
    fake.move(a, "inb_billing")
    fake.move(b, "inb_sales")
    rows = sync(fake, state, inboxes=(INBOX, "inb_billing"))
    assert sorted(ids(rows)) == sorted([(a, False), (b, True)])
    moved = next(r for r in rows if r["id"] == a)
    assert moved["cognee_node_set"] == ["Billing"] and "Inbox: Billing" in moved["content"]


def test_a_conversation_missing_from_its_listing_but_still_selected_is_kept():
    fake = FakeFront()
    fake.add_inbox("inb_billing", "Billing")
    a = fake.conversation("A")
    state = {}
    sync(fake, state, inboxes=(INBOX, "inb_billing"))
    # The listing missed it this time, but a lookup says it is still in a selected inbox.
    fake.membership[a] = {INBOX}
    real_get = fake.get

    def get(path, params=None):
        payload = real_get(path, params)
        if path == f"/inboxes/{INBOX}/conversations":
            payload["_results"] = [c for c in payload["_results"] if c["id"] != a]
        return payload

    fake.get = get
    assert sync(fake, state, inboxes=(INBOX, "inb_billing")) == []
    assert a in state["conversations"]


def test_archived_conversations_are_kept_by_default_or_left_out():
    fake = FakeFront()
    a = fake.conversation("A")
    state = {}
    sync(fake, state)
    fake.set_status(a, "archived")
    (row,) = sync(fake, state)
    assert "Status: archived" in row["content"]

    state2 = {}
    fake2 = FakeFront()
    b = fake2.conversation("B")
    sync(fake2, state2, include_archived=False)
    fake2.set_status(b, "archived")
    assert ids(sync(fake2, state2, include_archived=False)) == [(b, True)]


def test_include_comments_off_leaves_comments_out_and_switching_rerenders():
    fake = FakeFront()
    a = fake.conversation("A")
    fake.comment(a, "internal: VIP customer")
    state = {}
    (row,) = sync(fake, state, include_comments=False)
    assert "VIP" not in row["content"]
    assert not any(p.endswith("/comments") for p in fake.calls)
    (row,) = sync(fake, state, include_comments=True)
    assert "VIP" in row["content"]


def test_a_dropped_inbox_forgets_its_conversations():
    fake = FakeFront()
    fake.add_inbox("inb_billing", "Billing")
    a = fake.conversation("A")
    b = fake.conversation("B", inbox="inb_billing")
    state = {}
    sync(fake, state, inboxes=(INBOX, "inb_billing"))
    assert ids(sync(fake, state, inboxes=(INBOX,))) == [(b, True)]
    assert set(state["conversations"]) == {a}
    assert set(state["inboxes"]) == {INBOX}


def test_listings_and_messages_are_read_across_pages():
    fake = FakeFront(page_size=2)
    convs = [fake.conversation(f"C{i}") for i in range(5)]
    for i in range(4):
        fake.reply(convs[0], f"reply {i}")
    rows = sync(fake, {})
    assert sorted(r["id"] for r in rows) == sorted(convs)
    first = next(r for r in rows if r["id"] == convs[0])
    assert all(f"reply {i}" in first["content"] for i in range(4))


def test_a_next_page_on_another_host_is_refused():
    fake = FakeFront()
    fake.conversation("A")
    real_get = fake.get

    def get(path, params=None):
        payload = real_get(path, params)
        if path.endswith("/conversations"):
            payload["_pagination"]["next"] = "https://evil.example/inboxes/x?page_token=1"
        return payload

    fake.get = get
    stats = {}
    with pytest.raises(FrontAPIError, match="Every selected"):
        sync(fake, {}, stats=stats)


def test_a_failing_inbox_forgets_nothing_and_the_others_still_sync():
    fake = FakeFront()
    fake.add_inbox("inb_billing", "Billing")
    a = fake.conversation("A")
    b = fake.conversation("B", inbox="inb_billing")
    state = {}
    sync(fake, state, inboxes=(INBOX, "inb_billing"))
    fake.purge(a)
    fake.fail[f"/inboxes/{INBOX}/conversations"] = 403
    fake.reply(b, "new")
    stats = {}
    rows = sync(fake, state, inboxes=(INBOX, "inb_billing"), stats=stats)
    assert ids(rows) == [(b, False)]
    assert stats["failed_inboxes"] == 1 and a in state["conversations"]
    fake.fail = {}
    assert ids(sync(fake, state, inboxes=(INBOX, "inb_billing"))) == [(a, True)]


def test_an_inbox_that_keeps_failing_raises_on_its_third_run():
    fake = FakeFront()
    fake.add_inbox("inb_billing", "Billing")
    fake.conversation("B", inbox="inb_billing")
    fake.fail[f"/inboxes/{INBOX}"] = 500
    state = {}
    sync(fake, state, inboxes=(INBOX, "inb_billing"))
    sync(fake, state, inboxes=(INBOX, "inb_billing"))
    with pytest.raises(FrontAPIError, match="3 runs in a row"):
        sync(fake, state, inboxes=(INBOX, "inb_billing"))


def test_when_every_inbox_fails_the_run_raises():
    fake = FakeFront()
    fake.fail[f"/inboxes/{INBOX}"] = 500
    with pytest.raises(FrontAPIError, match="Every selected"):
        sync(fake, {})


def test_a_rejected_token_ends_the_run():
    fake = FakeFront()
    fake.fail["/me"] = 401
    with pytest.raises(FrontAuthError):
        sync(fake, {})


def test_a_rate_limit_stops_cleanly_and_the_next_run_resumes():
    fake = FakeFront()
    a = fake.conversation("A")
    b = fake.conversation("B")
    state, stats = {}, {}
    fake.fail[f"/conversations/{b}/messages"] = 429  # conversations are read in id order
    rows = sync(fake, state, stats=stats)
    assert ids(rows) == [(a, False)], "what was read before the limit is kept"
    assert stats["failed_rate_limit"] == 1 and b not in state["conversations"]
    fake.fail = {}
    assert ids(sync(fake, state)) == [(b, False)]


def test_a_rate_limit_before_the_missing_checks_forgets_nothing():
    fake = FakeFront()
    a = fake.conversation("A")
    state = {}
    sync(fake, state)
    fake.purge(a)
    fake.fail[f"/conversations/{a}"] = 429
    assert sync(fake, state) == []
    fake.fail = {}
    assert ids(sync(fake, state)) == [(a, True)]


def test_a_state_of_another_front_account_is_refused():
    fake = FakeFront()
    fake.conversation("A")
    state = {}
    sync(fake, state)
    fake.me = {"id": "cmp_other"}
    with pytest.raises(ValueError, match="another Front account"):
        sync(fake, state)


# --------------------------------------------------------------------- client
class _Response(io.BytesIO):
    def __init__(self, body: bytes):
        super().__init__(body)
        self.headers = {}


def _http_error(status, headers=None):
    return urllib.error.HTTPError(f"{API}/x", status, "err", headers or {}, None)


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
        if isinstance(outcome, _Response):
            return outcome
        body = outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode()
        return _Response(body)


def _client(*outcomes, **kw):
    sleeps = []
    client = FrontClient("front-secret", sleep=sleeps.append, **kw)
    client._open = _Opener(*outcomes)
    return client, sleeps


def test_the_client_sends_the_token():
    client, _ = _client({"ok": True})
    assert client.get("/inboxes/inb_1/conversations", {"limit": 100}) == {"ok": True}
    request = client._open.requests[0]
    assert request.full_url == f"{API}/inboxes/inb_1/conversations?limit=100"
    assert request.get_header("Authorization") == "Bearer front-secret"


def test_the_client_retries_5xx_and_waits_out_a_short_rate_limit():
    client, sleeps = _client(_http_error(503), _http_error(429, {"Retry-After": "3"}), {"ok": 1})
    assert client.get("/x") == {"ok": 1}
    assert sleeps == [1.0, 3.0]


def test_the_client_waits_for_the_window_when_the_rate_limit_is_nearly_used_up():
    class _Paced(_Response):
        def __init__(self, remaining, reset):
            super().__init__(b'{"ok": 1}')
            self.headers = {
                "x-ratelimit-remaining": str(remaining),
                "x-ratelimit-reset": str(reset),
            }

    client, sleeps = _client(_Paced(40, 1010), _Paced(1, 1004), _Paced(0, 1500))
    client._clock = lambda: 1000.0
    for _ in range(3):
        client.get("/x")
    assert sleeps == [4.0], "only a nearly used-up window waits, and never over 30 s"


def test_a_long_wait_is_not_slept_through():
    client, sleeps = _client(_http_error(429, {"Retry-After": "120"}))
    with pytest.raises(FrontRateLimitError):
        client.get("/x")
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
    assert client.get("/x") == {"ok": True}
    assert len(sleeps) == 1


def test_a_server_that_stays_unreachable_ends_the_run():
    client, _ = _client(*[TimeoutError()] * 5)
    with pytest.raises(FrontAPIError, match="could not be reached") as caught:
        client.get("/x")
    assert caught.value.ends_run


def test_a_401_ends_the_run_without_the_token_in_the_error():
    client, sleeps = _client(_http_error(401))
    with pytest.raises(FrontAuthError) as caught:
        client.get("/x")
    assert sleeps == [] and caught.value.ends_run
    error = caught.value
    while error is not None:
        assert "front-secret" not in str(error)
        error = error.__cause__ or error.__context__


def test_errors_name_the_endpoint_but_not_the_ids():
    client, _ = _client(_http_error(404))
    with pytest.raises(FrontAPIError) as caught:
        client.get("/conversations/cnv_abc123/messages")
    assert "/conversations/{id}/messages" in str(caught.value)
    assert "cnv_abc123" not in str(caught.value)
    assert caught.value.status == 404 and not caught.value.ends_run


def test_a_redirect_is_not_followed_and_reports_where_it_points():
    from cognee_community_connector_front.front import _NoRedirect

    target = f"{API}/conversations/cnv_survivor"
    client, _ = _client(_http_error(301, {"Location": target}))
    with pytest.raises(FrontAPIError, match="not followed") as caught:
        client.get("/conversations/cnv_gone")
    assert caught.value.status == 301 and caught.value.location == target
    assert len(client._open.requests) == 1
    assert _NoRedirect().redirect_request(None, None, 301, "Moved", {}, target) is None
    assert any(isinstance(h, _NoRedirect) for h in FrontClient("t")._opener.handlers)


@pytest.mark.parametrize("body", [b"", b"<html>login</html>", b"null", b"[1]"])
def test_a_200_without_json_ends_the_run(body):
    client, _ = _client(body)
    with pytest.raises(FrontAPIError, match="without JSON") as caught:
        client.get("/x")
    assert caught.value.ends_run


def test_check_active_is_asked_before_every_retry_wait():
    checks = []
    client, sleeps = _client(
        _http_error(503), _http_error(503), {"ok": 1}, check_active=lambda: checks.append(1)
    )
    client.get("/x")
    assert len(checks) == len(sleeps) == 2


@pytest.mark.parametrize(
    "url",
    ["http://api2.frontapp.com", "https://api2.frontapp.com/v1", "https://u:p@x.com", "", None],
)
def test_api_urls_that_could_leak_the_token_are_refused(url):
    with pytest.raises(ValueError, match="api_url"):
        FrontClient("t", api_url=url)


@pytest.mark.parametrize("token", ["front-secret\n", " front-secret ", "fr ont", "fr\x00ont", ""])
def test_a_token_with_whitespace_is_stripped_or_refused_without_echoing_it(token):
    if token.strip() == "front-secret":
        assert FrontClient(token)._auth == "Bearer front-secret"
        return
    with pytest.raises(ValueError) as caught:
        FrontClient(token)
    assert "ont" not in str(caught.value).replace("Front", "")


def test_the_client_repr_hides_the_token():
    assert "front-secret" not in repr(FrontClient("front-secret"))


def test_sync_through_the_real_client_sends_the_token_but_never_stores_it():
    fake = FakeFront(page_size=2)
    a = fake.conversation("A")
    fake.reply(a, "One")
    fake.reply(a, "Two")
    token, headers = "front-secret-token-123", []
    client = FrontClient(token)

    def opener(request):
        url = urllib.parse.urlparse(request.full_url)
        assert f"{url.scheme}://{url.netloc}" == API
        headers.append(request.get_header("Authorization"))
        params = dict(urllib.parse.parse_qsl(url.query))
        return _Response(json.dumps(fake.get(url.path, params)).encode())

    client._open = opener
    rows = sync(client, {})
    assert ids(rows) == [(a, False)] and "Two" in rows[0]["content"]
    assert headers and all(h == f"Bearer {token}" for h in headers)
    assert token not in json.dumps(rows)


# --------------------------------------------------------------------- factory
def test_source_validates_config():
    fake = FakeFront()
    with pytest.raises(ValueError, match="inboxes"):
        front_source(inboxes=[], client=fake)
    with pytest.raises(ValueError, match="inboxes"):
        front_source(inboxes=INBOX, client=fake)
    with pytest.raises(ValueError, match="inb_"):
        front_source(inboxes=["Support"], client=fake)
    with pytest.raises(ValueError, match="token"):
        front_source(inboxes=[INBOX])
    with pytest.raises(ValueError, match="not both"):
        front_source(inboxes=[INBOX], client=fake, service=FakeFront())


def test_credentials_are_arguments_only(monkeypatch):
    monkeypatch.setenv("FRONT_TOKEN", "from-env")
    monkeypatch.setenv("FRONT_API_TOKEN", "from-env")
    with pytest.raises(ValueError, match="token"):
        front_source(inboxes=[INBOX])


@pytest.mark.parametrize(
    "name",
    ["Front-Support", "front support", "frontSupport", "1front", "front__x", "front_", "a" * 52],
)
def test_a_resource_name_dlt_would_rename_is_refused(name):
    with pytest.raises(ValueError, match="resource_name"):
        front_source(inboxes=[INBOX], resource_name=name, client=FakeFront())


@pytest.mark.parametrize("name", ["front", "front_acme", "front_acme_2"])
def test_accepted_names_are_the_table_names_dlt_writes(name):
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    source = front_source(inboxes=[INBOX], resource_name=name, client=FakeFront())
    assert NamingConvention().normalize_table_identifier(source.name) == name


def test_a_cognee_without_table_scoped_cleanup_is_refused(monkeypatch):
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)
    with pytest.raises(RuntimeError, match="cognee 1"):
        front_source(inboxes=[INBOX], client=FakeFront())


def test_the_resource_is_merge_with_hard_delete_and_document_mode():
    from cognee.tasks.ingestion.dlt_utils import (
        DOCUMENT_SOURCE_ATTR,
        NODE_SET_COLUMN,
        PIPELINE_SCOPE_ATTR,
    )

    source = front_source(inboxes=[INBOX], client=FakeFront())
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "front"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "front"
    assert source.write_disposition == "merge"
    assert source.columns["_deleted"]["hard_delete"] is True
    assert source.columns[NODE_SET_COLUMN]["data_type"] == "json"


def test_check_active_runs_before_every_request():
    fake = FakeFront()
    fake.conversation("A")
    checks = []

    def check_active():
        checks.append(len(fake.calls))

    list(front_source(inboxes=[INBOX], client=fake, check_active=check_active))
    request_checks = [n for n in checks if n < len(fake.calls)]
    assert len(set(request_checks)) == len(fake.calls), "one check before each request"


def test_check_active_can_stop_a_sync_before_anything_is_emitted():
    fake = FakeFront()
    fake.conversation("A")

    def revoked():
        raise PermissionError("access revoked")

    with pytest.raises(ResourceExtractionError) as caught:
        list(front_source(inboxes=[INBOX], client=fake, check_active=revoked))
    assert _cause(caught.value, PermissionError) is not None
    assert fake.calls == []


# --------------------------------------------------------------------- dlt runs
def _pipeline(tmp_path, name="front_test"):
    import dlt

    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'front.db'}"),
        dataset_name="front_test",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _ids(pipeline, table="front"):
    with pipeline.sql_client() as sql:
        try:
            return sorted(r[0] for r in sql.execute_sql(f"SELECT id FROM {table}"))
        except DatabaseUndefinedRelation:
            return []


def test_e2e_dlt_merge_keeps_unchanged_and_removes_deleted(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeFront()
    a = fake.conversation("keep")
    b = fake.conversation("trash me")
    c = fake.conversation("merge me")
    pipeline.run(front_source(inboxes=[INBOX], client=fake))
    assert _ids(pipeline) == sorted([a, b, c])

    fake.comment(a, "note")
    fake.set_status(b, "deleted")
    fake.merge(c, a)
    source = front_source(inboxes=[INBOX], client=fake)
    pipeline.run(source)
    assert _ids(pipeline) == [a]
    assert source.cognee_sync_stats == {"scanned": 1, "deleted": 2, "skipped": 0, "failed": 0}
    assert all(type(v) is int for v in source.cognee_sync_stats.values())


def test_a_failed_load_keeps_the_state_so_nothing_is_lost(tmp_path, monkeypatch):
    from dlt.pipeline.pipeline import Pipeline

    pipeline, fake = _pipeline(tmp_path), FakeFront()
    a = fake.conversation("first")
    pipeline.run(front_source(inboxes=[INBOX], client=fake))
    b = fake.conversation("second")

    def boom(*_args, **_kwargs):
        raise RuntimeError("load failed")

    monkeypatch.setattr(Pipeline, "load", boom)
    with pytest.raises(RuntimeError):
        pipeline.run(front_source(inboxes=[INBOX], client=fake))
    monkeypatch.undo()
    pipeline.run(front_source(inboxes=[INBOX], client=fake))
    assert _ids(pipeline) == sorted([a, b])


def test_a_source_object_run_again_starts_with_fresh_stats(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeFront()
    fake.add_inbox("inb_billing", "Billing")
    fake.conversation("A")
    fake.fail["/inboxes/inb_billing"] = 500
    source = front_source(inboxes=[INBOX, "inb_billing"], client=fake)
    pipeline.run(source)
    assert source.cognee_sync_stats["failed_inboxes"] == 1
    fake.fail = {}
    pipeline.run(source)
    assert source.cognee_sync_stats["failed"] == 0
    assert "failed_inboxes" not in source.cognee_sync_stats


def test_an_inbox_that_keeps_failing_fails_the_pipeline_run(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeFront()
    fake.add_inbox("inb_billing", "Billing")
    fake.fail[f"/inboxes/{INBOX}"] = 500
    for _ in range(2):
        pipeline.run(front_source(inboxes=[INBOX, "inb_billing"], client=fake))
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(front_source(inboxes=[INBOX, "inb_billing"], client=fake))
    assert "3 runs in a row" in str(_cause(caught.value, FrontAPIError))


def test_two_accounts_cannot_share_one_table(tmp_path):
    pipeline = _pipeline(tmp_path)
    one, two = FakeFront(), FakeFront()
    one.conversation("one")
    two.me = {"id": "cmp_other"}
    two.conversation("two")
    pipeline.run(front_source(inboxes=[INBOX], client=one))
    pipeline.run(front_source(inboxes=[INBOX], client=two, resource_name="front_other"))
    assert len(_ids(pipeline)) == 1 and len(_ids(pipeline, "front_other")) == 1
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(front_source(inboxes=[INBOX], client=two))
    assert "another Front account" in str(_cause(caught.value, ValueError))
