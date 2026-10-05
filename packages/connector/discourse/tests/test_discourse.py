"""Unit tests for the Discourse dlt connector.

Two layers, all runnable in CI without a live forum:

* DB-free tests for listing pagination, ``/raw`` paging, topic→row flattening,
  the HTTP retry policy (via ``httpx.MockTransport``), and the generic document
  DataItem tagging (``source="discourse"``) that routes topics through cognify.
* dlt-pipeline tests (fake client, temp sqlite destination) covering the
  acceptance criteria: re-sync reflects edits, deleted topics drop out of the
  full-snapshot load (forget-on-delete), and errors abort instead of loading a
  partial snapshot.
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_discourse.discourse import (
    DISCOURSE_SOURCE_NAME,
    RAW_PAGE_SIZE,
    DiscourseAPIError,
    _fetch_topic_markdown,
    _iter_topics,
    _listing_paths,
    _paginate_listing,
    _topic_to_row,
)

BASE_URL = "https://forum.example.com"

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _topic(
    topic_id,
    title="Topic",
    slug=None,
    archetype="regular",
    posts_count=1,
    bumped_at="2024-01-01T00:00:00.000Z",
):
    return {
        "id": topic_id,
        "title": title,
        "slug": slug or f"topic-{topic_id}",
        "archetype": archetype,
        "posts_count": posts_count,
        "highest_post_number": posts_count,
        "bumped_at": bumped_at,
        "views": 10,
    }


def _post(number, body, user="alice"):
    """One post exactly as /raw/{id} renders it."""
    return f"{user} | 2024-01-01 00:00:00 UTC | #{number}\n\n{body}\n\n-------------------------\n"


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text
        self.headers = {}

    def json(self):
        return self._body


class FakeDiscourseClient:
    """Stand-in for DiscourseClient backed by in-memory fixtures.

    ``listings`` maps a listing path to a list of pages (each a list of topics).
    ``raw`` maps a topic id to a list of /raw page texts, or to an int status.
    """

    def __init__(self, listings, raw):
        self.base_url = BASE_URL
        self.listings = listings
        self.raw = raw
        self.calls = []

    def get(self, path, params=None):
        page = (params or {}).get("page")
        self.calls.append((path, page))
        if path.startswith("/raw/"):
            entry = self.raw.get(int(path.rsplit("/", 1)[1]), 404)
            if isinstance(entry, int):
                return FakeResponse(entry, {"errors": ["gone"]})
            index = (page or 1) - 1
            return FakeResponse(text=entry[index] if index < len(entry) else "\n")
        pages = self.listings.get(path)
        if pages is None:
            return FakeResponse(404, {"errors": ["not found"]})
        topics = pages[page] if page < len(pages) else []
        more = f"{path}?page={page + 1}" if page + 1 < len(pages) else None
        return FakeResponse(body={"topic_list": {"topics": topics, "more_topics_url": more}})


# ---------------------------------------------------------------------------
# Listing (DB-free)
# ---------------------------------------------------------------------------


def test_listing_paths_default_to_latest():
    assert _listing_paths(None, None) == ["/latest.json"]


def test_listing_paths_cover_categories_and_tags():
    assert _listing_paths([5, 7], ["howto"]) == ["/c/5.json", "/c/7.json", "/tag/howto.json"]


def test_paginate_listing_follows_more_topics_url():
    client = FakeDiscourseClient({"/latest.json": [[_topic(1)], [_topic(2)]]}, {})

    assert [t["id"] for t in _paginate_listing(client, "/latest.json")] == [1, 2]
    # Stops after the last advertised page instead of asking past the end.
    assert client.calls == [("/latest.json", 0), ("/latest.json", 1)]


def test_paginate_listing_raises_on_error_status():
    client = FakeDiscourseClient({}, {})
    with pytest.raises(DiscourseAPIError) as excinfo:
        list(_paginate_listing(client, "/c/99.json"))
    assert excinfo.value.status == 404


def test_iter_topics_dedupes_across_scopes_and_skips_private_messages():
    client = FakeDiscourseClient(
        {
            "/c/5.json": [[_topic(1), _topic(2)]],
            "/tag/howto.json": [[_topic(2), _topic(3, archetype="private_message")]],
        },
        {},
    )
    assert [t["id"] for t in _iter_topics(client, [5], ["howto"])] == [1, 2]


# ---------------------------------------------------------------------------
# /raw fetching (DB-free)
# ---------------------------------------------------------------------------


def test_fetch_topic_markdown_single_page():
    client = FakeDiscourseClient({}, {1: [_post(1, "question") + _post(2, "answer")]})

    content = _fetch_topic_markdown(client, 1)

    assert "question" in content
    assert "answer" in content
    # A short page means it was the last one: no second request.
    assert client.calls == [("/raw/1", None)]


def test_fetch_topic_markdown_follows_full_pages():
    full_page = "".join(_post(n, f"post {n}") for n in range(1, RAW_PAGE_SIZE + 1))
    client = FakeDiscourseClient({}, {1: [full_page, _post(101, "last post")]})

    content = _fetch_topic_markdown(client, 1)

    assert "post 1\n" in content
    assert "last post" in content
    assert client.calls == [("/raw/1", None), ("/raw/1", 2)]


def test_fetch_topic_markdown_stops_on_empty_page():
    # Exactly RAW_PAGE_SIZE posts: page 2 exists but comes back empty.
    full_page = "".join(_post(n, f"post {n}") for n in range(1, RAW_PAGE_SIZE + 1))
    client = FakeDiscourseClient({}, {1: [full_page]})

    assert _fetch_topic_markdown(client, 1).count("-------------------------") == RAW_PAGE_SIZE
    assert client.calls == [("/raw/1", None), ("/raw/1", 2)]


@pytest.mark.parametrize("status", [403, 404])
def test_fetch_topic_markdown_returns_none_when_gone(status):
    client = FakeDiscourseClient({}, {1: status})
    assert _fetch_topic_markdown(client, 1) is None


def test_fetch_topic_markdown_raises_on_other_errors():
    client = FakeDiscourseClient({}, {1: 500})
    with pytest.raises(DiscourseAPIError):
        _fetch_topic_markdown(client, 1)


# ---------------------------------------------------------------------------
# Topic → row / DataItem (DB-free)
# ---------------------------------------------------------------------------


def test_topic_to_row_flattens_topic():
    row = _topic_to_row(BASE_URL, _topic(42, "How do I X?", slug="how-do-i-x"), "body")

    assert row == {
        "id": "42",
        "url": f"{BASE_URL}/t/how-do-i-x/42",
        "title": "How do I X?",
        "content": "body",
    }
    # Activity counters are dropped so they can't churn the content-hash data_id.
    assert "views" not in row
    assert "bumped_at" not in row


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data=_topic_to_row(BASE_URL, _topic(42, "How do I X?"), "body text"),
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "42")

    item = _build_document_data_item(row, data_id, "discourse")

    assert item.external_metadata["source"] == "discourse"
    assert item.external_metadata["external_id"] == "42"
    assert item.external_metadata["url"] == f"{BASE_URL}/t/topic-42/42"
    assert item.data.startswith("# How do I X?")
    assert "body text" in item.data


def test_discourse_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_discourse.discourse import discourse_source

    source = discourse_source(base_url=BASE_URL)
    assert DISCOURSE_SOURCE_NAME == "discourse"
    assert document_source_tag(source) == "discourse"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_base_url_is_required(monkeypatch):
    from cognee_community_connector_discourse.discourse import discourse_source

    monkeypatch.delenv("DISCOURSE_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="DISCOURSE_BASE_URL"):
        discourse_source()


def test_api_key_requires_username(monkeypatch):
    from cognee_community_connector_discourse.discourse import discourse_source

    monkeypatch.delenv("DISCOURSE_API_USERNAME", raising=False)
    with pytest.raises(ValueError, match="api_username"):
        discourse_source(base_url=BASE_URL, api_key="secret")


def test_settings_fall_back_to_env(monkeypatch):
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_discourse.discourse import discourse_source

    monkeypatch.setenv("DISCOURSE_BASE_URL", BASE_URL)
    monkeypatch.setenv("DISCOURSE_API_KEY", "secret")
    monkeypatch.setenv("DISCOURSE_API_USERNAME", "system")
    assert document_source_tag(discourse_source()) == "discourse"


# ---------------------------------------------------------------------------
# HTTP client retry policy (httpx.MockTransport, no network)
# ---------------------------------------------------------------------------


@pytest.fixture
def no_sleep(monkeypatch):
    from cognee_community_connector_discourse import discourse

    sleeps = []
    monkeypatch.setattr(discourse.time, "sleep", sleeps.append)
    return sleeps


def _client_with(handler):
    import httpx

    from cognee_community_connector_discourse.discourse import DiscourseClient

    return DiscourseClient(
        BASE_URL, api_key="k", api_username="u", transport=httpx.MockTransport(handler)
    )


def test_client_retries_rate_limit_honouring_retry_after(no_sleep):
    import httpx

    responses = iter(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})]
    )
    client = _client_with(lambda request: next(responses))

    assert client.get("/latest.json").status_code == 200
    assert no_sleep == [7.0]


def test_client_retries_transport_errors(no_sleep):
    import httpx

    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={})

    assert _client_with(handler).get("/latest.json").status_code == 200
    assert len(attempts) == 2


def test_client_does_not_retry_client_errors(no_sleep):
    import httpx

    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(403, json={"errors": ["not allowed"]})

    assert _client_with(handler).get("/latest.json").status_code == 403
    assert len(attempts) == 1


def test_client_gives_up_after_max_retries(no_sleep):
    import httpx

    client = _client_with(lambda request: httpx.Response(503))
    # The final response is returned for the caller to turn into an error.
    assert client.get("/latest.json").status_code == 503
    assert len(no_sleep) == 4


def test_client_sends_api_auth_headers(no_sleep):
    import httpx

    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={})

    _client_with(handler).get("/latest.json")
    assert seen["api-key"] == "k"
    assert seen["api-username"] == "u"


def test_auth_error_message_points_at_credentials():
    client = SimpleNamespace(get=lambda path, params=None: FakeResponse(403, {"errors": ["no"]}))

    with pytest.raises(DiscourseAPIError, match="API key"):
        list(_paginate_listing(client, "/latest.json"))


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete
# ---------------------------------------------------------------------------


def _run_sync(dlt, tmp_path, client, **kwargs):
    """Run discourse_source through a dlt pipeline into a temp sqlite destination.

    Repeated calls with the same ``tmp_path`` reuse the pipeline, and therefore
    its resource state, exactly like consecutive cognee syncs.
    """
    from cognee_community_connector_discourse.discourse import discourse_source

    db_path = (tmp_path / "discourse.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="discourse_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="discourse_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(discourse_source(client=client, **kwargs))
    return pipeline


def _raw_fetches(client):
    """Topic ids of every /raw request the client made, in order."""
    return [int(path.rsplit("/", 1)[1]) for path, page in client.calls if path.startswith("/raw/")]


def _read_topics(pipeline):
    """Return {id: row-dict} for the discourse_topics table (positional read)."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM discourse_topics") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_topics_with_content(dlt_mod, tmp_path):
    client = FakeDiscourseClient(
        {"/latest.json": [[_topic(1, "Alpha"), _topic(2, "Beta")]]},
        {1: [_post(1, "alpha body")], 2: [_post(1, "beta body")]},
    )

    rows = _read_topics(_run_sync(dlt_mod, tmp_path, client))

    assert set(rows) == {"1", "2"}
    assert "alpha body" in rows["1"]["content"]
    assert rows["2"]["title"] == "Beta"


