"""DLT source for YouTube videos (incremental sync, captions, and forget-on-delete).

Fetches YouTube video metadata, descriptions, and captions, and yields them as a
dlt resource for cognee's ingestion pipeline.

Videos are ingested as *normal documents*: the source declares
``cognee_document_source = "youtube"``, so ``resolve_dlt_sources`` tags each row
``external_metadata["source"] = "youtube"``. Each video flows through the standard
cognify entity-extraction pipeline instead of the deterministic dlt-row path.

Two sync strategies are supported:
1. Playlist / Channel snapshot (``write_disposition="replace"``, default for channels/playlists):
   Walks the uploads playlist. Videos deleted or made private upstream drop out of the
   snapshot, and cognee's ``orphan_cleanup`` purges them from the graph and vector stores.
2. Tracked video list (``write_disposition="merge"``, default for explicit video_ids):
   Validates tracked video IDs against the YouTube API. Any video that no longer exists
   or is private is emitted with ``_deleted=True`` (hard-delete marker), causing dlt to
   remove it on merge.

Incremental sync:
- `published_after`: Filters out videos published before the cursor timestamp.
- Watermark optimization: Caches etag and content hash in dlt `resource_state`.
  Unchanged etags skip caption fetching entirely (0 extra API calls). If the etag
  changes only because of volatile statistics (views/likes), the content hash matches
  and caption re-fetching is skipped.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_youtube.auth import build_youtube_service
from cognee_community_connector_youtube.captions import fetch_video_captions

logger = get_logger("youtube_connector")

YOUTUBE_TABLE_NAME = "youtube_videos"
YOUTUBE_SOURCE_NAME = "youtube"

_MAX_RETRIES = 5
_EXTRA_HINT = (
    'The YouTube connector requires the "youtube" extra: pip install "cognee[youtube]" '
    "(provides google-api-python-client and youtube-transcript-api)."
)


# ---------------------------------------------------------------------------
# Quota management
# ---------------------------------------------------------------------------
class YouTubeQuota:
    """Moving-window quota limiter to keep API consumption within limits.

    The YouTube Data API v3 enforces a 10,000 units/day default quota:
    - playlistItems.list: 1 unit
    - videos.list: 1 unit (up to 50 videos per call)
    - channels.list: 1 unit
    """

    def __init__(
        self,
        daily_limit: int = 10000,
        rate_limit_per_second: float = 10.0,
    ) -> None:
        self.daily_limit = daily_limit
        self.rate_limit_per_second = rate_limit_per_second
        self.used_units = 0
        self._last_call_time = 0.0

    def consume(self, units: int = 1) -> None:
        """Consume quota units, applying rate-limit pacing and bounds check."""
        if self.used_units + units > self.daily_limit:
            raise RuntimeError(
                f"YouTube quota exceeded: attempted {units} units, but already used "
                f"{self.used_units}/{self.daily_limit} units."
            )

        now = time.time()
        min_interval = 1.0 / self.rate_limit_per_second
        elapsed = now - self._last_call_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

        self.used_units += units
        self._last_call_time = time.time()


# ---------------------------------------------------------------------------
# Parsing and hashing utilities
# ---------------------------------------------------------------------------
_ISO8601_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_duration_to_seconds(duration_str: str | None) -> int:
    """Parse an ISO 8601 duration string (e.g. 'PT1H2M30S', 'PT45S') to seconds."""
    if not duration_str:
        return 0

    match = _ISO8601_DURATION_RE.match(duration_str)
    if not match:
        return 0

    parts = match.groupdict()
    days = int(parts.get("days") or 0)
    hours = int(parts.get("hours") or 0)
    minutes = int(parts.get("minutes") or 0)
    seconds = int(parts.get("seconds") or 0)

    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def compute_content_hash(title: str, description: str) -> str:
    """Compute sha256 hash over the text fields that get ingested into cognify."""
    content_str = f"{title or ''}\n{description or ''}"
    return hashlib.sha256(content_str.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# API request helpers with retry & backoff
# ---------------------------------------------------------------------------
def _is_transient_error(exc: Exception) -> bool:
    """Check if an error from the YouTube client is transient and retryable."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in (429, 500, 502, 503, 504):
        return True
    exc_str = str(exc).lower()
    return "rate limit" in exc_str or ("quota exceeded" not in exc_str and "timed out" in exc_str)


def _execute_with_retry(request: Any, quota: YouTubeQuota | None = None, units: int = 1) -> dict:
    """Execute a Google API request with quota pacing and exponential backoff."""
    if quota:
        quota.consume(units)

    for attempt in range(_MAX_RETRIES):
        try:
            return request.execute()
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient_error(exc):
                raise
            delay = 2**attempt
            logger.warning(
                "YouTube API error: %s — retrying in %.1fs (%d/%d)",
                exc,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)

    raise RuntimeError("Retry budget exhausted")


