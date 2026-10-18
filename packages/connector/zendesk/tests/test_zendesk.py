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
mapping in cognee, and a real dlt merge into SQLite.
"""

import pytest
from fake_zendesk import AGENT, ALICE, SUB, FakeZendesk, article, comment, ticket

from cognee_community_connector_zendesk.zendesk import (
    ZendeskAPIError,
    ZendeskClient,
    article_to_row,
    html_to_text,
    sync_articles,
    sync_tickets,
    ticket_to_row,
    zendesk_source,
)


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
    assert row["id"] == 7
    assert row["title"] == "Ticket #7: Login fails"
    assert "Requester: Alice Customer <alice@example.com>" in row["content"]
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
    assert [r["id"] for r in tickets(fake, state)] == [1, 2]
    assert fake.calls[0][1] == {"start_time": 0}
    assert state["after_cursor"] == "2"

    assert tickets(fake, state) == []  # nothing changed
    fake.touch(ticket(2, "Billing (edited)"))
    rows = tickets(fake, state)
    assert [r["id"] for r in rows] == [2] and "Billing (edited)" in rows[0]["content"]


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
    assert tickets(fake, state) == [{"id": 4, "_deleted": True}]


def test_assignee_who_never_commented_is_still_named():
    fake = FakeZendesk()
    fake.touch(ticket(7), [comment(1, "I cannot log in.", author=11)])
    row = tickets(fake, {})[0]
    assert "Requester: Alice Customer <alice@example.com>" in row["content"]
    assert "Assignee: Sam Agent <sam@acme.test>" in row["content"]
    lookups = [p for p, _ in fake.calls if p == "/api/v2/users/show_many.json"]
    assert lookups == ["/api/v2/users/show_many.json"]  # one batched lookup, not one per user


def test_ticket_listed_several_times_in_one_window_keeps_its_newest_version():
    fake = FakeZendesk()
    fake.touch(ticket(5), [comment(1, "x")])
    fake.touch(ticket(5, "Edited"))
    fake.touch(ticket(5, status="deleted"))
    assert tickets(fake, {}) == [{"id": 5, "_deleted": True}]
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
    assert [r["id"] for r in rows] == [1, 2]
    assert not any("SSO token expired" in r["content"] for r in rows)
    assert tickets(fake, state, include_internal=False) == []


def test_export_page_cap_resumes_next_run():
    fake = FakeZendesk()
    fake.page_size = 2
    for i in range(1, 6):
        fake.touch(ticket(i), [comment(i, "x")])
    state = {}
    assert [r["id"] for r in tickets(fake, state, max_pages=2)] == [1, 2, 3, 4]
    assert [r["id"] for r in tickets(fake, state, max_pages=2)] == [5]


# --------------------------------------------------------------------- articles
def test_articles_first_run_edit_noop_archive_and_unpublish():
    fake = FakeZendesk()
    fake.articles = [article(5), article(6, "VPN setup"), article(9, "WIP", draft=True)]
    state = {}
    assert sorted(r["id"] for r in sync_articles(fake, state)) == [
        5,
        6,
    ]  # draft skipped
    assert list(sync_articles(fake, state)) == []  # no-op

    fake.articles[0] = article(5, body="<p>New steps</p>", updated="2026-10-06T09:00:00Z")
    rows = list(sync_articles(fake, state))
    assert [r["id"] for r in rows] == [5] and rows[0]["content"].startswith("New steps")

    fake.articles = [article(5, updated="2026-10-06T09:00:00Z")]  # 6 archived
    assert list(sync_articles(fake, state)) == [{"id": 6, "_deleted": True}]

    fake.articles = [article(5, updated="2026-10-07T09:00:00Z", draft=True)]  # unpublished
    assert list(sync_articles(fake, state)) == [{"id": 5, "_deleted": True}]
    assert state["versions"] == {}


def test_archived_article_that_is_published_again_comes_back():
    fake = FakeZendesk()
    fake.articles = [article(5), article(6, "VPN setup")]
    state = {}
    list(sync_articles(fake, state))
    fake.articles = [article(5)]  # 6 archived
    assert list(sync_articles(fake, state)) == [{"id": 6, "_deleted": True}]

    # Restoring an archived article brings it back as a draft: still skipped.
    fake.articles = [article(5), article(6, "VPN setup", draft=True)]
    assert list(sync_articles(fake, state)) == []
    # Published again: it is stored again.
    fake.articles = [article(5), article(6, "VPN setup", updated="2026-10-08T09:00:00Z")]
    rows = list(sync_articles(fake, state))
    assert [r["id"] for r in rows] == [6] and rows[0]["_deleted"] is False


def test_failed_article_listing_never_deletes():
    fake = FakeZendesk()
    fake.articles = [article(5)]
    state = {}
    list(sync_articles(fake, state))
    fake.fail_articles = True
    with pytest.raises(ZendeskAPIError):
        list(sync_articles(fake, state))
    assert state["versions"] == {"5": "2026-10-05T12:00:00Z"}


class _Listing:
    """Serves one article listing page as given, to check the count guard."""

    def __init__(self, page):
        self.page = page

    def get(self, path, **params):
        return self.page


def test_article_listing_that_does_not_add_up_never_deletes():
    fake = FakeZendesk()
    fake.articles = [article(5), article(6, "VPN setup")]
    state = {}
    list(sync_articles(fake, state))
    short = {"articles": [article(5)], "count": 2, "next_page": None}
    with pytest.raises(ZendeskAPIError, match="1 of 2"):
        list(sync_articles(_Listing(short), state))
    no_count = {"articles": [article(5)], "next_page": None}
    with pytest.raises(ZendeskAPIError, match="no count"):
        list(sync_articles(_Listing(no_count), state))
    assert set(state["versions"]) == {"5", "6"}  # nothing was forgotten


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


# --------------------------------------------------------------------- config / auth
def test_client_auth_headers():
    assert ZendeskClient(SUB, oauth_token="t")._auth == "Bearer t"
    assert ZendeskClient(SUB, email="a@b.c", api_token="k")._auth.startswith("Basic ")
    with pytest.raises(ValueError):
        ZendeskClient(SUB)


def test_client_waits_out_rate_limit_and_stops_on_bad_credentials(monkeypatch):
    import io
    import urllib.error

    from cognee_community_connector_zendesk import zendesk as module

    calls, sleeps = [], []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:  # the export allows 10 requests a minute
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many", {"Retry-After": "7"}, None
            )
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    assert ZendeskClient(SUB, oauth_token="t").get("/api/v2/x.json") == {"ok": True}
    assert sleeps == [7.0] and len(calls) == 2

    def expired(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(module.urllib.request, "urlopen", expired)
    with pytest.raises(ZendeskAPIError, match="expire"):
        ZendeskClient(SUB, oauth_token="t").get("/api/v2/x.json")
    assert sleeps == [7.0]  # no retries on a 401


@pytest.mark.parametrize("body", [b"", b"<html>maintenance</html>", b"[]", b"null"])
def test_a_200_without_a_json_object_raises_a_clean_error(monkeypatch, body):
    import io

    from cognee_community_connector_zendesk import zendesk as module

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda request, timeout: io.BytesIO(body))
    with pytest.raises(ZendeskAPIError, match="without a JSON object") as caught:
        ZendeskClient(SUB, oauth_token="t").get("/api/v2/help_center/articles.json")
    assert caught.value.status == 200 and not caught.value.ends_run


def test_404_names_the_endpoint_but_not_the_record(monkeypatch):
    import urllib.error

    from cognee_community_connector_zendesk import zendesk as module

    def missing(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(module.urllib.request, "urlopen", missing)
    with pytest.raises(ZendeskAPIError) as caught:
        ZendeskClient(SUB, oauth_token="t").get("/api/v2/tickets/48213/comments.json")
    assert "/api/v2/tickets/{id}/comments.json" in str(caught.value)
    assert "48213" not in str(caught.value)


def test_sync_through_the_real_client_sends_the_token_but_never_stores_it(monkeypatch):
    """The real ZendeskClient against FakeZendesk: URLs, query parameters and the
    auth header take the same path as against Zendesk, and the token never ends up
    in a stored row."""
    import io
    import json
    import urllib.parse

    from cognee_community_connector_zendesk import zendesk as module

    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "I cannot log in.")])
    fake.articles = [article(5)]
    token, headers = "zd-secret-token-123", []

    def urlopen(request, timeout):
        url = urllib.parse.urlparse(request.full_url)
        assert url.netloc == "acme.zendesk.com"
        headers.append(request.get_header("Authorization"))
        params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        return io.BytesIO(json.dumps(fake.get(url.path, **params)).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    client = ZendeskClient(SUB, oauth_token=token)
    rows = list(sync_tickets(client, {}, SUB)) + list(sync_articles(client, {}))

    assert [r["id"] for r in rows] == [1, 5]
    assert "I cannot log in." in rows[0]["content"]
    assert headers and all(h == f"Bearer {token}" for h in headers)
    assert token not in json.dumps(rows)


def test_client_refuses_pagination_link_to_another_host():
    with pytest.raises(ZendeskAPIError):
        ZendeskClient(SUB, oauth_token="t")._url("https://evil.example/next", {})


def test_source_validates_config(monkeypatch):
    monkeypatch.delenv("ZENDESK_SUBDOMAIN", raising=False)
    with pytest.raises(ValueError, match="subdomain"):
        zendesk_source(client=FakeZendesk())
    with pytest.raises(ValueError, match="Unknown"):
        zendesk_source(SUB, resources=["users"], client=FakeZendesk())


def test_resources_are_merge_with_hard_delete_and_document_mode():
    pytest.importorskip("dlt")
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
    with pytest.raises(Exception) as caught:
        list(source.resources["zendesk_acme_tickets"])
    causes, error = [], caught.value
    while error is not None:
        causes.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(e, AccessRevokedError) for e in causes)
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


def test_e2e_dlt_merge_keeps_unchanged_and_removes_deleted(tmp_path):
    """Real dlt merge into SQLite: an incremental run keeps rows it did not
    re-emit, an edit replaces a row, and tombstones remove rows."""
    dlt = pytest.importorskip("dlt")
    db = tmp_path / "zendesk.db"
    fake = FakeZendesk()

    def sync():
        pipeline = dlt.pipeline(
            pipeline_name="zendesk_e2e",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db}"),
            dataset_name="zendesk_e2e",
            pipelines_dir=str(tmp_path / "pipelines"),
        )
        pipeline.run(zendesk_source(SUB, client=fake))
        with pipeline.sql_client() as client:
            t = client.execute_sql("SELECT id, title FROM zendesk_acme_tickets ORDER BY id")
            a = client.execute_sql("SELECT id FROM zendesk_acme_articles ORDER BY id")
        return [tuple(r) for r in t], [r[0] for r in a]

    fake.touch(ticket(1, "Login fails"), [comment(1, "a")])
    fake.touch(ticket(2, "Billing"), [comment(2, "b")])
    fake.articles = [article(5), article(6, "VPN setup")]
    t, a = sync()
    assert [x[0] for x in t] == [1, 2] and a == [5, 6]

    fake.touch(ticket(2, "Billing question"))  # edit one ticket only
    t, a = sync()
    assert t == [(1, "Ticket #1: Login fails"), (2, "Ticket #2: Billing question")]

    fake.touch(ticket(1, status="deleted"))
    fake.articles = [article(5)]
    t, a = sync()
    assert [x[0] for x in t] == [2] and a == [5]


# --------------------------------------------------------------------- partial failure
def _pipeline(tmp_path):
    dlt = pytest.importorskip("dlt")
    return dlt.pipeline(
        pipeline_name="zendesk_partial",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'zendesk.db'}"),
        dataset_name="zendesk_partial",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _ids(pipeline, table):
    with pipeline.sql_client() as sql:
        try:
            return [r[0] for r in sql.execute_sql(f"SELECT id FROM {table} ORDER BY id")]
        except Exception:  # the table does not exist yet
            return []


def _state(pipeline, resource):
    return pipeline.state["sources"]["zendesk"]["resources"][resource]


def test_failing_articles_do_not_block_tickets(tmp_path):
    """Help Center switched off, say: tickets still sync and keep their progress."""
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.articles = [article(5)]
    fake.fail_articles = 403
    source = zendesk_source(SUB, client=fake)
    pipeline.run(source)  # does not raise
    assert _ids(pipeline, "zendesk_acme_tickets") == [1]
    assert _ids(pipeline, "zendesk_acme_articles") == []
    assert source.cognee_sync_stats == {
        "scanned": 1,
        "deleted": 0,
        "failed": 1,
        "failed_articles": 1,
    }

    fake.fail_articles = False  # fixed: the articles are picked up on the next run
    source = zendesk_source(SUB, client=fake)
    pipeline.run(source)
    assert _ids(pipeline, "zendesk_acme_articles") == [5]
    assert source.cognee_sync_stats["failed"] == 0


def test_failing_tickets_keep_their_cursor_and_do_not_block_articles(tmp_path):
    """The cursor moves while the export is read; a failure after that must put it
    back, or the tickets whose comments were never fetched would be skipped forever."""
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    pipeline.run(zendesk_source(SUB, resources=["tickets"], client=fake))
    cursor = _state(pipeline, "zendesk_acme_tickets")["after_cursor"]

    fake.touch(ticket(2, "Billing"), [comment(2, "b")])
    fake.articles = [article(5)]
    fake.fail_comments = {2: 500}
    source = zendesk_source(SUB, client=fake)
    pipeline.run(source)  # does not raise
    assert _state(pipeline, "zendesk_acme_tickets")["after_cursor"] == cursor
    assert _ids(pipeline, "zendesk_acme_tickets") == [1]
    assert _ids(pipeline, "zendesk_acme_articles") == [5]
    assert source.cognee_sync_stats["failed_tickets"] == 1

    fake.fail_comments = {}  # the next run reads the same window again
    pipeline.run(zendesk_source(SUB, client=fake))
    assert _ids(pipeline, "zendesk_acme_tickets") == [1, 2]


def test_a_run_where_everything_fails_raises(tmp_path):
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.fail_comments = {1: 500}
    fake.fail_articles = 500
    with pytest.raises(Exception, match="HTTP 500"):
        pipeline.run(zendesk_source(SUB, client=fake))
    with pytest.raises(Exception, match="HTTP 500"):  # also with only one resource selected
        pipeline.run(zendesk_source(SUB, resources=["articles"], client=fake))


@pytest.mark.parametrize("status", [401, 429])
def test_bad_credentials_or_an_exhausted_rate_limit_end_the_run(tmp_path, status):
    """These would fail the other resource the same way, so they are not isolated."""
    pipeline, fake = _pipeline(tmp_path), FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.articles = [article(5)]
    fake.fail_articles = status
    with pytest.raises(Exception, match=f"HTTP {status}"):
        pipeline.run(zendesk_source(SUB, client=fake))
    assert _ids(pipeline, "zendesk_acme_tickets") == []


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
    assert loaded == [[1, 2], [1, 2, 3, 4], [1, 2, 3, 4, 5]]


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
    assert source.cognee_sync_stats == {"scanned": 0, "deleted": 2, "failed": 0}


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
    dlt = pytest.importorskip("dlt")

    class AccessRevokedError(Exception):
        pass

    def revoked():
        raise AccessRevokedError

    fake = FakeZendesk()
    fake.touch(ticket(1), [comment(1, "a")])
    fake.articles = [article(5)]
    source = zendesk_source(SUB, resources=[resource], check_active=revoked, client=fake)
    pipeline = dlt.pipeline(pipeline_name="zendesk_guard", pipelines_dir=str(tmp_path))
    with pytest.raises(Exception) as caught:
        pipeline.extract(source)
    causes, error = [], caught.value
    while error is not None:
        causes.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(e, AccessRevokedError) for e in causes)
    assert fake.calls == []  # stopped before the first request