def test_new_reply_is_reflected_on_resync(dlt_mod, tmp_path):
    first = {"/latest.json": [[_topic(1, "Alpha")]]}
    _run_sync(dlt_mod, tmp_path, FakeDiscourseClient(first, {1: [_post(1, "v1")]}))

    # A reply bumps the topic and raises posts_count, so the signature moves.
    bumped = {"/latest.json": [[_topic(1, "Alpha", posts_count=2, bumped_at="2024-02-01")]]}
    pipeline = _run_sync(
        dlt_mod, tmp_path, FakeDiscourseClient(bumped, {1: [_post(1, "v2") + _post(2, "reply")]})
    )

    rows = _read_topics(pipeline)
    assert "v2" in rows["1"]["content"]
    assert "v1" not in rows["1"]["content"]


def test_deleted_topic_is_removed_on_resync(dlt_mod, tmp_path):
    raw = {1: [_post(1, "a")], 2: [_post(1, "b")]}
    _run_sync(
        dlt_mod,
        tmp_path,
        FakeDiscourseClient({"/latest.json": [[_topic(1), _topic(2)]]}, raw),
    )

    # Topic 1 deleted upstream: it no longer appears in the listing.
    pipeline = _run_sync(
        dlt_mod, tmp_path, FakeDiscourseClient({"/latest.json": [[_topic(2)]]}, raw)
    )

    rows = _read_topics(pipeline)
    # Absent from staging → orphan cleanup forgets it downstream.
    assert "1" not in rows
    assert "2" in rows


