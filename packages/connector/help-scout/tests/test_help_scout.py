"""Unit tests for the Help Scout dlt connector (no credentials or network needed).

* DB-free tests: HTML -> text, conversation/article row building, the "embedded
  threads may be incomplete" rule, paging, OAuth renewal and retries.
* dlt-pipeline tests (fake HTTP client, temp sqlite destination) for the
  acceptance criteria: incremental re-sync via ``modifiedSince``, forget-on-delete
  for deleted / spam / vanished conversations and for deleted articles, and a
  failed run that leaves the cursor and memory untouched.
"""

from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import NAMESPACE_OID, uuid5

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_help_scout import help_scout as hs
from cognee_community_connector_help_scout.help_scout import (
    ARTICLES_TABLE,
    CONVERSATIONS_TABLE,
    HELP_SCOUT_SOURCE_NAME,
    _Api,
    _conversation_row,
    _html_to_text,
    _iter_hal,
    _needs_full_threads,
    _OAuthToken,
    _Options,
    _retry_after,
    help_scout_source,
)

OPTS = _Options((), False, False, True, None, frozenset(), "published")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _thread(tid, body, ttype="customer", created="2026-01-01T10:00:00Z", who=("Ann", "Lee"), **kw):
    thread = {
        "id": tid,
        "type": ttype,
        "state": "published",
        "body": body,
        "createdAt": created,
        "createdBy": {"first": who[0], "last": who[1]},
    }
    thread.update(kw)
    return thread


def _conv(cid, subject="Refund", modified="2026-01-01T00:00:00Z", threads=None, **kw):
    threads = threads if threads is not None else [_thread(cid * 10, f"<p>Hello {cid}</p>")]
    conv = {
        "id": cid,
        "number": cid + 100,
        "subject": subject,
        "status": "active",
        "state": "published",
        "type": "email",
        "mailboxId": 1,
        "threads": len([t for t in threads if t["type"] != "note"]),
        "primaryCustomer": {"first": "Ann", "last": "Lee"},
        "assignee": {"first": "Bo", "last": "Kim"},
        "tags": [{"tag": "billing"}],
        "createdAt": "2026-01-01T00:00:00Z",
        "preview": "volatile preview text",
        "_links": {"web": {"href": f"https://secure.helpscout.net/conversation/{cid}"}},
        "_modified": modified,
        "_threads": threads,
    }
    conv.update(kw)
    return conv


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.test")
            raise httpx.HTTPStatusError("error", request=request, response=self)


