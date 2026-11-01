"""Tests for the Hacker News dlt connector.

Three layers, all deterministic (no live network):

* Unit tests with an injected fake HTTP client: topic filtering, Algolia
  query shape, pagination, thread rendering, HTML cleaning, cursor state,
  duplicate prevention, and the document-mode metadata tagging.
* dlt-pipeline tests (temp sqlite destination) covering the acceptance
  criteria: first sync, resync picks up new items, unchanged rows stay
  byte-identical, and vanished items drop out of the snapshot
  (forget-on-delete).
* Error tests: transient failures are retried, persistent failures abort the
  run (a partial snapshot must never drive deletions under ``replace``).
"""

import time
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest

from cognee_community_connector_hacker_news.hacker_news import (
    _ALGOLIA_SEARCH_URL,
    _clean_html,
    _fetch_comments,
    _iter_topic_hits,
    _story_to_row,
    _validate_topics,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _hit(sid, title="Rust 1.0 released", created_i=1700000000, url=None, author="alice"):
    """A minimal Algolia story hit."""
    return {
        "objectID": str(sid),
        "title": title,
        "url": url if url is not None else f"https://example.com/{sid}",
        "author": author,
        "points": 120,
        "num_comments": 3,
        "created_at": "2023-11-14T22:13:20Z",
        "created_at_i": created_i,
        "updated_at": "2023-11-15T10:00:00Z",
        "_tags": ["story", f"story_{sid}"],
    }


def _story_item(sid, kids=None, text=None, by="alice", t=1700000000):
    d = {"id": int(sid), "type": "story", "by": by, "time": t}
    if kids is not None:
        d["kids"] = kids
    if text is not None:
        d["text"] = text
    return d


def _comment_item(cid, text="<p>Hello world</p>", by="bob", t=1700001000, kids=None):
    d = {"id": int(cid), "type": "comment", "by": by, "time": t, "text": text, "parent": 1}
    if kids:
        d["kids"] = kids
    return d


def _http_error(status):
    import requests

    resp = requests.Response()
    resp.status_code = status
    resp.url = "https://example.test/"
    resp.headers["Retry-After"] = "0"
    return requests.HTTPError(f"{status} Server Error", response=resp)


class FakeHnHttp:
    """In-memory stand-in for the Algolia + Firebase HTTP APIs.

    Routes on the URL prefix, records every call, and can fail transiently
    (``fail="transient"``: first two calls 500, then succeed) or permanently
    (``fail="permanent"``) on the chosen API kind.
    """

    def __init__(self, hits_by_topic=None, items=None, fail=None, fail_kind="algolia"):
        self.hits_by_topic = hits_by_topic or {}
        self.items = items or {}
        self.fail = fail
        self.fail_kind = fail_kind
        self.calls = []
        self._failures_left = 2

    def get_json(self, url, params=None):
        kind = "algolia" if url.startswith(_ALGOLIA_SEARCH_URL) else "firebase"
        self.calls.append((kind, url, params))
        if self.fail and kind == self.fail_kind:
            if self.fail == "transient" and self._failures_left > 0:
                self._failures_left -= 1
                raise _http_error(500)
            if self.fail == "permanent":
                raise _http_error(500)
        if kind == "algolia":
            return self._algolia(params)
        item_id = url.rsplit("/", 1)[-1].removesuffix(".json")
        return self.items.get(item_id)

    def _algolia(self, params):
        hits = self.hits_by_topic.get(params["query"], [])
        per_page = params.get("hitsPerPage", 100)
        page = params.get("page", 0)
        chunk = hits[page * per_page : (page + 1) * per_page]
        nb_pages = max(1, -(-len(hits) // per_page))
        return {"hits": chunk, "nbHits": len(hits), "nbPages": nb_pages, "page": page}

    def algolia_calls(self):
        return [c for c in self.calls if c[0] == "algolia"]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Retries must not slow the suite; failure semantics are asserted, not timing.
    monkeypatch.setattr(time, "sleep", lambda s: None)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_topics_required():
    from cognee_community_connector_hacker_news import hacker_news_source

    with pytest.raises(ValueError):
        hacker_news_source([])
    with pytest.raises(ValueError):
        hacker_news_source(["   "])


def test_validate_topics_strips_and_rejects_empty():
    assert _validate_topics([" rust ", "AI agents"]) == ["rust", "AI agents"]
    with pytest.raises(ValueError):
        _validate_topics([])


# ---------------------------------------------------------------------------
# Discovery: topic filtering, pagination, query shape
# ---------------------------------------------------------------------------


def test_search_sends_topic_filtered_query():
    http = FakeHnHttp(hits_by_topic={"rust": [_hit(1)], "go": [_hit(2)]})
    hits = list(_iter_topic_hits(http, "rust", since_i=1699999999, max_stories=25))

    assert [h["objectID"] for h in hits] == ["1"]
    (_, url, params), *_ = http.algolia_calls()
    assert url == _ALGOLIA_SEARCH_URL
    assert params["query"] == "rust"
    assert params["tags"] == "story"
    assert params["numericFilters"] == "created_at_i>1699999999"
    # One topic searched once — the other topic's hits are untouched.
    assert len(http.algolia_calls()) == 1


def test_only_matching_topics_ingested():
    http = FakeHnHttp(hits_by_topic={"rust": [_hit(1)], "go": [_hit(2)]})
    rust_hits = list(_iter_topic_hits(http, "rust", since_i=0, max_stories=25))
    go_hits = list(_iter_topic_hits(http, "go", since_i=0, max_stories=25))
    assert [h["objectID"] for h in rust_hits] == ["1"]
    assert [h["objectID"] for h in go_hits] == ["2"]


def test_pagination_until_max_stories():
    # per_page is capped at 100 → 105 hits need two Algolia requests.
    http = FakeHnHttp(
        hits_by_topic={"rust": [_hit(i, created_i=1700000000 + i) for i in range(105)]}
    )
    hits = list(_iter_topic_hits(http, "rust", since_i=0, max_stories=150))
    assert len(hits) == 105
    assert len(http.algolia_calls()) == 2
    assert [c[2]["page"] for c in http.algolia_calls()] == [0, 1]


def test_stops_at_max_stories_without_extra_page():
    http = FakeHnHttp(hits_by_topic={"rust": [_hit(i, created_i=1700000000 + i) for i in range(8)]})
    hits = list(_iter_topic_hits(http, "rust", since_i=0, max_stories=5))
    assert len(hits) == 5
    # First page already satisfied the cap — no second request.
    assert len(http.algolia_calls()) == 1


def test_empty_results_yield_nothing():
    http = FakeHnHttp(hits_by_topic={})
    assert list(_iter_topic_hits(http, "rust", since_i=0, max_stories=25)) == []


def test_malformed_hit_aborts():
    http = FakeHnHttp(hits_by_topic={"rust": [{"objectID": "9"}]})  # no title
    with pytest.raises(ValueError, match="malformed story hit"):
        list(_iter_topic_hits(http, "rust", since_i=0, max_stories=25))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_clean_html_strips_tags_and_unescapes():
    assert _clean_html("<p>Hello &amp; goodbye</p><p>Line2<br>here</p>") == (
        "Hello & goodbye\n\nLine2\nhere"
    )
    assert _clean_html("") == ""


def test_story_to_row_renders_thread():
    hit = _hit(42, title="Ask HN: what editor?", author="alice")
    hit["url"] = None  # Ask HN posts carry no external url
    item = _story_item(42, kids=[101, 102], text="Which editor do you use?")
    comments = [
        {"author": "bob", "time": 1700001000, "text": "Neovim.", "level": 0},
        {"author": "carol", "time": 1700002000, "text": "VS Code.", "level": 1},
    ]
    row = _story_to_row(hit, item, comments, topic="editors")

    assert row["id"] == "hn-42"
    assert row["url"] == "https://news.ycombinator.com/item?id=42"  # Ask HN fallback
    assert row["title"] == "Ask HN: what editor?"
    assert "# Ask HN: what editor?" in row["content"]
    assert "Which editor do you use?" in row["content"]
    assert "## Discussion" in row["content"]
    assert "**bob**" in row["content"] and "Neovim." in row["content"]
    assert "> **carol**" in row["content"]  # nested reply quoted
    # Volatile counters must not leak into content (stable content-hash).
    assert "120" not in row["content"].replace("2023", "")


def test_fetch_comments_skips_gone_and_empty():
    http = FakeHnHttp(
        items={
            "101": _comment_item(101, text="<p>Kept</p>"),
            "102": {"id": 102, "deleted": True},
            "103": _comment_item(103, text="   "),  # empty after cleaning
        }
    )
    story = _story_item(1, kids=[101, 102, 103, 104])  # 104 missing entirely
    comments = _fetch_comments(http, story, max_comments=10, depth=1)
    assert [c["author"] for c in comments] == ["bob"]
    assert comments[0]["text"] == "Kept"


def test_fetch_comments_respects_depth_and_budget():
    http = FakeHnHttp(
        items={
            "201": _comment_item(201, text="top", kids=[202]),
            "202": _comment_item(202, text="nested", kids=[203]),
            "203": _comment_item(203, text="deep"),
        }
    )
    story = _story_item(1, kids=[201])
    assert [c["text"] for c in _fetch_comments(http, story, 10, depth=1)] == ["top"]
    assert [c["text"] for c in _fetch_comments(http, story, 10, depth=2)] == ["top", "nested"]
    assert [c["text"] for c in _fetch_comments(http, story, 1, depth=3)] == ["top"]


# ---------------------------------------------------------------------------
# Document-mode metadata (needs cognee)
# ---------------------------------------------------------------------------


def test_build_document_data_item_tags_source():
    pytest.importorskip("cognee")
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = SimpleNamespace(
        row_data={
            "id": "hn-42",
            "url": "https://news.ycombinator.com/item?id=42",
            "title": "Ask HN",
            "content": "body text",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "hn-42")

    item = _build_document_data_item(row, data_id, "hacker-news")

    # source="hacker-news" (not "dlt") routes the story through normal cognify.
    assert item.external_metadata["source"] == "hacker-news"
    assert item.external_metadata["url"] == "https://news.ycombinator.com/item?id=42"
    assert item.external_metadata["external_id"] == "hn-42"
    assert item.data_id == data_id
    assert item.data.startswith("# Ask HN")
    assert "body text" in item.data


def test_source_declares_document_marker():
    pytest.importorskip("cognee")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_hacker_news import hacker_news_source

    source = hacker_news_source(["rust"], http_client=FakeHnHttp())
    assert document_source_tag(source) == "hacker-news"


# ---------------------------------------------------------------------------
# dlt pipeline: sync, incremental cursor, forget-on-delete (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_sync(dlt_mod, tmp_path, topics, http, **kwargs):
    from cognee_community_connector_hacker_news import hacker_news_source

    db_path = (tmp_path / "hn.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="hn_test",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="hn_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(hacker_news_source(topics, http_client=http, **kwargs))
    return pipeline


def _read_items(pipeline):
    """Return {id: row-dict}; {} when the sync yielded no rows (no table created)."""
    try:
        with (
            pipeline.sql_client() as client,
            client.execute_query("SELECT id, url, title, content FROM hacker_news_items") as cursor,
        ):
            rows = cursor.fetchall()
    except Exception as exc:
        if "no such table" in str(exc):
            return {}
        raise
    return {
        row[0]: {"id": row[0], "url": row[1], "title": row[2], "content": row[3]} for row in rows
    }


def _cursors(pipeline):
    """Find the connector's per-topic cursor map anywhere in the dlt state."""
    found = {}

    def _walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("cursors"), dict):
                found.update(node["cursors"])
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(pipeline.state)
    return found


def _story_setup(sid, created_i, comments=True):
    hit = _hit(sid, created_i=created_i)
    item = _story_item(sid, kids=[1000 + sid] if comments else [])
    comment = _comment_item(1000 + sid, text=f"<p>Comment on {sid}</p>")
    return hit, item, comment


def test_first_sync_loads_stories_with_threads(dlt_mod, tmp_path):
    hit, item, comment = _story_setup(1, 1700000000)
    http = FakeHnHttp(hits_by_topic={"rust": [hit]}, items={"1": item, "1001": comment})

    rows = _read_items(_run_sync(dlt_mod, tmp_path, ["rust"], http))

    assert set(rows) == {"hn-1"}
    assert rows["hn-1"]["title"] == "Rust 1.0 released"
    assert "Comment on 1" in rows["hn-1"]["content"]
    assert rows["hn-1"]["url"] == "https://example.com/1"


def test_duplicate_story_across_topics_yielded_once(dlt_mod, tmp_path):
    hit, item, comment = _story_setup(7, 1700000000)
    http = FakeHnHttp(
        hits_by_topic={"rust": [hit], "systems": [hit]}, items={"7": item, "1007": comment}
    )

    rows = _read_items(_run_sync(dlt_mod, tmp_path, ["rust", "systems"], http))

    assert set(rows) == {"hn-7"}


def test_cursor_advances_and_new_items_picked_up(dlt_mod, tmp_path):
    hit1, item1, c1 = _story_setup(1, 1700000000)
    http = FakeHnHttp(hits_by_topic={"rust": [hit1]}, items={"1": item1, "1001": c1})
    pipeline = _run_sync(dlt_mod, tmp_path, ["rust"], http)
    assert _cursors(pipeline) == {"rust": 1700000000}

    hit2, item2, c2 = _story_setup(2, 1700000500)
    http2 = FakeHnHttp(
        hits_by_topic={"rust": [hit1, hit2]},
        items={"1": item1, "1001": c1, "2": item2, "1002": c2},
    )
    before = _read_items(pipeline)
    pipeline = _run_sync(dlt_mod, tmp_path, ["rust"], http2)
    after = _read_items(pipeline)

    assert _cursors(pipeline) == {"rust": 1700000500}
    assert set(after) == {"hn-1", "hn-2"}
    # Unchanged story keeps byte-identical content → stable data_id, no re-cognify.
    assert after["hn-1"]["content"] == before["hn-1"]["content"]


def test_vanished_story_forgotten_on_resync(dlt_mod, tmp_path):
    hit1, item1, c1 = _story_setup(1, 1700000000)
    hit2, item2, c2 = _story_setup(2, 1700000100)
    http = FakeHnHttp(
        hits_by_topic={"rust": [hit1, hit2]},
        items={"1": item1, "1001": c1, "2": item2, "1002": c2},
    )
    _run_sync(dlt_mod, tmp_path, ["rust"], http)

    # Story 1 disappears upstream entirely (deleted); story 2 stays.
    http2 = FakeHnHttp(hits_by_topic={"rust": [hit2]}, items={"2": item2, "1002": c2})
    rows = _read_items(_run_sync(dlt_mod, tmp_path, ["rust"], http2))

    # Absent from staging → orphan cleanup forgets it downstream.
    assert "hn-1" not in rows
    assert "hn-2" in rows


def test_deleted_flag_forgotten_on_resync(dlt_mod, tmp_path):
    hit, item, comment = _story_setup(3, 1700000000)
    http = FakeHnHttp(hits_by_topic={"rust": [hit]}, items={"3": item, "1003": comment})
    _run_sync(dlt_mod, tmp_path, ["rust"], http)

    http2 = FakeHnHttp(
        hits_by_topic={"rust": [hit]}, items={"3": {"id": 3, "deleted": True}, "1003": comment}
    )
    rows = _read_items(_run_sync(dlt_mod, tmp_path, ["rust"], http2))
    assert "hn-3" not in rows


def test_empty_topic_results_sync_cleanly(dlt_mod, tmp_path):
    rows = _read_items(_run_sync(dlt_mod, tmp_path, ["rust"], FakeHnHttp()))
    assert rows == {}


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_transient_algolia_error_retried_then_succeeds():
    http = FakeHnHttp(hits_by_topic={"rust": [_hit(1)]}, fail="transient", fail_kind="algolia")
    hits = list(_iter_topic_hits(http, "rust", since_i=0, max_stories=25))
    assert [h["objectID"] for h in hits] == ["1"]
    assert len(http.algolia_calls()) == 3  # 2 failures + 1 success


def test_persistent_algolia_error_aborts_sync(dlt_mod, tmp_path):
    from dlt.pipeline.exceptions import PipelineStepFailed

    http = FakeHnHttp(fail="permanent", fail_kind="algolia")
    with pytest.raises(PipelineStepFailed):
        _run_sync(dlt_mod, tmp_path, ["rust"], http)


def test_persistent_item_fetch_error_aborts_sync(dlt_mod, tmp_path):
    # A story whose thread cannot be fetched must abort, not silently drop —
    # under replace, a partial snapshot would wrongly forget live stories.
    from dlt.pipeline.exceptions import PipelineStepFailed

    hit, _, _ = _story_setup(5, 1700000000)
    http = FakeHnHttp(
        hits_by_topic={"rust": [hit]}, items={}, fail="permanent", fail_kind="firebase"
    )
    with pytest.raises(PipelineStepFailed):
        _run_sync(dlt_mod, tmp_path, ["rust"], http)