def test_topic_gone_between_listing_and_fetch_is_skipped(dlt_mod, tmp_path):
    client = FakeDiscourseClient(
        {"/latest.json": [[_topic(1), _topic(2)]]},
        {1: 404, 2: [_post(1, "still here")]},
    )

    rows = _read_topics(_run_sync(dlt_mod, tmp_path, client))

    assert set(rows) == {"2"}


def test_fetch_error_aborts_sync(dlt_mod, tmp_path):
    # A server error must abort the run (leaving staging/memory intact), not
    # commit a partial snapshot that orphan cleanup reconciles as a deletion.
    raw = {1: [_post(1, "a")], 2: [_post(1, "b")]}
    pipeline = _run_sync(
        dlt_mod,
        tmp_path,
        FakeDiscourseClient({"/latest.json": [[_topic(1), _topic(2)]]}, raw),
    )

    # Topic 1 got a reply (so it must be re-fetched) and the fetch fails.
    broken = FakeDiscourseClient(
        {"/latest.json": [[_topic(1, posts_count=2, bumped_at="2024-02-01"), _topic(2)]]},
        {1: 500, 2: raw[2]},
    )
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        _run_sync(dlt_mod, tmp_path, broken)

    # The previous snapshot is untouched.
    assert set(_read_topics(pipeline)) == {"1", "2"}


