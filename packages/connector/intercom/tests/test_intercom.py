"""Unit tests for the Intercom dlt connector.

Two layers, all runnable without an Intercom workspace:

* DB-free tests for rendering, pagination, retries, the incremental search
  window and the deletion sweeps, driven through ``sync_*`` with a fake
  ``requests`` session and a plain dict as state.
* dlt-pipeline tests (fake session, temp sqlite destination, ``merge`` like
  ``cognee.remember(..., write_disposition="merge")``) covering the acceptance
  criteria: first sync, edits re-sync, and deleted content is forgotten.
"""

import pytest
import requests

import cognee_community_connector_intercom.intercom as ic
from cognee_community_connector_intercom.intercom import (
    INTERCOM_SOURCE_NAME,
    IntercomAPI,
    IntercomSyncError,
    _check_token,
    article_to_row,
    conversation_to_row,
    forget_all,
    html_to_text,
    intercom_source,
    sync_articles,
    sync_contact_notes,
    sync_conversations,
)

BASE = "https://api.intercom.io"

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _conversation(cid, updated_at, subject="Refund", parts=(), state="open", total=None):
    return {
        "id": cid,
        "title": None,
        "created_at": 1_759_276_800,  # 2025-10-01
        "updated_at": updated_at,
        "state": state,
        "tags": {"tags": [{"name": "billing"}, {"name": "vip"}]},
        "source": {
            "subject": subject,
            "body": "<p>My card was charged twice.</p>",
            "author": {"type": "user", "name": "Asha Rao", "email": "asha@example.com"},
        },
        "conversation_parts": {
            "conversation_parts": list(parts),
            "total_count": len(parts) if total is None else total,
        },
    }


def _part(body, part_type="comment", author_type="admin", name="Ben"):
    return {"part_type": part_type, "body": body, "author": {"type": author_type, "name": name}}


