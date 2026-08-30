"""Unit tests for the Hacker News dlt connector.

Runnable in CI without network: Algolia pages are injected. Covers story and
comment flattening, pagination, since-cursor filtering, document-source
tagging, and full-snapshot forget-on-delete.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import NAMESPACE_OID, uuid5

import pytest

from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_hacker_news.hacker_news import (
    HN_SOURCE_NAME,
    _hit_to_row,
    _parse_since,
    _search_url,
    hacker_news_source,
)


def _story(object_id, title, created_at_i, text="story body"):
    return {
        "objectID": object_id,
        "title": title,
        "url": f"https://example.com/{object_id}",
        "author": "alice",
        "story_text": text,
        "comment_text": None,
        "created_at": datetime.fromtimestamp(created_at_i, tz=timezone.utc).isoformat(),
        "created_at_i": created_at_i,
    }


def _comment(object_id, created_at_i, text="comment body"):
    return {
        "objectID": object_id,
        "title": None,
        "url": None,
        "author": "bob",
        "story_text": None,
        "comment_text": f"<p>{text}</p>",
        "created_at": datetime.fromtimestamp(created_at_i, tz=timezone.utc).isoformat(),
        "created_at_i": created_at_i,
    }


def test_hit_to_row_story():
    row = _hit_to_row(_story("1", "Alpha", 1_704_067_200))
    assert row is not None
    assert row["id"] == "1"
    assert row["title"] == "Alpha"
    assert row["url"] == "https://example.com/1"
    assert row["content"] == "story body"
    assert row["author"] == "alice"


def test_hit_to_row_comment_strips_html_and_uses_hn_item_url():
    row = _hit_to_row(_comment("99", 1_704_067_200, "hello <b>world</b>"))
    assert row is not None
    assert row["id"] == "99"
    assert row["title"] == "HN item 99"
    assert row["url"] == "https://news.ycombinator.com/item?id=99"
    assert "hello world" in row["content"]
    assert "<p>" not in row["content"]


def test_hit_without_object_id_is_skipped():
    assert _hit_to_row({"title": "no id"}) is None


def test_parse_since_accepts_datetime_and_unix():
    assert _parse_since(100) == 100
    parsed = _parse_since(datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert parsed == 1_704_067_200
    assert _parse_since("2024-01-01T00:00:00Z") == 1_704_067_200
    assert _parse_since(None) is None


def test_search_url_includes_numeric_filter():
    url = _search_url("llm", "(story,comment)", 1, 50, 1_704_067_200)
    query = parse_qs(urlparse(url).query)
    assert query["query"] == ["llm"]
    assert query["page"] == ["1"]
    assert query["numericFilters"] == ["created_at_i>=1704067200"]


def test_hacker_news_source_requires_a_query():
    with pytest.raises(ValueError, match="at least one search query"):
        hacker_news_source([])


def test_hacker_news_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = hacker_news_source(
        ["llm"],
        fetch=lambda url: {"hits": [], "nbPages": 0},
    )
    assert HN_SOURCE_NAME == "hacker_news"
    assert document_source_tag(source) == "hacker_news"


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "1",
            "url": "https://example.com/1",
            "title": "Alpha",
            "content": "story body",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "1")
    item = _build_document_data_item(row, data_id, "hacker_news")
    assert item.external_metadata["source"] == "hacker_news"
    assert item.external_metadata["external_id"] == "1"
    assert item.data.startswith("# Alpha")


class FakeAlgolia:
    """Serves canned pages keyed by page number parsed from the request URL."""

    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        page = int(parse_qs(urlparse(url).query).get("page", ["0"])[0])
        hits = self.pages.get(page, [])
        return {"hits": hits, "nbPages": max(self.pages) + 1 if self.pages else 0, "page": page}


def _run_sync(dlt, tmp_path, fake, queries=None, **kwargs):
    db_path = (tmp_path / "hn.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="hn_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="hn_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        hacker_news_source(queries or ["llm"], fetch=fake, **kwargs),
    )
    return pipeline


def _read_items(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM hacker_news_items") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_stories_and_comments(dlt_mod, tmp_path):
    fake = FakeAlgolia(
        {
            0: [
                _story("1", "Alpha", 1_704_067_200),
                _comment("2", 1_704_067_201, "thread reply"),
            ]
        }
    )
    pipeline = _run_sync(dlt_mod, tmp_path, fake)
    rows = _read_items(pipeline)
    assert set(rows) == {"1", "2"}
    assert rows["1"]["title"] == "Alpha"
    assert "thread reply" in rows["2"]["content"]


def test_pagination_walks_until_nb_pages(dlt_mod, tmp_path):
    fake = FakeAlgolia(
        {
            0: [_story("1", "Alpha", 1_704_067_200)],
            1: [_story("3", "Gamma", 1_704_153_600)],
        }
    )
    pipeline = _run_sync(dlt_mod, tmp_path, fake, max_pages=5)
    rows = _read_items(pipeline)
    assert set(rows) == {"1", "3"}
    pages = [parse_qs(urlparse(url).query)["page"][0] for url in fake.urls]
    assert pages == ["0", "1"]


def test_since_cursor_is_sent_to_algolia(dlt_mod, tmp_path):
    fake = FakeAlgolia({0: [_story("3", "Gamma", 1_704_153_600)]})
    _run_sync(
        dlt_mod,
        tmp_path,
        fake,
        since=datetime(2024, 1, 15, tzinfo=timezone.utc),
    )
    query = parse_qs(urlparse(fake.urls[0]).query)
    assert query["numericFilters"][0].startswith("created_at_i>=")


def test_removed_item_is_forgotten_on_resync(dlt_mod, tmp_path):
    first = FakeAlgolia(
        {0: [_story("1", "Alpha", 1_704_067_200), _story("2", "Beta", 1_704_153_600)]}
    )
    _run_sync(dlt_mod, tmp_path, first)

    second = FakeAlgolia({0: [_story("2", "Beta", 1_704_153_600)]})
    pipeline = _run_sync(dlt_mod, tmp_path, second)
    rows = _read_items(pipeline)
    assert "1" not in rows
    assert "2" in rows
