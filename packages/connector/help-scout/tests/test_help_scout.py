"""Unit tests for the Help Scout dlt connector (no credentials or network needed).

The fake API follows the documented / observed behaviour: HAL paging with
``page.totalElements``, ``status=all``, ``modifiedSince``, chat threads truncated
when embedded, a ``Date`` header, ``404`` for a deleted conversation, the Docs
API's ``{page, pages, count, items}`` lists, and the two rate-limit headers.
"""

from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import NAMESPACE_OID, uuid5

import dlt
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_help_scout import help_scout as hs
from cognee_community_connector_help_scout.help_scout import (
    HELP_SCOUT_SOURCE_NAME,
    _Api,
    _conversation_row,
    _html_to_text,
    _iter_docs,
    _iter_hal,
    _needs_full_threads,
    _OAuthToken,
    _Options,
    _rate_limit_wait,
    _UnstableListingError,
    help_scout_source,
)

OPTS = _Options()
SERVER_NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


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
        "tags": [{"tag": "billing"}, {"tag": "vip"}],
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
        self.headers = {"Date": format_datetime(SERVER_NOW, usegmt=True), **(headers or {})}

    def json(self):
        return self._payload


class FakeHelpScout:
    """In-memory Help Scout: Inbox API v2 (HAL paging) + Docs API v1."""

    def __init__(self, conversations=(), collections=(), articles=(), page_size=2):
        self.conversations = list(conversations)
        self.collections = list(collections)  # [{"id", "name"}]
        self.articles = list(articles)
        self.page_size = page_size
        self.calls = []
        self.token_requests = 0
        self.valid_token = None
        self.fail_with = None
        self.on_page = None  # hook(base, query) called before a list page is served

    def post(self, url, data=None):
        assert url == hs.TOKEN_URL
        self.token_requests += 1
        self.valid_token = f"tok-{self.token_requests}"
        return FakeResponse(payload={"access_token": self.valid_token, "expires_in": 172800})

    def get(self, url, params=None, headers=None):
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}{parts.path}"
        query = dict(parse_qsl(parts.query))
        query.update({k: str(v) for k, v in (params or {}).items()})
        self.calls.append((base, query))
        if self.fail_with is not None:
            return FakeResponse(self.fail_with)
        if base.startswith(hs.DOCS_API):
            return self._docs(base, query)
        if (headers or {}).get("Authorization") != f"Bearer {self.valid_token}":
            return FakeResponse(401)
        return self._inbox(base, query)

    def _hal(self, base, query, key, items):
        if self.on_page:
            self.on_page(base, query)
            items = self._current(base, query) if key == "conversations" else items
        page = int(query.get("page", 1))
        start = (page - 1) * self.page_size
        chunk = items[start : start + self.page_size]
        links = {}
        if start + self.page_size < len(items):
            links["next"] = {"href": f"{base}?{urlencode({**query, 'page': page + 1})}"}
        meta = {"number": page, "size": self.page_size, "totalElements": len(items)}
        return FakeResponse(payload={"_embedded": {key: chunk}, "_links": links, "page": meta})

    def _public(self, conv, embed):
        public = {k: v for k, v in conv.items() if not k.startswith("_") or k == "_links"}
        if embed:
            threads = conv["_threads"]
            if conv["type"] == "chat":
                threads = threads[:1]  # Help Scout truncates embedded chats
            public["_embedded"] = {"threads": threads}
        return public

    def _current(self, base, query):
        items = [c for c in self.conversations if c["state"] != "deleted"]
        if "modifiedSince" in query:
            items = [c for c in items if c["_modified"] > query["modifiedSince"]]
        if "mailbox" in query:
            wanted = {int(m) for m in query["mailbox"].split(",")}
            items = [c for c in items if c["mailboxId"] in wanted]
        key = "_modified" if query.get("sortField") == "modifiedAt" else "number"
        items = sorted(items, key=lambda c: c[key])
        return [self._public(c, query.get("embed") == "threads") for c in items]

    def _inbox(self, base, query):
        if base == f"{hs.MAILBOX_API}/conversations":
            return self._hal(base, query, "conversations", self._current(base, query))
        tail = base.split(f"{hs.MAILBOX_API}/conversations/", 1)[1]
        cid = int(tail.split("/")[0])
        conv = next((c for c in self.conversations if c["id"] == cid), None)
        if conv is None:
            return FakeResponse(404)
        if tail.endswith("/threads"):
            return self._hal(base, query, "threads", conv["_threads"])
        return FakeResponse(payload=self._public(conv, False))

    def _docs(self, base, query):
        page = int(query.get("page", 1))
        if base == f"{hs.DOCS_API}/collections":
            body = {"page": page, "pages": 1, "count": len(self.collections)}
            return FakeResponse(payload={"collections": {**body, "items": self.collections}})
        if base.endswith("/articles") and "/collections/" in base:
            cid = base.split("/")[-2]
            refs = [
                {k: a.get(k) for k in ("id", "name", "status", "updatedAt", "lastPublishedAt")}
                for a in self.articles
                if a["collectionId"] == cid
                and (query.get("status") == "all" or a["status"] == "published")
            ]
            body = {"page": page, "pages": 1, "count": len(refs), "items": refs}
            return FakeResponse(payload={"articles": body})
        if base.startswith(f"{hs.DOCS_API}/articles/"):
            aid = base.split("/")[-1]
            article = next((a for a in self.articles if a["id"] == aid), None)
            return (
                FakeResponse(404) if article is None else FakeResponse(payload={"article": article})
            )
        raise AssertionError(f"unexpected url {base}")

    def calls_to(self, suffix):
        return [q for url, q in self.calls if url.endswith(suffix)]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(hs.time, "sleep", lambda _s: None)


