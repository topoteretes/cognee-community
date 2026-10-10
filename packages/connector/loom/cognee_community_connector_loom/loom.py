"""DLT source for Loom video transcripts & summaries (full-snapshot sync + forget-on-delete).

Fetches Loom workspace video recordings, AI-generated chapter summaries, and transcripts,
then formats them as structured markdown documents for cognee's ingestion pipeline.

Declares ``DOCUMENT_SOURCE_ATTR = "loom"``, routing video transcripts through Cognee's
standard cognify entity-extraction pipeline into the memory graph.

The source defaults to full snapshot replacement: ``write_disposition="replace"`` rewrites
staging with currently active workspace videos. Deleted or archived videos drop out of the
active snapshot and cognee's existing ``orphan_cleanup`` purges them from the knowledge graph.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from typing import Any, Iterable

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("loom_connector")

LOOM_TABLE_NAME = "loom_videos"
DEFAULT_BASE_URL = "https://api.loom.com/v1"


def get_retry_delay(response: httpx.Response, attempt: int, base_delay: float = 1.0) -> float:
    """Calculate backoff delay from Retry-After header or exponential backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            delay = float(retry_after)
            if math.isfinite(delay) and delay >= 0:
                return delay
        except (ValueError, TypeError):
            pass
    return base_delay * (2**attempt)