class FakeHelpScout:
    """In-memory Help Scout: Inbox API v2 (HAL paging) + Docs API v1."""

    def __init__(self, conversations=(), collections=(), articles=(), page_size=2):
        self.conversations = list(conversations)
        self.collections = list(collections)  # [{"id", "name"}]
        self.articles = list(articles)  # [{"id", "collectionId", "name", "text", ...}]
        self.page_size = page_size
        self.calls = []
        self.token_requests = 0
        self.valid_token = None
        self.fail_with = None

    # -- OAuth ----------------------------------------------------------
    def post(self, url, data=None):
        assert url == hs.TOKEN_URL
        assert data["grant_type"] == "client_credentials"
        self.token_requests += 1
        self.valid_token = f"tok-{self.token_requests}"
        return FakeResponse(payload={"access_token": self.valid_token, "expires_in": 172800})

    # -- GET ------------------------------------------------------------
    def get(self, url, params=None, headers=None):
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}{parts.path}"
        query = dict(parse_qsl(parts.query))
        query.update({k: str(v) for k, v in (params or {}).items()})
        self.calls.append((base, query))
        if self.fail_with is not None:
            return FakeResponse(self.fail_with)
        auth = (headers or {}).get("Authorization", "")
        if base.startswith(hs.DOCS_API):
            return self._docs(base, query)
        if auth != f"Bearer {self.valid_token}":
            return FakeResponse(401)
        return self._inbox(base, query)

    def _hal(self, base, query, key, items):
        page = int(query.get("page", 1))
        start = (page - 1) * self.page_size
        chunk = items[start : start + self.page_size]
        links = {}
        if start + self.page_size < len(items):
            links["next"] = {"href": f"{base}?{urlencode({**query, 'page': page + 1})}"}
        return FakeResponse(payload={"_embedded": {key: chunk}, "_links": links})

    def _inbox(self, base, query):
        if base == f"{hs.MAILBOX_API}/mailboxes":
            return self._hal(base, query, "mailboxes", [{"id": 1, "name": "Support"}])
        if base == f"{hs.MAILBOX_API}/conversations":
            items = list(self.conversations)
            if "modifiedSince" in query:
                items = [c for c in items if c["_modified"] > query["modifiedSince"]]
            if "mailbox" in query:
                wanted = {int(m) for m in query["mailbox"].split(",")}
                items = [c for c in items if c["mailboxId"] in wanted]
            out = []
            for conv in items:
                public = {k: v for k, v in conv.items() if not k.startswith("_") or k == "_links"}
                if query.get("embed") == "threads":
                    threads = conv["_threads"]
                    if conv["type"] == "chat":
                        threads = threads[:1]  # Help Scout truncates embedded chats
                    public["_embedded"] = {"threads": threads}
                out.append(public)
            return self._hal(base, query, "conversations", out)
        if base.startswith(f"{hs.MAILBOX_API}/conversations/") and base.endswith("/threads"):
            cid = int(base.split("/")[-2])
            conv = next(c for c in self.conversations if c["id"] == cid)
            return self._hal(base, query, "threads", conv["_threads"])
        raise AssertionError(f"unexpected url {base}")

    def _docs(self, base, query):
        page = int(query.get("page", 1))
        if base == f"{hs.DOCS_API}/collections":
            return FakeResponse(
                payload={"collections": {"page": page, "pages": 1, "items": self.collections}}
            )
        if base.endswith("/articles") and "/collections/" in base:
            cid = base.split("/")[-2]
            refs = [
                {k: a.get(k) for k in ("id", "name", "status", "updatedAt", "lastPublishedAt")}
                for a in self.articles
                if a["collectionId"] == cid
                and (query.get("status") == "all" or a["status"] == "published")
            ]
            return FakeResponse(payload={"articles": {"page": page, "pages": 1, "items": refs}})
        if base.startswith(f"{hs.DOCS_API}/articles/"):
            aid = base.split("/")[-1]
            return FakeResponse(
                payload={"article": next(a for a in self.articles if a["id"] == aid)}
            )
        raise AssertionError(f"unexpected url {base}")

    def calls_to(self, suffix):
        return [q for url, q in self.calls if url.endswith(suffix)]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(hs.time, "sleep", lambda _s: None)


# ---------------------------------------------------------------------------
# DB-free tests
# ---------------------------------------------------------------------------


def test_html_to_text_keeps_text_and_breaks():
    html = "<p>Hi &amp; welcome</p><ul><li>one</li><li>two</li></ul>a<br>b<script>x()</script>"
    assert _html_to_text(html) == "Hi & welcome\n- one\n- two\na\nb"
    assert _html_to_text(None) == ""


def test_conversation_row_renders_threads_oldest_first_and_skips_noise():
    threads = [
        _thread(2, "<p>Agent answer</p>", "message", "2026-01-01T11:00:00Z", ("Bo", "Kim")),
        _thread(1, "<p>Customer question</p>", "customer", "2026-01-01T10:00:00Z"),
        _thread(3, "secret note", "note", "2026-01-01T12:00:00Z"),
        _thread(4, "assigned to Bo", "lineitem", "2026-01-01T09:00:00Z"),
        _thread(5, "unsent draft", "message", "2026-01-01T13:00:00Z", state="draft"),
    ]
    row = _conversation_row(_conv(7, threads=threads), threads, OPTS, {1: "Support"})
    content = row["content"]
    assert row["id"] == "conversation:7"
    assert row["title"] == "#107 Refund"
    assert row["url"] == "https://secure.helpscout.net/conversation/7"
    assert content.index("Customer question") < content.index("Agent answer")
    for expected in ("Inbox: Support", "Status: active", "Customer: Ann Lee", "Tags: billing"):
        assert expected in content
    for hidden in ("secret note", "assigned to Bo", "unsent draft", "volatile preview"):
        assert hidden not in content
    assert set(row) == {"id", "title", "content", "url", "_deleted"}


