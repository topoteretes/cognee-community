"""Fireflies.ai document source for cognee.

The connector performs a paginated transcript sweep on every run. Selected
content is compared with persisted hashes so only new or changed documents
are emitted. IDs that
disappear from the sweep are emitted as dlt hard-delete rows so cognee's orphan
cleanup forgets them downstream.

Fireflies documents that ``date`` is the transcript creation timestamp.  The
cursor tracks newly-created transcripts; content comparison also detects
later edits and summaries that finish processing after the first sync.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("fireflies_connector")

FIREFLIES_API_URL = "https://api.fireflies.ai/graphql"
FIREFLIES_TABLE_NAME = "fireflies_transcripts"
FIREFLIES_SOURCE_NAME = "fireflies"
_SPEAKER_RECORDS_ATTR = "_cognee_fireflies_speaker_records"
_MAX_PAGE_SIZE = 50
_MAX_ATTEMPTS = 5

_LIST_TRANSCRIPTS_QUERY = """
query FirefliesTranscripts($limit: Int!, $skip: Int!) {
  transcripts(limit: $limit, skip: $skip) {
    id
    date
    title
    transcript_url
  }
}
"""


@dataclass(frozen=True)
class _FirefliesConfig:
    include_transcript: bool = True
    include_summary: bool = True
    include_action_items: bool = True
    include_speakers: bool = True
    page_size: int = _MAX_PAGE_SIZE


class FirefliesClient:
    """Small GraphQL client kept injectable for credential-free unit tests."""

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = FIREFLIES_API_URL,
        session: Any = None,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("Fireflies API key is required.")

        if session is None:
            try:
                import requests
            except ImportError as exc:
                raise ImportError(
                    "The Fireflies connector requires requests. "
                    "Install this package's dependencies."
                ) from exc
            session = requests.Session()

        self._api_key = api_key
        self._endpoint = endpoint
        self._session = session
        self._timeout = timeout

    def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        import requests

        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._session.post(
                    self._endpoint,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"query": query, "variables": variables},
                    timeout=self._timeout,
                )
            except (requests.ConnectionError, requests.Timeout):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
                time.sleep(2**attempt)
                continue
            status = getattr(response, "status_code", 200)
            if status not in (429, 500, 502, 503, 504) or attempt == _MAX_ATTEMPTS - 1:
                break
            delay = _retry_delay(getattr(response, "headers", {}), attempt)
            close = getattr(response, "close", None)
            if close:
                close()
            time.sleep(delay)
        response.raise_for_status()
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Fireflies returned a non-JSON response.") from exc

        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            # Upstream messages can echo input or credentials. Do not include
            # the response body in an exception that may be logged by dlt.
            raise RuntimeError("Fireflies GraphQL error; check API key, permissions and query.")

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("Fireflies GraphQL response did not contain a data object.")
        return data

    def list_transcripts(self, *, limit: int, skip: int) -> list[dict[str, Any]]:
        data = self._query(_LIST_TRANSCRIPTS_QUERY, {"limit": limit, "skip": skip})
        transcripts = data.get("transcripts")
        if not isinstance(transcripts, list):
            raise RuntimeError("Fireflies GraphQL response did not contain a transcripts list.")
        return transcripts

    def get_transcript(self, transcript_id: str, config: _FirefliesConfig) -> dict[str, Any]:
        data = self._query(
            _build_transcript_query(config),
            {"transcriptId": transcript_id},
        )
        transcript = data.get("transcript")
        if not isinstance(transcript, dict):
            raise RuntimeError(f"Fireflies transcript '{transcript_id}' was not returned.")
        return transcript


def _retry_delay(headers: dict, attempt: int) -> float:
    try:
        delay = float(headers.get("Retry-After", headers.get("retry-after", "")))
    except (TypeError, ValueError):
        return float(2**attempt)
    return delay if math.isfinite(delay) and delay >= 0 else float(2**attempt)


def _build_transcript_query(config: _FirefliesConfig) -> str:
    """Select only the content categories requested by the caller."""
    fields = ["id", "title", "date", "transcript_url"]

    if config.include_speakers:
        fields.append("speakers { id name }")
    if config.include_transcript:
        fields.append(
            "sentences { index speaker_name speaker_id text raw_text start_time end_time }"
        )

    summary_fields: list[str] = []
    if config.include_summary:
        summary_fields.extend(["overview", "short_summary", "gist", "keywords", "topics_discussed"])
    if config.include_action_items:
        summary_fields.append("action_items")
    if summary_fields:
        fields.append(f"summary {{ {' '.join(summary_fields)} }}")

    selection = "\n    ".join(fields)
    return f"""
