"""YouTube channel source for cognee."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = logging.getLogger("youtube_connector")

_API_URL = "https://www.googleapis.com/youtube/v3"
_VIDEO_PARTS = "contentDetails,snippet,statistics"


def _api_get(api_key: str, method: str, **params: str | int) -> dict[str, Any]:
    """Make a YouTube Data API request without exposing the key in errors."""
    url = f"{_API_URL}/{method}?{urlencode({**params, 'key': api_key})}"
    try:
        with urlopen(url, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        try:
            body = json.load(exc)
        except (ValueError, OSError):
            body = {}
        error = body.get("error", {}) if isinstance(body, dict) else {}
        message = (
            error.get("message", "request failed") if isinstance(error, dict) else "request failed"
        )
        message = str(message).replace(api_key, "[redacted]")
        raise RuntimeError(
            f"YouTube Data API {method} failed (HTTP {exc.code}): {message}"
        ) from None
    except URLError:
        raise RuntimeError(f"YouTube Data API {method} request failed.") from None
    except (ValueError, OSError):
        raise RuntimeError(f"YouTube Data API {method} returned invalid JSON.") from None

    if not isinstance(result, dict):
        raise RuntimeError(f"YouTube Data API {method} response must be a JSON object.")
    return result


def _uploads_playlist(api_key: str, channel_id: str) -> str:
    response = _api_get(api_key, "channels", part="contentDetails", id=channel_id)
    channels = response.get("items", [])
    if not isinstance(channels, list):
        raise RuntimeError("YouTube channels response has an invalid items field.")
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        content_details = channel.get("contentDetails")
        playlists = (
            content_details.get("relatedPlaylists") if isinstance(content_details, dict) else None
        )
        playlist_id = playlists.get("uploads") if isinstance(playlists, dict) else None
        if isinstance(playlist_id, str) and playlist_id:
            return playlist_id
    raise ValueError(f"No public uploads playlist was found for channel {channel_id!r}.")


def _list_upload_ids(api_key: str, playlist_id: str) -> set[str]:
    """Read every current video ID; this snapshot is used to find removals."""
    video_ids = set()
    page_token = None
    seen_tokens = set()
    while True:
        params: dict[str, str | int] = {
            "part": "contentDetails",
            "maxResults": 50,
            "playlistId": playlist_id,
        }
        if page_token:
            params["pageToken"] = page_token
        response = _api_get(api_key, "playlistItems", **params)
        items = response.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError("YouTube playlistItems response has an invalid items field.")
        for item in items:
            if not isinstance(item, dict):
                continue
            details = item.get("contentDetails")
            video_id = details.get("videoId") if isinstance(details, dict) else None
            if isinstance(video_id, str) and video_id:
                video_ids.add(video_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            return video_ids
        if not isinstance(page_token, str):
            raise RuntimeError("YouTube playlistItems response has an invalid page token.")
        if page_token in seen_tokens:
            raise RuntimeError("YouTube playlistItems response repeated a pagination token.")
        seen_tokens.add(page_token)


def _search_since(api_key: str, channel_id: str, published_after: str) -> set[str]:
    """Read videos published since the last successful sync."""
    video_ids = set()
    page_token = None
    seen_tokens = set()
    while True:
        params: dict[str, str | int] = {
            "channelId": channel_id,
            "maxResults": 50,
            "order": "date",
            "part": "id",
            "publishedAfter": published_after,
            "type": "video",
        }
        if page_token:
            params["pageToken"] = page_token
        response = _api_get(api_key, "search", **params)
        items = response.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError("YouTube search response has an invalid items field.")
        for item in items:
            if isinstance(item, dict):
                result_id = item.get("id")
                video_id = result_id.get("videoId") if isinstance(result_id, dict) else None
                if isinstance(video_id, str) and video_id:
                    video_ids.add(video_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            return video_ids
        if not isinstance(page_token, str) or page_token in seen_tokens:
            raise RuntimeError("YouTube search response has an invalid pagination token.")
        seen_tokens.add(page_token)


def _video_details(api_key: str, video_ids: set[str]) -> list[dict[str, Any]]:
    """Fetch metadata in API-supported batches of at most 50 IDs."""
    videos = []
    sorted_ids = sorted(video_ids)
    for start in range(0, len(sorted_ids), 50):
        response = _api_get(
            api_key,
            "videos",
            id=",".join(sorted_ids[start : start + 50]),
            part=_VIDEO_PARTS,
        )
        items = response.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError("YouTube videos response has an invalid items field.")
        videos.extend(
            item
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
        )
    return videos


def _fetch_transcript(video_id: str) -> str:
    """Fetch public captions through youtube-transcript-api when available."""
    try:
        from youtube_transcript_api import (
            AgeRestricted,
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
            VideoUnplayable,
            YouTubeTranscriptApi,
        )
    except ImportError as exc:
        raise ImportError(
            "Captions require youtube-transcript-api, included with "
            "`cognee-community-connector-youtube`."
        ) from exc

    try:
        transcript = YouTubeTranscriptApi().fetch(video_id)
    except (
        AgeRestricted,
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
        VideoUnplayable,
    ):
        return ""
    return " ".join(segment.text for segment in transcript if segment.text)


def _video_to_row(
    video: dict[str, Any], *, include_description: bool, captions: str = ""
) -> dict[str, Any]:
    """Map one API video to a stable document row."""
    video_id = video.get("id")
    if not isinstance(video_id, str) or not video_id:
        raise ValueError("YouTube video is missing a valid id.")
    snippet = video.get("snippet")
    snippet = snippet if isinstance(snippet, dict) else {}
    details = video.get("contentDetails")
    details = details if isinstance(details, dict) else {}
    statistics = video.get("statistics")
    statistics = statistics if isinstance(statistics, dict) else {}
    title = snippet.get("title") or ""
    title = title if isinstance(title, str) else ""
    description = (snippet.get("description") or "") if include_description else ""
    description = description if isinstance(description, str) else ""
    captions = captions if isinstance(captions, str) else ""
    tags = snippet.get("tags") or []
    tags = ", ".join(tag for tag in tags if isinstance(tag, str)) if isinstance(tags, list) else ""
    content = "\n\n".join(part for part in (title, description, captions) if part)
    return {
        "id": video_id,
        "title": title,
        "content": content,
        "description": description,
        "captions": captions,
        "published_at": snippet.get("publishedAt"),
        "channel_id": snippet.get("channelId"),
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "duration": details.get("duration"),
        "category_id": snippet.get("categoryId"),
        "tags": tags,
        "view_count": statistics.get("viewCount"),
        "_deleted": False,
    }


def youtube_source(
    channel_id: str | None = None,
    *,
    api_key: str | None = None,
    include_description: bool = True,
    include_captions: bool = True,
):
    """Create a DLT source for public videos from one YouTube channel.

    Args:
        channel_id: Public YouTube channel ID to sync.
        api_key: YouTube Data API key; defaults to ``YOUTUBE_API_KEY``.
        include_description: Include video descriptions in document text.
        include_captions: Fetch public captions via the optional transcript
            package. The official captions API requires OAuth instead.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The YouTube connector requires dlt. Install `cognee-community-connector-youtube`."
        ) from exc

    resolved_channel_id = channel_id or os.environ.get("YOUTUBE_CHANNEL_ID")
    resolved_api_key = api_key or os.environ.get("YOUTUBE_API_KEY")
    if not resolved_channel_id:
        raise ValueError("YouTube channel_id required: pass channel_id= or set YOUTUBE_CHANNEL_ID.")
    if not resolved_api_key:
        raise ValueError("YouTube API key required: pass api_key= or set YOUTUBE_API_KEY.")

    @dlt.resource(
        name="youtube_videos",
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def youtube_videos() -> Iterator[dict[str, Any]]:
        state = dlt.current.resource_state()
        previous_channel = state.get("channel_id")
        selection = {
            "include_description": include_description,
            "include_captions": include_captions,
        }
        if previous_channel is not None and previous_channel != resolved_channel_id:
            raise ValueError(
                "YouTube channel changed for this saved sync state; use a fresh pipeline."
            )
        if state.get("selection", selection) != selection:
            raise ValueError("YouTube content selection changed; use a fresh pipeline.")

        started_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        playlist_id = _uploads_playlist(resolved_api_key, resolved_channel_id)
        current_ids = _list_upload_ids(resolved_api_key, playlist_id)
        previous_ids = set(state.get("video_ids", []))
        published_after = state.get("published_after")
        if not published_after:
            changed_ids = current_ids
        else:
            changed_ids = _search_since(resolved_api_key, resolved_channel_id, published_after) | (
                current_ids - previous_ids
            )
        changed_ids &= current_ids - previous_ids
        videos = _video_details(resolved_api_key, changed_ids)

        rows = []
        for video in videos:
            if not isinstance(video.get("id"), str) or not video["id"]:
                logger.warning("YouTube: skipping malformed video record without an id.")
                continue
            captions = _fetch_transcript(video["id"]) if include_captions else ""
            try:
                rows.append(
                    _video_to_row(video, include_description=include_description, captions=captions)
                )
            except ValueError as exc:
                logger.warning("YouTube: skipping malformed video record: %s", exc)
        rows.extend({"id": video_id, "_deleted": True} for video_id in previous_ids - current_ids)

        yield from rows
        # Advance only after every page, metadata lookup, and requested transcript
        # has completed. A failure leaves the old cursor available for retry.
        state["channel_id"] = resolved_channel_id
        state["selection"] = selection
        state["video_ids"] = sorted(current_ids)
        state["published_after"] = started_at
        logger.info("YouTube: synced %d video change(s).", len(rows))

    resource = youtube_videos()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "youtube")
    return resource
