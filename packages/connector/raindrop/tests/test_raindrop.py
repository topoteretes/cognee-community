"""Unit tests for the Raindrop.io dlt connector.

All runnable without a live Raindrop token: the API is served by an in-memory
fake through ``httpx.MockTransport``, and the sync logic runs against a plain
dict standing in for dlt's resource state.
"""

import httpx
import pytest
from fake_raindrop import FakeRaindrop, bookmark

from cognee_community_connector_raindrop import raindrop
from cognee_community_connector_raindrop.raindrop import (
    RAINDROP_SOURCE_NAME,
    _api_get,
    _collection_paths,
    _fetch_page_text,
    _iter_raindrops,
    _render_bookmark,
    _retry_delay,
    raindrop_source,
    sync_bookmarks,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retries must not slow the suite down; record the delays instead."""
    delays = []
    monkeypatch.setattr(raindrop.time, "sleep", delays.append)
    return delays


def _sync(api, state, **kwargs):
    return list(sync_bookmarks(api.client(), state, **kwargs))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_bookmark_includes_annotations_and_context():
    item = bookmark(
        1,
        "Attention Is All You Need",
        tags=["ml", "papers"],
        excerpt="The transformer paper.",
        note="Re-read section 3.",
        highlights=[
            {"_id": "h1", "text": "Self-attention relates positions.", "note": "key idea"},
            {"_id": "h2", "text": "  ", "note": "empty highlight is skipped"},
        ],
    )

    content = _render_bookmark(item, "Research/AI", include_highlights=True)

    assert content.startswith("# Attention Is All You Need")
    assert "URL: https://example.com/1" in content
    assert "Collection: Research/AI" in content
    assert "Tags: ml, papers" in content
    assert "The transformer paper." in content
    assert "Re-read section 3." in content
    assert "> Self-attention relates positions.\nNote: key idea" in content
    assert "empty highlight is skipped" not in content
    # Volatile metadata stays out so it cannot churn the fingerprint.
    assert "lastUpdate" not in content and item["lastUpdate"] not in content


def test_render_bookmark_can_leave_out_highlights():
    item = bookmark(1, "Page", highlights=[{"_id": "h1", "text": "quoted"}])
    assert "quoted" not in _render_bookmark(item, "", include_highlights=False)


def test_render_bookmark_falls_back_to_link_for_untitled():
    item = bookmark(1, "")
    assert _render_bookmark(item, "", include_highlights=True).startswith("# https://example.com/1")


def test_collection_paths_resolve_nested_breadcrumbs():
    api = FakeRaindrop()
    api.collections = [{"_id": 1, "title": "Research"}]
    api.children = [
        {"_id": 2, "title": "AI", "parent": {"$id": 1}},
        {"_id": 3, "title": "Papers", "parent": {"$id": 2}},
    ]

    paths = _collection_paths(api.client())

    assert paths[1] == "Research"
    assert paths[3] == "Research/AI/Papers"
    assert paths[-1] == "Unsorted"


# ---------------------------------------------------------------------------
# Pagination and scope
# ---------------------------------------------------------------------------


def test_iter_raindrops_follows_pages_until_a_short_page():
    api = FakeRaindrop()
    for i in range(1, 54):
        api.put(bookmark(i, f"b{i}"))

    ids = [item["_id"] for item in _iter_raindrops(api.client(), None, True)]

    assert ids == list(range(1, 54))
    pages = [r.url.params["page"] for r in api.requests]
    assert pages == ["0", "1"]
    assert all(r.url.params["perpage"] == "50" for r in api.requests)


def test_iter_raindrops_scoped_collections_use_nested_and_dedupe():
    api = FakeRaindrop()
    api.children = [{"_id": 2, "title": "Child", "parent": {"$id": 1}}]
    api.put(bookmark(10, "in parent", collection_id=1))
    api.put(bookmark(11, "in child", collection_id=2))

    # Collection 2 is covered twice (directly, and nested under 1).
    ids = [item["_id"] for item in _iter_raindrops(api.client(), [1, 2], True)]

    assert sorted(ids) == [10, 11]
    assert {r.url.params.get("nested") for r in api.requests} == {"true"}


def test_iter_raindrops_without_nested_stays_in_the_collection():
    api = FakeRaindrop()
    api.children = [{"_id": 2, "title": "Child", "parent": {"$id": 1}}]
    api.put(bookmark(10, "in parent", collection_id=1))
    api.put(bookmark(11, "in child", collection_id=2))

    ids = [item["_id"] for item in _iter_raindrops(api.client(), [1], False)]

    assert ids == [10]


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def test_rate_limited_request_waits_and_retries(no_sleep):
    api = FakeRaindrop()
    api.failures = [(429, {"Retry-After": "7"}), (503, {})]

    data = _api_get(api.client(), "/collections")

    assert data["result"] is True
    assert no_sleep == [7.0, 2.0]  # Retry-After, then backoff for attempt 1


def test_permanent_error_is_not_retried(no_sleep):
    api = FakeRaindrop()
    api.failures = [(401, {})]

    with pytest.raises(httpx.HTTPStatusError):
        _api_get(api.client(), "/collections")
    assert no_sleep == []


def test_retries_give_up_after_the_budget():
    api = FakeRaindrop()
    api.failures = [(429, {})] * 10

    with pytest.raises(httpx.HTTPStatusError):
        _api_get(api.client(), "/collections")
    assert len(api.requests) == raindrop._MAX_RETRIES


def test_retry_delay_reads_rate_limit_reset(monkeypatch):
    monkeypatch.setattr(raindrop.time, "time", lambda: 1000.0)
    assert _retry_delay({"X-RateLimit-Reset": "1010"}, attempt=0) == 11.0
    # A reset far in the future is capped rather than stalling the sync.
    assert _retry_delay({"X-RateLimit-Reset": "99999"}, attempt=0) == raindrop._MAX_RETRY_DELAY
    assert _retry_delay({}, attempt=3) == 8.0


# ---------------------------------------------------------------------------
# Incremental sync and forget-on-delete
# ---------------------------------------------------------------------------


def test_first_run_backfills_and_second_run_is_a_no_op():
    api = FakeRaindrop()
    api.put(bookmark(1, "one"))
    api.put(bookmark(2, "two"))
    state = {}

    first = _sync(api, state)
    second = _sync(api, state)

    assert [row["id"] for row in first] == ["1", "2"]
    assert all(row["_deleted"] is False for row in first)
    assert second == []


def test_only_changed_bookmarks_are_re_emitted():
    api = FakeRaindrop()
    api.put(bookmark(1, "one"))
    api.put(bookmark(2, "two"))
    state = {}
    _sync(api, state)

    # A new highlight on bookmark 2 is a content change; bookmark 1 is untouched.
    api.bookmarks[2]["highlights"] = [{"_id": "h1", "text": "a fresh highlight"}]
    rows = _sync(api, state)

    assert [row["id"] for row in rows] == ["2"]
    assert "a fresh highlight" in rows[0]["content"]


def test_renaming_a_collection_re_emits_its_bookmarks():
    # The rename does not touch the bookmarks' lastUpdate, but their documents
    # carry the collection path, so the fingerprint still catches it.
    api = FakeRaindrop()
    api.put(bookmark(1, "one", collection_id=1))
    state = {}
    _sync(api, state)

    api.collections = [{"_id": 1, "title": "Renamed"}]
    rows = _sync(api, state)

    assert [row["id"] for row in rows] == ["1"]
    assert "Collection: Renamed" in rows[0]["content"]


def test_collection_path_uses_the_live_collection_id_field():
    api = FakeRaindrop()
    api.collections = [{"_id": 1, "title": "Research"}]
    api.put(bookmark(1, "live shape", collection_id=1))
    # The documented shape (collection.$id, no collectionId) still works.
    documented = bookmark(2, "documented shape")
    del documented["collectionId"]
    documented["collection"] = {"$id": 1}
    api.put(documented)

    rows = _sync(api, {})

    assert all("Collection: Research" in row["content"] for row in rows)
    assert len(rows) == 2


def test_bookmark_flagged_removed_is_tombstoned():
    api = FakeRaindrop()
    api.put(bookmark(1, "one"))
    api.put(bookmark(2, "two"))
    state = {}
    _sync(api, state)

    api.bookmarks[2]["removed"] = True
    rows = _sync(api, state)

    assert rows == [{"id": "2", "_deleted": True}]


def test_trashed_and_removed_bookmarks_are_tombstoned():
    api = FakeRaindrop()
    for i in (1, 2, 3):
        api.put(bookmark(i, f"b{i}"))
    state = {}
    _sync(api, state)

    api.trash_bookmark(2)  # moved to Trash
    del api.bookmarks[3]  # permanently deleted
    rows = _sync(api, state)

    assert rows == [{"id": "2", "_deleted": True}, {"id": "3", "_deleted": True}]
    assert set(state["fingerprints"]) == {"1"}
    # Tombstones are not repeated on the next run.
    assert _sync(api, state) == []


def test_restored_bookmark_is_ingested_again():
    api = FakeRaindrop()
    api.put(bookmark(1, "one"))
    api.put(bookmark(2, "two"))
    state = {}
    _sync(api, state)
    api.trash_bookmark(1)
    _sync(api, state)

    api.put(api.trash.pop(1))
    rows = _sync(api, state)

    assert [row["id"] for row in rows] == ["1"]
    assert rows[0]["_deleted"] is False


def test_empty_sweep_does_not_forget_everything():
    api = FakeRaindrop()
    api.put(bookmark(1, "one"))
    state = {}
    _sync(api, state)
    fingerprints = dict(state["fingerprints"])

    api.bookmarks.clear()  # e.g. a mis-scoped or glitched listing
    rows = _sync(api, state)

    assert rows == []
    assert state["fingerprints"] == fingerprints


def test_failed_listing_raises_and_keeps_state():
    api = FakeRaindrop()
    api.put(bookmark(1, "one"))
    state = {}
    _sync(api, state)
    fingerprints = dict(state["fingerprints"])

    api.failures = [(401, {})]
    with pytest.raises(httpx.HTTPStatusError):
        _sync(api, state)
    assert state["fingerprints"] == fingerprints


# ---------------------------------------------------------------------------
# Linked page content (opt-in)
# ---------------------------------------------------------------------------


def test_page_content_is_fetched_only_for_new_or_changed_bookmarks():
    api = FakeRaindrop()
    api.put(bookmark(1, "one"))
    api.put(bookmark(2, "two"))
    fetched = []

    def fetch_page(url):
        fetched.append(url)
        return f"text of {url}"

    state = {}
    first = _sync(api, state, fetch_page=fetch_page)
    api.bookmarks[1]["note"] = "edited"
    second = _sync(api, state, fetch_page=fetch_page)

    assert "## Page content\n\ntext of https://example.com/1" in first[0]["content"]
    assert fetched == [
        "https://example.com/1",
        "https://example.com/2",
        "https://example.com/1",
    ]
    assert [row["id"] for row in second] == ["1"]


def _page_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_fetch_page_text_strips_html_and_caps_length():
    seen = []

    def handler(request):
        seen.append(request)
        body = "<html><script>var x = 1;</script><body><h1>Hi</h1><p>there &amp; all</p></body>"
        return httpx.Response(200, headers={"content-type": "text/html"}, text=body)

    text = _fetch_page_text(_page_client(handler), "https://example.com/a", max_chars=8)

    assert text == "Hi there"
    # The page fetch must never carry the Raindrop token.
    assert "authorization" not in seen[0].headers


def test_fetch_page_text_skips_errors_binaries_and_odd_schemes():
    def handler(request):
        if request.url.path == "/missing":
            return httpx.Response(404)
        if request.url.path == "/boom":
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    client = _page_client(handler)

    assert _fetch_page_text(client, "https://example.com/missing", 100) == ""
    assert _fetch_page_text(client, "https://example.com/boom", 100) == ""
    assert _fetch_page_text(client, "https://example.com/file.pdf", 100) == ""
    assert _fetch_page_text(client, "file:///etc/passwd", 100) == ""


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def test_source_requires_a_token(monkeypatch):
    monkeypatch.delenv("RAINDROP_TOKEN", raising=False)
    with pytest.raises(ValueError, match="RAINDROP_TOKEN"):
        raindrop_source()


def test_source_rejects_non_positive_page_cap():
    with pytest.raises(ValueError, match="max_page_chars"):
        raindrop_source(token="t", max_page_chars=0)


def test_source_is_a_document_mode_merge_resource():
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    resource = raindrop_source(token="t")

    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == RAINDROP_SOURCE_NAME
    assert resource.write_disposition == "merge"
    assert resource.columns["_deleted"]["hard_delete"] is True


def test_make_client_sends_the_token_as_bearer():
    client = raindrop._make_client("secret-token")
    assert client.headers["Authorization"] == "Bearer secret-token"
    assert str(client.base_url).rstrip("/") == raindrop.API_BASE_URL