query FirefliesTranscript($transcriptId: String!) {{
  transcript(id: $transcriptId) {{
    {selection}
  }}
}}
"""


def _iter_transcript_metadata(
    client: Any, page_size: int = _MAX_PAGE_SIZE
) -> Iterator[dict[str, Any]]:
    """Yield the complete current transcript listing using skip pagination."""
    skip = 0
    seen: set[str] = set()
    while True:
        page = client.list_transcripts(limit=page_size, skip=skip)
        if not isinstance(page, list):
            raise RuntimeError("Fireflies client returned an invalid transcript page.")
        if len(page) > page_size:
            raise RuntimeError("Fireflies transcript page exceeded the requested limit.")
        for transcript in page:
            if not isinstance(transcript, dict) or not transcript.get("id"):
                raise RuntimeError("Fireflies transcript listing contained an item without an id.")
            transcript_id = str(transcript["id"])
            if transcript_id in seen:
                raise RuntimeError("Fireflies listing repeated an id; retry the complete sync.")
            seen.add(transcript_id)
            yield transcript
        if len(page) < page_size:
            return
        skip += len(page)


def sync_transcripts(
    client: Any,
    state: dict[str, Any],
    config: _FirefliesConfig | None = None,
    *,
    speaker_records: dict[str, list[dict[str, str]]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield new/changed transcripts and tombstones, then advance state.

    A transcript absent from ``known_ids`` is fetched even when its timestamp is
    at or below the cursor.  This handles restored/moved-in records and timestamp
    ties without duplicating already-known records.
    """
    config = config or _FirefliesConfig()
    known_ids = {str(value) for value in state.get("known_ids", [])}
    last_date = _as_timestamp(state.get("last_date"))
    newest_date = last_date
    previous_hashes = state.get("content_hashes", {})
    current_hashes: dict[str, str] = {}
    current_ids: set[str] = set()
    current_speakers: dict[str, list[dict[str, str]]] = {}
    changed = 0

    for metadata in _iter_transcript_metadata(client, config.page_size):
        transcript_id = str(metadata["id"])
        transcript_date = _as_timestamp(metadata.get("date"))
        current_ids.add(transcript_id)

        detail = client.get_transcript(transcript_id, config)
        if str(detail.get("id")) != transcript_id:
            raise RuntimeError("Fireflies transcript detail did not match the requested id.")
        merged = {**metadata, **detail, "id": transcript_id}
        current_speakers[transcript_id] = (
            _collect_speakers(merged) if config.include_speakers else []
        )
        row = _transcript_to_row(merged, config)
        fingerprint = hashlib.sha256(
            json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        current_hashes[transcript_id] = fingerprint
        if (
            transcript_id not in known_ids
            or transcript_date > last_date
            or previous_hashes.get(transcript_id) != fingerprint
        ):
            yield row
            changed += 1
        newest_date = max(newest_date, transcript_date, _as_timestamp(detail.get("date")))

    # Only a fully exhausted, validated listing can reach reconciliation.
    # An empty successful listing also removes the last known transcript.
    deleted_ids = known_ids - current_ids
    for transcript_id in sorted(deleted_ids):
        yield {"id": transcript_id, "_deleted": True}

    state["known_ids"] = sorted(current_ids)
    state["last_date"] = newest_date
    state["content_hashes"] = current_hashes
    if speaker_records is not None:
        # Publish the full selected speaker snapshot only after extraction
        # succeeds. Unchanged meetings are included so existing graphs can be
        # repaired without re-ingesting documents or repeating API requests.
        speaker_records.clear()
        speaker_records.update(current_speakers)
    logger.info(
        "Fireflies: synced %d new/changed transcript(s), %d deletion(s).",
        changed,
        len(deleted_ids),
    )


def _transcript_to_row(transcript: dict[str, Any], config: _FirefliesConfig) -> dict[str, Any]:
    transcript_id = str(transcript.get("id") or "")
    if not transcript_id:
        raise ValueError("A Fireflies transcript id is required.")

    sections: list[str] = []
    created_at = _format_created_at(transcript.get("date"))
    if created_at:
        sections.append(
            f"## Meeting metadata\n\n- Meeting ID: {transcript_id}\n- Created at: {created_at}"
        )

    if config.include_speakers:
        rendered = _render_speakers(transcript)
        if rendered:
            sections.append(f"## Speakers\n\n{rendered}")

    if config.include_summary:
        rendered = _render_summary(transcript.get("summary") or {})
        if rendered:
            sections.append(f"## Summary\n\n{rendered}")

    if config.include_action_items:
        action_items = _as_text((transcript.get("summary") or {}).get("action_items"))
        if action_items:
            sections.append(f"## Action items\n\n{action_items}")

    if config.include_transcript:
        names = {
            _as_text(speaker.get("id")): _as_text(speaker.get("name"))
            for speaker in transcript.get("speakers") or []
            if isinstance(speaker, dict)
        }
        rendered = _render_sentences(
            transcript.get("sentences") or [], transcript_id=transcript_id, speaker_names=names
        )
        if rendered:
            sections.append(f"## Transcript\n\n{rendered}")

    return {
        "id": transcript_id,
        "title": _as_text(transcript.get("title")) or f"Fireflies transcript {transcript_id}",
        "content": "\n\n".join(sections),
        "url": transcript.get("transcript_url"),
        "_deleted": False,
    }


def _collect_speakers(transcript: dict[str, Any]) -> list[dict[str, str]]:
    """Keep speaker identities separate from prose and scope them to a meeting."""
    speakers: dict[str, dict[str, str]] = {}
    entries = [
        (speaker.get("id"), speaker.get("name"))
        for speaker in transcript.get("speakers") or []
        if isinstance(speaker, dict)
    ]
    entries.extend(
        (sentence.get("speaker_id"), sentence.get("speaker_name"))
        for sentence in transcript.get("sentences") or []
        if isinstance(sentence, dict)
    )
    for raw_id, raw_name in entries:
        speaker_id, name = _as_text(raw_id), _as_text(raw_name)
        identity = f"id:{speaker_id}" if speaker_id else f"name:{name}"
        if identity not in speakers or (not speakers[identity]["raw_name"] and name):
            speakers[identity] = {
                "key": identity,
                "speaker_id": speaker_id,
                "raw_name": name,
                "name": _speaker_label(name, speaker_id, str(transcript["id"])),
            }
    return [
        {key: value for key, value in speaker.items() if key != "raw_name"}
        for speaker in speakers.values()
    ]


def _render_speakers(transcript: dict[str, Any]) -> str:
    lines = []
    for speaker in _collect_speakers(transcript):
        speaker_id = speaker["speaker_id"]
        suffix = f" (Fireflies speaker ID: {speaker_id})" if speaker_id else ""
        lines.append(f"- Speaker: {speaker['name']}{suffix}")
    return "\n".join(lines)


def _render_summary(summary: dict[str, Any]) -> str:
    labels = (
        ("Overview", "overview"),
        ("Short summary", "short_summary"),
        ("Gist", "gist"),
        ("Keywords", "keywords"),
        ("Topics discussed", "topics_discussed"),
    )
    parts = []
    seen: set[str] = set()
    for label, key in labels:
        value = _as_text(summary.get(key))
        if value and value not in seen:
            parts.append(f"**{label}:** {value}")
            seen.add(value)
    return "\n\n".join(parts)


def _render_sentences(
    sentences: list[Any], *, transcript_id: str = "", speaker_names: dict | None = None
) -> str:
    lines = []
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        text = _as_text(sentence.get("text")) or _as_text(sentence.get("raw_text"))
        if not text:
            continue
        speaker_id = _as_text(sentence.get("speaker_id"))
        name = sentence.get("speaker_name") or (speaker_names or {}).get(speaker_id)
        speaker_name = _speaker_label(name, speaker_id, transcript_id)
        attribution = speaker_name
        if speaker_id:
            attribution += f" (speaker_id: {speaker_id})"
        time_range = _format_time_range(sentence.get("start_time"), sentence.get("end_time"))
        prefix = f"[{time_range}] " if time_range else ""
        lines.append(f"- {prefix}{attribution} said: {text}")
    return "\n".join(lines)


def _speaker_fallback(speaker_id: str) -> str:
    return f"Speaker {speaker_id}" if speaker_id else "Unknown speaker"


def _speaker_label(name: Any, speaker_id: str, transcript_id: str) -> str:
    supplied_name = _as_text(name)
    label = supplied_name or _speaker_fallback(speaker_id)
    # Generic diarization labels do not identify the same person across meetings.
    if transcript_id and (
        not supplied_name
        or re.fullmatch(r"(?:speaker\s*\d+|unknown speaker)", label, re.IGNORECASE)
    ):
        label += f" in meeting {transcript_id}"
    return label


def _format_time_range(start: Any, end: Any) -> str:
    start_text = _format_seconds(start)
    end_text = _format_seconds(end)
    if start_text and end_text:
        return f"{start_text}-{end_text}"
    return start_text or end_text


def _format_seconds(value: Any) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_created_at(value: Any) -> str:
    timestamp = _as_timestamp(value)
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _as_timestamp(value: Any) -> float:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError):
        raise ValueError("Fireflies date must be a finite non-negative timestamp.") from None
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("Fireflies date must be a finite non-negative timestamp.")
    return timestamp


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(_as_text(item) for item in value if _as_text(item))
    return str(value).strip()