# ---------------------------------------------------------------------------
# Incremental fetch: signatures, cache, periodic full refresh
# ---------------------------------------------------------------------------


def test_signature_uses_listing_change_markers():
    from cognee_community_connector_discourse.discourse import _signature

    topic = _topic(1, posts_count=3, bumped_at="2024-05-01")
    assert _signature(topic) == ["2024-05-01", 3, 3]


def test_cache_key_is_order_independent_and_scope_specific():
    from cognee_community_connector_discourse.discourse import _cache_key

    assert _cache_key(BASE_URL, [7, 5], ["b", "a"]) == _cache_key(BASE_URL, [5, 7], ["a", "b"])
    assert _cache_key(BASE_URL, [5], None) != _cache_key(BASE_URL, [6], None)
    assert _cache_key(BASE_URL, None, None) != _cache_key("https://other.example", None, None)


@pytest.mark.parametrize(
    ("cache", "incremental", "every", "force", "expected"),
    [
        ({}, True, 7, False, True),  # nothing cached yet
        ({"topics": {"1": {}}, "syncs_since_full_refresh": 0}, True, 7, False, False),
        ({"topics": {"1": {}}, "syncs_since_full_refresh": 6}, True, 7, False, True),
        ({"topics": {"1": {}}, "syncs_since_full_refresh": 99}, True, None, False, False),
        ({"topics": {"1": {}}, "syncs_since_full_refresh": 0}, True, 7, True, True),
        ({"topics": {"1": {}}, "syncs_since_full_refresh": 0}, False, 7, False, True),
    ],
)
def test_is_full_refresh(cache, incremental, every, force, expected):
    from cognee_community_connector_discourse.discourse import _is_full_refresh

    assert _is_full_refresh(cache, incremental, every, force) is expected