def test_include_notes_adds_internal_notes():
    threads = [_thread(3, "secret note", "note")]
    options = _Options((), True, False, True, None, frozenset(), "published")
    assert "secret note" in _conversation_row(_conv(7), threads, options, {})["content"]


def test_needs_full_threads_rules():
    complete = [_thread(1, "a"), _thread(2, "b", "message")]
    assert not _needs_full_threads(_conv(1, threads=complete), complete)
    assert _needs_full_threads(_conv(1, threads=complete), complete[:1])  # count mismatch
    assert _needs_full_threads(_conv(1, threads=complete, type="chat"), complete)
    chat = [_thread(1, "hi", "beaconchat")]
    assert _needs_full_threads(_conv(1, threads=chat), chat)


def test_row_becomes_a_cognee_document_tagged_help_scout():
    threads = [_thread(1, "<p>My invoice is wrong</p>")]
    row = _conversation_row(_conv(7, threads=threads), threads, OPTS, {1: "Support"})
    dlt_row = SimpleNamespace(row_data=row, content_hash="h", table_name=CONVERSATIONS_TABLE)
    data_id = uuid5(NAMESPACE_OID, row["id"])

    item = _build_document_data_item(dlt_row, data_id, HELP_SCOUT_SOURCE_NAME)

    # cognee 1.4 keeps the tag in external_metadata; newer cores use system_metadata.
    meta = getattr(item, "system_metadata", None) or item.external_metadata
    assert meta["source"] == HELP_SCOUT_SOURCE_NAME  # routes through normal cognify
    assert meta["external_id"] == "conversation:7"
    assert item.data.startswith("# #107 Refund")
    assert "My invoice is wrong" in item.data


def test_retry_after_prefers_help_scout_header():
    assert _retry_after({"X-RateLimit-Retry-After": "7"}, 0) == 7.0
    assert _retry_after({}, 3) == 8.0


def test_token_is_requested_lazily_once_and_renewed_on_401():
    fake = FakeHelpScout()
    api = _Api(fake, _OAuthToken(fake, "id", "secret", None))
    assert fake.token_requests == 0
    api.get(f"{hs.MAILBOX_API}/mailboxes")
    api.get(f"{hs.MAILBOX_API}/mailboxes")
    assert fake.token_requests == 1
    fake.valid_token = "rotated-server-side"  # token expired -> 401 -> renew once
    api.get(f"{hs.MAILBOX_API}/mailboxes")
    assert fake.token_requests == 2


def test_static_token_rejected_raises_permission_error():
    fake = FakeHelpScout()
    api = _Api(fake, _OAuthToken(fake, None, None, "bad"))
    with pytest.raises(PermissionError):
        api.get(f"{hs.MAILBOX_API}/mailboxes")


def test_rate_limit_is_retried():
    class Flaky(FakeHelpScout):
        def __init__(self):
            super().__init__()
            self.left = 1

        def get(self, url, params=None, headers=None):
            if self.left:
                self.left -= 1
                return FakeResponse(429, headers={"X-RateLimit-Retry-After": "1"})
            return super().get(url, params, headers)

    fake = Flaky()
    api = _Api(fake, _OAuthToken(fake, "id", "secret", None))
    assert api.get(f"{hs.MAILBOX_API}/mailboxes")["_embedded"]["mailboxes"][0]["name"] == "Support"


def test_iter_hal_follows_next_and_stops_on_repeat():
    pages = {
        "u": {"_embedded": {"x": [1]}, "_links": {"next": {"href": "u2"}}},
        "u2": {"_embedded": {"x": [2]}, "_links": {"next": {"href": "u2"}}},
    }

    class Api:
        def get(self, url, params=None):
            return pages[url]

    assert list(_iter_hal(Api(), "u", None, "x")) == [1, 2]


