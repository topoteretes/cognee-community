"""Unit tests for the YouTube dlt connector.

Three layers, all runnable in CI without a live YouTube API key:

* DB-free tests for playlist resolution, row building, and error
  classification.
* Sync-strategy tests driving ``_sync`` with an in-memory fake client,
  covering the publishedAfter cursor, the full-page id sweep, multi-playlist
  aggregation, and deletions.
* dlt-pipeline tests (fake client, temp sqlite destination) covering the
  acceptance criteria: first-run backfill, incremental re-sync picks up only
  changes, and deleted videos vanish from staging (forget-on-delete).
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest

from cognee_community_connector_youtube.youtube import (
    YOUTUBE_SOURCE_NAME,
    YouTubeClient,
    _is_transient,
    _parse_ts,
    _resolve_playlists,
    _sync,
    _tombstone_row,
    _video_row,
    youtube_source,
)

T0 = "2026-09-01T10:00:00.000Z"  # old
T1 = "2026-09-01T12:00:00.000Z"  # at first sync
T2 = "2026-09-01T14:00:00.000Z"  # new upload between runs


def _item(vid="V1", title="Video 1", description="A description", published=T1):
    return {
        "contentDetails": {"videoId": vid},
        "snippet": {
            "title": title,
            "description": description,
            "publishedAt": published,
            "resourceId": {"videoId": vid},
        },
    }


class FakeYouTube:
    """Stand-in for YouTubeClient backed by in-memory fixtures."""

    def __init__(self, playlists, items):
        self.playlists = list(playlists)
        self.items = list(items)
        self.page_calls = 0

    def playlist_items(self, playlist_id: str, page_token: str | None = None) -> dict:
        self.page_calls += 1
        if page_token:
            return {"items": [], "nextPageToken": None}
        return {"items": list(self.items), "nextPageToken": None}


# ---------------------------------------------------------------------------
# Playlist resolution / row building (DB-free)
# ---------------------------------------------------------------------------


def test_resolve_playlists_derives_uploads_from_channel():
    assert _resolve_playlists("UCabc123", None) == ["UUabc123"]


def test_resolve_playlists_combines_channel_and_explicit_playlists():
    assert _resolve_playlists("UCabc", ["PLxy"]) == ["UUabc", "PLxy"]
    assert _resolve_playlists(None, ["PLxy"]) == ["PLxy"]


def test_resolve_playlists_ignores_non_channel_ids():
    assert _resolve_playlists("handle-name", None) == []


def test_video_row_flattens_playlist_item():
    row = _video_row(_item(vid="V1", title="My video", description="About it"))
    assert row["id"] == "V1"
    assert row["kind"] == "video"
    assert row["url"] == "https://www.youtube.com/watch?v=V1"
    assert row["title"] == "My video"
    assert "About it" in row["content"]
    assert "Published: " in row["content"]


def test_tombstone_row_marks_deletion():
    tomb = _tombstone_row("V1")
    assert tomb["_deleted"] is True
    assert tomb["kind"] == "video"


# ---------------------------------------------------------------------------
# Timestamp / error classification (DB-free)
# ---------------------------------------------------------------------------


def test_parse_ts_handles_both_suffixes():
    assert _parse_ts("2026-09-01T12:00:00.000Z") == _parse_ts("2026-09-01T12:00:00.000+00:00")
    assert _parse_ts("garbage") is None
    assert _parse_ts(None) is None


def test_is_transient_classification():
    def status_error(code):
        req = httpx.Request("GET", "https://www.googleapis.com/youtube/v3/playlistItems")
        resp = httpx.Response(code, request=req)
        return httpx.HTTPStatusError("err", request=req, response=resp)

    assert _is_transient(status_error(429)) is True
    assert _is_transient(status_error(503)) is True
    assert _is_transient(status_error(403)) is False  # quota/auth failures are permanent
    assert _is_transient(httpx.ConnectTimeout("t")) is True
    assert _is_transient(ValueError()) is False


# ---------------------------------------------------------------------------
# Sync strategies (pure, fake client)
# ---------------------------------------------------------------------------


def test_first_sync_yields_everything_and_records_cursors():
    client = FakeYouTube(["UUabc"], [_item(vid="V1"), _item(vid="V2", published=T0)])
    state: dict = {}
    rows = list(_sync(client, state))

    assert [r["id"] for r in rows] == ["V1", "V2"]
    assert _parse_ts(state["published_after"]) == _parse_ts(T1)
    assert state["video_ids"] == ["V1", "V2"]


def test_incremental_sync_reemits_only_new_uploads():
    client = FakeYouTube(["UUabc"], [_item(vid="V1", published=T1)])
    state: dict = {}
    list(_sync(client, state))

    # Nothing new: nothing is re-emitted, but the sweep still saw V1.
    assert list(_sync(client, state)) == []
    assert state["video_ids"] == ["V1"]

    # A new upload arrives alongside the old one.
    client.items = [_item(vid="V1", published=T1), _item(vid="V2", title="New", published=T2)]
    rows = list(_sync(client, state))
    assert [r["id"] for r in rows] == ["V2"]
    assert _parse_ts(state["published_after"]) == _parse_ts(T2)
    assert sorted(state["video_ids"]) == ["V1", "V2"]


def test_aggregates_multiple_playlists():
    client = FakeYouTube(["UUabc", "PLxy"], [_item(vid="V1")])
    state: dict = {}
    rows = list(_sync(client, state))
    assert [r["id"] for r in rows] == ["V1", "V1"]  # same video listed twice → upsert is idempotent
    assert state["video_ids"] == ["V1"]


def test_vanished_video_is_tombstoned():
    client = FakeYouTube(["UUabc"], [_item(vid="V1"), _item(vid="V2", published=T0)])
    state: dict = {}
    list(_sync(client, state))

    # V1 vanished (deleted / made private); V2 remains untouched.
    client.items = [_item(vid="V2", published=T0)]
    rows = [r for r in _sync(client, state) if r.get("_deleted")]
    assert [r["id"] for r in rows] == ["V1"]
    assert state["video_ids"] == ["V2"]


def test_paging_walks_all_pages():
    # Simulate two pages: first returns a page token, second returns the rest.
    client = FakeYouTube(["UUabc"], [_item(vid="V1")])

    def two_pages(playlist_id, page_token=None):
        client.page_calls += 1
        if page_token is None:
            return {"items": [_item(vid="V1")], "nextPageToken": "tok"}
        return {"items": [_item(vid="V2", published=T2)], "nextPageToken": None}

    client.playlist_items = two_pages
    state: dict = {}
    rows = list(_sync(client, state))
    assert [r["id"] for r in rows] == ["V1", "V2"]
    assert client.page_calls == 2


# ---------------------------------------------------------------------------
# Source wiring (DB-free)
# ---------------------------------------------------------------------------


def test_youtube_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = youtube_source(channel_id="UCabc", api_key="test-key")
    assert YOUTUBE_SOURCE_NAME == "youtube"
    assert document_source_tag(source) == "youtube"


def test_youtube_source_requires_api_key():
    with pytest.raises(ValueError):
        youtube_source(channel_id="UCabc", api_key=None, client=None)


def test_youtube_source_requires_scope():
    with pytest.raises(ValueError):
        youtube_source(channel_id=None, playlist_ids=None, api_key="k", client=None)


def test_document_data_item_tags_source():
    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = SimpleNamespace(
        row_data={
            "id": "V1",
            "kind": "video",
            "url": "https://www.youtube.com/watch?v=V1",
            "title": "My video",
            "content": "My video\n\nAbout it",
            "_deleted": False,
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "V1")

    item = _build_document_data_item(row, data_id, "youtube")

    assert item.external_metadata["source"] == "youtube"
    assert item.external_metadata["url"] == "https://www.youtube.com/watch?v=V1"
    assert item.data_id == data_id
    assert item.data.startswith("# My video")


# ---------------------------------------------------------------------------
# dlt pipeline: incremental merge + forget-on-delete (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _make_pipeline(dlt, tmp_path, name):
    db_path = (tmp_path / f"{name}.db").as_posix()
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="youtube_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _read_rows(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, kind, title, content FROM youtube") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"kind": row[1], "title": row[2], "content": row[3]} for row in rows}


def test_pipeline_backfill_and_incremental_resync(dlt_mod, tmp_path):
    client = FakeYouTube(["UUabc"], [_item(vid="V1", title="First video", published=T1)])
    pipeline = _make_pipeline(dlt_mod, tmp_path, "youtube_resync")
    pipeline.run(youtube_source(channel_id="UCabc", api_key="k", client=client))

    rows = _read_rows(pipeline)
    assert set(rows) == {"V1"}
    assert rows["V1"]["title"] == "First video"

    # Incremental run: a new upload appears; the old one is untouched.
    client.items = [_item(vid="V1", published=T1), _item(vid="V2", title="Second", published=T2)]
    pipeline.run(youtube_source(channel_id="UCabc", api_key="k", client=client))

    rows = _read_rows(pipeline)
    assert set(rows) == {"V1", "V2"}
    assert rows["V2"]["title"] == "Second"


def test_pipeline_vanished_video_is_removed(dlt_mod, tmp_path):
    client = FakeYouTube(["UUabc"], [_item(vid="V1"), _item(vid="V2", published=T0)])
    pipeline = _make_pipeline(dlt_mod, tmp_path, "youtube_forget")
    pipeline.run(youtube_source(channel_id="UCabc", api_key="k", client=client))
    assert set(_read_rows(pipeline)) == {"V1", "V2"}

    # V1 vanished upstream (deleted / private); V2 remains untouched.
    client.items = [_item(vid="V2", published=T0)]
    pipeline.run(youtube_source(channel_id="UCabc", api_key="k", client=client))

    rows = _read_rows(pipeline)
    assert "V1" not in rows
    assert "V2" in rows


def test_client_key_and_base_shape():
    client = YouTubeClient("k", playlists=["UUabc"])
    captured = {}

    def fake_get(url, params, timeout):
        captured.update(url=url, params=params)
        return httpx.Response(200, json={"items": []}, request=httpx.Request("GET", url))

    import httpx as httpx_mod

    original = httpx_mod.get
    httpx_mod.get = fake_get
    try:
        client.playlist_items("UUabc")
    finally:
        httpx_mod.get = original
    assert captured["url"].endswith("/youtube/v3/playlistItems")
    assert captured["params"]["key"] == "k"
    assert captured["params"]["playlistId"] == "UUabc"
