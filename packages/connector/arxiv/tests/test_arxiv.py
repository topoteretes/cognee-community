"""Unit tests for the arXiv dlt connector.

Two layers, all runnable in CI without a live network connection:

* DB-free tests for arXiv entry→markdown rendering, date parsing, query
  building, and pagination logic.
* dlt-pipeline tests (mocked httpx + feedparser, temp sqlite destination)
  covering the acceptance criteria: re-sync reflects new papers, and papers
  that fall outside the date range drop out of the full-snapshot load
  (forget-on-delete).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest

from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_arxiv.arxiv import (
    ARXIV_SOURCE_NAME,
    _arxiv_escape,
    _build_query_url,
    _entry_to_row,
    _iter_papers,
    _parse_date,
    _render_markdown,
    arxiv_source,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _entry(
    arxiv_id: str = "2301.12345",
    title: str = "Test Paper",
    authors: list[dict] | None = None,
    summary: str = "Test abstract.",
    published: str = "2024-01-15T18:00:00Z",
    updated: str = "2024-01-16T18:00:00Z",
    categories: list[dict] | None = None,
) -> dict:
    """Build a minimal feedparser-style arXiv entry."""
    if authors is None:
        authors = [{"name": "Alice Smith"}]
    if categories is None:
        categories = [{"term": "cs.AI", "scheme": "http://arxiv.org/schemas/atom", "label": None}]
    return {
        "id": f"http://arxiv.org/abs/{arxiv_id}",
        "title": title,
        "authors": authors,
        "summary": summary,
        "published": published,
        "updated": updated,
        "tags": categories,
    }


def _feed(total_results: int, entries: list[dict]) -> dict:
    """Build a minimal feedparser-style feed object."""
    return {
        "feed": {"opensearch_totalresults": str(total_results)},
        "entries": entries,
    }


def _yesterday() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


class FakeHTTPXClient:
    """Stand-in for httpx.Client backed by in-memory feed data."""

    def __init__(self, feeds: dict[str, dict]):
        self._feeds = feeds
        self._calls: list[str] = []

    def get(self, url: str):
        self._calls.append(url)
        feed = self._feeds.get(url)
        if feed is None:
            raise ValueError(f"No mock feed for URL: {url}")
        return SimpleNamespace(text=_serialize_feed(feed), raise_for_status=lambda: None)


def _serialize_feed(feed: dict) -> str:
    """Serialize a feed dict to a minimal Atom XML string for feedparser."""
    entries_xml = ""
    for entry in feed.get("entries", []):
        authors_xml = "".join(
            f"<author><name>{a['name']}</name></author>"
            for a in entry.get("authors", [])
        )
        tags_xml = "".join(
            f'<category term="{t["term"]}"/>' for t in entry.get("tags", [])
        )
        entries_xml += (
            f"<entry>"
            f"<id>{entry['id']}</id>"
            f"<title>{entry['title']}</title>"
            f"{authors_xml}"
            f"<summary>{entry['summary']}</summary>"
            f"<published>{entry['published']}</published>"
            f"<updated>{entry['updated']}</updated>"
            f"{tags_xml}"
            f"</entry>"
        )

    total = feed["feed"]["opensearch_totalresults"]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"'
        ' xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<opensearch:totalResults>{total}</opensearch:totalResults>"
        f"{entries_xml}"
        "</feed>"
    )


# ---------------------------------------------------------------------------
# Query building / escaping (DB-free)
# ---------------------------------------------------------------------------


def test_arxiv_escape():
    assert _arxiv_escape("hello world") == "hello+world"
    assert _arxiv_escape("agent memory") == "agent+memory"


def test_build_query_url():
    url = _build_query_url("cat:cs.AI", 0, 50)
    assert "search_query=cat%3Acs.AI" in url
    assert "start=0" in url
    assert "max_results=50" in url
    assert "sortBy=submittedDate" in url
    assert "sortOrder=descending" in url


# ---------------------------------------------------------------------------
# Date parsing (DB-free)
# ---------------------------------------------------------------------------


def test_parse_date_valid():
    assert _parse_date("2024-01-15T18:00:00Z") == datetime(2024, 1, 15, 18, 0, tzinfo=timezone.utc)


def test_parse_date_none_or_empty():
    assert _parse_date("") is None
    assert _parse_date(None) is None


def test_parse_date_invalid():
    assert _parse_date("not-a-date") is None


# ---------------------------------------------------------------------------
# Entry → row / rendering (DB-free)
# ---------------------------------------------------------------------------


def test_entry_to_row_flattens_entry():
    entry = _entry(
        arxiv_id="2301.12345v2",
        title="  My Paper\nWith Newlines  ",
        authors=[{"name": "Alice"}, {"name": "Bob"}],
        summary="  Abstract text\nwith newlines  ",
        categories=[{"term": "cs.AI"}, {"term": "cs.CL"}],
    )
    row = _entry_to_row(entry)

    assert row["id"] == "2301.12345"  # version stripped
    assert row["title"] == "My Paper With Newlines"
    assert row["authors"] == "Alice, Bob"
    assert row["categories"] == "cs.AI, cs.CL"
    assert row["abstract"] == "Abstract text with newlines"
    assert "content" in row


def test_render_markdown():
    entry = _entry(
        arxiv_id="2301.12345",
        title="Great Paper",
        authors=[{"name": "Alice"}],
        summary="We present a novel approach.",
    )
    md = _render_markdown(entry)

    assert "# Great Paper" in md
    assert "**Authors:** Alice" in md
    assert "## Abstract" in md
    assert "We present a novel approach." in md
    assert "http://arxiv.org/abs/2301.12345" in md


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "2301.12345",
            "url": "http://arxiv.org/abs/2301.12345",
            "title": "Great Paper",
            "authors": "Alice",
            "categories": "cs.AI",
            "published": "2024-01-15T18:00:00Z",
            "abstract": "We present a novel approach.",
            "content": "# Great Paper\n\n**Authors:** Alice\n\n## Abstract\n\nWe present a novel approach.",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "2301.12345")

    item = _build_document_data_item(row, data_id, "arxiv")

    assert item.external_metadata["source"] == "arxiv"
    assert item.external_metadata["url"] == "http://arxiv.org/abs/2301.12345"
    assert item.external_metadata["external_id"] == "2301.12345"
    assert item.data_id == data_id
    assert "Great Paper" in item.data


def test_arxiv_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_arxiv.arxiv import arxiv_source

    source = arxiv_source(categories=["cs.AI"])
    assert ARXIV_SOURCE_NAME == "arxiv"
    assert document_source_tag(source) == "arxiv"


# ---------------------------------------------------------------------------
# Pagination and date filtering (DB-free)
# ---------------------------------------------------------------------------


def test_iter_papers_respects_date_filter():
    """Papers older than start_dt are excluded."""
    now = _yesterday()
    old_date = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_date = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    entries = [
        _entry("p1", published=new_date),
        _entry("p2", published=old_date),
    ]
    feed = _feed(2, entries)
    url = _build_query_url("cat:cs.AI", 0, 100)
    client = FakeHTTPXClient({url: feed})

    start_dt = now - timedelta(days=30)
    papers = list(_iter_papers(client, "cat:cs.AI", start_dt, None))

    assert len(papers) == 1
    assert papers[0]["id"] == "p1"


def test_iter_papers_paginates():
    """Multiple pages are fetched until total_results is exhausted."""
    now = _yesterday()
    date = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    entries = [_entry(f"p{i}", published=date) for i in range(3)]
    page1_feed = _feed(3, entries[:2])
    page2_feed = _feed(3, entries[2:])

    url1 = _build_query_url("cat:cs.AI", 0, 100)
    url2 = _build_query_url("cat:cs.AI", 2, 100)
    client = FakeHTTPXClient({url1: page1_feed, url2: page2_feed})

    start_dt = now - timedelta(days=30)
    papers = list(_iter_papers(client, "cat:cs.AI", start_dt, None))

    assert len(papers) == 3
    assert {p["id"] for p in papers} == {"p0", "p1", "p2"}


def test_iter_papers_respects_limit():
    """Only up to `limit` papers are returned."""
    now = _yesterday()
    date = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    entries = [_entry(f"p{i}", published=date) for i in range(5)]
    feed = _feed(5, entries)
    url = _build_query_url("cat:cs.AI", 0, 100)
    client = FakeHTTPXClient({url: feed})

    start_dt = now - timedelta(days=30)
    papers = list(_iter_papers(client, "cat:cs.AI", start_dt, 3))

    assert len(papers) == 3


# ---------------------------------------------------------------------------
# Source construction (DB-free)
# ---------------------------------------------------------------------------


def test_source_defaults_to_cs_ai():
    """When no query/categories/author given, defaults to cat:cs.AI."""
    from cognee_community_connector_arxiv.arxiv import arxiv_source

    source = arxiv_source()
    assert document_source_tag(source) == "arxiv"


def test_source_with_query_and_author():
    from cognee_community_connector_arxiv.arxiv import arxiv_source

    source = arxiv_source(query="agent memory", author="Smith")
    assert document_source_tag(source) == "arxiv"


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-date-range (needs dlt + feedparser)
# ---------------------------------------------------------------------------


def _run_sync(dlt, tmp_path, entries, query="cat:cs.AI", start_date=None):
    """Run arxiv_source through a dlt pipeline into a temp sqlite destination."""
    from cognee_community_connector_arxiv.arxiv import arxiv_source

    now = _yesterday()
    date = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    feed = _feed(len(entries), entries)
    url = _build_query_url(query, 0, 100)
    client = FakeHTTPXClient({url: feed})

    kwargs = {}
    if start_date:
        kwargs["start_date"] = start_date

    db_path = (tmp_path / "arxiv.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="arxiv_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="arxiv_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(arxiv_source(http_client=client, **kwargs))
    return pipeline


def _read_papers(pipeline):
    """Return {id: row-dict} for the arxiv_papers table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            "SELECT id, title, content FROM arxiv_papers"
        ) as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


