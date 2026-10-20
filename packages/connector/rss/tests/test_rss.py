"""Unit tests for the RSS / Atom dlt connector.

Every feed is in-memory XML via an injected fetch function — no network, no
credentials, so these run in CI. Coverage:

  - feed HTML is stripped to plain text (content:encoded / atom:content wins
    over description / summary)
  - the incremental signal is the entry's updated/published timestamp, with a
    content-hash fallback when a feed carries no usable timestamps
  - backfill yields every entry and records per-entry state
  - incremental re-sync yields ONLY new and updated entries
  - entries that vanish from a fetched feed become hard-delete markers
    (forget-on-delete), and removing a feed URL tombstones its entries
  - a malformed / empty / failing feed is skipped without mass-deleting its
    prior entries
  - the dlt resource is wired with merge + id PK + the hard_delete column and
    declares the document-source marker
  - a real dlt merge removes the marked row (end-to-end forget-on-delete) and
    persists the incremental state across pipeline runs

The end-to-end "deletion removes it from memory" guarantee is provided by the
existing ``orphan_cleanup`` path in cognee core; here we prove the connector
emits the markers that drive it, and that dlt acts on them.
"""

import calendar

import pytest

from cognee_community_connector_rss.rss import (
    RSS_SOURCE_NAME,
    _clean_html,
    _entry_timestamp,
    _feed_tag,
    _parse_feed,
    rss_source,
    sync_feeds,
)

FEED_URL = "https://example.com/feed.xml"
OTHER_FEED_URL = "https://example.org/atom.xml"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>Example Blog</title>
  <link>https://example.com/</link>
  <description>Example blog feed</description>
  <item>
    <guid isPermaLink="false">post-1</guid>
    <title>First &amp; foremost</title>
    <link>https://example.com/posts/1</link>
    <pubDate>Sat, 07 Sep 2024 08:00:00 GMT</pubDate>
    <description>short excerpt</description>
    <content:encoded><![CDATA[<p>Full body <b>one</b></p>]]></content:encoded>
  </item>
  <item>
    <guid>https://example.com/posts/2</guid>
    <title>Second post</title>
    <link>https://example.com/posts/2</link>
    <pubDate>Sun, 08 Sep 2024 09:30:00 GMT</pubDate>
    <description>Description only body</description>
  </item>
</channel>
</rss>"""

# post-2 edited (same guid, newer pubDate); post-3 is new.
RSS_XML_UPDATED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>Example Blog</title>
  <link>https://example.com/</link>
  <description>Example blog feed</description>
  <item>
    <guid isPermaLink="false">post-1</guid>
    <title>First &amp; foremost</title>
    <link>https://example.com/posts/1</link>
    <pubDate>Sat, 07 Sep 2024 08:00:00 GMT</pubDate>
    <description>short excerpt</description>
    <content:encoded><![CDATA[<p>Full body <b>one</b></p>]]></content:encoded>
  </item>
  <item>
    <guid>https://example.com/posts/2</guid>
    <title>Second post (edited)</title>
    <link>https://example.com/posts/2</link>
    <pubDate>Mon, 09 Sep 2024 10:00:00 GMT</pubDate>
    <description>Edited body</description>
  </item>
  <item>
    <guid isPermaLink="false">post-3</guid>
    <title>Third post</title>
    <link>https://example.com/posts/3</link>
    <pubDate>Mon, 09 Sep 2024 11:00:00 GMT</pubDate>
    <description>New arrival</description>
  </item>
</channel>
</rss>"""

# post-2 deleted upstream: the feed no longer carries it.
RSS_XML_REMOVED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>Example Blog</title>
  <link>https://example.com/</link>
  <description>Example blog feed</description>
  <item>
    <guid isPermaLink="false">post-1</guid>
    <title>First &amp; foremost</title>
    <link>https://example.com/posts/1</link>
    <pubDate>Sat, 07 Sep 2024 08:00:00 GMT</pubDate>
    <description>short excerpt</description>
    <content:encoded><![CDATA[<p>Full body <b>one</b></p>]]></content:encoded>
  </item>
</channel>
</rss>"""

ATOM_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Feed</title>
  <link rel="alternate" href="https://example.org/"/>
  <updated>2024-09-07T08:00:00Z</updated>
  <id>urn:uuid:example-feed</id>
  <entry>
    <id>urn:uuid:entry-1</id>
    <title>Atom entry</title>
    <link rel="alternate" href="https://example.org/e1"/>
    <published>2024-09-06T08:00:00Z</published>
    <updated>2024-09-07T08:00:00Z</updated>
    <summary>atom summary text</summary>
    <content type="html">&lt;p&gt;Atom &lt;b&gt;content&lt;/b&gt;&lt;/p&gt;</content>
  </entry>
</feed>"""

