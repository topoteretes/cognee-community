"""Unit tests for the Crisp connector, against ``FakeCrisp`` (see fake_crisp.py).

Coverage: conversation rendering (visitor, operator and internal note labels; names, by
default no email addresses, phone numbers or IPs; state events left out; files; time
order), the first sync and later runs that only re-read changed conversations, new
messages, edits and removals that do not move ``updated_at`` (picked up through the
recent window and the rotation), resolving and renaming, a conversation that becomes
empty, a removed conversation (404), one removed mid-sync, empty conversations,
visitor_fields, include_notes switched, paging of the list and of messages, the request
budget and rate limits, another website's state, the HTTP client (Basic auth and tier,
HEAD 404, retries, the watermark pause, redirects, a body without data, the key), config
validation, check_active, the sync stats, and real dlt merges into SQLite.
"""

import base64
import io
import json
import urllib.error
import urllib.parse

import pytest
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from dlt.extract.exceptions import ResourceExtractionError
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_crisp import WEBSITE, FakeCrisp

from cognee_community_connector_crisp import crisp_source
from cognee_community_connector_crisp.crisp import (
    CrispAPIError,
    CrispAuthError,
    CrispClient,
    CrispRateLimitError,
    conversation_to_row,
    sync_website,
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
    return list(sync_website(fake, state, WEBSITE, stats, now_ms=lambda: fake.now, **kw))


def ids(rows):
    return sorted((r["id"], r["_deleted"]) for r in rows)


def reads(fake, since=0):
    return sorted({p.split("/")[4] for _m, p in fake.calls[since:] if p.endswith("/messages")})


# --------------------------------------------------------------------- rendering
def test_a_conversation_is_one_document_with_labels_and_no_contact_details():
    fake = FakeCrisp()
    sid = fake.conversation("My card payment failed twice.")
    fake.operator(sid, "Sorry! Can you try again now?")
    fake.note(sid, "Stripe outage this morning.")
    fake.file(sid, "receipt.pdf")
    fake.resolve(sid)
    fake.conversations[sid]["meta"]["segments"] = ["vip", "billing"]
    (row,) = sync(fake, {})
    content = row["content"]
    assert row["id"] == sid and row["title"] == "Conversation with Jane Doe"
    assert row["url"] == f"https://app.crisp.chat/website/{WEBSITE}/inbox/{sid}/"
    assert content.startswith("Conversation with Jane Doe\nState: resolved\nVisitor: Jane Doe\n")
    assert "Segments: billing, vip" in content
    assert "--- Visitor (Jane Doe)," in content and "--- Operator (Alice Smith)," in content
    assert "--- Internal note (Alice Smith)," in content and "(file: receipt.pdf)" in content
    assert "state:resolved" not in content and "namespace" not in content
    for secret in ("jane@example.com", "+15550100", "203.0.113.5", "Berlin"):
        assert secret not in content
    order = [content.index(x) for x in ("failed twice", "try again", "Stripe", "receipt")]
    assert order == sorted(order)


def test_visitor_fields_choose_the_header_and_switching_rerenders():
    fake = FakeCrisp()
    sid = fake.conversation("Where is my parcel?")
    state = {}
    sync(fake, state)
    (row,) = sync(fake, state, visitor_fields=("country", "city", "email"))
    content = row["content"]
    assert "Country: DE\nCity: Berlin\nEmail: jane@example.com" in content
    assert "Jane Doe" not in content, "nickname not asked for: no name in the header or labels"
    assert "--- Visitor," in content
    for secret in ("+15550100", "203.0.113.5"):
        assert secret not in content
    assert sync(fake, state, visitor_fields=("country", "city", "email")) == []
    fake.conversations[sid]["meta"]["email"] = "jane@new.example"
    (row,) = sync(fake, state, visitor_fields=("country", "city", "email"))
    assert "Email: jane@new.example" in row["content"]


def test_a_conversation_renders_the_same_whatever_order_crisp_sends():
    fake = FakeCrisp()
    sid = fake.conversation("one")
    fake.operator(sid, "two")
    conv, msgs = fake.conversations[sid], fake.messages[sid]
    assert conversation_to_row(conv, msgs, WEBSITE) == conversation_to_row(
        conv, msgs[::-1], WEBSITE
    )


def test_a_subject_becomes_the_title_and_an_address_nickname_is_dropped():
    conv = {
        "session_id": "session_x",
        "state": "pending",
        "meta": {"nickname": "jane@example.com", "subject": "Refund"},
    }
    row = conversation_to_row(
        conv,
        [{"type": "text", "from": "user", "content": "hi", "timestamp": 1, "fingerprint": 1}],
        WEBSITE,
    )
    assert row["title"] == "Refund" and "--- Visitor, " in row["content"]
    assert "jane@example.com" not in row["content"]


def test_a_conversation_with_only_events_has_no_document():
    conv = {"session_id": "session_x", "state": "resolved", "meta": {}}
    event = {
        "type": "event",
        "content": {"namespace": "state:resolved"},
        "timestamp": 1,
        "fingerprint": 1,
    }
    assert conversation_to_row(conv, [event], WEBSITE) is None


# --------------------------------------------------------------------- incremental
def test_first_run_reads_everything_then_only_changes():
    fake = FakeCrisp()
    a = fake.conversation("A")
    b = fake.conversation("B")
    fake.now += 10 * DAY  # both are old now: no recent window, rotation off
    state, stats = {}, {}
    assert ids(sync(fake, state, stats=stats, rotation=0)) == sorted([(a, False), (b, False)])
    assert stats == {"scanned": 2, "deleted": 0, "skipped": 0, "failed": 0}
    mark = len(fake.calls)
    assert sync(fake, state, rotation=0) == []
    assert reads(fake, mark) == []
    fake.operator(a, "Fixed.")
    mark = len(fake.calls)
    assert ids(sync(fake, state, rotation=0)) == [(a, False)]
    assert reads(fake, mark) == [a]


def test_an_edit_in_a_recent_conversation_is_picked_up_on_the_next_sync():
    fake = FakeCrisp()
    a = fake.conversation("My order is 1234.")
    state = {}
    sync(fake, state, rotation=0)
    fp = fake.messages[a][0]["fingerprint"]
    fake.edit(a, fp, "My order is 5678.")  # updated_at does not move
    (row,) = sync(fake, state, rotation=0)
    assert "5678" in row["content"] and "1234" not in row["content"]


def test_a_removed_message_in_a_recent_conversation_leaves_the_document():
    fake = FakeCrisp()
    a = fake.conversation("Hello")
    fp = fake.visitor(a, "my password is hunter2")
    state = {}
    sync(fake, state, rotation=0)
    fake.remove(a, fp)
    (row,) = sync(fake, state, rotation=0)
    assert "hunter2" not in row["content"]


def test_an_edit_in_an_old_conversation_is_picked_up_by_the_rotation():
    fake = FakeCrisp()
    convs = [fake.conversation(f"C{i}") for i in range(4)]
    state = {}
    sync(fake, state, rotation=2)
    fake.now += 30 * DAY
    target = convs[3]
    fake.edit(target, fake.messages[target][0]["fingerprint"], "edited late")
    found = []
    for _ in range(2):  # two runs of two cover all four in turn
        found += [r for r in sync(fake, state, rotation=2) if "edited late" in r["content"]]
    assert [r["id"] for r in found] == [target]


def test_the_rotation_reads_its_batch_and_moves_on():
    fake = FakeCrisp()
    convs = sorted(fake.conversation(f"C{i}") for i in range(5))
    state = {}
    sync(fake, state, rotation=0)
    fake.now += 30 * DAY
    mark = len(fake.calls)
    sync(fake, state, rotation=2)
    first = reads(fake, mark)
    mark = len(fake.calls)
    sync(fake, state, rotation=2)
    second = reads(fake, mark)
    assert first == convs[:2] and second == convs[2:4]


def test_resolving_and_renaming_are_seen_although_updated_at_does_not_move():
    fake = FakeCrisp()
    a = fake.conversation("A")
    state = {}
    fake.now += 10 * DAY
    sync(fake, state, rotation=0)
    fake.resolve(a)
    assert "State: resolved" in sync(fake, state, rotation=0)[0]["content"]
    fake.rename(a, "Janet Doe")
    assert "Visitor: Janet Doe" in sync(fake, state, rotation=0)[0]["content"]


def test_a_conversation_whose_messages_are_all_removed_is_forgotten():
    fake = FakeCrisp()
    a = fake.conversation("oops, wrong chat")
    state = {}
    sync(fake, state)
    fake.remove(a, fake.messages[a][0]["fingerprint"])
    assert ids(sync(fake, state)) == [(a, True)]


def test_a_removed_conversation_is_forgotten_only_on_a_404():
    fake = FakeCrisp()
    a = fake.conversation("A")
    b = fake.conversation("B")
    state = {}
    sync(fake, state)
    fake.delete(a)
    assert ids(sync(fake, state)) == [(a, True)]
    assert b in state["conversations"]


def test_a_conversation_removed_while_syncing_is_skipped_then_forgotten():
    """Removed after the list was read: its history answers 404, so it is skipped this
    run while the others sync, and the next run forgets it."""
    fake = FakeCrisp()
    a = fake.conversation("A")
    b = fake.conversation("B")
    state = {}
    sync(fake, state)
    fake.visitor(a, "More")
    fake.visitor(b, "More too")
    fake.fail[f"/website/{WEBSITE}/conversation/{a}/messages"] = 404
    assert ids(sync(fake, state)) == [(b, False)]
    assert a in state["conversations"]
    del fake.fail[f"/website/{WEBSITE}/conversation/{a}/messages"]
    fake.delete(a)
    assert ids(sync(fake, state)) == [(a, True)]


def test_a_conversation_missing_from_the_list_but_still_there_is_kept():
    fake = FakeCrisp()
    a = fake.conversation("A")
    state = {}
    sync(fake, state)
    real = fake.request

    def request(method, path, params=None):
        answer = real(method, path, params)
        if path.endswith("/conversations/1"):
            answer = [c for c in answer if c["session_id"] != a]
        return answer

    fake.request = request
    assert sync(fake, state) == []
    assert a in state["conversations"]


def test_empty_conversations_are_listed_but_make_no_document():
    fake = FakeCrisp()
    empty = fake.conversation(None)
    state = {}
    assert sync(fake, state) == []
    params = [c for c in fake.calls if c[1].endswith("/conversations/1")]
    assert params and empty not in state["conversations"]
    fake.visitor(empty, "now I write")
    assert ids(sync(fake, state)) == [(empty, False)]


def test_include_notes_off_and_switching_rerenders():
    fake = FakeCrisp()
    a = fake.conversation("A")
    fake.note(a, "internal: VIP")
    state = {}
    fake.now += 10 * DAY
    (row,) = sync(fake, state, include_notes=False, rotation=0)
    assert "VIP" not in row["content"]
    (row,) = sync(fake, state, include_notes=True, rotation=0)
    assert "VIP" in row["content"]


def test_the_list_and_long_histories_are_read_across_pages():
    fake = FakeCrisp(list_page=2, message_page=3)
    convs = [fake.conversation(f"C{i}") for i in range(5)]
    for i in range(7):
        fake.operator(convs[0], f"reply {i}")
    rows = sync(fake, {})
    assert sorted(r["id"] for r in rows) == sorted(convs)
    first = next(r for r in rows if r["id"] == convs[0])
    assert all(f"reply {i}" in first["content"] for i in range(7))


def test_the_request_budget_stops_cleanly_and_the_next_run_resumes():
    fake = FakeCrisp()
    convs = [fake.conversation(f"C{i}") for i in range(8)]
    state, stats = {}, {}
    # 2 list pages, then 2 requests a conversation (the history, then an empty older page)
    rows = sync(fake, state, stats=stats, max_requests=10)
    assert stats["failed_rate_limit"] == 1 and len(rows) == 4
    rows = sync(fake, state, max_requests=100)
    assert len(rows) == 4 and set(state["conversations"]) == set(convs)


def test_a_rate_limit_stops_cleanly():
    fake = FakeCrisp()
    a = fake.conversation("A")
    fake.fail[f"/website/{WEBSITE}/conversation/{a}/messages"] = 429
    stats = {}
    assert sync(fake, {}, stats=stats) == []
    assert stats["failed_rate_limit"] == 1


def test_a_rejected_token_raises():
    fake = FakeCrisp()
    fake.fail[f"/website/{WEBSITE}/conversations/1"] = 401
    with pytest.raises(CrispAuthError):
        sync(fake, {})


def test_a_state_of_another_website_is_refused():
    fake = FakeCrisp()
    with pytest.raises(ValueError, match="another Crisp website"):
        list(sync_website(fake, {"website": "00000000-0000-0000-0000-000000000000"}, WEBSITE, {}))


# --------------------------------------------------------------------- client
class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status
        self.headers = {}


def _http_error(status, headers=None):
    return urllib.error.HTTPError("https://api.crisp.chat/v1/x", status, "err", headers or {}, None)


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
        return _Response(json.dumps(outcome).encode())


def _client(*outcomes, **kw):
    sleeps = []
    client = CrispClient("ident-1", "key-secret", sleep=sleeps.append, **kw)
    client._open = _Opener(*outcomes)
    return client, sleeps


def test_the_client_sends_basic_auth_and_the_tier():
    client, _ = _client({"error": False, "reason": "listed", "data": [1]})
    assert client.request("GET", "/website/x/conversations/1", {"include_empty": 1}) == [1]
    request = client._open.requests[0]
    assert request.full_url == "https://api.crisp.chat/v1/website/x/conversations/1?include_empty=1"
    expected = "Basic " + base64.b64encode(b"ident-1:key-secret").decode()
    assert request.get_header("Authorization") == expected
    assert request.get_header("X-crisp-tier") == "website"


def test_head_returns_the_status_and_a_404_is_not_an_error():
    client, _ = _client(_Response(b"", status=200), _http_error(404))
    assert client.request("HEAD", "/x") == 200
    assert client.request("HEAD", "/x") == 404


def test_the_client_retries_5xx_and_waits_out_a_short_rate_limit():
    client, sleeps = _client(_http_error(503), _http_error(420, {"Retry-After": "2"}), {"data": []})
    assert client.request("GET", "/x") == []
    assert sleeps == [1.0, 2.0]


def test_the_client_pauses_when_a_watermark_reads_high():
    calm = _Response(json.dumps({"data": [1]}).encode())
    calm.headers = {"x-rate-limit-watermarks": "g=low;u=low;r=low"}
    busy = _Response(json.dumps({"data": [2]}).encode())
    busy.headers = {"x-rate-limit-watermarks": "g=low;u=high;r=low"}
    client, sleeps = _client(calm, busy)
    assert client.request("GET", "/x") == [1]
    assert sleeps == []
    assert client.request("GET", "/x") == [2]
    assert sleeps == [10.0]


def test_a_long_wait_is_not_slept_through():
    client, sleeps = _client(_http_error(429, {"Retry-After": "900"}))
    with pytest.raises(CrispRateLimitError):
        client.request("GET", "/x")
    assert sleeps == []


def test_a_401_raises_without_the_key():
    client, _ = _client(_http_error(401))
    with pytest.raises(CrispAuthError) as caught:
        client.request("GET", "/x")
    error = caught.value
    while error is not None:
        assert "key-secret" not in str(error)
        error = error.__cause__ or error.__context__


def test_a_redirect_is_not_followed_and_errors_hide_ids():
    from cognee_community_connector_crisp.crisp import _NoRedirect

    client, _ = _client(_http_error(302))
    with pytest.raises(CrispAPIError, match="not followed") as caught:
        client.request("GET", f"/website/{WEBSITE}/conversation/session_{WEBSITE}/messages")
    assert WEBSITE not in str(caught.value)
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://e/") is None


@pytest.mark.parametrize("body", [b"<html>", b'{"error": true, "reason": "invalid"}', b"[]"])
def test_an_answer_without_data_fails(body):
    client, _ = _client(_Response(body))
    with pytest.raises(CrispAPIError, match="no data"):
        client.request("GET", "/x")


def test_the_client_repr_hides_the_key():
    assert "key-secret" not in repr(CrispClient("i", "key-secret"))


@pytest.mark.parametrize("field", ["identifier", "key"])
def test_credentials_with_spaces_are_refused_without_echoing_them(field):
    kw = {"identifier": "i", "key": "k", field: "xq7 zv9"}
    with pytest.raises(ValueError, match=field) as caught:
        CrispClient(**kw)
    assert "xq7" not in str(caught.value)


def test_sync_through_the_real_client_never_stores_the_key():
    fake = FakeCrisp()
    fake.conversation("Hello")
    client = CrispClient("ident-1", "key-secret-123")

    def opener(request):
        url = urllib.parse.urlparse(request.full_url)
        assert url.netloc == "api.crisp.chat" and url.path.startswith("/v1/")
        params = dict(urllib.parse.parse_qsl(url.query))
        answer = fake.request(request.get_method(), url.path.removeprefix("/v1"), params)
        if request.get_method() == "HEAD":
            if answer == 404:
                raise _http_error(404)
            return _Response(b"", status=answer)
        return _Response(json.dumps({"error": False, "data": answer}).encode())

    client._open = opener
    rows = list(sync_website(client, {}, WEBSITE, {}, now_ms=lambda: fake.now))
    assert len(rows) == 1 and "Hello" in rows[0]["content"]
    assert "key-secret-123" not in json.dumps(rows)


# --------------------------------------------------------------------- factory
def test_source_validates_config():
    fake = FakeCrisp()
    with pytest.raises(ValueError, match="website_id"):
        crisp_source(website_id="my-site", client=fake)
    with pytest.raises(ValueError, match="identifier"):
        crisp_source(website_id=WEBSITE)
    with pytest.raises(ValueError, match="rotation"):
        crisp_source(website_id=WEBSITE, rotation=-1, client=fake)
    with pytest.raises(ValueError, match="max_requests"):
        crisp_source(website_id=WEBSITE, max_requests=5, client=fake)
    with pytest.raises(ValueError, match="tier"):
        crisp_source(website_id=WEBSITE, identifier="i", key="k", tier="admin")
    with pytest.raises(ValueError, match="not both"):
        crisp_source(website_id=WEBSITE, client=fake, service=FakeCrisp())
    with pytest.raises(ValueError, match="visitor_fields"):
        crisp_source(website_id=WEBSITE, client=fake, visitor_fields=["address"])
    with pytest.raises(ValueError, match="visitor_fields"):
        crisp_source(website_id=WEBSITE, client=fake, visitor_fields="email")


def test_credentials_are_arguments_only(monkeypatch):
    monkeypatch.setenv("CRISP_IDENTIFIER", "from-env")
    monkeypatch.setenv("CRISP_KEY", "from-env")
    with pytest.raises(ValueError, match="identifier"):
        crisp_source(website_id=WEBSITE)


@pytest.mark.parametrize(
    "name", ["Crisp-Chat", "crisp chat", "crispChat", "1crisp", "crisp__x", "crisp_", "a" * 52]
)
def test_a_resource_name_dlt_would_rename_is_refused(name):
    with pytest.raises(ValueError, match="resource_name"):
        crisp_source(website_id=WEBSITE, resource_name=name, client=FakeCrisp())


@pytest.mark.parametrize("name", ["crisp", "crisp_shop", "crisp_shop_2"])
def test_accepted_names_are_the_table_names_dlt_writes(name):
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    source = crisp_source(website_id=WEBSITE, resource_name=name, client=FakeCrisp())
    assert NamingConvention().normalize_table_identifier(source.name) == name


def test_a_cognee_without_table_scoped_cleanup_is_refused(monkeypatch):
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)
    with pytest.raises(RuntimeError, match="cognee 1"):
        crisp_source(website_id=WEBSITE, client=FakeCrisp())