@pytest.fixture(autouse=True)
def _need_feedparser():
    pytest.importorskip("feedparser")


def test_first_sync_loads_papers_with_rendered_content(dlt_mod, tmp_path, monkeypatch):
    now = _yesterday()
    date = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = [_entry("p1", title="Alpha", published=date, summary="alpha body")]
    feed = _feed(1, entries)
    url = _build_query_url("cat:cs.AI", 0, 100)
    client = FakeHTTPXClient({url: feed})

    from cognee_community_connector_arxiv.arxiv import arxiv_source

    db_path = (tmp_path / "arxiv_first.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="arxiv_first",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="arxiv_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(arxiv_source(http_client=client))

    rows = _read_papers(pipeline)
    assert set(rows) == {"p1"}
    assert "alpha body" in rows["p1"]["content"]


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    now = _yesterday()
    date = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = [_entry("p1", title="Alpha", published=date, summary="v1")]
    feed = _feed(1, entries)
    url = _build_query_url("cat:cs.AI", 0, 100)
    client = FakeHTTPXClient({url: feed})

    from cognee_community_connector_arxiv.arxiv import arxiv_source

    db_path = (tmp_path / "arxiv_edit.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="arxiv_edit",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="arxiv_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(arxiv_source(http_client=client))

    # Updated content: replace the feed with new summary.
    entries = [_entry("p1", title="Alpha", published=date, summary="v2")]
    feed = _feed(1, entries)
    client = FakeHTTPXClient({url: feed})
    pipeline = dlt_mod.pipeline(
        pipeline_name="arxiv_edit",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="arxiv_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(arxiv_source(http_client=client))

    rows = _read_papers(pipeline)
    assert "v2" in rows["p1"]["content"]
    assert "v1" not in rows["p1"]["content"]


def test_old_paper_is_removed_on_resync(dlt_mod, tmp_path):
    """Papers older than the lookback window drop out on re-sync (forget-on-delete)."""
    now = _yesterday()
    new_date = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    old_date = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")

    entries = [
        _entry("p1", published=new_date),
        _entry("p2", published=old_date),
    ]
    feed = _feed(2, entries)
    url = _build_query_url("cat:cs.AI", 0, 100)
    client = FakeHTTPXClient({url: feed})

    from cognee_community_connector_arxiv.arxiv import arxiv_source

    db_path = (tmp_path / "arxiv_forget.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="arxiv_forget",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="arxiv_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    # First sync: both papers are loaded (no date filter in source).
    pipeline.run(arxiv_source(http_client=client, start_date="2020-01-01"))

    rows = _read_papers(pipeline)
    assert "p1" in rows
    assert "p2" in rows

    # Second sync: narrowed date range — old paper drops out.
    entries = [_entry("p1", published=new_date)]
    feed = _feed(1, entries)
    client = FakeHTTPXClient({url: feed})
    pipeline = dlt_mod.pipeline(
        pipeline_name="arxiv_forget",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="arxiv_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    pipeline.run(arxiv_source(http_client=client, start_date="2020-01-01"))
    rows = _read_papers(pipeline)
    assert "p1" in rows
    # p2 is absent from the new snapshot → orphan cleanup forgets it.
    assert "p2" not in rows