# A non-feed response (e.g. an HTML error page served with HTTP 200): feedparser
# flags it bozo and recovers zero entries, so the connector must skip the feed.
MALFORMED_XML = b"<html><body><h1>503 Service Unavailable</h1><p>Try again later.</p></body></html>"

EMPTY_FEED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Empty</title><link>https://example.com/</link>
<description>nothing here</description></channel></rss>"""


def _fetch_map(payloads):
    """Build a fetch fn from {url: bytes}; unknown URLs raise (fetch failure)."""

    def fetch(url):
        if url not in payloads:
            raise FileNotFoundError(url)
        return payloads[url]

    return fetch


def _epoch(*args):
    return calendar.timegm((*args, 0, 0, 0))


def _ids(rows):
    return [row["id"] for row in rows]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_clean_html_strips_tags_unescapes_and_collapses_whitespace():
    raw = "<p>Hello&nbsp;<b>world</b></p>\n<p>  second   line </p>"
    assert _clean_html(raw) == "Hello world second line"
    assert _clean_html("") == ""
    assert _clean_html(None) == ""


def test_entry_timestamp_prefers_updated_over_published():
    updated = _epoch(2024, 9, 7, 8, 0, 0)
    published = _epoch(2024, 9, 6, 8, 0, 0)
    assert _entry_timestamp({"updated_parsed": (2024, 9, 7, 8, 0, 0)}) == updated
    assert _entry_timestamp({"published_parsed": (2024, 9, 6, 8, 0, 0)}) == published
    assert (
        _entry_timestamp(
            {"updated_parsed": (2024, 9, 7, 8, 0, 0), "published_parsed": (2024, 9, 6, 8, 0, 0)}
        )
        == updated
    )
    assert _entry_timestamp({}) is None


def test_feed_tag_is_stable_and_url_specific():
    assert _feed_tag(FEED_URL) == _feed_tag(FEED_URL)
    assert _feed_tag(FEED_URL) != _feed_tag(OTHER_FEED_URL)
    assert len(_feed_tag(FEED_URL)) == 12


def test_parse_feed_handles_rss_and_atom():
    assert len(_parse_feed(RSS_XML).entries) == 2
    assert len(_parse_feed(ATOM_XML).entries) == 1


# ---------------------------------------------------------------------------
# sync_feeds — backfill / incremental / deletion
# ---------------------------------------------------------------------------
def test_backfill_rss_yields_all_entries_with_full_content():
    state = {}
    rows = list(sync_feeds(_fetch_map({FEED_URL: RSS_XML}), [FEED_URL], state))

    assert len(rows) == 2
    assert all(row["_deleted"] is False for row in rows)
    first = rows[0]
    assert first["id"] == f"{_feed_tag(FEED_URL)}:post-1"  # guid + feed namespacing
    assert first["title"] == "First & foremost"  # entity unescaped
    assert first["content"] == "Full body one"  # content:encoded wins, HTML stripped
    assert first["url"] == "https://example.com/posts/1"
    # An entry without content:encoded falls back to its description.
    assert rows[1]["content"] == "Description only body"
    # Per-entry state recorded for the next incremental run.
    assert set(state["entries"]) == {rows[0]["id"], rows[1]["id"]}
    assert state["entries"][rows[0]["id"]]["ts"] == _epoch(2024, 9, 7, 8, 0, 0)


def test_backfill_atom_prefers_content_over_summary():
    state = {}
    rows = list(sync_feeds(_fetch_map({OTHER_FEED_URL: ATOM_XML}), [OTHER_FEED_URL], state))

    row = rows[0]
    assert row["id"] == f"{_feed_tag(OTHER_FEED_URL)}:urn:uuid:entry-1"
    assert row["content"] == "Atom content"  # content wins over summary
    assert row["url"] == "https://example.org/e1"
    assert row["title"] == "Atom entry"
    # updated (not published) is the change signal.
    assert state["entries"][row["id"]]["ts"] == _epoch(2024, 9, 7, 8, 0, 0)


def test_same_guid_in_two_feeds_yields_distinct_ids():
    state = {}
    rows = list(
        sync_feeds(
            _fetch_map({FEED_URL: RSS_XML, OTHER_FEED_URL: RSS_XML}),
            [FEED_URL, OTHER_FEED_URL],
            state,
        )
    )

    # Every id is unique across feeds despite identical guids.
    assert len({row["id"] for row in rows}) == len(rows)
    assert len(rows) == 4


def test_incremental_yields_only_new_and_updated_entries():
    state = {}
    list(sync_feeds(_fetch_map({FEED_URL: RSS_XML}), [FEED_URL], state))
    second = list(sync_feeds(_fetch_map({FEED_URL: RSS_XML_UPDATED}), [FEED_URL], state))

    # post-1 unchanged (same timestamp) is skipped; post-2 edited (newer
    # pubDate, same guid) and post-3 new are emitted.
    assert _ids(second) == [
        f"{_feed_tag(FEED_URL)}:https://example.com/posts/2",
        f"{_feed_tag(FEED_URL)}:post-3",
    ]
    assert second[0]["content"] == "Edited body"
    assert state["entries"][f"{_feed_tag(FEED_URL)}:https://example.com/posts/2"]["ts"] == _epoch(
        2024, 9, 9, 10, 0, 0
    )


def test_incremental_no_changes_is_a_noop():
    state = {}
    list(sync_feeds(_fetch_map({FEED_URL: RSS_XML}), [FEED_URL], state))
    rows = list(sync_feeds(_fetch_map({FEED_URL: RSS_XML}), [FEED_URL], state))
    assert rows == []


def test_entries_without_timestamps_fall_back_to_content_hash():
    no_dates = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Dates</title><link>https://d.example/</link>
    <description>x</description>
    <item><guid>a</guid><title>A</title><description>original</description></item>
    </channel></rss>"""
    edited = no_dates.replace(b"original", b"edited")

    state = {}
    list(sync_feeds(_fetch_map({FEED_URL: no_dates}), [FEED_URL], state))
    # Unchanged content: nothing re-emitted despite no timestamps.
    assert list(sync_feeds(_fetch_map({FEED_URL: no_dates}), [FEED_URL], state)) == []
    # Changed content: re-emitted even without a timestamp bump.
    rows = list(sync_feeds(_fetch_map({FEED_URL: edited}), [FEED_URL], state))
    assert _ids(rows) == [f"{_feed_tag(FEED_URL)}:a"]
    assert rows[0]["content"] == "edited"


