import sys
import types
from io import BytesIO
from urllib.error import HTTPError

import pytest

from cognee_community_connector_youtube.youtube import (
    _api_get,
    _fetch_transcript,
    _list_upload_ids,
    _search_since,
    _uploads_playlist,
    _video_to_row,
    youtube_source,
)


def test_api_get_does_not_expose_api_key_on_auth_or_quota_failure(monkeypatch):
    error = HTTPError(
        "https://www.googleapis.com/youtube/v3/videos",
        403,
        "Forbidden",
        {},
        BytesIO(b'{"error":{"message":"invalid key secret-key"}}'),
    )

    def raise_error(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("cognee_community_connector_youtube.youtube.urlopen", raise_error)

    with pytest.raises(RuntimeError, match="HTTP 403") as raised:
        _api_get("secret-key", "videos", part="snippet")
    assert "secret-key" not in str(raised.value)
    assert "[redacted]" in str(raised.value)


def test_api_get_surfaces_quota_errors(monkeypatch):
    error = HTTPError(
        "https://www.googleapis.com/youtube/v3/search",
        403,
        "Forbidden",
        {},
        BytesIO(b'{"error":{"message":"quotaExceeded"}}'),
    )

    def raise_error(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("cognee_community_connector_youtube.youtube.urlopen", raise_error)

    with pytest.raises(
        RuntimeError,
        match=r"YouTube Data API search failed \(HTTP 403\): quotaExceeded",
    ):
        _api_get("test-key", "search", part="id")


def test_playlist_pagination_includes_final_partial_page(monkeypatch):
    requests = []
    responses = [
        {"items": [{"contentDetails": {"videoId": "v1"}}], "nextPageToken": "next"},
        {
            "items": [
                {"contentDetails": {"videoId": "v2"}},
                {"snippet": {}},
                None,
            ]
        },
    ]

    def fake_api_get(api_key, method, **params):
        requests.append((api_key, method, params))
        return responses.pop(0)

    monkeypatch.setattr("cognee_community_connector_youtube.youtube._api_get", fake_api_get)

    assert _list_upload_ids("test-key", "uploads") == {"v1", "v2"}
    assert requests[0][2]["maxResults"] == 50
    assert requests[1][2]["pageToken"] == "next"
    assert requests[1][2]["playlistId"] == "uploads"


def test_empty_upload_playlist_and_channel(monkeypatch):
    monkeypatch.setattr(
        "cognee_community_connector_youtube.youtube._api_get",
        lambda *_args, **_kwargs: {"items": []},
    )
    assert _list_upload_ids("test-key", "uploads") == set()
    with pytest.raises(ValueError, match="No public uploads playlist"):
        _uploads_playlist("test-key", "channel")


def test_search_since_paginates_and_passes_published_after(monkeypatch):
    requests = []
    responses = [
        {"items": [{"id": {"videoId": "v1"}}], "nextPageToken": "next"},
        {"items": [{"id": {"videoId": "v2"}}, {"id": "malformed"}]},
    ]

    def fake_api_get(api_key, method, **params):
        requests.append((method, params))
        return responses.pop(0)

    monkeypatch.setattr("cognee_community_connector_youtube.youtube._api_get", fake_api_get)

    assert _search_since("test-key", "channel", "2026-10-01T00:00:00Z") == {"v1", "v2"}
    assert requests[0][1]["publishedAfter"] == "2026-10-01T00:00:00Z"
    assert requests[1][1]["pageToken"] == "next"


def test_video_mapping_keeps_unicode_and_optional_metadata():
    row = _video_to_row(
        {
            "id": "video-1",
            "snippet": {
                "title": "Résumé 東京 🎥",
                "description": "Descripción\n第二行",
                "publishedAt": "2026-10-01T00:00:00Z",
                "tags": ["café", "世界"],
            },
            "statistics": {"viewCount": "42"},
        },
        include_description=True,
        captions="字幕 text",
    )

    assert row["id"] == "video-1"
    assert row["content"] == "Résumé 東京 🎥\n\nDescripción\n第二行\n\n字幕 text"
    assert row["tags"] == "café, 世界"
    assert row["duration"] is None
    assert row["video_url"] == "https://www.youtube.com/watch?v=video-1"

    without_description = _video_to_row(
        {"id": "video-2", "snippet": {"title": "Title"}}, include_description=False
    )
    assert without_description["description"] == ""
    assert without_description["content"] == "Title"
    assert without_description["published_at"] is None


def test_video_mapping_requires_stable_identity():
    with pytest.raises(ValueError, match="valid id"):
        _video_to_row({"snippet": {}}, include_description=True)


def test_fetch_transcript_treats_disabled_or_missing_captions_as_empty(monkeypatch):
    class TranscriptNotFoundError(Exception):
        pass

    class TranscriptsDisabledError(Exception):
        pass

    class VideoUnavailableError(Exception):
        pass

    class AgeRestrictedError(Exception):
        pass

    class VideoUnplayableError(Exception):
        pass

    class Api:
        def fetch(self, _video_id):
            raise TranscriptsDisabledError()

    module = types.ModuleType("youtube_transcript_api")
    module.NoTranscriptFound = TranscriptNotFoundError
    module.TranscriptsDisabled = TranscriptsDisabledError
    module.VideoUnavailable = VideoUnavailableError
    module.AgeRestricted = AgeRestrictedError
    module.VideoUnplayable = VideoUnplayableError
    module.YouTubeTranscriptApi = Api
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", module)

    assert _fetch_transcript("v1") == ""


def test_fetch_transcript_treats_upcoming_live_event_as_unavailable(monkeypatch):
    from youtube_transcript_api import VideoUnplayable

    class Api:
        def fetch(self, video_id):
            raise VideoUnplayable(
                video_id,
                "This live event will begin in a few moments.",
                [],
            )

    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", Api)

    assert _fetch_transcript("UgHmZZXHX-4") == ""


def test_fetch_transcript_treats_age_restricted_video_as_unavailable(monkeypatch):
    from youtube_transcript_api import AgeRestricted

    class Api:
        def fetch(self, video_id):
            raise AgeRestricted(video_id)

    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", Api)

    assert _fetch_transcript("v1") == ""


def test_fetch_transcript_joins_caption_segments(monkeypatch):
    class Api:
        def fetch(self, _video_id):
            return [
                types.SimpleNamespace(text="こんにちは"),
                types.SimpleNamespace(text="world"),
            ]

    module = types.ModuleType("youtube_transcript_api")
    module.NoTranscriptFound = type("NoTranscriptFound", (Exception,), {})
    module.TranscriptsDisabled = type("TranscriptsDisabled", (Exception,), {})
    module.VideoUnavailable = type("VideoUnavailable", (Exception,), {})
    module.AgeRestricted = type("AgeRestricted", (Exception,), {})
    module.VideoUnplayable = type("VideoUnplayable", (Exception,), {})
    module.YouTubeTranscriptApi = Api
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", module)

    assert _fetch_transcript("v1") == "こんにちは world"


def test_fetch_transcript_propagates_unexpected_fetch_failures(monkeypatch):
    class Api:
        def fetch(self, _video_id):
            raise RuntimeError("temporarily blocked")

    module = types.ModuleType("youtube_transcript_api")
    module.NoTranscriptFound = type("NoTranscriptFound", (Exception,), {})
    module.TranscriptsDisabled = type("TranscriptsDisabled", (Exception,), {})
    module.VideoUnavailable = type("VideoUnavailable", (Exception,), {})
    module.AgeRestricted = type("AgeRestricted", (Exception,), {})
    module.VideoUnplayable = type("VideoUnplayable", (Exception,), {})
    module.YouTubeTranscriptApi = Api
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", module)

    with pytest.raises(RuntimeError, match="temporarily blocked"):
        _fetch_transcript("v1")


@pytest.mark.parametrize("error_name", ["rate_limit", "request_failure", "parse_failure"])
def test_fetch_transcript_propagates_transcript_api_failures(monkeypatch, error_name):
    from requests.exceptions import HTTPError as RequestsHTTPError
    from youtube_transcript_api import (
        IpBlocked,
        YouTubeDataUnparsable,
        YouTubeRequestFailed,
    )

    errors = {
        "rate_limit": IpBlocked("v1"),
        "request_failure": YouTubeRequestFailed("v1", RequestsHTTPError("network failure")),
        "parse_failure": YouTubeDataUnparsable("v1"),
    }
    error = errors[error_name]

    class Api:
        def fetch(self, _video_id):
            raise error

    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", Api)

    with pytest.raises(type(error)):
        _fetch_transcript("v1")


def test_source_requires_channel_and_api_key(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_CHANNEL_ID", raising=False)

    with pytest.raises(ValueError, match="channel_id required"):
        youtube_source(api_key="test-key")
    with pytest.raises(ValueError, match="API key required"):
        youtube_source(channel_id="channel")


def test_source_resource_uses_merge_and_hard_delete():
    pytest.importorskip("dlt")
    resource = youtube_source("channel", api_key="test-key", include_captions=False)
    schema = resource.compute_table_schema()

    assert resource.name == "youtube_videos"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True
    disposition = schema["write_disposition"]
    if isinstance(disposition, dict):
        disposition = disposition.get("disposition")
    assert disposition == "merge"


def test_dlt_source_syncs_incrementally_and_forgets_removed_videos(tmp_path, monkeypatch):
    dlt = pytest.importorskip("dlt")
    calls = []
    search_count = 0

    def fake_api_get(api_key, method, **params):
        nonlocal search_count
        calls.append((method, params.copy()))
        if method == "channels":
            return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "uploads"}}}]}
        if method == "playlistItems":
            if len([call for call in calls if call[0] == "playlistItems"]) == 1:
                ids = ["v1", "v2"]
            elif len([call for call in calls if call[0] == "playlistItems"]) == 2:
                ids = ["v1", "v3"]
            else:
                ids = ["v1", "v3"]
            return {"items": [{"contentDetails": {"videoId": value}} for value in ids]}
        if method == "search":
            assert params["publishedAfter"].endswith("Z")
            search_count += 1
            return {"items": [{"id": {"videoId": "v3"}}] if search_count == 1 else []}
        if method == "videos":
            return {
                "items": [
                    {
                        "id": video_id,
                        "snippet": {
                            "title": f"Title {video_id}",
                            "description": "Description",
                        },
                    }
                    for video_id in params["id"].split(",")
                ]
            }
        raise AssertionError(f"Unexpected API method: {method}")

    monkeypatch.setattr("cognee_community_connector_youtube.youtube._api_get", fake_api_get)
    db_path = tmp_path / "youtube.db"
    pipeline_dir = str(tmp_path / "state")

    def run_sync():
        pipeline = dlt.pipeline(
            pipeline_name="youtube_sync_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
            dataset_name="youtube",
            pipelines_dir=pipeline_dir,
        )
        pipeline.run(youtube_source("channel", api_key="test-key", include_captions=False))
        with pipeline.sql_client() as client:
            return client.execute_sql("SELECT id, title FROM youtube_videos ORDER BY id")

    assert [row[0] for row in run_sync()] == ["v1", "v2"]
    rows = run_sync()
    assert rows == [("v1", "Title v1"), ("v3", "Title v3")]
    assert [row[0] for row in run_sync()] == ["v1", "v3"]
    assert [method for method, _ in calls].count("search") == 2