def fireflies_source(
    api_key: str | None = None,
    *,
    include_transcript: bool = True,
    include_summary: bool = True,
    include_action_items: bool = True,
    include_speakers: bool = True,
    page_size: int = _MAX_PAGE_SIZE,
    client: Any = None,
):
    """Return a speaker-aware, incremental dlt resource for Fireflies meetings."""
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The Fireflies connector requires dlt. Install this package's dependencies."
        ) from exc

    if not any((include_transcript, include_summary, include_action_items, include_speakers)):
        raise ValueError("Select at least one Fireflies content type to ingest.")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= _MAX_PAGE_SIZE
    ):
        raise ValueError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}.")

    if client is None:
        resolved_key = api_key or os.environ.get("FIREFLIES_API_KEY")
        if not resolved_key:
            raise ValueError("Fireflies API key required: pass api_key= or set FIREFLIES_API_KEY.")
        client = FirefliesClient(resolved_key)

    config = _FirefliesConfig(
        include_transcript=include_transcript,
        include_summary=include_summary,
        include_action_items=include_action_items,
        include_speakers=include_speakers,
        page_size=page_size,
    )
    speaker_records: dict[str, list[dict[str, str]]] = {}

    @dlt.resource(
        name=FIREFLIES_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def fireflies_transcripts():
        yield from sync_transcripts(
            client, dlt.current.resource_state(), config, speaker_records=speaker_records
        )

    # Route each row through the normal document -> cognify extraction path.
    setattr(fireflies_transcripts, DOCUMENT_SOURCE_ATTR, FIREFLIES_SOURCE_NAME)
    setattr(fireflies_transcripts, _SPEAKER_RECORDS_ATTR, speaker_records)
    return fireflies_transcripts
