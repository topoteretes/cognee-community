"""DLT source for YouTube channel uploads (publishedAfter cursor + forget-on-delete).

Pulls a YouTube channel's public uploads — video titles, descriptions, and
metadata — into cognee incrementally — "ask my channel".  The source is a
single dlt resource meant to be handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_youtube import youtube_source

    await cognee.remember(
        youtube_source(channel_id="UC..."),
        dataset_name="my_channel",
        primary_key="id",
        write_disposition="merge",   # REQUIRED (see .. important:: below)
        max_rows_per_table=0,        # REQUIRED for a real channel (see .. note:: below)
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**.  The add pipeline defaults
   to ``"replace"`` (drop + reload the table each run); on the second,
   incremental sync that would wipe everything but the small delta.  Always
   pass ``"merge"``.

Design
------
* **Auth** — a YouTube Data API v3 key (no OAuth dance).  Pass ``api_key=``
  or set ``YOUTUBE_API_KEY``.  The connector only issues reads.  With an API
  key only *public* videos are visible — that is a platform constraint, not a
  connector choice.
* **Primary key** — the video id.  Combined with ``write_disposition="merge"``
  this gives idempotent upserts.
* **Scope** — pass a ``channel_id`` (its uploads playlist, ``UU...``, is
  derived deterministically) and/or explicit ``playlist_ids``.  Listing the
  uploads playlist covers every public video on the channel.
* **Incremental cursor** — the ``publishedAfter`` semantics via the newest
  ``publishedAt`` recorded in dlt's resource state: later runs re-emit only
  newer uploads.  The playlists are still paged in full every run, because the
  deletion sweep needs to see every live id.
* **Forget-on-delete** — the YouTube Data API has no deletion feed, so each
  run diffs the full id set against the previous run's (kept in resource
  state).  Vanished videos (deleted, or made private/unlisted) are emitted
  with the ``_deleted`` hard-delete marker; dlt removes those rows on
  ``merge`` and cognee's existing ``orphan_cleanup`` purges them from the
  graph + vector + relational stores.  No parallel cleanup path.
* **Captions** — deliberately not fetched.  ``captions.download`` requires
  OAuth even for public videos, which API-key auth cannot provide (the issue's
  "watch out for"); ingest therefore covers video metadata and descriptions.
* **Document mode** — the resource declares ``cognee_document_source =
  "youtube"`` (the same marker notion/google-drive use), so each row flows
  through the standard cognify entity-extraction pipeline as a normal text
  document instead of the deterministic dlt-row schema-context path.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows
   from the dlt destination (default 50).  For a real channel pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.

Privacy
-------
This reads the public metadata of the channel/playlists you point it at.  It
is **opt-in**: nothing is fetched until you construct a source and call
``remember``.  Use a dedicated dataset so you can ``cognee.forget`` the whole
thing in one call.  Mind the API key's daily quota (list calls cost 1 unit
each).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("youtube_connector")

# dlt resource / staging-table name for YouTube videos.
YOUTUBE_SOURCE_NAME = "youtube"
YOUTUBE_TABLE_NAME = "youtube"

_API_BASE = "https://www.googleapis.com/youtube/v3"

# Retry budget for rate-limited / transient YouTube API responses.
_MAX_RETRIES = 5

_MAX_PAGES = 2000  # hard stop: 100k playlist items

_EXTRA_HINT = (
    'The YouTube connector requires the "youtube" extra: pip install "cognee[youtube]" '
    "(provides dlt and httpx)."
)


def youtube_source(
    channel_id: str | None = None,
    playlist_ids: list[str] | None = None,
    api_key: str | None = None,
    client: Any = None,
):
    """Create a dlt resource that yields videos from a channel/playlists.

    Args:
        channel_id: A channel id (``UC...``); its uploads playlist (``UU...``)
            is derived automatically. Falls back to ``YOUTUBE_CHANNEL_ID``.
        playlist_ids: Additional playlist ids to ingest. At least one of
            ``channel_id`` / ``playlist_ids`` (or their env fallbacks) is
            required.
        api_key: A YouTube Data API v3 key. Falls back to ``YOUTUBE_API_KEY``.
        client: Pre-built client (mainly a test-injection point); when omitted
            a :class:`YouTubeClient` is built from the parameters above.

    Returns:
        A dlt resource suitable for ``cognee.remember(...)`` with
        ``write_disposition="merge"``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        resolved_key = api_key or os.environ.get("YOUTUBE_API_KEY")
        resolved_channel = channel_id or os.environ.get("YOUTUBE_CHANNEL_ID")
        playlists = _resolve_playlists(resolved_channel, playlist_ids)
        if not resolved_key:
            raise ValueError("YouTube API key required: pass api_key= or set YOUTUBE_API_KEY.")
        if not playlists:
            raise ValueError(
                "Nothing to ingest: pass channel_id= or playlist_ids= (or set YOUTUBE_CHANNEL_ID)."
            )
        client = YouTubeClient(resolved_key, playlists=playlists)

    @dlt.resource(
        name=YOUTUBE_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker (matching gmail.py /
        # google_drive.py): rows where it is True are removed from the dlt
        # destination on merge, which propagates the deletion through
        # cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def youtube():
        resource_state = dlt.current.resource_state()
        yield from _sync(client, resource_state)

    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(youtube, DOCUMENT_SOURCE_ATTR, YOUTUBE_SOURCE_NAME)
    return youtube


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class YouTubeClient:
    """Minimal YouTube Data API v3 client over httpx, with retry/backoff.

    Exposes exactly the operation the connector needs: ``playlist_items``.
    Any object with the same method can be injected in its place (tests).
    """

    def __init__(
        self,
        api_key: str,
        playlists: list[str],
        base_url: str = _API_BASE,
        timeout: float = 30.0,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self.playlists = list(playlists)

    def playlist_items(self, playlist_id: str, page_token: str | None = None) -> dict:
        """One page of a playlist's items, oldest-first (stable for sweeps)."""
        params: dict[str, Any] = {
            "part": "snippet,contentDetails",
            "maxResults": 50,
            "playlistId": playlist_id,
        }
        if page_token:
            params["pageToken"] = page_token
        return self._get("playlistItems", params=params)

    def _get(self, path: str, params: dict) -> dict:
        import httpx

        params = {**params, "key": self._api_key}
        for attempt in range(_MAX_RETRIES):
            try:
                resp = httpx.get(
                    f"{self._base_url}/{path}",
                    params=params,
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                    raise
                delay = _retry_after(getattr(exc, "response", None), attempt)
                logger.warning(
                    "YouTube: %s — retrying in %.1fs (%d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)


def _resolve_playlists(channel_id: str | None, playlist_ids: list[str] | None) -> list[str]:
    """Build the playlist list from a channel id and/or explicit playlists.

    A channel's uploads playlist is the channel id with the ``UC`` prefix
    swapped for ``UU`` — a documented, deterministic mapping, so no extra API
    call is needed to resolve it.
    """
    playlists = list(playlist_ids or [])
    if channel_id and channel_id.startswith("UC") and len(channel_id) > 2:
        playlists.insert(0, f"UU{channel_id[2:]}")
    return playlists


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / timeout / network errors worth retrying."""
    import httpx

    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


def _retry_after(response: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = None
    if response is not None:
        header = response.headers.get("retry-after") or response.headers.get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


# ---------------------------------------------------------------------------
# Row builders (pure — unit-testable)
# ---------------------------------------------------------------------------


def _video_row(item: dict) -> dict:
    """Flatten a playlist item into a video document row."""
    snippet = item.get("snippet") or {}
    video_id = (
        (item.get("contentDetails") or {}).get("videoId")
        or (snippet.get("resourceId") or {}).get("videoId")
        or ""
    )
    title = snippet.get("title") or ""
    description = (snippet.get("description") or "").strip()
    published = (snippet.get("publishedAt") or "").strip()
    content = title
    if description:
        content += f"\n\n{description}"
    if published:
        content += f"\n\nPublished: {published}"
    return {
        "id": video_id,
        "kind": "video",
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
        "title": title,
        "content": content,
        "_deleted": False,
    }


def _tombstone_row(video_id: str) -> dict:
    """A hard-delete marker row for a vanished video."""
    return {"id": video_id, "kind": "video", "_deleted": True}


# ---------------------------------------------------------------------------
# Sync strategies (pure given client + state dict — unit-testable)
# ---------------------------------------------------------------------------


def _sync(client: Any, state: dict) -> Iterator[dict]:
    """Run one sync pass against ``client``, recording cursors in ``state``.

    Only videos published at/after the recorded ``published_after`` cursor are
    re-emitted (title/description edits do not bump ``publishedAt`` on
    YouTube, so the cursor is about new uploads; metadata edits flow through
    on a full re-backfill).  The playlists are paged in full so the id sweep
    can emit hard-delete markers for videos that vanished — deleted, or made
    private/unlisted — because the YouTube Data API has no deletion feed.
    """
    cursor = state.get("published_after")
    cursor_dt = _parse_ts(cursor)
    prev_ids: set[str] = set(state.get("video_ids") or [])
    curr_ids: set[str] = set()
    max_published = cursor_dt

    for playlist_id in client.playlists:
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            payload = client.playlist_items(playlist_id, page_token=page_token)
            for item in payload.get("items", []):
                vid = (item.get("contentDetails") or {}).get("videoId") or ""
                if not vid:
                    continue
                # Alive either way — record it for the deletion sweep before
                # the cursor filter, or unchanged videos would be mistaken
                # for vanished ones.
                curr_ids.add(vid)
                published = _parse_ts((item.get("snippet") or {}).get("publishedAt"))
                if published is not None and (max_published is None or published > max_published):
                    max_published = published
                if cursor_dt is not None and (published is None or published <= cursor_dt):
                    # Not newer than everything already synced (the API's own
                    # publishedAfter is exclusive). Edge case: a video with
                    # publishedAt exactly equal to the cursor uploaded after
                    # the last run would be skipped — the same trade-off the
                    # publishedAfter filter itself makes.
                    continue
                yield _video_row(item)
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

    for vid in sorted(prev_ids - curr_ids):
        yield _tombstone_row(vid)

    state["video_ids"] = sorted(curr_ids)
    state["published_after"] = max_published.isoformat() if max_published else cursor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    """Parse an RFC 3339 timestamp ('...Z' or '...+00:00') to UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