def test_entry_without_guid_or_link_gets_stable_derived_id():
    bare = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Bare</title><link>https://b.example/</link>
    <description>x</description>
    <item><title>Bare item</title><description>body</description></item>
    </channel></rss>"""

    state = {}
    rows = list(sync_feeds(_fetch_map({FEED_URL: bare}), [FEED_URL], state))
    assert rows[0]["id"].startswith(f"{_feed_tag(FEED_URL)}:")
    # The derived id is stable: a re-sync emits nothing new.
    assert list(sync_feeds(_fetch_map({FEED_URL: bare}), [FEED_URL], state)) == []


def test_vanished_entry_emits_hard_delete_marker():
    state = {}
    list(sync_feeds(_fetch_map({FEED_URL: RSS_XML}), [FEED_URL], state))
    # post-2 is no longer in the feed: it is gone upstream.
    rows = list(sync_feeds(_fetch_map({FEED_URL: RSS_XML_REMOVED}), [FEED_URL], state))

    assert rows == [{"id": f"{_feed_tag(FEED_URL)}:https://example.com/posts/2", "_deleted": True}]
    assert f"{_feed_tag(FEED_URL)}:https://example.com/posts/2" not in state["entries"]


def test_removed_feed_url_tombstones_its_entries():
    state = {}
    list(
        sync_feeds(
            _fetch_map({FEED_URL: RSS_XML, OTHER_FEED_URL: ATOM_XML}),
            [FEED_URL, OTHER_FEED_URL],
            state,
        )
    )
    # OTHER_FEED_URL dropped from the configuration: its entries are forgotten.
    rows = list(sync_feeds(_fetch_map({FEED_URL: RSS_XML}), [FEED_URL], state))

    atom_tag = _feed_tag(OTHER_FEED_URL)
    assert _ids(rows) == [f"{atom_tag}:urn:uuid:entry-1"]
    assert rows[0]["_deleted"] is True
    assert all(key.startswith(f"{_feed_tag(FEED_URL)}:") for key in state["entries"])


def test_malformed_feed_is_skipped_without_mass_delete():
    state = {}
    list(
        sync_feeds(
            _fetch_map({FEED_URL: RSS_XML, OTHER_FEED_URL: ATOM_XML}),
            [FEED_URL, OTHER_FEED_URL],
            state,
        )
    )
    # OTHER_FEED_URL now returns garbage: its entries must survive untouched.
    rows = list(
        sync_feeds(
            _fetch_map({FEED_URL: RSS_XML, OTHER_FEED_URL: MALFORMED_XML}),
            [FEED_URL, OTHER_FEED_URL],
            state,
        )
    )

    assert _ids(rows) == []  # nothing tombstoned off a bad response
    assert any(key.startswith(f"{_feed_tag(OTHER_FEED_URL)}:") for key in state["entries"])


def test_empty_feed_is_skipped_without_mass_delete():
    state = {}
    list(sync_feeds(_fetch_map({FEED_URL: RSS_XML}), [FEED_URL], state))
    rows = list(sync_feeds(_fetch_map({FEED_URL: EMPTY_FEED_XML}), [FEED_URL], state))

    assert _ids(rows) == []  # zero entries is not evidence of deletion
    assert len(state["entries"]) == 2


def test_fetch_failure_is_skipped_and_other_feeds_still_sync():
    state = {}
    list(
        sync_feeds(
            _fetch_map({FEED_URL: RSS_XML, OTHER_FEED_URL: ATOM_XML}),
            [FEED_URL, OTHER_FEED_URL],
            state,
        )
    )
    # OTHER_FEED_URL raises (network down); FEED_URL has changes.
    rows = list(
        sync_feeds(_fetch_map({FEED_URL: RSS_XML_UPDATED}), [FEED_URL, OTHER_FEED_URL], state)
    )

    emitted = [row for row in rows if not row["_deleted"]]
    assert {row["content"] for row in emitted} == {"Edited body", "New arrival"}
    # The unreachable feed's entries were neither tombstoned nor dropped.
    assert all(not row["id"].startswith(f"{_feed_tag(OTHER_FEED_URL)}:") for row in rows)
    assert any(key.startswith(f"{_feed_tag(OTHER_FEED_URL)}:") for key in state["entries"])


def test_duplicate_feed_urls_are_deduplicated():
    rows = list(sync_feeds(_fetch_map({FEED_URL: RSS_XML}), [FEED_URL, FEED_URL], {}))
    assert len(rows) == 2  # each entry emitted once, not twice


def test_title_only_entry_falls_back_to_title_as_content():
    title_only = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>TO</title><link>https://t.example/</link>
    <description>x</description>
    <item><guid>t1</guid><title>Just a headline</title></item>
    </channel></rss>"""

    rows = list(sync_feeds(_fetch_map({FEED_URL: title_only}), [FEED_URL], {}))
    assert rows[0]["content"] == "Just a headline"
    assert rows[0]["title"] == "Just a headline"