# ---------------------------------------------------------------------------
# Video fetching and synchronization
# ---------------------------------------------------------------------------
def _resolve_uploads_playlist_id(
    service: Any,
    channel_id: str,
    quota: YouTubeQuota | None = None,
) -> str:
    """Find the uploads playlist ID for a given channel ID."""
    # Fast path: YouTube channel ID UC... maps to uploads playlist UU...
    if channel_id.startswith("UC") and len(channel_id) == 24:
        return "UU" + channel_id[2:]

    # API fallback
    req = service.channels().list(id=channel_id, part="contentDetails")
    res = _execute_with_retry(req, quota=quota, units=1)
    items = res.get("items", [])
    if not items:
        raise ValueError(f"Channel not found: {channel_id}")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def _iter_playlist_video_ids(
    service: Any,
    playlist_id: str,
    published_after: str | None = None,
    quota: YouTubeQuota | None = None,
) -> Iterator[str]:
    """Iterate video IDs from a playlist, filtering by published_after when provided."""
    page_token = None
    while True:
        req = service.playlistItems().list(
            playlistId=playlist_id,
            part="snippet,contentDetails",
            maxResults=50,
            pageToken=page_token,
        )
        res = _execute_with_retry(req, quota=quota, units=1)
        items = res.get("items", [])

        for item in items:
            content_details = item.get("contentDetails", {})
            snippet = item.get("snippet", {})
            video_id = content_details.get("videoId") or snippet.get("resourceId", {}).get(
                "videoId"
            )
            published_at = content_details.get("videoPublishedAt") or snippet.get("publishedAt")

            if not video_id:
                continue

            if published_after and published_at and published_at <= published_after:
                # Playlist items are generally in reverse chronological order; skip older
                continue

            yield video_id

        page_token = res.get("nextPageToken")
        if not page_token:
            break


def _fetch_videos_batch(
    service: Any,
    video_ids: Sequence[str],
    quota: YouTubeQuota | None = None,
) -> list[dict]:
    """Fetch video resources in batches of up to 50."""
    results = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        req = service.videos().list(
            id=",".join(chunk),
            part="snippet,contentDetails,statistics",
            maxResults=50,
        )
        res = _execute_with_retry(req, quota=quota, units=1)
        results.extend(res.get("items", []))
    return results


def _build_video_row(
    video: dict,
    captions: str,
    _deleted: bool = False,
) -> dict[str, Any]:
    """Build a document row matching cognee document expectations."""
    video_id = video.get("id")
    snippet = video.get("snippet", {})
    content_details = video.get("contentDetails", {})
    statistics = video.get("statistics", {})

    title = snippet.get("title", "")
    description = snippet.get("description", "")

    # Clean combined document content for entity extraction
    content_parts = [title]
    if description:
        content_parts.append(description)
    if captions:
        content_parts.append(captions)
    content = "\n\n".join(content_parts)

    duration_iso = content_details.get("duration", "")
    duration_sec = parse_duration_to_seconds(duration_iso)
    tags = ", ".join(snippet.get("tags", [])) if snippet.get("tags") else ""

    try:
        view_count = int(statistics.get("viewCount", 0))
    except (ValueError, TypeError):
        view_count = 0

    try:
        like_count = int(statistics.get("likeCount", 0))
    except (ValueError, TypeError):
        like_count = 0

    return {
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "description": description,
        "content": content,
        "captions": captions,
        "channel_id": snippet.get("channelId", ""),
        "channel_title": snippet.get("channelTitle", ""),
        "published_at": snippet.get("publishedAt", ""),
        "duration": duration_iso,
        "duration_seconds": duration_sec,
        "tags": tags,
        "view_count": view_count,
        "like_count": like_count,
        "_deleted": _deleted,
    }


def _deleted_row(video_id: str) -> dict[str, Any]:
    """Emit a hard-delete row instructing dlt to purge the video."""
    return {
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "",
        "description": "",
        "content": "",
        "captions": "",
        "channel_id": "",
        "channel_title": "",
        "published_at": "",
        "duration": "",
        "duration_seconds": 0,
        "tags": "",
        "view_count": 0,
        "like_count": 0,
        "_deleted": True,
    }


