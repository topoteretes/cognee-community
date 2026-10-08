"""DLT source for Granola meeting notes (full-snapshot sync + forget-on-delete).

Fetches Granola meeting notes and renders their summaries, private notes, and
transcripts to markdown, then yields them as a dlt resource for cognee's ingestion
pipeline.

Unlike relational dlt paths, Granola notes are ingested as *normal documents*:
the source declares ``cognee_document_source = "granola"`` via ``DOCUMENT_SOURCE_ATTR``,
so ``resolve_dlt_sources`` tags each row with ``system_metadata["source"] = "granola"``.
``is_dlt_sourced`` therefore returns False and each note flows through the standard
cognify entity-extraction pipeline (chunking + LLM knowledge-graph extraction).

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging with
the notes currently visible to the integration each run. Deletions propagate
automatically:
- Personal keys: deleted notes simply vanish from the listing.
- Workspace keys: deleted notes are flagged with ``deleted_at`` and filtered out.
In both cases, deleted notes drop out of the snapshot and cognee's ``orphan_cleanup``
removes them from the graph and vector stores. Unchanged notes keep a stable content-hash
``id`` and are not re-cognified.
"""

import hashlib
import os
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.sources.helpers.rest_client.paginators import JSONResponseCursorPaginator
from dlt.sources.rest_api import RESTAPIConfig, rest_api_source

logger = get_logger("granola_connector")

# dlt resource / staging-table name for Granola notes.
GRANOLA_TABLE_NAME = "granola_notes"
GRANOLA_SOURCE_NAME = "granola"
GRANOLA_BASE_URL = "https://public-api.granola.ai"


class GranolaCursorPaginator(JSONResponseCursorPaginator):
    """Paginator for Granola API cursor pagination.

    Follows the ``cursor`` field in the response when ``hasMore`` is true.
    Guards against contract violations where ``hasMore`` is true but ``cursor``
    is null or empty, preventing infinite loops.
    """

    def __init__(
        self,
        cursor_path: str = "cursor",
        cursor_param: str = "cursor",
        has_more_path: str = "hasMore",
    ) -> None:
        super().__init__(
            cursor_path=cursor_path,
            cursor_param=cursor_param,
            has_more_path=has_more_path,
        )

    def update_state(self, response: Any, data: Any = None) -> None:
        super().update_state(response, data)
        # Guard: if next cursor reference is missing or empty, terminate pagination
        if not self._next_reference:
            self._has_next_page = False


def _format_speaker(speaker: dict[str, Any] | None) -> str:
    """Format speaker identity from Granola transcript speaker schema.

    Never invents names. Uses resolved name when available, alongside
    relative attribution ('me' / 'them'), or diarization label ('Speaker A').
    """
    if not speaker or not isinstance(speaker, dict):
        return "Unknown"

    name = speaker.get("name")
    attribution = speaker.get("attribution")
    diarization_label = speaker.get("diarization_label")

    if name and attribution:
        return f"{attribution} ({name})"
    if name:
        return name
    if attribution:
        return attribution
    if diarization_label:
        return diarization_label
    return "Unknown"


def _render_transcript_item(item: dict[str, Any]) -> str:
    """Render a single transcript line with timestamp and speaker."""
    start_time = item.get("start_time") or ""
    speaker = _format_speaker(item.get("speaker"))
    text = (item.get("text") or "").strip()

    if start_time:
        return f"[{start_time}] {speaker}: {text}"
    return f"{speaker}: {text}"