def _src(fake, **kw):
    kw.setdefault("app_id", "id")
    kw.setdefault("app_secret", "secret")
    return help_scout_source(service=fake, **kw)


def _api(fake, stats=None, max_wait=120.0, check_active=None):
    return _Api(
        fake,
        _OAuthToken(fake, "id", "secret", None),
        stats if stats is not None else {},
        check_active,
        max_wait,
        "Inbox API",
    )


# ---------------------------------------------------------------------------
# DB-free
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
    row = _conversation_row(_conv(7, threads=threads), threads, OPTS)
    content = row["content"]
    assert row["id"] == "conversation:7"
    assert row["title"] == "#107 Refund"
    assert content.index("Customer question") < content.index("Agent answer")
    for expected in ("Status: active", "Customer: Ann Lee", "Tags: billing, vip"):
        assert expected in content
    for hidden in ("secret note", "assigned to Bo", "unsent draft", "volatile preview"):
        assert hidden not in content
    assert set(row) == {"id", "title", "content", "url", "_deleted"}


def test_shuffled_tags_and_renamed_inbox_keep_the_same_content():
    threads = [_thread(1, "hi")]
    a = _conversation_row(_conv(7, threads=threads), threads, OPTS)
    shuffled = _conv(7, threads=threads, tags=[{"tag": "vip"}, {"tag": "billing"}], mailboxId=9)
    b = _conversation_row(shuffled, threads, OPTS)
    assert a == b  # same hash -> no re-extraction
    assert "Inbox" not in a["content"]


def test_include_notes_adds_internal_notes():
    threads = [_thread(3, "secret note", "note")]
    row = _conversation_row(_conv(7), threads, _Options(include_notes=True))
    assert "secret note" in row["content"]


def test_needs_full_threads_rules():
    complete = [_thread(1, "a"), _thread(2, "b", "message")]
    assert not _needs_full_threads(_conv(1, threads=complete), complete)
    assert _needs_full_threads(_conv(1, threads=complete), complete[:1])
    assert _needs_full_threads(_conv(1, threads=complete, type="chat"), complete)


def test_rate_limit_headers_inbox_and_docs(monkeypatch):
    assert _rate_limit_wait({"X-RateLimit-Retry-After": "7"}, 0) == 7.0
    assert _rate_limit_wait({"X-RateLimit-Reset": "600"}, 0) == 600.0  # Docs API
    monkeypatch.setattr(hs.time, "time", lambda: 1_800_000_000)
    assert _rate_limit_wait({"X-RateLimit-Reset": "1800000030"}, 0) == 30.0
    assert _rate_limit_wait({}, 1) == 4.0


def test_long_rate_limit_stops_instead_of_sleeping(monkeypatch):
    slept, calls = [], []
    monkeypatch.setattr(hs.time, "sleep", slept.append)
    fake = FakeHelpScout()

    def limited(*a, **k):
        calls.append(1)
        return FakeResponse(429, headers={"X-RateLimit-Retry-After": "900"})

    fake.get = limited
    with pytest.raises(hs.RateLimitedError):
        _api(fake).get(f"{hs.MAILBOX_API}/conversations")
    assert slept == [] and len(calls) == 1  # no 15-minute sleep, no retries