def test_the_resource_is_merge_with_hard_delete_and_document_mode():
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    source = crisp_source(website_id=WEBSITE.upper(), client=FakeCrisp())
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "crisp"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "crisp"
    assert source.write_disposition == "merge"
    assert source.columns["_deleted"]["hard_delete"] is True


def test_check_active_runs_before_every_request_and_can_stop_the_sync():
    fake = FakeCrisp()
    fake.conversation("A")
    checks = []
    list(
        crisp_source(
            website_id=WEBSITE, client=fake, check_active=lambda: checks.append(len(fake.calls))
        )
    )
    request_checks = [n for n in checks if n < len(fake.calls)]
    assert len(set(request_checks)) == len(fake.calls)

    fresh = FakeCrisp()

    def revoked():
        raise PermissionError("access revoked")

    with pytest.raises(ResourceExtractionError) as caught:
        list(crisp_source(website_id=WEBSITE, client=fresh, check_active=revoked))
    assert _cause(caught.value, PermissionError) is not None and fresh.calls == []


# --------------------------------------------------------------------- dlt runs
def _pipeline(tmp_path):
    import dlt

    return dlt.pipeline(
        pipeline_name="crisp_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'crisp.db'}"),
        dataset_name="crisp_test",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _ids(pipeline, table="crisp"):
    with pipeline.sql_client() as sql:
        try:
            return sorted(r[0] for r in sql.execute_sql(f"SELECT id FROM {table}"))
        except DatabaseUndefinedRelation:
            return []


