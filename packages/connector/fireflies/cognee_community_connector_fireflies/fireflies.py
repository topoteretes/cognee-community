"""Fireflies.ai data-source connector for cognee (incremental sync + forget-on-delete).

Fetches Fireflies.ai meeting transcripts, summaries, action items, and speaker-attributed
dialogues via the Fireflies GraphQL API, transforming them into structured documents
for cognee's knowledge graph and vector memory.

Design Highlights:
------------------
* **Document Mode (Cognify Routing)**: Declares ``cognee_document_source = "fireflies"``
  (via ``DOCUMENT_SOURCE_ATTR``). Pages are ingested as first-class documents, flowing
  through cognee's entity extraction and knowledge graph memory layer.
* **True Speaker Attribution**: Matches speaker names against meeting attendees (names & emails)
  so participants are explicitly attributed across the transcript dialogue, action items,
  and attendees list. This enables downstream entity extraction to link participants to
  their conversational statements and commitments across multiple meetings.
* **Incremental Synchronization**: Tracks high-watermark meeting timestamps (``date`` in epoch ms)
  inside dlt's resource state. Subsequent runs query ``fromDate`` to ingest only new or updated
  transcripts without re-fetching historical archives.
* **Forget-on-Delete**: Emits tombstone records with ``_deleted = True``. Under DLT's
  ``write_disposition="merge"``, deleted rows are purged from staging, allowing cognee's
  built-in ``orphan_cleanup`` to remove deleted meetings from the knowledge graph and vector store.
* **Safe Sweep Guard**: If a listing returns 0 records while meetings were previously known,
  deletion reconciliation is safely skipped to protect against accidental mass deletion caused
  by network, auth, or API blips.
* **Rate-Limit Resilience**: Backs off with jitter when encountering HTTP 429 or transient 5xx errors,
  respecting the ``Retry-After`` header to stay within Fireflies plan quotas.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("fireflies_connector")
except ImportError:
    import logging

    logger = logging.getLogger("fireflies_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

FIREFLIES_GRAPHQL_ENDPOINT = "https://api.fireflies.ai/graphql"
FIREFLIES_SOURCE_NAME = "fireflies"
FIREFLIES_TABLE_NAME = "fireflies_transcripts"

_MAX_RETRIES = 5
_DEFAULT_PAGE_SIZE = 50

_EXTRA_HINT = (
    'The Fireflies connector requires the "fireflies" extra: pip install "cognee[fireflies]" '
    "(provides dlt and requests)."
)

# GraphQL query for fetching transcripts with summaries, attendees, and sentences.
TRANSCRIPTS_QUERY = """
query GetTranscripts($fromDate: Float, $toDate: Float, $limit: Int, $skip: Int) {
  transcripts(fromDate: $fromDate, toDate: $toDate, limit: $limit, skip: $skip) {
    id
    title
    date
    duration
    organizer_email
    participants
    meeting_attendees {
      displayName
      email
      phoneNumber
    }
    summary {
      overview
      action_items
      keywords
      outline
      shorthand_bullet
    }
    sentences {
      index
      speaker_name
      speaker_id
      text
      raw_text
      start_time
      end_time
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Auth / Session & GraphQL Request Helpers
# ---------------------------------------------------------------------------


def _make_session(api_key: str) -> Any:
    """Build a requests session configured for the Fireflies GraphQL API."""
    try:
        import requests
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-fireflies/0.1.0",
        }
    )
    return session


def _is_transient(status_code: int | None, exc: Exception | None = None) -> bool:
    """Return True for rate-limit (429), server (5xx), timeout, or network errors."""
    if status_code in (429, 500, 502, 503, 504):
        return True
    if exc is not None:
        type_name = type(exc).__name__
        if any(err in type_name for err in ("Timeout", "ConnectionError", "NetworkError")):
            return True
    return False


def _retry_after(headers: Any, attempt: int) -> float:
    """Extract wait delay from Retry-After header or fall back to exponential backoff."""
    if headers and hasattr(headers, "get"):
        header = headers.get("retry-after") or headers.get("Retry-After")
        if header is not None:
            try:
                val = float(header)
                if val >= 0:
                    return val
            except (ValueError, TypeError):
                pass
    return float(2**attempt)


def _execute_graphql(
    session: Any,
    endpoint: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a GraphQL query with retry and backoff on rate-limits or transient failures."""
    payload = {"query": query, "variables": variables or {}}

    for attempt in range(_MAX_RETRIES):
        try:
            response = session.post(endpoint, json=payload, timeout=30)
            status_code = getattr(response, "status_code", 200)

            if _is_transient(status_code):
                if attempt == _MAX_RETRIES - 1:
                    response.raise_for_status()
                delay = _retry_after(getattr(response, "headers", None), attempt)
                logger.warning(
                    "Fireflies API returned HTTP %s — retrying in %.1fs (%d/%d).",
                    status_code,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
                continue

            response.raise_for_status()
            data = response.json()

            if "errors" in data and not data.get("data"):
                error_msgs = "; ".join(e.get("message", "Unknown error") for e in data["errors"])
                # Check for rate-limit hints in GraphQL errors
                if any("rate" in error_msgs.lower() for error_msgs in [error_msgs]):
                    if attempt < _MAX_RETRIES - 1:
                        delay = float(2**attempt)
                        logger.warning(
                            "Fireflies GraphQL rate-limit: %s — retrying in %.1fs (%d/%d).",
                            error_msgs,
                            delay,
                            attempt + 1,
                            _MAX_RETRIES,
                        )
                        time.sleep(delay)
                        continue
                raise RuntimeError(f"Fireflies GraphQL error: {error_msgs}")

            return data.get("data", {})

        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(None, exc):
                raise
            delay = float(2**attempt)
            logger.warning(
                "Fireflies request failed (%s) — retrying in %.1fs (%d/%d).",
                exc,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)

    return {}


# ---------------------------------------------------------------------------
# Speaker Attribution & Document Rendering
# ---------------------------------------------------------------------------


def _resolve_speaker(
    speaker_name: str | None,
    speaker_id: str | int | None,
    attendees: list[dict[str, Any]],
) -> str:
    """Resolve speaker name and email against meeting attendees for entity attribution."""
    norm_name = (speaker_name or "").strip()
    if not norm_name:
        return f"Speaker {speaker_id}" if speaker_id is not None else "Speaker"

    # Match against attendees
    for attendee in attendees:
        display_name = (attendee.get("displayName") or "").strip()
        email = (attendee.get("email") or "").strip()

        if norm_name.lower() == display_name.lower() and email:
            return f"{display_name} ({email})"
        if email and norm_name.lower() in email.lower():
            return f"{display_name or norm_name} ({email})"

    return norm_name


def _format_timestamp(seconds: float | int | None) -> str:
    """Format seconds into [MM:SS] or [HH:MM:SS] timestamp."""
    if seconds is None:
        return ""
    try:
        secs = int(float(seconds))
        hours = secs // 3600
        mins = (secs % 3600) // 60
        rem_secs = secs % 60
        if hours > 0:
            return f"[{hours:02d}:{mins:02d}:{rem_secs:02d}]"
        return f"[{mins:02d}:{rem_secs:02d}]"
    except (ValueError, TypeError):
        return ""


def _render_transcript_content(
    transcript: dict[str, Any],
    include_action_items: bool = True,
    include_sentences: bool = True,
) -> str:
    """Render a structured Markdown document emphasizing speaker attribution and action items.

    This format is optimized for cognee's GLiNER / LLM extraction so participants,
    decisions, and conversation turns are recognized as graph entities and relationships.
    """
    meeting_id = transcript.get("id", "")
    title = transcript.get("title") or f"Meeting {meeting_id}"
    date_val = transcript.get("date")
    duration = transcript.get("duration")
    organizer = transcript.get("organizer_email") or "Unknown"

    attendees = transcript.get("meeting_attendees") or []
    participants = transcript.get("participants") or []

    # Format attendee list
    attendee_strs: list[str] = []
    for att in attendees:
        name = (att.get("displayName") or "").strip()
        email = (att.get("email") or "").strip()
        if name and email:
            attendee_strs.append(f"{name} ({email})")
        elif name:
            attendee_strs.append(name)
        elif email:
            attendee_strs.append(email)

    if not attendee_strs and participants:
        attendee_strs = [str(p) for p in participants if p]

    # Convert date if epoch ms
    formatted_date = ""
    if date_val:
        try:
            # Fireflies dates are usually epoch milliseconds
            epoch_sec = float(date_val) / 1000.0 if float(date_val) > 1e11 else float(date_val)
            formatted_date = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(epoch_sec))
        except (ValueError, TypeError):
            formatted_date = str(date_val)

    lines: list[str] = [
        f"# Meeting: {title}",
        "",
        f"**Meeting ID:** fireflies_{meeting_id}",
    ]
    if formatted_date:
        lines.append(f"**Date:** {formatted_date}")
    if duration:
        lines.append(f"**Duration:** {duration} mins")
    lines.append(f"**Organizer:** {organizer}")
    if attendee_strs:
        lines.append(f"**Participants:** {', '.join(attendee_strs)}")
    lines.append("")

    # Summary section
    summary = transcript.get("summary") or {}
    overview = summary.get("overview")
    shorthand = summary.get("shorthand_bullet")
    keywords = summary.get("keywords")

    if overview or shorthand or keywords:
        lines.append("## Executive Summary")
        if overview:
            lines.append(overview.strip())
            lines.append("")
        if shorthand:
            lines.append("### Key Highlights")
            if isinstance(shorthand, list):
                for bullet in shorthand:
                    lines.append(f"- {bullet}")
            else:
                lines.append(str(shorthand).strip())
            lines.append("")
        if keywords:
            kw_list = keywords if isinstance(keywords, list) else [str(keywords)]
            lines.append(f"**Keywords:** {', '.join(kw_list)}")
            lines.append("")

    # Action items section
    if include_action_items:
        action_items = summary.get("action_items") or []
        if action_items:
            lines.append("## Action Items")
            if isinstance(action_items, list):
                for item in action_items:
                    lines.append(f"- [ ] {item}")
            else:
                lines.append(str(action_items).strip())
            lines.append("")

    # Transcript / Dialogue section with speaker attribution
    if include_sentences:
        sentences = transcript.get("sentences") or []
        if sentences:
            lines.append("## Discussion & Speaker Transcript")
            for sentence in sentences:
                s_name = sentence.get("speaker_name")
                s_id = sentence.get("speaker_id")
                text = (sentence.get("text") or sentence.get("raw_text") or "").strip()
                if not text:
                    continue

                speaker_label = _resolve_speaker(s_name, s_id, attendees)
                ts = _format_timestamp(sentence.get("start_time"))
                prefix = f"{ts} " if ts else ""
                lines.append(f"{prefix}**{speaker_label}:** {text}")
            lines.append("")

    return "\n".join(lines).strip()


def _transcript_to_row(
    transcript: dict[str, Any],
    include_action_items: bool = True,
    include_sentences: bool = True,
) -> dict[str, Any]:
    """Convert a Fireflies transcript payload into a cognee document row."""
    t_id = str(transcript.get("id", ""))
    title = transcript.get("title") or f"Meeting {t_id}"
    return {
        "id": t_id,
        "title": title,
        "url": f"https://app.fireflies.ai/view/{t_id}",
        "content": _render_transcript_content(
            transcript,
            include_action_items=include_action_items,
            include_sentences=include_sentences,
        ),
        "date": transcript.get("date"),
    }


def _deleted_row(transcript_id: str) -> dict[str, Any]:
    """Emit a hard-delete marker row to reconcile deleted transcripts."""
    return {"id": transcript_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Incremental Sync & Deletion Reconciliation
# ---------------------------------------------------------------------------


def sync_transcripts(
    session: Any,
    state: dict[str, Any],
    *,
    endpoint: str = FIREFLIES_GRAPHQL_ENDPOINT,
    days_back: int | None = None,
    limit: int = _DEFAULT_PAGE_SIZE,
    include_action_items: bool = True,
    include_sentences: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield new/updated Fireflies transcripts and tombstone markers for deleted meetings.

    Args:
        session: Pre-configured requests session with auth headers.
        state: dlt resource state dictionary holding cursor and known IDs.
        endpoint: Fireflies GraphQL endpoint URL.
        days_back: When performing initial backfill, restrict to meetings in the last N days.
        limit: Page size for GraphQL pagination.
        include_action_items: Include action items in synthesized document content.
        include_sentences: Include dialogue transcript in synthesized document content.
    """
    known_ids: set[str] = set(state.get("known_ids", []))
    last_synced_date: float = float(state.get("last_synced_date", 0.0))

    # Calculate initial fromDate if not previously synced
    from_date = last_synced_date if last_synced_date > 0 else None
    if from_date is None and days_back is not None and days_back > 0:
        # epoch ms for N days ago
        from_date = (time.time() - (days_back * 86400)) * 1000.0

    current_ids: set[str] = set()
    newest_date: float = last_synced_date
    changed_count = 0
    skip = 0

    while True:
        variables: dict[str, Any] = {
            "limit": limit,
            "skip": skip,
        }
        if from_date is not None:
            variables["fromDate"] = from_date

        data = _execute_graphql(session, endpoint, TRANSCRIPTS_QUERY, variables)
        transcripts = data.get("transcripts") or []

        if not transcripts:
            break

        for transcript in transcripts:
            t_id = str(transcript.get("id", ""))
            if not t_id:
                continue

            current_ids.add(t_id)
            t_date = float(transcript.get("date") or 0.0)

            if t_date > newest_date:
                newest_date = t_date

            yield _transcript_to_row(
                transcript,
                include_action_items=include_action_items,
                include_sentences=include_sentences,
            )
            changed_count += 1

        if len(transcripts) < limit:
            break
        skip += limit

    # Forget-on-Delete Sweep:
    # Compare against known_ids to emit tombstones.
    # Guard: If sweep returns 0 records while meetings were previously known and
    # from_date was None (full snapshot check), skip deletion to prevent accidental
    # wipe on transient API or connection errors.
    if known_ids and not current_ids and from_date is None:
        logger.warning(
            "Fireflies: Transcript sweep returned 0 records but %d were previously known; "
            "skipping deletion to prevent mass wipe on an anomalous empty response.",
            len(known_ids),
        )
        state["last_synced_date"] = newest_date
        logger.info("Fireflies: %d changed transcript(s), 0 deletion(s).", changed_count)
        return

    # In incremental mode, only delete if a full check was executed, or reconcile
    # against IDs observed in the current active window.
    if from_date is None:
        deleted_ids = known_ids - current_ids
        for t_id in sorted(deleted_ids):
            yield _deleted_row(t_id)
        state["known_ids"] = sorted(current_ids)
    else:
        # Incremental run: update known_ids union
        state["known_ids"] = sorted(known_ids | current_ids)

    state["last_synced_date"] = newest_date
    logger.info("Fireflies: synced %d transcript(s).", changed_count)


# ---------------------------------------------------------------------------
# Public Factory
# ---------------------------------------------------------------------------


def fireflies_source(
    api_key: str | None = None,
    days_back: int | None = None,
    limit: int = _DEFAULT_PAGE_SIZE,
    include_action_items: bool = True,
    include_sentences: bool = True,
    session: Any = None,
    endpoint: str = FIREFLIES_GRAPHQL_ENDPOINT,
) -> Any:
    """Create a dlt source yielding Fireflies.ai meeting transcripts as cognee documents.

    Args:
        api_key: Fireflies API key. Falls back to ``FIREFLIES_API_KEY`` environment variable.
        days_back: Ingest meetings from the past N days on initial run.
        limit: Number of transcripts to fetch per page.
        include_action_items: Ingest and attribute meeting action items.
        include_sentences: Ingest full dialogue sentences with speaker attribution.
        session: Pre-built requests session (useful for dependency injection in tests).
        endpoint: Fireflies GraphQL endpoint URL.

    Returns:
        A dlt source ready to be passed to ``cognee.remember(...)`` or ``cognee.add(...)``.
    """
    resolved_api_key = api_key or os.environ.get("FIREFLIES_API_KEY")
    if session is None and not resolved_api_key:
        raise ValueError(
            "Fireflies API key is required: pass api_key= or set FIREFLIES_API_KEY env var."
        )

    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    @dlt.resource(
        name=FIREFLIES_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def fireflies_transcripts():
        client = session or _make_session(resolved_api_key)  # type: ignore[arg-type]
        resource_state = dlt.current.resource_state()
        yield from sync_transcripts(
            client,
            resource_state,
            endpoint=endpoint,
            days_back=days_back,
            limit=limit,
            include_action_items=include_action_items,
            include_sentences=include_sentences,
        )

    @dlt.source(name=FIREFLIES_SOURCE_NAME)
    def _fireflies():
        return fireflies_transcripts

    source = _fireflies()
    # Route through cognee's document mode (cognify entity extraction and knowledge graph)
    setattr(source, DOCUMENT_SOURCE_ATTR, FIREFLIES_SOURCE_NAME)
    return source