def test_short_rate_limit_is_retried():
    fake = FakeHelpScout([_conv(1)])
    real, left = fake.get, [1]

    def flaky(url, params=None, headers=None):
        if left[0]:
            left[0] -= 1
            return FakeResponse(429, headers={"X-RateLimit-Retry-After": "1"})
        return real(url, params, headers)

    fake.get = flaky
    assert _api(fake).get(f"{hs.MAILBOX_API}/conversations")["_embedded"]["conversations"]


def test_token_is_requested_lazily_and_renewed_on_401():
    fake = FakeHelpScout([_conv(1)])
    api = _api(fake)
    api.get(f"{hs.MAILBOX_API}/conversations")
    assert fake.token_requests == 1
    fake.valid_token = "rotated"
    api.get(f"{hs.MAILBOX_API}/conversations")
    assert fake.token_requests == 2


def test_errors_carry_status_but_no_url_or_ids():
    fake = FakeHelpScout()
    stats = {}
    api = _Api(fake, _OAuthToken(fake, None, None, "bad"), stats, None, 120.0, "Inbox API")
    with pytest.raises(PermissionError) as info:
        api.get(f"{hs.MAILBOX_API}/conversations/12345/threads")
    assert "401" in str(info.value)
    assert "12345" not in str(info.value) and "http" not in str(info.value)
    assert stats["failed_auth"] == 1


def test_check_active_runs_before_every_request():
    fake = FakeHelpScout([_conv(1)])
    seen = []
    _api(fake, check_active=lambda: seen.append(1)).get(f"{hs.MAILBOX_API}/conversations")
    assert len(seen) == 1


def test_repeated_next_link_and_empty_middle_page_raise():
    class Api:
        name = "Inbox API"
        calls = 0

        def get(self, url, params=None):
            self.calls += 1
            assert self.calls <= 4, "pagination exceeded the test request budget"
            return {"_embedded": {"x": [{"id": 1}]}, "_links": {"next": {"href": "same"}}}

    with pytest.raises(_UnstableListingError):
        list(_iter_hal(Api(), "u", None, "x"))

    class Docs:
        name = "Docs API"

        def get(self, url, params=None):
            assert params["page"] <= 4, "pagination exceeded the test request budget"
            return {"articles": {"page": params["page"], "pages": 3, "items": []}}

    with pytest.raises(_UnstableListingError):
        list(_iter_docs(Docs(), "u", "articles", {}))


def test_document_is_tagged_help_scout():
    threads = [_thread(1, "<p>My invoice is wrong</p>")]
    row = _conversation_row(_conv(7, threads=threads), threads, OPTS)
    dlt_row = SimpleNamespace(row_data=row, content_hash="h", table_name="help_scout_conversations")
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, row["id"]), "help_scout")
    assert item.system_metadata["source"] == HELP_SCOUT_SOURCE_NAME
    assert "My invoice is wrong" in item.data


def test_source_configuration_and_validation():
    source = _src(FakeHelpScout(), resource_name="hs_acme", docs_api_key="k")
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == HELP_SCOUT_SOURCE_NAME
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "hs_acme"
    assert set(source.resources) == {"hs_acme_conversations", "hs_acme_articles"}
    for resource in source.resources.values():
        assert resource.write_disposition == "merge"
        assert resource._hints["columns"]["_deleted"]["hard_delete"] is True
    assert isinstance(source.cognee_sync_stats, dict)
    fake = FakeHelpScout()
    assert help_scout_source(client=fake, access_token="t") is not None  # alias
    with pytest.raises(TypeError):
        help_scout_source("hs_acme")  # keyword-only
    with pytest.raises(ValueError, match="credentials"):
        help_scout_source(service=fake)
    with pytest.raises(ValueError, match="resource_name"):
        _src(fake, resource_name="Bad Name")
    with pytest.raises(ValueError, match="both"):
        help_scout_source(service=fake, client=FakeHelpScout(), access_token="t")


def test_old_cognee_core_is_refused(monkeypatch):
    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    with pytest.raises(RuntimeError, match=r"1\.6\.1"):
        _src(FakeHelpScout())


# ---------------------------------------------------------------------------
# dlt pipeline tests (temp sqlite)
# ---------------------------------------------------------------------------


