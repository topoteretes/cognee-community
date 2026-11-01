"""Unit tests for the Substack connector.

Two layers, all runnable in CI without a live feed:

* DB-free tests for RSS parsing, HTML rendering, the paywall-truncation
  marker, and entry -> row flattening.
* dlt-pipeline tests (fake requests session, temp sqlite destination)
  covering the acceptance criteria: re-sync reflects edits, and a post that
  disappears from the feed drops out of the full-snapshot load
  (forget-on-delete).
"""

import pytest

from cognee_community_connector_substack.substack import (
    SUBSTACK_SOURCE_NAME,
    _clean_html,
    _entry_to_row,
    _is_partial,
    _parse_entries,
    substack_source,
)

FEED_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
<title>Example Newsletter</title>
{items}
</channel>
</rss>
"""

ITEM_TEMPLATE = """<item>
<title>{title}</title>
<link>{link}</link>
<guid>{guid}</guid>
<content:encoded><![CDATA[{content}]]></content:encoded>
</item>
"""


def _item(guid, *, title="A post", link=None, content="<p>Hello</p>"):
    return ITEM_TEMPLATE.format(
        title=title,
        link=link or f"https://example.substack.com/p/{guid}",
        guid=guid,
        content=content,
    )


def _feed(*items):
    return FEED_TEMPLATE.format(items="\n".join(items))


class _Resp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class FakeSession:
    """Minimal stand-in for a ``requests`` session serving a fixed feed."""

    def __init__(self, feed_text, status_code=200):
        self.feed_text = feed_text
        self.status_code = status_code
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        return _Resp(self.feed_text, self.status_code)


# ---------------------------------------------------------------------------
# Feed parsing (DB-free)
# ---------------------------------------------------------------------------
def test_parse_entries_reads_title_link_guid_and_content():
    feed = _feed(_item("p1", title="Post One", content="<p>Body one</p>"))
    entries = _parse_entries(feed)

    assert len(entries) == 1
    assert entries[0]["id"] == "p1"
    assert entries[0]["title"] == "Post One"
    assert entries[0]["link"] == "https://example.substack.com/p/p1"
    assert entries[0]["content"] == "<p>Body one</p>"


def test_parse_entries_falls_back_to_link_when_guid_missing():
    feed = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>No guid</title><link>https://example.substack.com/p/no-guid</link></item>
</channel></rss>"""
    entries = _parse_entries(feed)
    assert entries[0]["id"] == "https://example.substack.com/p/no-guid"


def test_parse_entries_handles_multiple_items_in_order():
    feed = _feed(_item("p1"), _item("p2"))
    entries = _parse_entries(feed)
    assert [e["id"] for e in entries] == ["p1", "p2"]


# ---------------------------------------------------------------------------
# Rendering (DB-free)
# ---------------------------------------------------------------------------
def test_clean_html_strips_tags_preserves_paragraphs_and_code():
    raw = "<p>Hello&nbsp;<b>world</b></p><pre><code>x = 1\ny = 2</code></pre>"
    cleaned = _clean_html(raw)
    assert "Hello world" in cleaned
    assert "```" in cleaned
    assert "x = 1\ny = 2" in cleaned
    assert _clean_html("") == ""
    assert _clean_html(None) == ""


def test_is_partial_detects_known_paywall_markers():
    assert _is_partial("<p>Keep reading with a 7-day free trial</p>") is True
    assert _is_partial("<p>This post is for paid subscribers</p>") is True
    assert _is_partial("<p>Just a normal post</p>") is False


def test_entry_to_row_flattens_entry():
    entry = {
        "id": "p1",
        "title": "My Post",
        "link": "https://example.substack.com/p/p1",
        "content": "<p>body text</p>",
    }
    row = _entry_to_row(entry)

    assert row["id"] == "p1"
    assert row["title"] == "My Post"
    assert row["url"] == "https://example.substack.com/p/p1"
    assert "body text" in row["content"]
    assert "truncated" not in row["content"]


def test_entry_to_row_flags_paywalled_content_as_partial():
    entry = {
        "id": "p1",
        "title": "Paid Post",
        "link": "https://example.substack.com/p/p1",
        "content": "<p>Preview text. Keep reading with a 7-day free trial</p>",
    }
    row = _entry_to_row(entry)
    assert "truncated" in row["content"].lower()


# ---------------------------------------------------------------------------
# substack_source — dlt wiring — requires dlt
# ---------------------------------------------------------------------------
def test_substack_source_requires_feed_url_or_publication():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="feed_url or publication"):
        substack_source(client=FakeSession(_feed()))


def test_substack_source_derives_feed_url_from_publication():
    pytest.importorskip("dlt")
    session = FakeSession(_feed(_item("p1")))
    source = substack_source(publication="example", client=session)
    list(source)
    assert session.calls == ["https://example.substack.com/feed"]


def test_substack_source_declares_document_marker():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = substack_source(feed_url="https://x.substack.com/feed", client=FakeSession(_feed()))
    assert SUBSTACK_SOURCE_NAME == "substack"
    assert document_source_tag(source) == "substack"


def test_substack_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="cognee\\[substack\\]"):
        substack_source(feed_url="https://x.substack.com/feed")


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete (needs dlt)
# ---------------------------------------------------------------------------
def _run_sync(dlt, tmp_path, feed_text):
    db_path = (tmp_path / "substack.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="substack_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="substack_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        substack_source(feed_url="https://example.substack.com/feed", client=FakeSession(feed_text))
    )
    return pipeline


def _read_posts(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM substack_posts") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_posts_with_rendered_content(dlt_mod, tmp_path):
    feed = _feed(_item("p1", title="Alpha", content="<p>alpha body</p>"))
    pipeline = _run_sync(dlt_mod, tmp_path, feed)

    rows = _read_posts(pipeline)
    assert set(rows) == {"p1"}
    assert "alpha body" in rows["p1"]["content"]


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    feed_v1 = _feed(_item("p1", title="Alpha", content="<p>v1</p>"))
    _run_sync(dlt_mod, tmp_path, feed_v1)

    feed_v2 = _feed(_item("p1", title="Alpha", content="<p>v2</p>"))
    pipeline = _run_sync(dlt_mod, tmp_path, feed_v2)

    rows = _read_posts(pipeline)
    assert "v2" in rows["p1"]["content"]
    assert "v1" not in rows["p1"]["content"]


def test_removed_post_is_forgotten_on_resync(dlt_mod, tmp_path):
    feed_v1 = _feed(
        _item("p1", title="Alpha", content="<p>a</p>"),
        _item("p2", title="Beta", content="<p>b</p>"),
    )
    _run_sync(dlt_mod, tmp_path, feed_v1)

    # p1 no longer in the feed at all — unpublished, or aged out of the
    # feed's recent-posts window (see README's Known limitation) — either
    # way it is absent from the replace load, so it falls out of staging.
    feed_v2 = _feed(_item("p2", title="Beta", content="<p>b</p>"))
    pipeline = _run_sync(dlt_mod, tmp_path, feed_v2)

    rows = _read_posts(pipeline)
    assert "p1" not in rows
    assert "p2" in rows
