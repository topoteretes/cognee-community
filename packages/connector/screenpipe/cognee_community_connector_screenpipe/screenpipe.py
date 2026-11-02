"""Screenpipe connector for cognee — local meeting, memory, and screen context.

Connects to Screenpipe's local REST API (by default at ``http://localhost:3030``)
and feeds finished meeting transcripts, structured memories, and opt-in windowed
screen OCR text directly into cognee via the standard DLT ingestion subsystem::

    import cognee
    from cognee_community_connector_screenpipe import screenpipe_source

    # Ingest finished meetings and memories into cognee memory
    await cognee.remember(
        screenpipe_source(),
        dataset_name="screenpipe_context",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Localhost by default** — connects to ``http://localhost:3030`` with zero credentials
  needed. Optional ``api_key`` (Bearer token) and custom ``base_url`` supported
  for remote or tunneled deployments.
* **Document-mode routing** — attaches ``DOCUMENT_SOURCE_ATTR = "screenpipe"`` so
  records flow through cognee's text-document cognify pipeline.
* **Meeting transcripts** — finished meetings (where ``meeting_end`` is populated)
  are fetched with attendee, app, and speaker-labeled transcript chunks, formatted
  into cohesive documents under ``meeting:{id}``.
* **Memories** — structured user memories from ``/memories`` under ``memory:{id}``.
* **Opt-in screen OCR** — disabled by default to avoid noise; when enabled, requires
  an explicit ``app_allowlist`` and aggregates frames into deterministic hourly
  per-application documents (``screen:{device}:{app}:{hour}``).
* **Incremental sync & tombstone reconciliation** — uses ``write_disposition="merge"``
  with ``_deleted`` boolean hard-delete marker to propagate deletions and purges.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("screenpipe_connector")
except ImportError:
    import logging
    logger = logging.getLogger("screenpipe_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

SCREENPIPE_SOURCE_NAME = "screenpipe"
SCREENPIPE_MEETINGS_TABLE = "screenpipe_meetings"
SCREENPIPE_MEMORIES_TABLE = "screenpipe_memories"
SCREENPIPE_SCREEN_TABLE = "screenpipe_screen_text"


# ---------------------------------------------------------------------------
# HTTP Client Helper
# ---------------------------------------------------------------------------
def _request_screenpipe(
    endpoint: str,
    base_url: str = "http://localhost:3030",
    api_key: str | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> Any:
    """Fetch JSON from Screenpipe REST API.

    Bypasses environment HTTP proxies for localhost destinations.
    """
    clean_base = base_url.rstrip("/")
    path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    full_url = f"{clean_base}{path}"

    if params:
        query_string = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if query_string:
            full_url = f"{full_url}?{query_string}"

    headers = {
        "Accept": "application/json",
        "User-Agent": "cognee-community-connector-screenpipe/0.1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(full_url, headers=headers)

    # Disable proxy for localhost / 127.0.0.1
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            data = response.read()
            return json.loads(data.decode("utf-8"))
    except Exception as exc:
        logger.debug("Screenpipe API request to %s failed: %s", full_url, exc)
        raise


# ---------------------------------------------------------------------------
# Data Extractors & Formatters
# ---------------------------------------------------------------------------
def _format_transcript_segments(segments: list[dict[str, Any]]) -> str:
    """Format speaker-labeled transcript chunks into clean text."""
    lines: list[str] = []
    for seg in segments:
        speaker = seg.get("speaker") or seg.get("speaker_name") or "Speaker"
        text = (seg.get("text") or seg.get("transcription") or "").strip()
        timestamp = seg.get("timestamp") or seg.get("start_time") or ""
        if not text:
            continue
        if timestamp:
            lines.append(f"[{speaker}] ({timestamp}): {text}")
        else:
            lines.append(f"[{speaker}]: {text}")
    return "\n".join(lines)


def iter_screenpipe_meetings(
    base_url: str = "http://localhost:3030",
    api_key: str | None = None,
    device_id: str | None = None,
    client: Any = None,
) -> Iterator[dict[str, Any]]:
    """Yield meeting documents from Screenpipe.

    Only finished meetings (with ``meeting_end`` populated) are ingested.
    Fetches transcript segments via ``/meetings/{id}/transcript``.
    """
    fetch_fn = client or (lambda ep, **kw: _request_screenpipe(ep, base_url, api_key, **kw))

    try:
        data = fetch_fn("/meetings")
    except Exception as exc:
        logger.warning("Could not list Screenpipe meetings: %s", exc)
        return

    meetings = data if isinstance(data, list) else data.get("data", [])
    if not isinstance(meetings, list):
        return

    for meeting in meetings:
        if not isinstance(meeting, dict):
            continue

        meeting_id = meeting.get("id")
        if not meeting_id:
            continue

        # Check for tombstones / deletions
        if meeting.get("is_deleted") or meeting.get("_deleted"):
            yield {
                "id": f"meeting:{meeting_id}",
                "meeting_id": str(meeting_id),
                "_deleted": True,
            }
            continue

        meeting_end = meeting.get("meeting_end")
        # Ingest only finished meetings
        if not meeting_end:
            logger.debug("Skipping unfinished meeting %s (meeting_end not set)", meeting_id)
            continue

        title_str = meeting.get("title") or "Untitled Meeting"
        app_name = meeting.get("app_name") or meeting.get("app") or "Unknown"
        meeting_start = meeting.get("meeting_start") or ""
        attendees = meeting.get("attendees") or []
        attendees_str = ", ".join(attendees) if isinstance(attendees, list) else str(attendees)
        user_notes = meeting.get("user_notes") or meeting.get("notes") or ""

        # Fetch transcript
        transcript_body = ""
        try:
            t_data = fetch_fn(f"/meetings/{meeting_id}/transcript")
            segments = t_data if isinstance(t_data, list) else t_data.get("segments", t_data.get("data", []))
            if isinstance(segments, list):
                transcript_body = _format_transcript_segments(segments)
        except Exception as exc:
            logger.debug("Could not fetch transcript for meeting %s: %s", meeting_id, exc)

        parts = [
            f"Meeting: {title_str}",
            f"Application: {app_name}",
            f"Start: {meeting_start}",
            f"End: {meeting_end}",
        ]
        if attendees_str:
            parts.append(f"Attendees: {attendees_str}")
        if user_notes:
            parts.append(f"Notes:\n{user_notes}")
        if transcript_body:
            parts.append(f"Transcript:\n{transcript_body}")

        full_content = "\n\n".join(parts)

        yield {
            "id": f"meeting:{meeting_id}",
            "meeting_id": str(meeting_id),
            "title": f"Meeting: {title_str}",
            "content": full_content,
            "app_name": app_name,
            "meeting_start": meeting_start,
            "meeting_end": meeting_end,
            "device_id": device_id or meeting.get("device_id") or "default",
            "attendees": attendees_str,
            "user_notes": user_notes,
            "updated_at": meeting_end,
            "_deleted": False,
        }


def iter_screenpipe_memories(
    base_url: str = "http://localhost:3030",
    api_key: str | None = None,
    device_id: str | None = None,
    client: Any = None,
) -> Iterator[dict[str, Any]]:
    """Yield structured memories from Screenpipe."""
    fetch_fn = client or (lambda ep, **kw: _request_screenpipe(ep, base_url, api_key, **kw))

    try:
        data = fetch_fn("/memories")
    except Exception as exc:
        logger.warning("Could not list Screenpipe memories: %s", exc)
        return

    memories = data if isinstance(data, list) else data.get("data", [])
    if not isinstance(memories, list):
        return

    for mem in memories:
        if not isinstance(mem, dict):
            continue

        mem_id = mem.get("id")
        if not mem_id:
            continue

        if mem.get("is_deleted") or mem.get("_deleted"):
            yield {
                "id": f"memory:{mem_id}",
                "memory_id": str(mem_id),
                "_deleted": True,
            }
            continue

        content = (mem.get("content") or mem.get("text") or "").strip()
        if not content:
            continue

        tags = mem.get("tags") or []
        tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
        source_ref = mem.get("source_reference") or mem.get("source") or ""
        created_at = mem.get("created_at") or ""
        updated_at = mem.get("updated_at") or created_at

        title_suffix = tags_str if tags_str else f"#{mem_id}"
        yield {
            "id": f"memory:{mem_id}",
            "memory_id": str(mem_id),
            "title": f"Memory: {title_suffix}",
            "content": content,
            "tags": tags_str,
            "source_reference": source_ref,
            "device_id": device_id or mem.get("device_id") or "default",
            "created_at": created_at,
            "updated_at": updated_at,
            "_deleted": False,
        }


def iter_screenpipe_screen_text(
    base_url: str = "http://localhost:3030",
    api_key: str | None = None,
    app_allowlist: list[str] | set[str] | None = None,
    device_id: str | None = None,
    client: Any = None,
) -> Iterator[dict[str, Any]]:
    """Yield aggregated hourly screen OCR text for allowlisted applications."""
    if not app_allowlist:
        logger.debug("Screen text ingestion skipped (app_allowlist not provided)")
        return

    fetch_fn = client or (lambda ep, **kw: _request_screenpipe(ep, base_url, api_key, **kw))
    allowlist_set = {app.lower() for app in app_allowlist}

    try:
        data = fetch_fn(
            "/search",
            params={"content_type": "ocr", "filter_pii": "true", "limit": 100},
        )
    except Exception as exc:
        logger.warning("Could not query Screenpipe search: %s", exc)
        return

    frames = data if isinstance(data, list) else data.get("data", [])
    if not isinstance(frames, list):
        return

    # Group deduplicated lines by (app_name, hour_str)
    hourly_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    seen_lines: set[str] = set()

    for frame in frames:
        if not isinstance(frame, dict):
            continue

        app_name = (frame.get("app_name") or frame.get("app") or "unknown").strip()
        if app_name.lower() not in allowlist_set:
            continue

        text = (frame.get("text") or frame.get("ocr_text") or "").strip()
        if not text:
            continue

        ts_str = frame.get("timestamp") or datetime.now(timezone.utc).isoformat()
        # Parse or truncate to hour bucket YYYY-MM-DDTHH
        hour_bucket = ts_str[:13] if len(ts_str) >= 13 else "unknown"

        for line in text.splitlines():
            line_clean = line.strip()
            if len(line_clean) > 3 and line_clean not in seen_lines:
                seen_lines.add(line_clean)
                hourly_groups[(app_name, hour_bucket)].append(line_clean)

    for (app_name, hour_bucket), lines in hourly_groups.items():
        doc_id = f"screen:{device_id or 'default'}:{app_name}:{hour_bucket}"
        yield {
            "id": doc_id,
            "title": f"Screen Activity: {app_name} ({hour_bucket})",
            "content": "\n".join(lines),
            "app_name": app_name,
            "hour_bucket": hour_bucket,
            "device_id": device_id or "default",
            "_deleted": False,
        }


# ---------------------------------------------------------------------------
# Public dlt Resource & Source Factory
# ---------------------------------------------------------------------------
def screenpipe_source(
    base_url: str = "http://localhost:3030",
    api_key: str | None = None,
    device_id: str | None = None,
    include_meetings: bool = True,
    include_memories: bool = True,
    include_screen_text: bool = False,
    app_allowlist: list[str] | None = None,
    client: Any = None,
):
    """Return dlt resources configured for Screenpipe ingestion into cognee.

    Args:
        base_url: Screenpipe local or remote REST endpoint.
        api_key: Optional Bearer token.
        device_id: Machine identifier for multi-device scoping.
        include_meetings: Ingest finished meetings and transcripts.
        include_memories: Ingest structured user memories.
        include_screen_text: Ingest windowed OCR text (requires app_allowlist).
        app_allowlist: List of allowed application names for screen OCR.
        client: Optional mock fetch callable for testing.

    Returns:
        A list of dlt resources ready for ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Screenpipe connector requires dlt: pip install "cognee[dlt]".'
        ) from exc

    resources = []

    if include_meetings:
        @dlt.resource(
            name=SCREENPIPE_MEETINGS_TABLE,
            primary_key="id",
            write_disposition="merge",
            columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
        )
        def screenpipe_meetings():
            yield from iter_screenpipe_meetings(base_url, api_key, device_id, client)

        setattr(screenpipe_meetings, DOCUMENT_SOURCE_ATTR, SCREENPIPE_SOURCE_NAME)
        resources.append(screenpipe_meetings)

    if include_memories:
        @dlt.resource(
            name=SCREENPIPE_MEMORIES_TABLE,
            primary_key="id",
            write_disposition="merge",
            columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
        )
        def screenpipe_memories():
            yield from iter_screenpipe_memories(base_url, api_key, device_id, client)

        setattr(screenpipe_memories, DOCUMENT_SOURCE_ATTR, SCREENPIPE_SOURCE_NAME)
        resources.append(screenpipe_memories)

    if include_screen_text:
        @dlt.resource(
            name=SCREENPIPE_SCREEN_TABLE,
            primary_key="id",
            write_disposition="merge",
            columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
        )
        def screenpipe_screen_text():
            yield from iter_screenpipe_screen_text(
                base_url, api_key, app_allowlist, device_id, client
            )

        setattr(screenpipe_screen_text, DOCUMENT_SOURCE_ATTR, SCREENPIPE_SOURCE_NAME)
        resources.append(screenpipe_screen_text)

    return resources