def _pipeline(tmp_path, name="help_scout_test"):
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'hs.db'}"),
        dataset_name="help_scout",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _run(tmp_path, fake, **kw):
    pipeline = _pipeline(tmp_path)
    source = _src(fake, **kw)
    pipeline.run(source)
    return pipeline, source


def _rows(pipeline, table="help_scout_conversations"):
    query = f"SELECT id, content FROM {table} ORDER BY id"
    with pipeline.sql_client() as sql, sql.execute_query(query) as cur:
        return {r[0]: r[1] for r in cur.fetchall()}


def _state(pipeline, name="help_scout", table="help_scout_conversations"):
    return pipeline.state["sources"][name]["resources"][table]


def test_first_run_backfills_with_embedded_threads_and_server_clock_cursor(tmp_path):
    chat = [_thread(1, "chat hi", "chat"), _thread(2, "chat bye", "chat")]
    fake = FakeHelpScout(
        [_conv(1), _conv(2), _conv(3, type="chat", threads=chat), _conv(4, status="spam")]
    )
    pipeline, source = _run(tmp_path, fake)
    rows = _rows(pipeline)
    assert set(rows) == {"conversation:1", "conversation:2", "conversation:3"}
    assert "chat bye" in rows["conversation:3"]
    assert len([u for u, _ in fake.calls if u.endswith("/threads")]) == 1
    listing = fake.calls_to("/conversations")[0]
    assert listing["status"] == "all" and listing["embed"] == "threads"
    # Cursor = Help Scout's Date header minus the overlap, not the local clock.
    assert _state(pipeline)["modified_since"] == "2026-10-20T11:55:00Z"
    assert source.cognee_sync_stats["conversations_synced"] == 3
    assert source.cognee_sync_stats["conversations_thread_requests"] == 1


def test_second_run_only_fetches_changes(tmp_path):
    fake = FakeHelpScout([_conv(1), _conv(2)])
    pipeline, _ = _run(tmp_path, fake)
    before = _rows(pipeline)
    fake.conversations[0] = _conv(
        1, modified="2999-01-01T00:00:00Z", threads=[_thread(9, "Edited")]
    )
    fake.calls.clear()
    pipeline, _ = _run(tmp_path, fake)
    after = _rows(pipeline)
    assert fake.calls_to("/conversations")[0]["modifiedSince"] == "2026-10-20T11:55:00Z"
    assert "Edited" in after["conversation:1"]
    assert after["conversation:2"] == before["conversation:2"]


def test_vanished_conversation_is_forgotten_after_a_recheck(tmp_path):
    fake = FakeHelpScout([_conv(1), _conv(2), _conv(3)])
    _run(tmp_path, fake)
    fake.conversations = [c for c in fake.conversations if c["id"] != 2]  # gone: 404
    pipeline, source = _run(tmp_path, fake)
    assert set(_rows(pipeline)) == {"conversation:1", "conversation:3"}
    assert source.cognee_sync_stats["conversations_forgotten"] == 1


def test_delete_in_the_middle_of_reconcile_listing_never_forgets_a_live_one(tmp_path):
    """Reviewer's case: 60 conversations, one deleted after page 1 is read."""
    fake = FakeHelpScout([_conv(i) for i in range(1, 61)], page_size=25)
    _run(tmp_path, fake)

    def delete_once(base, query):
        if query.get("sortField") == "number" and query.get("page") == "2":
            fake.on_page = None
            fake.conversations = [c for c in fake.conversations if c["id"] != 5]

    fake.on_page = delete_once
    pipeline, source = _run(tmp_path, fake)
    rows = _rows(pipeline)
    assert "conversation:43" in rows  # the live one that would have shifted
    assert len(rows) == 60  # nothing forgotten from a moving listing
    assert source.cognee_sync_stats.get("conversations_reconcile_skipped") == 1
    pipeline, _ = _run(tmp_path, fake)  # next run: the deleted one goes
    rows = _rows(pipeline)
    assert "conversation:5" not in rows and len(rows) == 59


def test_a_live_conversation_missing_from_the_listing_is_kept(tmp_path):
    fake = FakeHelpScout([_conv(1), _conv(2)])
    _run(tmp_path, fake)
    real = fake._current

    def hide_two(base, query):
        items = real(base, query)
        return [c for c in items if c["id"] != 2] if query.get("sortField") == "number" else items

    fake._current = hide_two
    pipeline, _ = _run(tmp_path, fake)
    assert "conversation:2" in _rows(pipeline)  # the re-check says it is alive