def test_full_refresh_every_must_be_positive():
    from cognee_community_connector_discourse.discourse import discourse_source

    with pytest.raises(ValueError, match="full_refresh_every"):
        discourse_source(base_url=BASE_URL, full_refresh_every=0)


def _two_topic_forum(**topic_overrides):
    listings = {"/latest.json": [[_topic(1, "Alpha", **topic_overrides), _topic(2, "Beta")]]}
    raw = {1: [_post(1, "alpha body")], 2: [_post(1, "beta body")]}
    return listings, raw


def test_unchanged_topics_are_not_refetched(dlt_mod, tmp_path):
    listings, raw = _two_topic_forum()
    first = FakeDiscourseClient(listings, raw)
    _run_sync(dlt_mod, tmp_path, first)
    assert _raw_fetches(first) == [1, 2]

    second = FakeDiscourseClient(listings, raw)
    pipeline = _run_sync(dlt_mod, tmp_path, second)

    # Nothing changed: no bodies fetched, yet the snapshot is still complete.
    assert _raw_fetches(second) == []
    rows = _read_topics(pipeline)
    assert set(rows) == {"1", "2"}
    assert "alpha body" in rows["1"]["content"]


def test_only_changed_topics_are_refetched(dlt_mod, tmp_path):
    listings, raw = _two_topic_forum()
    _run_sync(dlt_mod, tmp_path, FakeDiscourseClient(listings, raw))

    listings, _ = _two_topic_forum(posts_count=2, bumped_at="2024-02-01")
    raw[1] = [_post(1, "alpha body") + _post(2, "new reply")]
    client = FakeDiscourseClient(listings, raw)
    rows = _read_topics(_run_sync(dlt_mod, tmp_path, client))

    assert _raw_fetches(client) == [1]
    assert "new reply" in rows["1"]["content"]


def test_deleted_reply_triggers_refetch(dlt_mod, tmp_path):
    listings, raw = _two_topic_forum(posts_count=2)
    raw[1] = [_post(1, "question") + _post(2, "spam reply")]
    _run_sync(dlt_mod, tmp_path, FakeDiscourseClient(listings, raw))

    # A moderator deletes the reply: posts_count drops, the signature moves.
    listings, _ = _two_topic_forum(posts_count=1)
    raw[1] = [_post(1, "question")]
    client = FakeDiscourseClient(listings, raw)
    rows = _read_topics(_run_sync(dlt_mod, tmp_path, client))

    assert _raw_fetches(client) == [1]
    assert "spam reply" not in rows["1"]["content"]


def test_title_change_is_applied_without_refetch(dlt_mod, tmp_path):
    listings, raw = _two_topic_forum()
    _run_sync(dlt_mod, tmp_path, FakeDiscourseClient(listings, raw))

    renamed = {"/latest.json": [[_topic(1, "Alpha (solved)"), _topic(2, "Beta")]]}
    client = FakeDiscourseClient(renamed, raw)
    rows = _read_topics(_run_sync(dlt_mod, tmp_path, client))

    # Title comes from the listing every run; only the body is cached.
    assert _raw_fetches(client) == []
    assert rows["1"]["title"] == "Alpha (solved)"