# ---------------------------------------------------------------------------
# Public dlt source factory
# ---------------------------------------------------------------------------
def youtube_source(
    api_key: str | None = None,
    channel_id: str | None = None,
    playlist_id: str | None = None,
    video_ids: list[str] | None = None,
    published_after: str | datetime | None = None,
    write_disposition: str | None = None,
    caption_languages: Sequence[str] = ("en",),
    client: Any = None,
    quota: YouTubeQuota | None = None,
):
    """Create a dlt source that yields YouTube videos as documents.

    Args:
        api_key: YouTube Data API key. Falls back to ``YOUTUBE_API_KEY`` env var.
        channel_id: Ingest videos from this YouTube channel.
        playlist_id: Ingest videos from this playlist.
        video_ids: Ingest specific video IDs.
        published_after: ISO 8601 timestamp string or datetime; only videos
            published after this cursor are synced.
        write_disposition: "replace" (full snapshot) or "merge" (incremental with
            hard deletes). Defaults to "replace" for channels/playlists, and
            "merge" for explicit video_ids.
        caption_languages: Preferred language codes for captions (default: ``("en",)``).
        client: Pre-built YouTube API client (test-injection point).
        quota: Custom ``YouTubeQuota`` rate limiter.

    Returns:
        A dlt source yielding YouTube video documents for ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if not (channel_id or playlist_id or video_ids):
        raise ValueError("At least one of channel_id, playlist_id, or video_ids must be provided.")

    # Standardize write_disposition
    default_disp = "merge" if video_ids and not (channel_id or playlist_id) else "replace"
    resolved_write_disp = write_disposition or default_disp

    # Standardize published_after string
    published_after_str: str | None = None
    if isinstance(published_after, datetime):
        published_after_str = published_after.isoformat()
    elif isinstance(published_after, str):
        published_after_str = published_after

    quota_limiter = quota or YouTubeQuota()

    @dlt.resource(
        name=YOUTUBE_TABLE_NAME,
        primary_key="id",
        write_disposition=resolved_write_disp,
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def youtube_videos():
        service = client or build_youtube_service(api_key=api_key)
        resource_state = dlt.current.resource_state()
        watermarks = resource_state.setdefault("watermarks", {})
        tracked_ids = set(resource_state.get("tracked_video_ids", []))

        # Check for cursor in resource_state if not explicitly passed
        effective_published_after = published_after_str or resource_state.get("last_published_at")
        highest_published_at = effective_published_after

        # Step 1: Collect candidate video IDs to fetch
        candidate_ids: list[str] = []
        if video_ids:
            candidate_ids.extend(video_ids)
        if playlist_id:
            candidate_ids.extend(
                _iter_playlist_video_ids(
                    service, playlist_id, effective_published_after, quota_limiter
                )
            )
        elif channel_id:
            upl_playlist = _resolve_uploads_playlist_id(service, channel_id, quota_limiter)
            candidate_ids.extend(
                _iter_playlist_video_ids(
                    service, upl_playlist, effective_published_after, quota_limiter
                )
            )

        # Deduplicate while preserving order
        unique_candidate_ids = list(dict.fromkeys(candidate_ids))

        # Step 2: Fetch metadata for candidate videos via videos.list
        live_videos = _fetch_videos_batch(service, unique_candidate_ids, quota_limiter)
        live_video_map = {v["id"]: v for v in live_videos}

        # Step 3: Handle deletions in "merge" mode
        if resolved_write_disp == "merge":
            # Any previously tracked video that is now absent or private emits _deleted=True
            all_tracked = tracked_ids.union(video_ids or [])
            for vid in all_tracked:
                if vid not in live_video_map:
                    yield _deleted_row(vid)
                    tracked_ids.discard(vid)

        # Step 4: Process live videos with watermark optimization
        for video in live_videos:
            vid = video["id"]
            etag = video.get("etag", "")
            snippet = video.get("snippet", {})
            title = snippet.get("title", "")
            description = snippet.get("description", "")
            published_at = snippet.get("publishedAt", "")

            if published_at and (
                highest_published_at is None or published_at > highest_published_at
            ):
                highest_published_at = published_at

            content_hash = compute_content_hash(title, description)
            cached_wm = watermarks.get(vid)

            captions: str
            if cached_wm and cached_wm.get("etag") == etag:
                # Etag match: Reuse cached captions (0 extra API calls)
                captions = cached_wm.get("captions", "")
            elif cached_wm and cached_wm.get("content_hash") == content_hash:
                # Etag changed but ingested content hash is identical (only volatile stats changed)
                captions = cached_wm.get("captions", "")
                cached_wm["etag"] = etag
            else:
                # Content changed or new video: re-fetch captions
                captions = fetch_video_captions(vid, languages=caption_languages)
                watermarks[vid] = {
                    "etag": etag,
                    "content_hash": content_hash,
                    "captions": captions,
                }

            if resolved_write_disp == "merge":
                tracked_ids.add(vid)

            yield _build_video_row(video, captions, _deleted=False)

        # Update persisted state
        resource_state["tracked_video_ids"] = list(tracked_ids)
        if highest_published_at:
            resource_state["last_published_at"] = highest_published_at

        logger.info("YouTube connector: processed %d live videos.", len(live_videos))

    @dlt.source(name=YOUTUBE_SOURCE_NAME)
    def _youtube():
        return youtube_videos

    source = _youtube()
    setattr(source, DOCUMENT_SOURCE_ATTR, YOUTUBE_SOURCE_NAME)
    return source