def test_conversation_modified_while_paging_is_not_skipped(tmp_path):
    fake = FakeHelpScout(
        [_conv(i, modified=f"2026-01-01T00:00:0{i}Z") for i in range(1, 6)], page_size=2
    )

    def bump_one(base, query):
        if query.get("sortField") == "modifiedAt" and query.get("page") == "2":
            fake.on_page = None
            fake.conversations[0]["_modified"] = "2026-01-01T00:00:59Z"

    fake.on_page = bump_one
    pipeline, _ = _run(tmp_path, fake)
    assert len(_rows(pipeline)) == 5


def test_404_on_threads_tombstones_instead_of_failing(tmp_path):
    chat = [_thread(1, "a", "chat"), _thread(2, "b", "chat")]
    fake = FakeHelpScout([_conv(1, type="chat", threads=chat), _conv(2)])
    _run(tmp_path, fake)
    fake.conversations[0]["_modified"] = "2999-01-01T00:00:00Z"
    real = fake._inbox

    def gone_before_threads(base, query):
        if base.endswith("/1/threads"):
            return FakeResponse(404)
        return real(base, query)

    fake._inbox = gone_before_threads
    pipeline, _ = _run(tmp_path, fake)
    assert set(_rows(pipeline)) == {"conversation:2"}


def test_adding_an_inbox_later_backfills_it_and_removing_forgets_it(tmp_path):
    fake = FakeHelpScout([_conv(1, mailboxId=1), _conv(2, mailboxId=2)])
    pipeline, _ = _run(tmp_path, fake, mailbox_ids=[1])
    assert set(_rows(pipeline)) == {"conversation:1"}
    pipeline, _ = _run(tmp_path, fake, mailbox_ids=[1, 2])  # unchanged convs, new inbox
    assert set(_rows(pipeline)) == {"conversation:1", "conversation:2"}
    pipeline, _ = _run(tmp_path, fake, mailbox_ids=[2])
    assert set(_rows(pipeline)) == {"conversation:2"}


def test_reconcile_every_n_runs(tmp_path):
    fake = FakeHelpScout([_conv(1), _conv(2)])
    _run(tmp_path, fake, reconcile_every=3)
    fake.conversations = [fake.conversations[0]]
    for expected in (2, 2, 1):  # runs 2 and 3 skip the full id listing; run 4 reconciles
        fake.calls.clear()
        pipeline, _ = _run(tmp_path, fake, reconcile_every=3)
        assert len(_rows(pipeline)) == expected
    assert any(q.get("sortField") == "number" for q in fake.calls_to("/conversations"))


def test_deleted_or_spam_in_delta_is_tombstoned_without_reconcile(tmp_path):
    fake = FakeHelpScout([_conv(1), _conv(2)])
    _run(tmp_path, fake, reconcile=False)
    fake.conversations[1]["status"] = "spam"
    fake.conversations[1]["_modified"] = "2999-01-01T00:00:00Z"
    pipeline, _ = _run(tmp_path, fake, reconcile=False)
    assert set(_rows(pipeline)) == {"conversation:1"}


def test_long_rate_limit_stops_cleanly_and_keeps_state(tmp_path):
    fake = FakeHelpScout([_conv(1)])
    pipeline, _ = _run(tmp_path, fake)
    cursor = _state(pipeline)["modified_since"]
    fake.fail_with = 429
    real = fake.get

    def limited(url, params=None, headers=None):
        response = real(url, params, headers)
        response.headers["X-RateLimit-Retry-After"] = "900"
        return response

    fake.get = limited
    pipeline, source = _run(tmp_path, fake)  # no exception
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
    assert _state(pipeline)["modified_since"] == cursor
    assert set(_rows(pipeline)) == {"conversation:1"}


def test_failed_run_keeps_cursor_and_memory(tmp_path):
    fake = FakeHelpScout([_conv(1)])
    pipeline, _ = _run(tmp_path, fake)
    cursor = _state(pipeline)["modified_since"]
    fake.fail_with = 400
    with pytest.raises(PipelineStepFailed):
        _run(tmp_path, fake)
    pipeline = _pipeline(tmp_path)
    assert _state(pipeline)["modified_since"] == cursor
    assert set(_rows(pipeline)) == {"conversation:1"}