def test_silent_edit_waits_for_full_refresh(dlt_mod, tmp_path):
    # An edit to an existing post leaves the signature unchanged: incremental
    # runs keep the cached body until the periodic full refresh re-fetches it.
    listings, raw = _two_topic_forum()
    _run_sync(dlt_mod, tmp_path, FakeDiscourseClient(listings, raw), full_refresh_every=2)

    raw[1] = [_post(1, "alpha body, edited")]
    rows = _read_topics(
        _run_sync(dlt_mod, tmp_path, FakeDiscourseClient(listings, raw), full_refresh_every=2)
    )
    assert "edited" not in rows["1"]["content"]

    client = FakeDiscourseClient(listings, raw)
    rows = _read_topics(_run_sync(dlt_mod, tmp_path, client, full_refresh_every=2))
    assert _raw_fetches(client) == [1, 2]
    assert "edited" in rows["1"]["content"]


def test_full_refresh_every_nth_run(dlt_mod, tmp_path):
    listings, raw = _two_topic_forum()
    fetches_per_run = []
    for _ in range(7):
        client = FakeDiscourseClient(listings, raw)
        _run_sync(dlt_mod, tmp_path, client, full_refresh_every=3)
        fetches_per_run.append(len(_raw_fetches(client)))

    # Run 1 has no cache, then every 3rd run re-fetches everything.
    assert fetches_per_run == [2, 0, 0, 2, 0, 0, 2]


def test_force_full_refresh_refetches_everything(dlt_mod, tmp_path):
    listings, raw = _two_topic_forum()
    _run_sync(dlt_mod, tmp_path, FakeDiscourseClient(listings, raw))

    client = FakeDiscourseClient(listings, raw)
    _run_sync(dlt_mod, tmp_path, client, force_full_refresh=True)

    assert _raw_fetches(client) == [1, 2]


def test_incremental_disabled_always_fetches(dlt_mod, tmp_path):
    listings, raw = _two_topic_forum()
    for _ in range(2):
        client = FakeDiscourseClient(listings, raw)
        _run_sync(dlt_mod, tmp_path, client, incremental=False)
        assert _raw_fetches(client) == [1, 2]


def test_deleted_topic_leaves_the_cache(dlt_mod, tmp_path):
    listings, raw = _two_topic_forum()
    _run_sync(dlt_mod, tmp_path, FakeDiscourseClient(listings, raw))
    _run_sync(dlt_mod, tmp_path, FakeDiscourseClient({"/latest.json": [[_topic(2)]]}, raw))

    # Topic 1 comes back (e.g. restored by a moderator): it was evicted from the
    # cache when it disappeared, so its body is fetched fresh.
    client = FakeDiscourseClient(listings, raw)
    rows = _read_topics(_run_sync(dlt_mod, tmp_path, client))

    assert _raw_fetches(client) == [1]
    assert set(rows) == {"1", "2"}


def test_failed_run_keeps_previous_cache(dlt_mod, tmp_path):
    listings, raw = _two_topic_forum()
    _run_sync(dlt_mod, tmp_path, FakeDiscourseClient(listings, raw))

    bumped, _ = _two_topic_forum(posts_count=2, bumped_at="2024-02-01")
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        _run_sync(dlt_mod, tmp_path, FakeDiscourseClient(bumped, {1: 500, 2: raw[2]}))

    # The aborted run committed nothing: back on the original listing, the
    # first run's cache still serves both topics.
    client = FakeDiscourseClient(listings, raw)
    _run_sync(dlt_mod, tmp_path, client)
    assert _raw_fetches(client) == []


def test_caches_are_kept_per_forum_scope(dlt_mod, tmp_path):
    # cognee runs every dlt source through one shared pipeline: syncing a
    # different scope must neither reuse nor wipe another scope's cache.
    listings = {
        "/c/5.json": [[_topic(1)]],
        "/c/6.json": [[_topic(2)]],
    }
    raw = {1: [_post(1, "in five")], 2: [_post(1, "in six")]}
    _run_sync(dlt_mod, tmp_path, FakeDiscourseClient(listings, raw), category_ids=[5])

    other = FakeDiscourseClient(listings, raw)
    _run_sync(dlt_mod, tmp_path, other, category_ids=[6])
    assert _raw_fetches(other) == [2]

    again = FakeDiscourseClient(listings, raw)
    _run_sync(dlt_mod, tmp_path, again, category_ids=[5])
    assert _raw_fetches(again) == []
