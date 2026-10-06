"""Fireflies.ai document source for cognee.

The connector performs a lightweight, paginated transcript-id sweep on every
run.  New transcripts (or transcripts beyond the stored ``date`` cursor) are
fetched in full and rendered as speaker-aware markdown documents.  IDs that
disappear from the sweep are emitted as dlt hard-delete rows so cognee's orphan
cleanup forgets them downstream.

Fireflies documents that ``date`` is the transcript creation timestamp.  The
cursor therefore detects newly-created transcripts; it cannot independently
detect later edits to an existing transcript.
"""

from __future__ import annotations

import os
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
_MAX_PAGE_SIZE = 50

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
        response = self._session.post(
            self._endpoint,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables},
            timeout=self._timeout,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Fireflies returned a non-JSON response.") from exc

        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            messages = "; ".join(
                str(error.get("message", error)) if isinstance(error, dict) else str(error)
                for error in errors
            )
            raise RuntimeError(f"Fireflies GraphQL error: {messages}")

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

    def get_transcript(
        self, transcript_id: str, config: _FirefliesConfig
    ) -> dict[str, Any]:
        data = self._query(
            _build_transcript_query(config),
            {"transcriptId": transcript_id},
        )
        transcript = data.get("transcript")
        if not isinstance(transcript, dict):
            raise RuntimeError(f"Fireflies transcript '{transcript_id}' was not returned.")
        return transcript


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
        summary_fields.extend(
            ["overview", "short_summary", "gist", "keywords", "topics_discussed"]
        )
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
    while True:
        page = client.list_transcripts(limit=page_size, skip=skip)
        if not isinstance(page, list):
            raise RuntimeError("Fireflies client returned an invalid transcript page.")
        for transcript in page:
            if not isinstance(transcript, dict) or not transcript.get("id"):
                raise RuntimeError("Fireflies transcript listing contained an item without an id.")
            yield transcript
        if len(page) < page_size:
            return
        skip += len(page)


def sync_transcripts(
    client: Any,
    state: dict[str, Any],
    config: _FirefliesConfig | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield newly-created transcripts and tombstones, then advance state.

    A transcript absent from ``known_ids`` is fetched even when its timestamp is
    at or below the cursor.  This handles restored/moved-in records and timestamp
    ties without duplicating already-known records.
    """
    config = config or _FirefliesConfig()
    known_ids = {str(value) for value in state.get("known_ids", [])}
    last_date = _as_timestamp(state.get("last_date"))
    newest_date = last_date
    current_ids: set[str] = set()
    changed = 0

    for metadata in _iter_transcript_metadata(client, config.page_size):
        transcript_id = str(metadata["id"])
        transcript_date = _as_timestamp(metadata.get("date"))
        current_ids.add(transcript_id)

        if transcript_id in known_ids and transcript_date <= last_date:
            continue

        detail = client.get_transcript(transcript_id, config)
        merged = {**metadata, **detail, "id": transcript_id}
        yield _transcript_to_row(merged, config)
        changed += 1
        newest_date = max(newest_date, transcript_date, _as_timestamp(detail.get("date")))

    # A zero-result sweep after previous successful runs is more likely a scope,
    # permission, or transient API problem than a genuine deletion of everything.
    if known_ids and not current_ids:
        logger.warning(
            "Fireflies returned 0 transcripts while %d were known; preserving state "
            "and skipping deletion reconciliation.",
            len(known_ids),
        )
        return

    deleted_ids = known_ids - current_ids
    for transcript_id in sorted(deleted_ids):
        yield {"id": transcript_id, "_deleted": True}

    state["known_ids"] = sorted(current_ids)
    state["last_date"] = newest_date
    logger.info(
        "Fireflies: synced %d new transcript(s), %d deletion(s).",
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
        sections.append(f"## Meeting metadata\n\n- Created at: {created_at}")

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
        rendered = _render_sentences(transcript.get("sentences") or [])
        if rendered:
            sections.append(f"## Transcript\n\n{rendered}")

    return {
        "id": transcript_id,
        "title": _as_text(transcript.get("title")) or f"Fireflies transcript {transcript_id}",
        "content": "\n\n".join(sections),
        "url": transcript.get("transcript_url"),
        "_deleted": False,
    }


def _render_speakers(transcript: dict[str, Any]) -> str:
    speakers: dict[str, str] = {}
    for speaker in transcript.get("speakers") or []:
        if not isinstance(speaker, dict):
            continue
        speaker_id = _as_text(speaker.get("id"))
        name = _as_text(speaker.get("name")) or _speaker_fallback(speaker_id)
        speakers[speaker_id or name] = name

    # Some responses omit the top-level speaker list; preserve attribution from
    # sentences instead of silently dropping the people involved.
    for sentence in transcript.get("sentences") or []:
        if not isinstance(sentence, dict):
            continue
        speaker_id = _as_text(sentence.get("speaker_id"))
        name = _as_text(sentence.get("speaker_name")) or _speaker_fallback(speaker_id)
        speakers.setdefault(speaker_id or name, name)

    lines = []
    for speaker_id, name in speakers.items():
        suffix = f" (Fireflies speaker ID: {speaker_id})" if speaker_id != name else ""
        lines.append(f"- Speaker: {name}{suffix}")
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


def _render_sentences(sentences: list[Any]) -> str:
    lines = []
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        text = _as_text(sentence.get("text")) or _as_text(sentence.get("raw_text"))
        if not text:
            continue
        speaker_id = _as_text(sentence.get("speaker_id"))
        speaker_name = _as_text(sentence.get("speaker_name")) or _speaker_fallback(speaker_id)
        attribution = speaker_name
        if speaker_id:
            attribution += f" (speaker_id: {speaker_id})"
        time_range = _format_time_range(sentence.get("start_time"), sentence.get("end_time"))
        prefix = f"[{time_range}] " if time_range else ""
        lines.append(f"- {prefix}{attribution} said: {text}")
    return "\n".join(lines)


def _speaker_fallback(speaker_id: str) -> str:
    return f"Speaker {speaker_id}" if speaker_id else "Unknown speaker"


def _format_time_range(start: Any, end: Any) -> str:
    start_text = _format_seconds(start)
    end_text = _format_seconds(end)
    if start_text and end_text:
        return f"{start_text}-{end_text}"
    return start_text or end_text


def _format_seconds(value: Any) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
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
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}.")

    if client is None:
        resolved_key = api_key or os.environ.get("FIREFLIES_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Fireflies API key required: pass api_key= or set FIREFLIES_API_KEY."
            )
        client = FirefliesClient(resolved_key)

    config = _FirefliesConfig(
        include_transcript=include_transcript,
        include_summary=include_summary,
        include_action_items=include_action_items,
        include_speakers=include_speakers,
        page_size=page_size,
    )

    @dlt.resource(
        name=FIREFLIES_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def fireflies_transcripts():
        yield from sync_transcripts(client, dlt.current.resource_state(), config)

    # Route each row through the normal document -> cognify extraction path.
    setattr(fireflies_transcripts, DOCUMENT_SOURCE_ATTR, FIREFLIES_SOURCE_NAME)
    return fireflies_transcripts