def test_two_sources_in_one_dataset_keep_separate_state(tmp_path):
    acme = FakeHelpScout([_conv(1), _conv(2)])
    globex = FakeHelpScout([_conv(7)])
    pipeline = _pipeline(tmp_path)
    pipeline.run(_src(acme, resource_name="hs_acme"))
    pipeline.run(_src(globex, resource_name="hs_globex"))
    acme.conversations = [acme.conversations[0]]
    pipeline.run(_src(acme, resource_name="hs_acme"))
    pipeline.run(_src(globex, resource_name="hs_globex"))
    assert set(_rows(pipeline, "hs_acme_conversations")) == {"conversation:1"}
    assert set(_rows(pipeline, "hs_globex_conversations")) == {"conversation:7"}


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


def _docs_run(tmp_path, fake, **kw):
    return _run(tmp_path, fake, include_conversations=False, docs_api_key="docs-key", **kw)


def test_articles_are_ingested_and_only_changes_refetched(tmp_path):
    fake = FakeHelpScout(
        collections=[{"id": "c1", "name": "Billing"}], articles=[_article("a1"), _article("a2")]
    )
    pipeline, _ = _docs_run(tmp_path, fake)
    rows = _rows(pipeline, "help_scout_articles")
    assert set(rows) == {"article:a1", "article:a2"}
    assert "Billing" not in rows["article:a1"]  # collection name not in the text
    assert all("pageSize" not in q for _, q in fake.calls)
    fake.calls.clear()
    _docs_run(tmp_path, fake)
    assert not [u for u, _ in fake.calls if "/articles/" in u]
    fake.articles[0] = _article("a1", updated="2026-02-01T00:00:00Z", text="<p>New text</p>")
    fake.calls.clear()
    pipeline, _ = _docs_run(tmp_path, fake)
    assert [u for u, _ in fake.calls if "/articles/" in u] == [f"{hs.DOCS_API}/articles/a1"]
    assert "New text" in _rows(pipeline, "help_scout_articles")["article:a1"]


def test_deleted_or_unpublished_article_is_forgotten(tmp_path):
    fake = FakeHelpScout(
        collections=[{"id": "c1"}], articles=[_article("a1"), _article("a2"), _article("a3")]
    )
    _docs_run(tmp_path, fake)
    fake.articles = [_article("a1"), _article("a2", status="notpublished")]
    pipeline, _ = _docs_run(tmp_path, fake)
    assert set(_rows(pipeline, "help_scout_articles")) == {"article:a1"}


def test_empty_article_answer_raises_instead_of_storing_a_blank(tmp_path):
    fake = FakeHelpScout(collections=[{"id": "c1"}], articles=[_article("a1")])
    real = fake._docs

    def blank(base, query):
        if base.endswith("/articles/a1"):
            return FakeResponse(payload={"article": {}})
        return real(base, query)

    fake._docs = blank
    with pytest.raises(PipelineStepFailed, match="empty article"):
        _docs_run(tmp_path, fake)


def test_collection_filter(tmp_path):
    fake = FakeHelpScout(
        collections=[{"id": "c1"}, {"id": "c2"}],
        articles=[_article("a1", "c1"), _article("b1", "c2")],
    )
    pipeline, _ = _docs_run(tmp_path, fake, collection_ids=["c2"])
    assert set(_rows(pipeline, "help_scout_articles")) == {"article:b1"}


def test_removing_a_large_inbox_forgets_all_of_it(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "MAX_VERIFY", 3)  # a "mass vanish" guard of 3
    fake = FakeHelpScout([_conv(i, mailboxId=2) for i in range(1, 7)] + [_conv(9, mailboxId=1)])
    _run(tmp_path, fake, mailbox_ids=[1, 2])
    fake.conversations = fake.conversations[-1:]  # the 6 in inbox 2 vanish: mass vanish
    pipeline, source = _run(tmp_path, fake, mailbox_ids=[1, 2])
    assert len(_rows(pipeline)) == 4  # first bounded batch of three is confirmed
    assert source.cognee_sync_stats["conversations_forgotten"] == 3
    fake.conversations = [_conv(i, mailboxId=2) for i in range(1, 7)] + [_conv(9, mailboxId=1)]
    pipeline, _ = _run(tmp_path, fake, mailbox_ids=[1])  # scope change: no cap
    assert set(_rows(pipeline)) == {"conversation:9"}