def _article(aid, title, body, state="published"):
    return {
        "id": aid,
        "title": title,
        "description": f"About {title.lower()}",
        "body": body,
        "state": state,
        "url": f"https://help.example.com/articles/{aid}",
    }


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeIntercomSession:
    """Stand-in for a ``requests`` session talking to the Intercom REST API."""

    def close(self):
        self.closed = True

    def __init__(self, conversations=(), articles=(), contacts=None, page_size=2, workspace="ws1"):
        self.workspace = workspace  # the app id_code GET /me reports
        self.conversations = {c["id"]: c for c in conversations}
        self.articles = list(articles)
        self.contacts = contacts or {}  # {contact_id: {"name": ..., "notes": [...]}}
        self.page_size = page_size  # small pages so pagination is exercised
        self.calls = []  # (method, path, params, body)
        self.scripted = []  # FakeResponses returned before normal handling

    def request(self, method, url, params=None, json=None, timeout=None):
        path = url.removeprefix(BASE)
        self.calls.append((method, path, params, json))
        if self.scripted:
            return self.scripted.pop(0)
        if method == "GET" and path == "/me":
            return FakeResponse(200, {"type": "admin", "app": {"id_code": self.workspace}})
        if method == "GET" and path == "/conversations":
            items = [{"id": cid} for cid in sorted(self.conversations)]
            return self._cursor_page(items, "conversations", (params or {}).get("starting_after"))
        if method == "POST" and path == "/conversations/search":
            since = json["query"]["value"]
            items = [
                {"id": c["id"], "updated_at": c["updated_at"]}
                for c in sorted(self.conversations.values(), key=lambda c: c["id"])
                if c["updated_at"] > since
            ]
            starting_after = json["pagination"].get("starting_after")
            return self._cursor_page(items, "conversations", starting_after)
        if method == "GET" and path.startswith("/conversations/"):
            cid = path.rsplit("/", 1)[1]
            if cid not in self.conversations:
                return FakeResponse(404, {"type": "error.list"})
            return FakeResponse(200, self.conversations[cid])
        if method == "GET" and path == "/articles":
            per_page, page = params["per_page"], params["page"]
            chunk = self.articles[(page - 1) * per_page : page * per_page]
            total_pages = max(1, -(-len(self.articles) // per_page))
            return FakeResponse(200, {"data": chunk, "pages": {"total_pages": total_pages}})
        if method == "GET" and path == "/contacts":
            items = [{"id": k, "name": v["name"]} for k, v in sorted(self.contacts.items())]
            return self._cursor_page(items, "data", (params or {}).get("starting_after"))
        if method == "GET" and path.startswith("/contacts/") and path.endswith("/notes"):
            notes = self.contacts[path.split("/")[2]]["notes"]
            return self._numbered_page(notes, path, params or {})
        raise AssertionError(f"unexpected request {method} {path}")

    def _numbered_page(self, items, path, params):
        # Shape copied from the live API: a contact's notes use numbered pages
        # whose ``pages.next`` is a URL string, not a ``starting_after`` cursor.
        page = params.get("page", 1)
        chunk = items[(page - 1) * self.page_size : page * self.page_size]
        total_pages = max(1, -(-len(items) // self.page_size))
        pages = {"page": page, "per_page": self.page_size, "total_pages": total_pages}
        if page < total_pages:
            pages["next"] = f"{BASE}{path}?per_page={self.page_size}&page={page + 1}"
        return FakeResponse(200, {"type": "list", "data": chunk, "pages": pages})

    def _cursor_page(self, items, key, starting_after):
        start = 0
        if starting_after:
            start = [str(i["id"]) for i in items].index(starting_after) + 1
        chunk = items[start : start + self.page_size]
        pages = {}
        if start + self.page_size < len(items):
            pages["next"] = {"starting_after": str(chunk[-1]["id"])}
        return FakeResponse(200, {key: chunk, "pages": pages})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr("cognee_community_connector_intercom.intercom.time.sleep", sleeps.append)
    return sleeps


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------


def test_html_to_text_keeps_paragraphs_and_unescapes():
    html = "<p>Hello&nbsp;<b>there</b></p><p>Line&amp;two<br>three</p><ul><li>a</li></ul>"
    assert html_to_text(html) == "Hello there\nLine&two\nthree\na"
    assert html_to_text(None) == ""


def test_conversation_document_orders_parts_and_labels_notes():
    conversation = _conversation(
        "c1",
        100,
        parts=[
            _part("<p>Sorry about that, refunding now.</p>"),
            _part("<p>Checked Stripe: duplicate charge.</p>", part_type="note"),
            _part(None, part_type="assignment"),  # no text → skipped
            _part("Thanks!", author_type="user", name="Asha Rao"),
        ],
    )

    row = conversation_to_row(conversation)

    assert row["id"] == "c1"
    assert row["title"] == "Refund"
    assert row["_deleted"] is False
    assert row["content"].split("\n\n") == [
        "Intercom conversation c1, started 2025-10-01. State: open.",
        "Tags: billing, vip",
        "Customer Asha Rao: My card was charged twice.",
        "Agent Ben: Sorry about that, refunding now.",
        "Agent Ben (internal note): Checked Stripe: duplicate charge.",
        "Customer Asha Rao: Thanks!",
    ]
    assert "asha@example.com" not in row["content"]  # names, never emails


def test_truncated_conversation_is_noted():
    row = conversation_to_row(_conversation("c1", 100, parts=[_part("hi")], total=700))
    assert row["content"].endswith("(Only the 1 most recent of 700 messages are included.)")


def test_article_document():
    row = article_to_row(_article("a1", "Refund policy", "<h2>Refunds</h2><p>Within 30 days.</p>"))
    assert row == {
        "id": "a1",
        "title": "Refund policy",
        "content": "About refund policy\n\nRefunds\nWithin 30 days.",
        "url": "https://help.example.com/articles/a1",
        "_deleted": False,
    }


def test_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = intercom_source(session=FakeIntercomSession())
    assert INTERCOM_SOURCE_NAME == "intercom"
    assert document_source_tag(source) == "intercom"


def test_source_scopes_its_dlt_state_by_resource_name():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, pipeline_name_for_source

    default = intercom_source(session=FakeIntercomSession())
    named = intercom_source(session=FakeIntercomSession(), resource_name="intercom_eu")
    assert getattr(default, PIPELINE_SCOPE_ATTR) == "intercom"
    assert list(default.resources) == [
        "intercom_conversations",
        "intercom_articles",
        "intercom_contact_notes",
    ]
    assert getattr(named, PIPELINE_SCOPE_ATTR) == "intercom_eu"
    assert next(iter(named.resources)) == "intercom_eu_conversations"
    assert pipeline_name_for_source(default, "a") != pipeline_name_for_source(default, "b")
    assert pipeline_name_for_source(default, "a") != "ingest_dlt_source"  # not the shared one


def test_source_validates_configuration():
    with pytest.raises(ValueError, match="token is required"):
        intercom_source()
    with pytest.raises(ValueError, match="base_url must be"):
        intercom_source(token="tok", base_url="api.intercom.io")
    with pytest.raises(ValueError, match="snake_case"):
        intercom_source(token="tok", resource_name="Intercom-EU")
    with pytest.raises(ValueError, match="at most 48"):
        intercom_source(token="tok", resource_name="i" * 49)


def test_source_refuses_a_cognee_without_document_sync(monkeypatch):
    monkeypatch.delattr(ic.dlt_utils, "DOCUMENT_SYNC_VERSION")
    with pytest.raises(RuntimeError, match="document sync cleanup"):
        intercom_source(token="tok")


def test_source_requires_something_to_sync():
    with pytest.raises(ValueError, match="Nothing to sync"):
        intercom_source(
            session=FakeIntercomSession(), include_conversations=False, include_articles=False
        )


# ---------------------------------------------------------------------------
# HTTP: pagination and retries (DB-free)
# ---------------------------------------------------------------------------


def test_paginate_follows_starting_after():
    session = FakeIntercomSession(conversations=[_conversation(f"c{i}", 1) for i in range(5)])
    api = IntercomAPI(session, BASE)

    ids = [item["id"] for item in api.paginate("GET", "/conversations", "conversations")]

    assert ids == ["c0", "c1", "c2", "c3", "c4"]
    assert [call[2].get("starting_after") for call in session.calls] == [None, "c1", "c3"]


def test_paginate_fails_on_repeated_cursor():
    session = FakeIntercomSession()
    stuck = FakeResponse(200, {"data": [{"id": "x"}], "pages": {"next": {"starting_after": "x"}}})
    session.scripted = [stuck, stuck]
    # Stopping quietly would make the partial listing look complete and tombstone
    # everything after it, so a cursor that does not move fails the run instead.
    with pytest.raises(IntercomSyncError, match="repeated a page cursor"):
        list(IntercomAPI(session, BASE).paginate("GET", "/contacts", "data"))


def test_emptied_listing_forgets_nothing_until_the_type_is_switched_off():
    session = _workspace()
    state = {}
    list(sync_articles(IntercomAPI(session, BASE), False, state))

    session.articles.clear()
    with pytest.raises(IntercomSyncError, match="Refusing to forget"):
        list(sync_articles(IntercomAPI(session, BASE), False, state))
    assert state["known_ids"] == ["a1", "a2"]

    rows = list(forget_all(state))  # what include_articles=False does
    assert rows == [{"id": "a1", "_deleted": True}, {"id": "a2", "_deleted": True}]


def test_stats_and_check_active():
    session = _workspace()
    stats, calls = {}, []
    list(sync_articles(IntercomAPI(session, BASE), False, {}, stats, lambda: calls.append(1)))
    assert (stats["scanned"], stats["deleted"], stats.get("failed", 0)) == (2, 0, 0)
    assert len(calls) == 2  # once per published row

    def revoked():
        raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        list(sync_articles(IntercomAPI(session, BASE), False, {}, {}, revoked))


def test_malformed_token_is_rejected_without_echoing_it():
    assert _check_token(" tok\n") == "tok"
    with pytest.raises(ValueError) as error:
        _check_token("to k")
    assert "to k" not in str(error.value)


def test_service_and_session_are_one_client():
    with pytest.raises(ValueError, match="not both"):
        intercom_source(service=FakeIntercomSession(), session=FakeIntercomSession())


def test_paginate_follows_numbered_pages():
    notes = [{"id": f"n{i}", "created_at": 1, "body": "x"} for i in range(5)]
    session = FakeIntercomSession(contacts={"u1": {"name": "Asha", "notes": notes}})

    ids = [
        n["id"] for n in IntercomAPI(session, BASE).paginate("GET", "/contacts/u1/notes", "data")
    ]

    assert ids == ["n0", "n1", "n2", "n3", "n4"]  # all three pages, not just the first
    assert [call[2].get("page") for call in session.calls] == [None, 2, 3]


def test_paginate_stops_on_an_empty_numbered_page():
    session = FakeIntercomSession()
    empty = FakeResponse(200, {"data": [], "pages": {"next": f"{BASE}/x?page=2", "page": 1}})
    session.scripted = [empty]
    assert list(IntercomAPI(session, BASE).paginate("GET", "/x", "data")) == []
    assert len(session.calls) == 1


def test_rate_limit_is_retried_after_the_advertised_delay(_no_sleep):
    session = FakeIntercomSession(conversations=[_conversation("c1", 1)])
    session.scripted = [FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(503)]

    payload = IntercomAPI(session, BASE).request("GET", "/conversations/c1")

    assert payload["id"] == "c1"
    assert _no_sleep == [7.0, 2.0]  # Retry-After honoured, then exponential backoff


def test_auth_error_is_not_retried(_no_sleep):
    session = FakeIntercomSession()
    session.scripted = [FakeResponse(401)]
    api = IntercomAPI(session, BASE)
    with pytest.raises(requests.HTTPError, match="access token"):
        api.request("GET", "/conversations")
    assert _no_sleep == []
    assert api.stats["failed_auth"] == 1


def test_server_errors_stop_after_three_attempts(_no_sleep):
    session = FakeIntercomSession()
    session.scripted = [FakeResponse(500)] * 3
    with pytest.raises(IntercomSyncError, match="HTTP 500"):
        IntercomAPI(session, BASE).request("GET", "/conversations")
    assert _no_sleep == [1.0, 2.0]


def test_rate_limit_stops_once_the_wait_budget_is_spent(_no_sleep):
    session = FakeIntercomSession()
    session.scripted = [FakeResponse(429, headers={"Retry-After": "45"})] * 3
    api = IntercomAPI(session, BASE)

    with pytest.raises(IntercomSyncError, match="rate-limiting"):
        api.request("GET", "/conversations")
    assert _no_sleep == [45.0]  # a second 45 s wait would pass the 60 s budget
    assert api.stats["failed_rate_limit"] == 1


# ---------------------------------------------------------------------------
# Sync logic (DB-free)
# ---------------------------------------------------------------------------


def test_first_conversation_sync_fetches_everything_and_saves_cursor():
    session = FakeIntercomSession(
        conversations=[_conversation("c1", 100), _conversation("c2", 200)]
    )
    state = {}

    rows = list(sync_conversations(IntercomAPI(session, BASE), state))

    assert [r["id"] for r in rows] == ["c1", "c2"]
    assert (state["known_ids"], state["cursor"]) == (["c1", "c2"], 200)
    assert set(state["digests"]) == {"c1", "c2"}
    search = next(c for c in session.calls if c[1] == "/conversations/search")
    assert search[3]["query"] == {"field": "updated_at", "operator": ">", "value": 0}


def test_later_sync_searches_from_the_cursor_with_overlap():
    session = FakeIntercomSession(
        conversations=[_conversation("c1", 1_000), _conversation("c2", 2_000)]
    )
    api = IntercomAPI(session, BASE)
    state = {}
    list(sync_conversations(api, state))

    session.conversations["c2"] = _conversation("c2", 9_000, parts=[_part("Refund sent.")])
    session.calls.clear()
    rows = list(sync_conversations(api, state))

    search = next(c for c in session.calls if c[1] == "/conversations/search")
    assert search[3]["query"]["value"] == 2_000 - 300  # five-minute overlap
    retrieved = [c[1] for c in session.calls if c[1].startswith("/conversations/c")]
    assert retrieved == ["/conversations/c2"]  # c1 unchanged → not re-fetched
    assert "Refund sent." in rows[0]["content"]
    assert state["cursor"] == 9_000


def test_unchanged_documents_are_not_re_emitted():
    session = _workspace()
    api = IntercomAPI(session, BASE)
    conversations, articles, stats = {}, {}, {}
    list(sync_conversations(api, conversations))
    list(sync_articles(api, False, articles))

    # Both fall inside the five-minute overlap, so both are read again; nothing changed.
    assert list(sync_conversations(api, conversations, stats)) == []
    assert (stats["skipped"], stats["changed"]) == (2, 0)
    assert list(sync_articles(api, False, articles)) == []

    session.articles[1]["body"] = "<p>Download them from Billing, or ask us.</p>"
    assert [row["id"] for row in sync_articles(api, False, articles)] == ["a2"]


def test_deleted_conversation_is_tombstoned():
    session = FakeIntercomSession(conversations=[_conversation("c1", 1), _conversation("c2", 2)])
    api = IntercomAPI(session, BASE)
    state = {}
    list(sync_conversations(api, state))

    del session.conversations["c1"]
    rows = list(sync_conversations(api, state))

    assert {"id": "c1", "_deleted": True} in rows
    assert state["known_ids"] == ["c2"]


def test_conversation_deleted_mid_sync_is_skipped_not_tombstoned():
    session = FakeIntercomSession(conversations=[_conversation("c1", 1), _conversation("c2", 2)])
    real_request = session.request

    def request(method, url, **kwargs):
        if url.endswith("/conversations/c1"):  # deleted between the sweep and the fetch
            session.calls.append((method, url, None, None))
            return FakeResponse(404)
        return real_request(method, url, **kwargs)

    session.request = request
    state = {}
    rows = list(sync_conversations(IntercomAPI(session, BASE), state))

    assert [r["id"] for r in rows] == ["c2"]
    # Still in the swept id set, so the next run's sweep is what forgets it.
    assert state["known_ids"] == ["c1", "c2"]


def test_conversation_created_mid_sync_is_remembered_for_later_deletion():
    session = FakeIntercomSession(conversations=[_conversation("c1", 1), _conversation("c2", 2)])
    real_request = session.request
    swept = []

    def request(method, url, **kwargs):
        response = real_request(method, url, **kwargs)
        if url.endswith("/conversations") and not swept:  # c2 created after the first sweep
            swept.append(True)
            response._payload["conversations"] = [{"id": "c1"}]
        return response

    session.request = request
    api = IntercomAPI(session, BASE)
    state = {}
    rows = list(sync_conversations(api, state))
    assert [r["id"] for r in rows] == ["c1", "c2"]
    assert state["known_ids"] == ["c1", "c2"]

    # If it is then deleted, the next sweep must still be able to forget it.
    del session.conversations["c2"]
    assert {"id": "c2", "_deleted": True} in list(sync_conversations(api, state))


def test_other_errors_abort_before_any_tombstone():
    session = FakeIntercomSession(conversations=[_conversation("c1", 1)])
    session.scripted = [FakeResponse(403)]
    state = {"known_ids": ["c1", "gone"]}
    with pytest.raises(requests.HTTPError):
        list(sync_conversations(IntercomAPI(session, BASE), state))
    assert state == {"known_ids": ["c1", "gone"]}  # untouched


def test_unpublished_article_is_skipped_and_tombstoned():
    session = FakeIntercomSession(
        articles=[_article(f"a{i}", f"Article {i}", "<p>x</p>") for i in range(60)]
    )
    api = IntercomAPI(session, BASE)
    state = {}
    rows = list(sync_articles(api, False, state))
    assert len(rows) == 60  # two pages of 50

    session.articles[0]["state"] = "draft"
    rows = list(sync_articles(api, False, state))

    assert {"id": "a0", "_deleted": True} in rows
    assert "a0" not in state["known_ids"]
    assert any(r["id"] == "a0" for r in sync_articles(api, True, {}))  # drafts opt-in


def test_contact_notes_are_opt_in_and_tombstoned():
    contacts = {
        "u1": {"name": "Asha Rao", "notes": [{"id": "n1", "created_at": 1, "body": "<p>VIP</p>"}]},
        "u2": {"name": "", "notes": [{"id": "n2", "created_at": 1, "body": "Churn risk"}]},
    }
    session = FakeIntercomSession(contacts=contacts)

    state = {}
    rows = list(sync_contact_notes(IntercomAPI(session, BASE), state))
    assert [r["title"] for r in rows] == ["Note on Asha Rao", "Note on contact u2"]

    contacts["u1"]["notes"] = []
    rows = list(sync_contact_notes(IntercomAPI(session, BASE), state))
    assert {"id": "n1", "_deleted": True} in rows


# ---------------------------------------------------------------------------
# dlt pipeline: merge + _deleted hard delete (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_sync(dlt, tmp_path, session):
    """Run intercom_source the way cognee.remember(write_disposition="merge") does."""
    db_path = (tmp_path / "intercom.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="intercom_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="intercom_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(intercom_source(session=session), write_disposition="merge", primary_key="id")
    return pipeline


def _read(pipeline, table):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, content FROM {table}") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def _workspace():
    return FakeIntercomSession(
        conversations=[_conversation("c1", 100), _conversation("c2", 200)],
        articles=[
            _article("a1", "Refund policy", "<p>Within 30 days.</p>"),
            _article("a2", "Invoices", "<p>Download them from Billing.</p>"),
        ],
    )


def test_first_sync_loads_conversations_and_articles(dlt_mod, tmp_path):
    pipeline = _run_sync(dlt_mod, tmp_path, _workspace())

    assert set(_read(pipeline, "intercom_conversations")) == {"c1", "c2"}
    assert "Within 30 days." in _read(pipeline, "intercom_articles")["a1"]


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    session = _workspace()
    _run_sync(dlt_mod, tmp_path, session)

    session.conversations["c2"] = _conversation("c2", 900, parts=[_part("Refund sent.")])
    pipeline = _run_sync(dlt_mod, tmp_path, session)

    conversations = _read(pipeline, "intercom_conversations")
    assert "Refund sent." in conversations["c2"]
    assert set(conversations) == {"c1", "c2"}  # unchanged c1 survives the merge


def _run(dlt, tmp_path, session, **kwargs):
    db_path = (tmp_path / "intercom.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="intercom_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="intercom_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    source = intercom_source(session=session, **kwargs)
    pipeline.run(source, write_disposition="merge", primary_key="id")
    return pipeline


def test_contact_notes_are_not_fetched_unless_enabled(dlt_mod, tmp_path):
    session = _workspace()
    session.contacts = {
        "u1": {"name": "Asha", "notes": [{"id": "n1", "created_at": 1, "body": "VIP"}]}
    }
    _run(dlt_mod, tmp_path, session)
    assert not any(call[1].startswith("/contacts") for call in session.calls)


def test_switching_a_content_type_off_forgets_it(dlt_mod, tmp_path):
    session = _workspace()
    session.contacts = {
        "u1": {"name": "Asha", "notes": [{"id": "n1", "created_at": 1, "body": "VIP"}]}
    }
    pipeline = _run(dlt_mod, tmp_path, session, include_contact_notes=True)
    assert set(_read(pipeline, "intercom_contact_notes")) == {"n1"}

    pipeline = _run(dlt_mod, tmp_path, session)  # contact notes switched off again

    assert _read(pipeline, "intercom_contact_notes") == {}
    assert set(_read(pipeline, "intercom_conversations")) == {"c1", "c2"}  # others untouched


def test_source_closes_its_sessions_and_reports_stats(dlt_mod, tmp_path, monkeypatch):
    session = _workspace()
    tokens = []
    monkeypatch.setattr(ic, "_make_session", lambda token: tokens.append(token) or session)
    db_path = (tmp_path / "own.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="intercom_own",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="intercom_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    source = intercom_source(token="tok\n")

    pipeline.run(source, write_disposition="merge", primary_key="id")

    assert tokens == ["tok", "tok"]  # conversations and articles; notes are off
    assert getattr(session, "closed", False)
    assert source.cognee_sync_stats["complete"] == 1
    assert source.cognee_sync_stats["scanned"] == 4  # 2 conversations + 2 articles


def test_deleted_conversation_and_unpublished_article_are_removed(dlt_mod, tmp_path):
    session = _workspace()
    _run_sync(dlt_mod, tmp_path, session)

    del session.conversations["c1"]
    session.articles[0]["state"] = "draft"
    pipeline = _run_sync(dlt_mod, tmp_path, session)

    # Tombstoned rows are hard-deleted by the merge, so cognee's read-back no
    # longer sees them and orphan cleanup forgets them.
    assert set(_read(pipeline, "intercom_conversations")) == {"c2"}
    assert set(_read(pipeline, "intercom_articles")) == {"a2"}


def test_complete_waits_for_every_content_type():
    stats = {}
    run = ic._RunStats(stats, ("conversations", "articles", "contact_notes"))
    for name in ("conversations", "articles"):
        run.start(name)["scanned"] = 2
        run.finish(name)
    assert (stats["scanned"], stats["complete"]) == (4, 0)

    run.start("contact_notes")
    run.finish("contact_notes")
    assert stats["complete"] == 1


def _pipeline(dlt, tmp_path, name):
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}"
        ),
        dataset_name="intercom_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def test_a_second_workspace_needs_its_own_resource_name(dlt_mod, tmp_path):
    from cognee.tasks.ingestion.dlt_utils import pipeline_name_for_source
    from dlt.pipeline.exceptions import PipelineStepFailed

    first = _workspace()
    second = _workspace()  # the same conversation and article ids
    second.workspace = "ws2"

    def sync(session, **kwargs):
        source = intercom_source(session=session, **kwargs)
        # Name the pipeline as cognee does: one per (dataset, scope).
        pipeline = _pipeline(dlt_mod, tmp_path, pipeline_name_for_source(source, "intercom"))
        pipeline.run(source, write_disposition="merge", primary_key="id")
        return pipeline

    sync(first)
    with pytest.raises(PipelineStepFailed, match="belongs to workspace ws2"):
        sync(second)  # same resource_name: it would overwrite the first workspace
    sync(second, resource_name="intercom_two")
    del first.conversations["c1"]
    pipeline = sync(first)
    other = sync(second, resource_name="intercom_two")

    assert set(_read(pipeline, "intercom_conversations")) == {"c2"}
    assert set(_read(other, "intercom_two_conversations")) == {"c1", "c2"}


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_the_token_never_reaches_errors_or_logs(
    dlt_mod, tmp_path, monkeypatch, caplog, capsys, status
):
    from dlt.pipeline.exceptions import PipelineStepFailed

    token = "ic_LeakCanary0123456789"
    session = _workspace()
    echo = {"type": "error.list", "errors": [{"message": f"Bad token {token}"}]}
    session.scripted = [FakeResponse(status, echo, headers={"Retry-After": "1"})] * 3
    monkeypatch.setattr(ic, "_make_session", lambda secret: session)
    source = intercom_source(token=f" {token}\n")

    with pytest.raises(PipelineStepFailed) as failure:
        _pipeline(dlt_mod, tmp_path, f"intercom_leak_{status}").run(
            source, write_disposition="merge", primary_key="id"
        )

    chain, error = [], failure.value
    while error is not None:
        chain.append(f"{error!r} {error}")
        error = error.__cause__ or error.__context__
    output = capsys.readouterr()
    assert token not in " ".join(chain)
    assert token not in caplog.text + output.out + output.err
    assert source.cognee_sync_stats["complete"] == 0
    assert source.cognee_sync_stats["failed_auth"] == (1 if status in (401, 403) else 0)
    assert source.cognee_sync_stats["failed_rate_limit"] == (1 if status == 429 else 0)


def test_control_characters_and_lone_surrogates_are_dropped(dlt_mod, tmp_path):
    session = _workspace()
    session.conversations["c1"]["source"]["subject"] = "Ref\x00und\x1b"
    session.conversations["c1"]["conversation_parts"]["conversation_parts"] = [
        _part("Refund \ud800sent.")
    ]
    pipeline = _run_sync(dlt_mod, tmp_path, session)

    content = _read(pipeline, "intercom_conversations")["c1"]
    assert "Ben: Refund sent." in content
    assert conversation_to_row(session.conversations["c1"])["title"] == "Refund"