def test_source_configuration_and_validation(monkeypatch):
    for var in ("HELPSCOUT_APP_ID", "HELPSCOUT_APP_SECRET", "HELPSCOUT_ACCESS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("HELPSCOUT_DOCS_API_KEY", raising=False)
    source = help_scout_source(app_id="a", app_secret="b", docs_api_key="k", client=FakeHelpScout())
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == HELP_SCOUT_SOURCE_NAME
    assert set(source.resources) == {CONVERSATIONS_TABLE, ARTICLES_TABLE}
    for resource in source.resources.values():
        assert resource.write_disposition == "merge"
        assert resource._hints["columns"]["_deleted"]["hard_delete"] is True

    only_inbox = help_scout_source(app_id="a", app_secret="b", client=FakeHelpScout())
    assert set(only_inbox.resources) == {CONVERSATIONS_TABLE}
    with pytest.raises(ValueError, match="credentials"):
        help_scout_source(client=FakeHelpScout())
    with pytest.raises(ValueError, match="Docs API key"):
        help_scout_source(app_id="a", app_secret="b", include_articles=True, client=FakeHelpScout())
    with pytest.raises(ValueError, match="Nothing to ingest"):
        help_scout_source(include_conversations=False, client=FakeHelpScout())


# ---------------------------------------------------------------------------
# dlt pipeline tests (temp sqlite)
# ---------------------------------------------------------------------------


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="help_scout_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'hs.db'}"),
        dataset_name="help_scout",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _run(tmp_path, fake, **kwargs):
    kwargs.setdefault("app_id", "id")
    kwargs.setdefault("app_secret", "secret")
    pipeline = _pipeline(tmp_path)
    pipeline.run(help_scout_source(client=fake, **kwargs))
    return pipeline


def _rows(pipeline, table=CONVERSATIONS_TABLE):
    query = f"SELECT id, content FROM {table} ORDER BY id"
    with pipeline.sql_client() as sql, sql.execute_query(query) as cur:
        return {r[0]: r[1] for r in cur.fetchall()}


def _cursor(pipeline):
    resources = pipeline.state["sources"][HELP_SCOUT_SOURCE_NAME]["resources"]
    return resources[CONVERSATIONS_TABLE]["modified_since"]


def test_first_run_backfills_with_embedded_threads(tmp_path):
    chat_threads = [_thread(1, "chat hi", "chat"), _thread(2, "chat bye", "chat")]
    fake = FakeHelpScout(
        [
            _conv(1),
            _conv(2),
            _conv(3, type="chat", threads=chat_threads),
            _conv(4, status="spam"),
            _conv(5, state="deleted"),
        ]
    )
    pipeline = _run(tmp_path, fake)
    rows = _rows(pipeline)
    assert set(rows) == {"conversation:1", "conversation:2", "conversation:3"}
    assert "chat bye" in rows["conversation:3"]  # full chat fetched, not the truncated copy
    # Only the chat conversation needed its own threads request.
    assert len([u for u, _ in fake.calls if u.endswith("/threads")]) == 1
    listing = fake.calls_to("/conversations")
    assert listing[0]["embed"] == "threads"
    assert listing[0]["status"] == "all"
    assert "modifiedSince" not in listing[0]
    assert _cursor(pipeline).endswith("Z")


def test_second_run_only_fetches_changes(tmp_path):
    fake = FakeHelpScout([_conv(1, modified="2026-01-01T00:00:00Z"), _conv(2)])
    pipeline = _run(tmp_path, fake)
    before = _rows(pipeline)
    cursor = _cursor(pipeline)

    edited = _conv(1, modified="2999-01-01T00:00:00Z", threads=[_thread(10, "<p>Edited</p>")])
    fake.conversations[0] = edited
    fake.calls.clear()
    after = _rows(_run(tmp_path, fake))

    changed_listing = fake.calls_to("/conversations")[0]
    assert changed_listing["modifiedSince"] == cursor
    assert "Edited" in after["conversation:1"]
    assert after["conversation:2"] == before["conversation:2"]


def test_vanished_conversation_is_forgotten_by_reconcile(tmp_path):
    fake = FakeHelpScout([_conv(1), _conv(2), _conv(3)])
    _run(tmp_path, fake)
    fake.conversations = [c for c in fake.conversations if c["id"] != 2]
    assert set(_rows(_run(tmp_path, fake))) == {"conversation:1", "conversation:3"}


def test_reconcile_off_skips_full_listing(tmp_path):
    fake = FakeHelpScout([_conv(1), _conv(2)])
    _run(tmp_path, fake, reconcile=False)
    fake.conversations = [fake.conversations[0]]
    fake.calls.clear()
    rows = _rows(_run(tmp_path, fake, reconcile=False))
    assert len(fake.calls_to("/conversations")) == 1  # changed-only listing
    assert "conversation:2" in rows  # documented trade-off: no id diff


