"""Grain connector for cognee.

The connector exposes Grain recordings, transcripts, and human-curated
highlights as normal cognee documents.  It uses a lightweight recording sweep
on every run for deletion detection, but emits only new or changed documents.
The sweep and the incremental cursor live in dlt resource state.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("grain_connector")

GRAIN_API_BASE = "https://api.grain.com/_/public-api/v2"
GRAIN_API_VERSION = "2026-10-01"
GRAIN_SOURCE_NAME = "grain"
GRAIN_TABLE_NAME = "grain_content"

CONTENT_RECORDINGS = "recordings"
CONTENT_TRANSCRIPTS = "transcripts"
CONTENT_HIGHLIGHTS = "highlights"
CONTENT_TYPES = frozenset({CONTENT_RECORDINGS, CONTENT_TRANSCRIPTS, CONTENT_HIGHLIGHTS})

TRANSCRIPT_SEARCH_WEIGHT = 1.0
RECORDING_SEARCH_WEIGHT = 1.0
HIGHLIGHT_SEARCH_WEIGHT = 3.0

_MAX_RETRIES = 5
_REQUEST_TIMEOUT = 30


def _make_session(api_token: str) -> Any:
    """Create a read-only Grain API session authenticated by bearer token."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise ImportError(
            'The Grain connector requires "requests". Install this package with its dependencies.'
        ) from exc

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
            "Public-Api-Version": GRAIN_API_VERSION,
        }
    )
    return session