def _build_note_content(note: dict[str, Any], include_transcript: bool = True) -> str:
    """Assemble note metadata, AI summary, private notes, and transcript into markdown."""
    title = note.get("title") or "Untitled Note"
    owner = note.get("owner") or {}
    owner_str = ""
    if isinstance(owner, dict):
        owner_name = owner.get("name")
        owner_email = owner.get("email")
        if owner_name and owner_email:
            owner_str = f"{owner_name} ({owner_email})"
        elif owner_email:
            owner_str = owner_email
        elif owner_name:
            owner_str = owner_name

    cal_event = note.get("calendar_event") or {}
    date_str = ""
    organiser_str = ""
    if isinstance(cal_event, dict):
        date_str = cal_event.get("scheduled_start_time") or ""
        organiser_str = cal_event.get("organiser") or ""
    if not date_str:
        date_str = note.get("created_at") or ""

    attendees = note.get("attendees") or []
    attendee_names: list[str] = []
    for att in attendees:
        if isinstance(att, dict):
            name = att.get("name") or att.get("email")
            if name:
                attendee_names.append(name)
    attendees_str = ", ".join(attendee_names)

    note_id = note.get("id") or ""

    lines = [f"# {title}", ""]
    if date_str:
        lines.append(f"- Date: {date_str}")
    if owner_str:
        lines.append(f"- Owner: {owner_str}")
    if organiser_str:
        lines.append(f"- Organiser: {organiser_str}")
    if attendees_str:
        lines.append(f"- Attendees: {attendees_str}")
    if note_id:
        lines.append(f"- Note ID: {note_id}")

    # Summary section (prefer markdown, fallback to plain text)
    summary_md = note.get("summary_markdown")
    summary_text = note.get("summary_text")
    if summary_md:
        lines.extend(["", "## Summary", summary_md.strip()])
    elif summary_text:
        lines.extend(["", "## Summary", summary_text.strip()])

    # Private notes (if populated for the user's personal key)
    private_md = note.get("private_notes_markdown")
    private_text = note.get("private_notes_text")
    if private_md:
        lines.extend(["", "## Private Notes", private_md.strip()])
    elif private_text:
        lines.extend(["", "## Private Notes", private_text.strip()])

    # Transcript section (if enabled and present)
    if include_transcript:
        transcript = note.get("transcript")
        if isinstance(transcript, list) and transcript:
            lines.extend(["", "## Transcript"])
            for item in transcript:
                if isinstance(item, dict):
                    rendered_item = _render_transcript_item(item)
                    if rendered_item:
                        lines.append(rendered_item)

    return "\n".join(lines).strip()


def _note_to_row(note: dict[str, Any], include_transcript: bool = True) -> dict[str, Any]:
    """Convert a Granola note detail dictionary into a document row."""
    note_id = str(note.get("id") or "")
    content = _build_note_content(note, include_transcript=include_transcript)
    content_hash = hashlib.sha256((note_id + content).encode("utf-8")).hexdigest()

    return {
        "id": content_hash,
        "title": note.get("title") or "Untitled Note",
        "url": note.get("web_url") or "",
        "content": content,
    }


def granola_source(
    api_key: str | None = None,
    include_transcript: bool = True,
    client: Any = None,
):
    """Create a dlt source that yields Granola meeting notes as markdown documents.

    Args:
        api_key: Granola API key (Bearer token). Falls back to ``GRANOLA_API_KEY`` env var.
        include_transcript: Whether to include meeting transcripts in the document content.
        client: Pre-built ``requests.Session`` (useful for test injection / custom transport).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    resolved_api_key = api_key or os.environ.get("GRANOLA_API_KEY")
    if not resolved_api_key:
        raise ValueError(
            "Granola integration token required: pass api_key= or set GRANOLA_API_KEY."
        )

    # Note detail query parameters
    detail_params: dict[str, str] = {}
    if include_transcript:
        detail_params["include"] = "transcript"

    config: RESTAPIConfig = {
        "client": {
            "base_url": GRANOLA_BASE_URL,
            "auth": {"token": resolved_api_key},
            "session": client,
        },
        "resources": [
            {
                "name": "granola_pages_list",
                "write_disposition": "replace",
                "primary_key": "id",
                "endpoint": {
                    "path": "/v1/notes",
                    "data_selector": "notes",
                    "params": {"page_size": 30},
                    "paginator": GranolaCursorPaginator(
                        cursor_path="cursor",
                        cursor_param="cursor",
                        has_more_path="hasMore",
                    ),
                },
                "processing_steps": [{"filter": lambda note: not note.get("deleted_at")}],
            },
            {
                "name": GRANOLA_TABLE_NAME,
                "write_disposition": "replace",
                "primary_key": "id",
                "endpoint": {
                    "path": "/v1/notes/{resources.granola_pages_list.id}",
                    "params": detail_params,
                },
                "processing_steps": [
                    {"map": lambda note: _note_to_row(note, include_transcript=include_transcript)}
                ],
            },
        ],
    }

    source = rest_api_source(config)

    # Unselect the intermediate parent listing resource so only the transformed
    # document table (granola_notes) is staged and ingested into cognee memory.
    source.resources["granola_pages_list"].selected = False

    # Opt into cognee document mode: rows flow through full LLM cognify entity extraction
    setattr(source, DOCUMENT_SOURCE_ATTR, GRANOLA_SOURCE_NAME)
    return source