def test_deleted_or_spam_in_delta_is_tombstoned_even_without_reconcile(tmp_path):
    fake = FakeHelpScout([_conv(1), _conv(2)])
    _run(tmp_path, fake, reconcile=False)
    fake.conversations = [
        _conv(1, state="deleted", modified="2999-01-01T00:00:00Z"),
        _conv(2, status="spam", modified="2999-01-01T00:00:00Z"),
    ]
    assert _rows(_run(tmp_path, fake, reconcile=False)) == {}


def test_include_spam_keeps_spam(tmp_path):
    fake = FakeHelpScout([_conv(1, status="spam")])
    assert set(_rows(_run(tmp_path, fake, include_spam=True))) == {"conversation:1"}


def test_failed_run_keeps_cursor_and_memory(tmp_path):
    fake = FakeHelpScout([_conv(1)])
    pipeline = _run(tmp_path, fake)
    cursor = _cursor(pipeline)
    fake.fail_with = 400
    with pytest.raises(PipelineStepFailed):
        _run(tmp_path, fake)
    pipeline = _pipeline(tmp_path)
    assert _cursor(pipeline) == cursor
    assert set(_rows(pipeline)) == {"conversation:1"}


def test_mailbox_filter_is_sent(tmp_path):
    fake = FakeHelpScout([_conv(1), _conv(2, mailboxId=9)])
    rows = _rows(_run(tmp_path, fake, mailbox_ids=[9]))
    assert set(rows) == {"conversation:2"}
    assert fake.calls_to("/conversations")[0]["mailbox"] == "9"


# --- Docs articles ---------------------------------------------------------


def _article(aid, collection="c1", status="published", updated="2026-01-01T00:00:00Z", text=None):
    return {
        "id": aid,
        "collectionId": collection,
        "name": f"Article {aid}",
        "status": status,
        "text": text or f"<p>How to {aid}</p>",
        "publicUrl": f"https://docs.example.com/article/{aid}",
        "updatedAt": updated,
        "lastPublishedAt": updated,
    }


def _docs_run(tmp_path, fake, **kwargs):
    return _run(tmp_path, fake, include_conversations=False, docs_api_key="docs-key", **kwargs)


def test_articles_are_ingested_and_only_changes_refetched(tmp_path):
    fake = FakeHelpScout(
        collections=[{"id": "c1", "name": "Billing"}],
        articles=[_article("a1"), _article("a2")],
    )
    rows = _rows(_docs_run(tmp_path, fake), ARTICLES_TABLE)
    assert set(rows) == {"article:a1", "article:a2"}
    assert "Collection: Billing" in rows["article:a1"]

    fake.calls.clear()
    _docs_run(tmp_path, fake)
    assert not [u for u, _ in fake.calls if "/articles/" in u]  # nothing changed

    fake.articles[0] = _article("a1", updated="2026-02-01T00:00:00Z", text="<p>New text</p>")
    fake.calls.clear()
    rows = _rows(_docs_run(tmp_path, fake), ARTICLES_TABLE)
    assert [u for u, _ in fake.calls if "/articles/" in u] == [f"{hs.DOCS_API}/articles/a1"]
    assert "New text" in rows["article:a1"]


def test_deleted_or_unpublished_article_is_forgotten(tmp_path):
    fake = FakeHelpScout(
        collections=[{"id": "c1", "name": "Billing"}],
        articles=[_article("a1"), _article("a2"), _article("a3")],
    )
    _docs_run(tmp_path, fake)
    fake.articles = [_article("a1"), _article("a2", status="notpublished")]
    assert set(_rows(_docs_run(tmp_path, fake), ARTICLES_TABLE)) == {"article:a1"}


def test_collection_filter(tmp_path):
    fake = FakeHelpScout(
        collections=[{"id": "c1", "name": "A"}, {"id": "c2", "name": "B"}],
        articles=[_article("a1", "c1"), _article("b1", "c2")],
    )
    rows = _rows(_docs_run(tmp_path, fake, collection_ids=["c2"]), ARTICLES_TABLE)
    assert set(rows) == {"article:b1"}
