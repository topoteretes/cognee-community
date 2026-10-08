"""Unit and integration tests for the YouTube dlt connector.

All tests are runnable in CI without live API credentials:
- DB-free tests for ISO duration parsing, content hashing, quota pacing, and captions fallback.
- API mocking for playlistItems pagination, incremental publishedAfter cursor, and etag watermark.
- dlt pipeline tests (sqlite destination) verifying full-snapshot forget-on-delete and merge-mode
  hard-delete.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag

from cognee_community_connector_youtube.auth import build_youtube_service
from cognee_community_connector_youtube.captions import fetch_video_captions
from cognee_community_connector_youtube.youtube import (
    YOUTUBE_SOURCE_NAME,
    YouTubeQuota,
    compute_content_hash,
    parse_duration_to_seconds,
    youtube_source,
)

# ---------------------------------------------------------------------------
# Test Doubles / Fakes
# ---------------------------------------------------------------------------


class FakeExecutable:
    def __init__(self, response: dict) -> None:
        self._response = response

    def execute(self) -> dict:
        return self._response


class FakeYouTubeService:
    """Mock Google API discovery client for YouTube Data API v3."""

    def __init__(
        self,
        playlists: dict[str, list[dict]] | None = None,
        videos: dict[str, dict] | None = None,
        channels: dict[str, dict] | None = None,
    ) -> None:
        self._playlists = playlists or {}
        self._videos = videos or {}
        self._channels = channels or {}

    def channels(self) -> Any:
        service_self = self

        class ChannelsResource:
            def list(self, id: str, part: str) -> FakeExecutable:
                ch = service_self._channels.get(id)
                items = [ch] if ch else []
                return FakeExecutable({"items": items})

        return ChannelsResource()

    def playlistItems(self) -> Any:
        service_self = self

        class PlaylistItemsResource:
            def list(
                self,
                playlistId: str,
                part: str,
                maxResults: int = 50,
                pageToken: str | None = None,
            ) -> FakeExecutable:
                items = service_self._playlists.get(playlistId, [])
                page_size = maxResults
                start = int(pageToken) if pageToken else 0
                page_items = items[start : start + page_size]
                next_token = str(start + page_size) if (start + page_size) < len(items) else None
                return FakeExecutable({"items": page_items, "nextPageToken": next_token})

        return PlaylistItemsResource()

    def videos(self) -> Any:
        service_self = self

        class VideosResource:
            def list(self, id: str, part: str, maxResults: int = 50) -> FakeExecutable:
                req_ids = [vid.strip() for vid in id.split(",") if vid.strip()]
                found = [
                    service_self._videos[vid] for vid in req_ids if vid in service_self._videos
                ]
                return FakeExecutable({"items": found})

        return VideosResource()


def _make_video_item(
    video_id: str,
    title: str = "Test Video",
    description: str = "Test Description",
    published_at: str = "2024-01-01T00:00:00Z",
    etag: str = "etag_1",
    duration: str = "PT10M",
    view_count: int = 100,
) -> dict:
    return {
        "id": video_id,
        "etag": etag,
        "snippet": {
            "title": title,
            "description": description,
            "publishedAt": published_at,
            "channelId": "UC_channel_123",
            "channelTitle": "Test Channel",
            "tags": ["python", "ai"],
        },
        "contentDetails": {
            "duration": duration,
        },
        "statistics": {
            "viewCount": str(view_count),
            "likeCount": "10",
        },
    }


def _make_playlist_item(video_id: str, published_at: str = "2024-01-01T00:00:00Z") -> dict:
    return {
        "contentDetails": {
            "videoId": video_id,
            "videoPublishedAt": published_at,
        },
        "snippet": {
            "resourceId": {"videoId": video_id},
            "publishedAt": published_at,
        },
    }


# ---------------------------------------------------------------------------
# Basic / Utility Tests
# ---------------------------------------------------------------------------


def test_document_source_attr():
    source = youtube_source(api_key="test_key", video_ids=["v1"])
    assert YOUTUBE_SOURCE_NAME == "youtube"
    assert document_source_tag(source) == "youtube"


def test_parse_duration_to_seconds():
    assert parse_duration_to_seconds("PT1H") == 3600
    assert parse_duration_to_seconds("PT1M30S") == 90
    assert parse_duration_to_seconds("PT45S") == 45
    assert parse_duration_to_seconds("P1DT2H3M4S") == 86400 + 7200 + 180 + 4
    assert parse_duration_to_seconds("") == 0
    assert parse_duration_to_seconds(None) == 0


def test_compute_content_hash():
    h1 = compute_content_hash("Title A", "Desc A")
    h2 = compute_content_hash("Title A", "Desc A")
    h3 = compute_content_hash("Title A", "Desc B")
    assert h1 == h2
    assert h1 != h3


def test_auth_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="YouTube API key required"):
        build_youtube_service(api_key=None)


# ---------------------------------------------------------------------------
# Acceptance Tests
# ---------------------------------------------------------------------------


def test_playlist_enumeration():
    """Verify playlistItems.list pagination retrieves all pages."""
    items = [
        _make_playlist_item("v1"),
        _make_playlist_item("v2"),
        _make_playlist_item("v3"),
    ]
    videos = {
        "v1": _make_video_item("v1"),
        "v2": _make_video_item("v2"),
        "v3": _make_video_item("v3"),
    }
    fake_service = FakeYouTubeService(playlists={"PL_test": items}, videos=videos)

    source = youtube_source(playlist_id="PL_test", client=fake_service)
    with patch(
        "cognee_community_connector_youtube.youtube.fetch_video_captions",
        return_value="captions",
    ):
        rows = list(source)

    assert len(rows) == 3
    assert [r["id"] for r in rows] == ["v1", "v2", "v3"]
    assert rows[0]["captions"] == "captions"


def test_publishedafter_cursor_filters_old_videos():
    """Incremental cursor: videos published before cursor are not yielded."""
    items = [
        _make_playlist_item("v_new", published_at="2024-06-01T00:00:00Z"),
        _make_playlist_item("v_old", published_at="2024-01-01T00:00:00Z"),
    ]
    videos = {
        "v_new": _make_video_item("v_new", published_at="2024-06-01T00:00:00Z"),
        "v_old": _make_video_item("v_old", published_at="2024-01-01T00:00:00Z"),
    }
    fake_service = FakeYouTubeService(playlists={"PL_test": items}, videos=videos)

    source = youtube_source(
        playlist_id="PL_test",
        published_after="2024-03-01T00:00:00Z",
        client=fake_service,
    )
    with patch("cognee_community_connector_youtube.youtube.fetch_video_captions", return_value=""):
        rows = list(source)

    assert len(rows) == 1
    assert rows[0]["id"] == "v_new"


def test_etag_watermark_skips_caption_refetch(dlt_mod, tmp_path):
    """Unchanged etag -> 0 caption API calls on re-sync."""
    video = _make_video_item("v1", etag="etag_100")
    fake_service = FakeYouTubeService(videos={"v1": video})

    db_path = (tmp_path / "watermark.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="yt_watermark",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="yt_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    caption_mock = MagicMock(return_value="transcribed words")

    with patch("cognee_community_connector_youtube.youtube.fetch_video_captions", caption_mock):
        pipeline.run(youtube_source(video_ids=["v1"], client=fake_service))
        assert caption_mock.call_count == 1

        # Re-sync with identical etag
        pipeline.run(youtube_source(video_ids=["v1"], client=fake_service))
        # Caption fetch count must remain 1 (no extra call)
        assert caption_mock.call_count == 1


def test_etag_mismatch_triggers_refetch(dlt_mod, tmp_path):
    """Changed etag and changed content -> captions re-fetched and watermark updated."""
    video_v1 = _make_video_item("v1", title="Original Title", etag="etag_v1")
    fake_service = FakeYouTubeService(videos={"v1": video_v1})

    db_path = (tmp_path / "mismatch.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="yt_mismatch",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="yt_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    caption_mock = MagicMock(side_effect=["captions v1", "captions v2"])

    with patch("cognee_community_connector_youtube.youtube.fetch_video_captions", caption_mock):
        pipeline.run(youtube_source(video_ids=["v1"], client=fake_service))
        assert caption_mock.call_count == 1

        # Update video title and etag
        video_v2 = _make_video_item("v1", title="Updated Title", etag="etag_v2")
        fake_service._videos["v1"] = video_v2

        pipeline.run(youtube_source(video_ids=["v1"], client=fake_service))
        assert caption_mock.call_count == 2


def test_etag_mismatch_volatile_stats_only_keeps_captions(dlt_mod, tmp_path):
    """Changed etag with identical title/desc (e.g. view count bump) -> keep cached captions."""
    video_v1 = _make_video_item("v1", etag="etag_v1", view_count=100)
    fake_service = FakeYouTubeService(videos={"v1": video_v1})

    db_path = (tmp_path / "volatile.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="yt_volatile",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="yt_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    caption_mock = MagicMock(return_value="cached transcript")

    with patch("cognee_community_connector_youtube.youtube.fetch_video_captions", caption_mock):
        pipeline.run(youtube_source(video_ids=["v1"], client=fake_service))
        assert caption_mock.call_count == 1

        # Only view count and etag changed, title & description remain identical
        video_v2 = _make_video_item("v1", etag="etag_v2", view_count=200)
        fake_service._videos["v1"] = video_v2

        pipeline.run(youtube_source(video_ids=["v1"], client=fake_service))
        # Captions should NOT be re-fetched!
        assert caption_mock.call_count == 1


def test_deleted_video_falls_out_of_snapshot(dlt_mod, tmp_path):
    """replace mode: absent video removed from destination table."""
    items_run1 = [_make_playlist_item("v1"), _make_playlist_item("v2")]
    videos = {"v1": _make_video_item("v1"), "v2": _make_video_item("v2")}
    fake_service = FakeYouTubeService(playlists={"PL_replace": items_run1}, videos=videos)

    db_path = (tmp_path / "replace.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="yt_replace",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="yt_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    with patch("cognee_community_connector_youtube.youtube.fetch_video_captions", return_value=""):
        pipeline.run(youtube_source(playlist_id="PL_replace", client=fake_service))

        with (
            pipeline.sql_client() as client,
            client.execute_query("SELECT id FROM youtube_videos") as cur,
        ):
            rows = [r[0] for r in cur.fetchall()]
        assert set(rows) == {"v1", "v2"}

        # v1 is deleted / dropped from playlist
        fake_service._playlists["PL_replace"] = [_make_playlist_item("v2")]

        pipeline.run(youtube_source(playlist_id="PL_replace", client=fake_service))

        with (
            pipeline.sql_client() as client,
            client.execute_query("SELECT id FROM youtube_videos") as cur,
        ):
            rows = [r[0] for r in cur.fetchall()]
        assert rows == ["v2"]


def test_deleted_video_hard_delete_marker():
    """merge mode: missing or private tracked video emits _deleted=True."""
    videos = {"v1": _make_video_item("v1")}
    fake_service = FakeYouTubeService(videos=videos)

    source = youtube_source(video_ids=["v1", "v2_deleted"], client=fake_service)
    with patch("cognee_community_connector_youtube.youtube.fetch_video_captions", return_value=""):
        rows = list(source)

    # v2_deleted was requested in video_ids but missing from API -> emits _deleted=True
    deleted_rows = [r for r in rows if r["_deleted"] is True]
    live_rows = [r for r in rows if r["_deleted"] is False]

    assert len(deleted_rows) == 1
    assert deleted_rows[0]["id"] == "v2_deleted"
    assert len(live_rows) == 1
    assert live_rows[0]["id"] == "v1"


def test_missing_captions_not_an_error():
    """No transcript available -> document is still cleanly ingested with empty captions."""
    video = _make_video_item("v_no_cap", title="Video No Captions", description="Desc")
    fake_service = FakeYouTubeService(videos={"v_no_cap": video})

    source = youtube_source(video_ids=["v_no_cap"], client=fake_service)
    with patch("cognee_community_connector_youtube.youtube.fetch_video_captions", return_value=""):
        rows = list(source)

    assert len(rows) == 1
    assert rows[0]["id"] == "v_no_cap"
    assert rows[0]["captions"] == ""
    assert "Video No Captions" in rows[0]["content"]
    assert "Desc" in rows[0]["content"]


def test_quota_pacing():
    """YouTubeQuota rate-limits calls and enforces limits."""
    quota = YouTubeQuota(daily_limit=5, rate_limit_per_second=20.0)

    start = time.time()
    for _ in range(4):
        quota.consume(1)
    duration = time.time() - start

    assert quota.used_units == 4
    assert duration >= 0.1  # Pacing should have taken at least 3 intervals

    # Exceeding quota raises RuntimeError
    with pytest.raises(RuntimeError, match="YouTube quota exceeded"):
        quota.consume(2)


def test_watermark_not_updated_on_failure(dlt_mod, tmp_path):
    """Exception mid-run -> dlt doesn't commit resource_state."""
    items = [_make_playlist_item("v1")]
    videos = {"v1": _make_video_item("v1")}
    fake_service = FakeYouTubeService(playlists={"PL_fail": items}, videos=videos)

    db_path = (tmp_path / "fail.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="yt_fail",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="yt_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    def exploding_captions(video_id, **kwargs):
        raise ValueError("Fatal error mid-stream")

    with (
        patch(
            "cognee_community_connector_youtube.youtube.fetch_video_captions",
            exploding_captions,
        ),
        pytest.raises(Exception),  # noqa: B017
    ):
        pipeline.run(youtube_source(playlist_id="PL_fail", client=fake_service))

    # Pipeline state should have no committed watermark for v1
    # Verify by running normally afterwards: captions will be called because nothing was saved
    normal_captions = MagicMock(return_value="recovered")
    with patch("cognee_community_connector_youtube.youtube.fetch_video_captions", normal_captions):
        pipeline.run(youtube_source(playlist_id="PL_fail", client=fake_service))
        assert normal_captions.call_count == 1


def test_captions_fetch_handles_exceptions():
    """Verify fetch_video_captions gracefully catches transcript exceptions."""
    with patch("youtube_transcript_api.YouTubeTranscriptApi.list_transcripts") as mock_list:
        mock_list.side_effect = Exception("Disabled")
        result = fetch_video_captions("v_err")
        assert result == ""
