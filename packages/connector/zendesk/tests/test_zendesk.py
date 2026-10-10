"""Unit tests for the Zendesk connector.

The Zendesk API is mocked by ``FakeZendesk`` (no credentials, no network). Its
responses follow what a real trial account returned:

  - the incremental ticket export returns a ticket again when only a comment
    was added, and deleted tickets come back with ``status="deleted"``
  - comments carry a ``public`` flag (internal notes are ``public=False``)
  - archived Help Center articles disappear from the listing; drafts are flagged

Coverage: rendering (thread, internal notes, HTML articles), first export and
cursor resume, comment-only change, ticket deletion, a ticket listed several
times in one window (the newest version wins, so delete-then-restore works),
article edit / archive / unpublish / no-op, failed listing never deletes, export
page cap, rate-limit retry, auth/config errors, dlt wiring, the row-to-document
mapping in cognee, and a real dlt merge into SQLite.
"""

import pytest

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

SUB = "acme"
ALICE = {"id": 11, "name": "Alice Customer", "email": "alice@example.com"}
AGENT = {"id": 22, "name": "Sam Agent", "email": "sam@acme.test"}


def ticket(tid, subject="Login fails", status="open", **extra):
    return {
        "id": tid,
        "subject": subject,
        "status": status,
        "priority": "high",
        "type": "problem",
        "tags": ["sso", "login"],
        "requester_id": 11,
        "assignee_id": 22,
        "created_at": "2026-10-05T10:00:00Z",
        "updated_at": "2026-10-05T10:00:00Z",
        **extra,
    }


def comment(cid, body, author=11, public=True, at="2026-10-05T10:00:00Z"):
    return {
        "id": cid,
        "plain_body": body,
        "body": body,
        "author_id": author,
        "public": public,
        "created_at": at,
    }


def article(
    aid,
    title="Reset your password",
    body="<p>Go to <b>Settings</b>.</p>",
    updated="2026-10-05T12:00:00Z",
    draft=False,
):
    return {
        "id": aid,
        "title": title,
        "body": body,
        "updated_at": updated,
        "draft": draft,
        "html_url": f"https://{SUB}.zendesk.com/hc/en-us/articles/{aid}",
        "locale": "en-us",
        "label_names": ["account"],
    }


class FakeZendesk:
    """Serves the export as a change log: each ``touch`` appends a ticket version."""

    def __init__(self):
        self.log = []  # ticket versions in change order
        self.comments = {}  # ticket id -> comments
        self.articles = []
        self.fail_articles = False
        self.calls = []
        self.page_size = 1000

    def touch(self, t, comments=None):
        self.log.append(dict(t))
        if comments is not None:
            self.comments[t["id"]] = comments

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "/api/v2/incremental/tickets/cursor.json":
            start = int(params["cursor"]) if "cursor" in params else 0
            page = self.log[start : start + self.page_size]
            end = start + len(page)
            return {
                "tickets": page,
                "after_cursor": str(end),
                "end_of_stream": end >= len(self.log),
            }
        if path.startswith("/api/v2/tickets/") and path.endswith("/comments.json"):
            tid = int(path.split("/")[4])
            return {
                "comments": self.comments.get(tid, []),
                "users": [ALICE, AGENT],
                "next_page": None,
            }
        if path == "/api/v2/help_center/articles.json":
            if self.fail_articles:
                raise ZendeskAPIError("HTTP 500")
            return {"articles": list(self.articles), "next_page": None}
        raise AssertionError(path)


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


def test_failed_article_listing_never_deletes():
    fake = FakeZendesk()
    fake.articles = [article(5)]
    state = {}
    list(sync_articles(fake, state))
    fake.fail_articles = True
    with pytest.raises(ZendeskAPIError):
        list(sync_articles(fake, state))
    assert state["versions"] == {"5": "2026-10-05T12:00:00Z"}


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
    assert set(source.resources) == {"zendesk_tickets", "zendesk_articles"}
    for resource in source.resources.values():
        assert resource.write_disposition == "merge"
        assert resource.compute_table_schema()["columns"]["_deleted"]["hard_delete"] is True
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "zendesk"
    assert set(zendesk_source(SUB, resources=["articles"], client=FakeZendesk()).resources) == {
        "zendesk_articles"
    }


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
            t = client.execute_sql("SELECT id, title FROM zendesk_tickets ORDER BY id")
            a = client.execute_sql("SELECT id FROM zendesk_articles ORDER BY id")
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


def test_pipeline_scope_is_set_when_cognee_supports_it():
    from cognee_community_connector_zendesk.zendesk import PIPELINE_SCOPE_ATTR

    source = zendesk_source(SUB, client=FakeZendesk())
    if PIPELINE_SCOPE_ATTR is None:
        pytest.skip("cognee < 1.6 has no PIPELINE_SCOPE_ATTR")
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "zendesk:acme"