# ---------------------------------------------------------------------------
# rss_source — dlt wiring — requires dlt
# ---------------------------------------------------------------------------
def test_rss_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")

    resource = rss_source([FEED_URL], fetch=_fetch_map({FEED_URL: RSS_XML}))
    assert resource.name == "rss_entries"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_rss_source_declares_document_marker():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    resource = rss_source([FEED_URL], fetch=_fetch_map({FEED_URL: RSS_XML}))
    # resolve_dlt_sources routes on this marker (not the name); keep it stable.
    assert RSS_SOURCE_NAME == "rss"
    assert document_source_tag(resource) == "rss"


def test_rss_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="cognee-community-connector-rss"):
        rss_source([FEED_URL])


def test_rss_source_validates_feed_urls():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="feed_urls"):
        rss_source([])
    with pytest.raises(ValueError, match="feed_urls"):
        rss_source(["https://ok.example/feed", ""])


# ---------------------------------------------------------------------------
# End-to-end: a real dlt merge acts on the hard-delete marker, and the
# incremental state persists across pipeline runs
# ---------------------------------------------------------------------------
def test_forget_on_delete_and_incremental_end_to_end_through_a_real_dlt_pipeline(tmp_path):
    dlt = pytest.importorskip("dlt")

    db_path = (tmp_path / "rss.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_rss_e2e",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="feeds",
        pipelines_dir=str(tmp_path / "state"),
    )

    # Sync #1: three entries land in the destination.
    pipeline.run(
        rss_source(
            [FEED_URL, OTHER_FEED_URL],
            fetch=_fetch_map({FEED_URL: RSS_XML, OTHER_FEED_URL: ATOM_XML}),
        )
    )
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM rss_entries")[0][0] == 3

    # Sync #2 (same pipeline → persisted dlt state): the Atom entry is deleted
    # upstream and replaced by a new one. The connector emits a hard-delete
    # marker plus the new row; the merge drops the deleted one and keeps the
    # rest.
    atom2 = ATOM_XML.replace(b"urn:uuid:entry-1", b"urn:uuid:entry-2")
    pipeline.run(
        rss_source(
            [FEED_URL, OTHER_FEED_URL],
            fetch=_fetch_map({FEED_URL: RSS_XML_UPDATED, OTHER_FEED_URL: atom2}),
        )
    )
    with pipeline.sql_client() as client:
        remaining = {row[0] for row in client.execute_sql("SELECT id FROM rss_entries")}

    feed_tag, atom_tag = _feed_tag(FEED_URL), _feed_tag(OTHER_FEED_URL)
    # Atom entry-1 vanished from its feed → forgotten from the destination.
    assert f"{atom_tag}:urn:uuid:entry-1" not in remaining
    # The new Atom entry and every live RSS entry are present — nothing else.
    assert f"{atom_tag}:urn:uuid:entry-2" in remaining
    assert {
        f"{feed_tag}:post-1",
        f"{feed_tag}:https://example.com/posts/2",
        f"{feed_tag}:post-3",
    } <= remaining
    assert len(remaining) == 4