def _request(
    session: Any,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> Any:
    """Call Grain, retrying rate limits and transient server failures."""
    url = path if path.startswith("http") else f"{GRAIN_API_BASE}{path}"
    for attempt in range(_MAX_RETRIES):
        response = session.request(
            method,
            url,
            json=json_body,
            timeout=_REQUEST_TIMEOUT,
        )
        if response.status_code not in {429, 500, 502, 503, 504}:
            response.raise_for_status()
            return response.json()

        if attempt == _MAX_RETRIES - 1:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = float(2**attempt)
        logger.warning(
            "Grain returned HTTP %d; retrying in %.1fs (%d/%d).",
            response.status_code,
            delay,
            attempt + 1,
            _MAX_RETRIES,
        )
        time.sleep(delay)

    raise RuntimeError("Grain request retry loop exited unexpectedly.")  # pragma: no cover


def _iter_recordings(
    session: Any,
    *,
    include_highlights: bool,
    include_participants: bool,
    recording_filter: Mapping[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield every in-scope recording across Grain cursor pagination."""
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        body: dict[str, Any] = {}
        if recording_filter:
            body["filter"] = dict(recording_filter)
        include = {
            name: True
            for name, enabled in (
                ("highlights", include_highlights),
                ("participants", include_participants),
            )
            if enabled
        }
        if include:
            body["include"] = include
        if cursor:
            body["cursor"] = cursor

        response = _request(session, "POST", "/recordings", json_body=body)
        yield from response.get("recordings", []) or []

        next_cursor = response.get("cursor")
        if not next_cursor:
            return
        if next_cursor in seen_cursors:
            raise RuntimeError("Grain returned a repeated pagination cursor.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _recording_created_at(recording: Mapping[str, Any]) -> str:
    """Return Grain's best available creation cursor for a recording.

    Some API generations expose ``created_at``/``created_datetime``.  Current
    v2 recording objects expose ``start_datetime`` instead, so it is the safe
    fallback and keeps the connector compatible with both response shapes.
    """
    return str(
        recording.get("created_at")
        or recording.get("created_datetime")
        or recording.get("start_datetime")
        or ""
    )


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _format_milliseconds(milliseconds: Any) -> str:
    try:
        total_seconds = max(0, int(milliseconds) // 1000)
    except (TypeError, ValueError):
        return "00:00:00"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _recording_content(recording: Mapping[str, Any]) -> str:
    participants = [
        str(person.get("name") or person.get("email") or "").strip()
        for person in recording.get("participants", []) or []
        if isinstance(person, Mapping)
    ]
    lines = [
        "Grain meeting recording metadata.",
        f"Started: {recording.get('start_datetime') or ''}",
        f"Ended: {recording.get('end_datetime') or ''}",
        f"Duration: {_format_milliseconds(recording.get('duration_ms'))}",
        f"Source: {recording.get('source') or ''}",
        f"Media type: {recording.get('media_type') or ''}",
    ]
    tags = [str(tag) for tag in recording.get("tags", []) or [] if tag]
    if participants:
        lines.append(f"Participants: {', '.join(participants)}")
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    return "\n".join(lines)


def _recording_row(recording: Mapping[str, Any]) -> dict[str, Any]:
    recording_id = str(recording["id"])
    return {
        "id": f"recording:{recording_id}",
        "recording_id": recording_id,
        "content_type": "recording",
        "created_at": _recording_created_at(recording),
        "title": str(recording.get("title") or "Untitled Grain recording"),
        "content": _recording_content(recording),
        "url": recording.get("url"),
        "search_weight": RECORDING_SEARCH_WEIGHT,
        "_deleted": False,
    }


def _render_transcript(segments: Iterable[Mapping[str, Any]]) -> str:
    """Render Grain's JSON transcript into readable, timestamped text."""
    lines: list[str] = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        speaker = str(segment.get("speaker") or "Unknown speaker").strip()
        timestamp = _format_milliseconds(segment.get("start"))
        lines.append(f"[{timestamp}] {speaker}: {text}")
    return "\n".join(lines)


def _transcript_row(
    recording: Mapping[str, Any], transcript: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    recording_id = str(recording["id"])
    title = str(recording.get("title") or "Untitled Grain recording")
    return {
        "id": f"transcript:{recording_id}",
        "recording_id": recording_id,
        "content_type": "transcript",
        "created_at": _recording_created_at(recording),
        "title": f"Transcript — {title}",
        "content": _render_transcript(transcript),
        "url": recording.get("url"),
        "search_weight": TRANSCRIPT_SEARCH_WEIGHT,
        "_deleted": False,
    }


def _highlight_row(recording: Mapping[str, Any], highlight: Mapping[str, Any]) -> dict[str, Any]:
    highlight_id = str(highlight["id"])
    recording_id = str(recording["id"])
    label = str(highlight.get("text") or "Grain highlight").strip()
    transcript = str(highlight.get("transcript") or "").strip()
    speakers = [str(name) for name in highlight.get("speakers", []) or [] if name]
    tags = [str(tag) for tag in highlight.get("tags", []) or [] if tag]

    lines = [
        "Human-curated Grain highlight. Content priority: high.",
        f"Recording: {recording.get('title') or 'Untitled Grain recording'}",
        f"Timestamp: {_format_milliseconds(highlight.get('timestamp'))}",
        f"Highlight: {label}",
    ]
    if transcript:
        lines.append(f"Transcript: {transcript}")
    if speakers:
        lines.append(f"Speakers: {', '.join(speakers)}")
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")

    return {
        "id": f"highlight:{highlight_id}",
        "recording_id": recording_id,
        "content_type": "highlight",
        "created_at": str(highlight.get("created_datetime") or _recording_created_at(recording)),
        "title": f"Highlight — {label}",
        "content": "\n".join(lines),
        "url": highlight.get("url") or recording.get("url"),
        "search_weight": HIGHLIGHT_SEARCH_WEIGHT,
        "_deleted": False,
    }


def _deleted_row(document_id: str) -> dict[str, Any]:
    return {"id": document_id, "_deleted": True}


def _transcript_fingerprint(recording: Mapping[str, Any]) -> str:
    """Fingerprint listing fields capable of signalling transcript metadata changes."""
    keys = (
        "id",
        "title",
        "created_at",
        "created_datetime",
        "start_datetime",
        "end_datetime",
        "duration_ms",
        "url",
    )
    return _fingerprint({key: recording.get(key) for key in keys})


def _selected_content(content_types: Iterable[str] | str | None) -> frozenset[str]:
    if content_types is None:
        return CONTENT_TYPES
    if isinstance(content_types, str):
        content_types = [content_types]
    selected = frozenset(str(kind).strip().lower() for kind in content_types)
    invalid = selected - CONTENT_TYPES
    if invalid:
        choices = ", ".join(sorted(CONTENT_TYPES))
        raise ValueError(
            f"Unknown Grain content type(s): {sorted(invalid)}. Choose from: {choices}."
        )
    if not selected:
        raise ValueError("Select at least one Grain content type.")
    return selected


def sync_grain(
    session: Any,
    state: dict[str, Any],
    *,
    content_types: Iterable[str] | str | None = None,
    recording_ids: Iterable[str] | None = None,
    recording_filter: Mapping[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield the initial snapshot or an incremental delta plus deletions.

    The API is swept for IDs on every run because Grain has no polling delete
    feed. Expensive transcript calls and emitted DLT rows are limited to new or
    changed selected documents. State is advanced only after a complete,
    successful sweep, so a partial API failure cannot turn live documents into
    deletion tombstones.
    """
    selected = _selected_content(content_types)
    scoped_ids = {str(value) for value in recording_ids} if recording_ids else None
    previous_fingerprints = dict(state.get("document_fingerprints") or {})
    previous_cursor = str(state.get("last_created_at") or "")
    current_fingerprints: dict[str, str] = {}
    current_recording_ids: set[str] = set()
    newest_cursor = previous_cursor
    changed = 0

    recordings = _iter_recordings(
        session,
        include_highlights=CONTENT_HIGHLIGHTS in selected,
        include_participants=CONTENT_RECORDINGS in selected,
        recording_filter=recording_filter,
    )
    for recording in recordings:
        if not recording.get("id"):
            logger.warning("Grain returned a recording without an id; skipping it.")
            continue
        recording_id = str(recording["id"])
        if scoped_ids is not None and recording_id not in scoped_ids:
            continue
        current_recording_ids.add(recording_id)

        created_at = _recording_created_at(recording)
        if created_at > newest_cursor:
            newest_cursor = created_at

        if CONTENT_RECORDINGS in selected:
            row = _recording_row(recording)
            row_fingerprint = _fingerprint(row)
            current_fingerprints[row["id"]] = row_fingerprint
            if (
                row["id"] not in previous_fingerprints
                or created_at > previous_cursor
                or previous_fingerprints[row["id"]] != row_fingerprint
            ):
                yield row
                changed += 1

        if CONTENT_TRANSCRIPTS in selected:
            document_id = f"transcript:{recording_id}"
            transcript_fingerprint = _transcript_fingerprint(recording)
            current_fingerprints[document_id] = transcript_fingerprint
            if (
                document_id not in previous_fingerprints
                or created_at > previous_cursor
                or previous_fingerprints[document_id] != transcript_fingerprint
            ):
                transcript = _request(
                    session,
                    "GET",
                    f"/recordings/{recording_id}/transcript",
                )
                yield _transcript_row(recording, transcript or [])
                changed += 1

        if CONTENT_HIGHLIGHTS in selected:
            for highlight in recording.get("highlights", []) or []:
                if not highlight.get("id"):
                    logger.warning("Grain returned a highlight without an id; skipping it.")
                    continue
                row = _highlight_row(recording, highlight)
                row_fingerprint = _fingerprint(row)
                current_fingerprints[row["id"]] = row_fingerprint
                if (
                    row["id"] not in previous_fingerprints
                    or previous_fingerprints[row["id"]] != row_fingerprint
                ):
                    yield row
                    changed += 1

    deleted_ids = sorted(previous_fingerprints.keys() - current_fingerprints.keys())
    for document_id in deleted_ids:
        yield _deleted_row(document_id)

    state["document_fingerprints"] = current_fingerprints
    state["known_recording_ids"] = sorted(current_recording_ids)
    state["last_created_at"] = newest_cursor
    logger.info(
        "Grain: %d changed document(s), %d deletion(s); cursor=%s.",
        changed,
        len(deleted_ids),
        newest_cursor or "<empty>",
    )


def grain_source(
    api_token: str | None = None,
    *,
    content_types: Iterable[str] | str | None = None,
    recording_ids: Iterable[str] | None = None,
    recording_filter: Mapping[str, Any] | None = None,
    session: Any = None,
):
    """Return an incremental Grain document resource for ``cognee.remember``.

    Args:
        api_token: Grain personal/workspace API token. Falls back to
            ``GRAIN_API_TOKEN``.
        content_types: Any of ``recordings``, ``transcripts``, ``highlights``.
            All three are selected by default.
        recording_ids: Optional explicit recording-id allowlist.
        recording_filter: Optional Grain List Recordings filter object, such as
            ``{"team": "..."}`` or ``{"participant_scope": "external"}``.
        session: Injected HTTP session, primarily for tests.
    """
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise ImportError(
            "The Grain connector requires dlt. Install this package with its dependencies."
        ) from exc

    selected = _selected_content(content_types)
    resolved_token = api_token or os.environ.get("GRAIN_API_TOKEN")
    if session is None and not resolved_token:
        raise ValueError("Grain API token required: pass api_token= or set GRAIN_API_TOKEN.")

    @dlt.resource(
        name=GRAIN_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def grain_content():
        client = session or _make_session(str(resolved_token))
        yield from sync_grain(
            client,
            dlt.current.resource_state(),
            content_types=selected,
            recording_ids=recording_ids,
            recording_filter=recording_filter,
        )

    resource = grain_content()
    setattr(resource, DOCUMENT_SOURCE_ATTR, GRAIN_SOURCE_NAME)
    return resource