def test_e2e_dlt_merge_keeps_unchanged_and_removes_deleted(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeCrisp()
    a = fake.conversation("keep")
    b = fake.conversation("delete me")
    pipeline.run(crisp_source(website_id=WEBSITE, client=fake))
    assert _ids(pipeline) == sorted([a, b])
    fake.now += 10 * DAY
    fake.operator(a, "reply")
    fake.delete(b)
    source = crisp_source(website_id=WEBSITE, client=fake, rotation=0)
    pipeline.run(source)
    assert _ids(pipeline) == [a]
    assert source.cognee_sync_stats == {"scanned": 1, "deleted": 1, "skipped": 0, "failed": 0}
    assert all(type(v) is int for v in source.cognee_sync_stats.values())


def test_a_failed_load_keeps_the_state_so_nothing_is_lost(tmp_path, monkeypatch):
    from dlt.pipeline.pipeline import Pipeline

    pipeline, fake = _pipeline(tmp_path), FakeCrisp()
    a = fake.conversation("first")
    pipeline.run(crisp_source(website_id=WEBSITE, client=fake))
    b = fake.conversation("second")

    def boom(*_args, **_kwargs):
        raise RuntimeError("load failed")

    monkeypatch.setattr(Pipeline, "load", boom)
    with pytest.raises(RuntimeError):
        pipeline.run(crisp_source(website_id=WEBSITE, client=fake))
    monkeypatch.undo()
    pipeline.run(crisp_source(website_id=WEBSITE, client=fake))
    assert _ids(pipeline) == sorted([a, b])


def test_a_source_object_run_again_starts_with_fresh_stats(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeCrisp()
    for i in range(8):
        fake.conversation(f"C{i}")
    fake.now += 10 * DAY
    source = crisp_source(website_id=WEBSITE, client=fake, max_requests=10, rotation=0)
    pipeline.run(source)
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
    pipeline.run(source)
    assert source.cognee_sync_stats["failed"] == 0
    assert "failed_rate_limit" not in source.cognee_sync_stats


def test_two_websites_cannot_share_one_table(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeCrisp()
    fake.conversation("A")
    pipeline.run(crisp_source(website_id=WEBSITE, client=fake))
    other = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(crisp_source(website_id=other, client=fake))
    assert "another Crisp website" in str(_cause(caught.value, ValueError))