def format_seconds(seconds: float | int | None) -> str:
    """Format seconds into MM:SS or HH:MM:SS string."""
    if seconds is None:
        return "00:00"
    total_sec = int(seconds)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class LoomClient:
    """HTTP client for Loom Developer API with exponential backoff on HTTP 429 and 5xx."""

    def __init__(
        self,
        api_token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.api_token = api_token or os.getenv("LOOM_API_TOKEN")
        if not self.api_token:
            raise ValueError(
                "Loom API token is required. Set LOOM_API_TOKEN env var or pass api_token."
            )
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "User-Agent": "cognee-community-connector-loom/0.1.0",
        }
        self.client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> LoomClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def get_with_retry(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute GET request with retry backoff for rate limits and server errors."""
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}/{endpoint.lstrip('/')}"
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.get(url, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == self.max_retries:
                        response.raise_for_status()
                    delay = get_retry_delay(response, attempt)
                    logger.warning(
                        "Loom request to %s returned %d. Backing off for %.2fs (attempt %d/%d)",
                        url,
                        response.status_code,
                        delay,
                        attempt + 1,
                        self.max_retries,
                    )
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                return response.json()
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    raise
                delay = 1.0 * (2**attempt)
                logger.warning(
                    "Network error connecting to Loom: %s. Retrying in %.2fs (attempt %d/%d)",
                    exc,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(delay)

        if last_exc:
            raise last_exc
        raise RuntimeError("Unexpected failure in get_with_retry")

    def list_videos(
        self,
        cursor: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a page of workspace videos from Loom."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if after:
            params["created_after"] = after

        return self.get_with_retry("videos", params=params)

    def get_transcript(self, video_id: str) -> list[dict[str, Any]]:
        """Fetch transcript segments for a video if not included inline."""
        try:
            res = self.get_with_retry(f"videos/{video_id}/transcript")
            if isinstance(res, dict):
                return res.get("segments") or res.get("transcript") or []
            if isinstance(res, list):
                return res
            return []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise


def video_to_document(
    video: dict[str, Any],
    transcript_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert Loom video metadata and transcript segments into a rich Markdown document."""
    video_id = str(video.get("id") or "")
    title = video.get("title") or "Untitled Loom Video"
    description = video.get("description") or ""
    url = video.get("url") or f"https://www.loom.com/share/{video_id}"
    created_at = video.get("created_at") or video.get("createdAt") or ""
    updated_at = video.get("updated_at") or video.get("updatedAt") or created_at
    duration = video.get("duration") or 0
    duration_str = format_seconds(duration)

    creator = video.get("creator") or {}
    creator_name = creator.get("name") or "Team Member"
    creator_email = creator.get("email") or ""

    # Chapters or AI Summary bullets
    chapters = video.get("chapters") or []
    chapter_lines: list[str] = []
    for chap in chapters:
        c_title = chap.get("title") or ""
        c_time = format_seconds(chap.get("timestamp") or chap.get("start_time"))
        c_summary = chap.get("summary") or ""
        line = f"- **[{c_time}] {c_title}**"
        if c_summary:
            line += f": {c_summary}"
        chapter_lines.append(line)

    # Transcript segments
    segments = transcript_segments or video.get("transcript") or video.get("segments") or []
    transcript_lines: list[str] = []
    if isinstance(segments, list):
        for seg in segments:
            if isinstance(seg, dict):
                text = seg.get("text") or ""
                speaker = seg.get("speaker") or "Speaker"
                start = format_seconds(seg.get("start_time") or seg.get("start"))
                if text:
                    transcript_lines.append(f"**[{start}] {speaker}**: {text}")
            elif isinstance(seg, str) and seg:
                transcript_lines.append(seg)
    elif isinstance(segments, str) and segments:
        transcript_lines.append(segments)

    creator_info = (
        f"- **Creator**: {creator_name} ({creator_email})"
        if creator_email
        else f"- **Creator**: {creator_name}"
    )
    doc_parts = [
        f"# Video: {title}",
        "",
        creator_info,
        f"- **Created At**: {created_at}",
        f"- **Duration**: {duration_str}",
        f"- **URL**: {url}",
    ]

    if description:
        doc_parts.extend(["", "## Description", description])

    if chapter_lines:
        doc_parts.extend(["", "## Chapters & AI Summary", *chapter_lines])

    if transcript_lines:
        doc_parts.extend(["", "## Spoken Transcript", *transcript_lines])
    else:
        doc_parts.extend(
            ["", "## Spoken Transcript", "*(No transcript available for this recording)*"]
        )

    markdown_text = "\n".join(doc_parts)
    raw_hash = hashlib.sha256(f"{video_id}_{updated_at}".encode("utf-8")).hexdigest()

    return {
        "id": video_id,
        "title": title,
        "url": url,
        "created_at": created_at,
        "updated_at": updated_at,
        "creator": creator_name,
        "text": markdown_text,
        "content": markdown_text,
        "raw_hash": raw_hash,
        "metadata": {
            "source": "loom",
            "video_id": video_id,
            "title": title,
            "creator": creator_name,
            "duration": duration,
        },
    }


def fetch_loom_videos(
    api_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    fetch_transcripts: bool = True,
    incremental: bool = False,
    limit: int = 50,
    transport: httpx.BaseTransport | None = None,
) -> Iterable[dict[str, Any]]:
    """Yield Loom video documents for dlt ingestion."""
    import dlt

    state = dlt.current.resource_state() if incremental else {}
    last_watermark = state.get("last_updated_after") if incremental else None

    client = LoomClient(
        api_token=api_token,
        base_url=base_url,
        transport=transport,
    )

    cursor: str | None = None
    max_updated = last_watermark

    try:
        while True:
            response = client.list_videos(
                cursor=cursor,
                limit=limit,
                after=last_watermark,
            )
            videos = response.get("videos") or response.get("data") or []
            if not videos:
                break

            for video in videos:
                transcript_segments = None
                video_id = str(video.get("id") or "")
                # If transcript not included inline, fetch via sub-endpoint
                if fetch_transcripts and "transcript" not in video and "segments" not in video:
                    transcript_segments = client.get_transcript(video_id)

                doc = video_to_document(video, transcript_segments=transcript_segments)
                video_updated = doc["updated_at"]
                if video_updated and (max_updated is None or video_updated > max_updated):
                    max_updated = video_updated

                yield doc

            cursor = response.get("next_cursor") or response.get("cursor")
            if not cursor or len(videos) < limit:
                break

        if incremental and max_updated:
            state["last_updated_after"] = max_updated
    finally:
        client.close()


def loom_source(
    api_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    fetch_transcripts: bool = True,
    incremental: bool = False,
    limit: int = 50,
    transport: httpx.BaseTransport | None = None,
) -> Any:
    """Create a dlt source for Loom video transcripts."""
    import dlt

    @dlt.resource(
        name=LOOM_TABLE_NAME,
        write_disposition="merge" if incremental else "replace",
        primary_key="id",
    )
    def videos() -> Iterable[dict[str, Any]]:
        yield from fetch_loom_videos(
            api_token=api_token,
            base_url=base_url,
            fetch_transcripts=fetch_transcripts,
            incremental=incremental,
            limit=limit,
            transport=transport,
        )

    @dlt.source(name="loom")
    def source() -> Any:
        return videos

    created_source = source()
    setattr(created_source, DOCUMENT_SOURCE_ATTR, "loom")
    return created_source
