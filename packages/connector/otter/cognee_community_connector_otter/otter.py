"""dlt source for Otter.ai conversation transcripts (merge + _deleted tombstone + incremental).

Fetches Otter.ai meeting conversations and transcripts via the Public API,
then yields them as a dlt resource for cognee's ingestion pipeline.

Otter.ai conversations are ingested as *normal documents* via cognee's
document-mode marker, flowing through the standard cognify entity-extraction
pipeline.

Sync model: ``write_disposition="merge"`` with a ``_deleted`` boolean column.
Each run:
1. Pages the ``/conversations`` endpoint, filtering by ``created_at`` cursor
   for anything new since the last sync (plus a 5-minute overlap window).
2. On full syncs (first run), emits ``_deleted=True`` tombstones for any
   conversation id that is no longer reachable through the active listing
   (deleted, moved, permissions changed).

A transient API error aborts the run before any tombstone is emitted, so a
partial snapshot never drives mass deletions. Permanent errors (auth, 404 on
the root endpoint) raise immediately.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("otter_connector")

OTTER_TABLE_NAME = "otter_conversations"
OTTER_SOURCE_NAME = "otter"
OTTER_API_BASE = "https://api.otter.ai/v1"

_MAX_RETRIES = 5
_OVERLAP_WINDOW_MINUTES = 5
_PAGE_LIMIT = 100

_EXTRA_HINT = (
    'The Otter.ai connector requires the "otter" extra: pip install "cognee[otter]" '
    "(provides dlt and httpx)."
)


def otter_source(
    api_key: str | None = None,
    include_shared: bool = False,
    channel_id: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Otter.ai meeting transcripts.

    Args:
        api_key: Otter.ai Public API key (Enterprise workspace). Falls back
            to the ``OTTER_API_KEY`` environment variable.
        include_shared: Whether to include conversations shared with the user.
        channel_id: Optional filter to restrict sync to a specific channel.
        client: Pre-built HTTP client callable (test-injection point).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        resolved_key = api_key or os.environ.get("OTTER_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Otter.ai API key required: pass it explicitly or set the "
                "OTTER_API_KEY environment variable."
            )

        session = httpx.Client(
            base_url=OTTER_API_BASE,
            headers={"Authorization": f"Bearer {resolved_key}"},
            timeout=30.0,
        )

        def _api_request(method: str, path: str, **kwargs: Any) -> Any:
            for attempt in range(_MAX_RETRIES):
                try:
                    response = session.request(method, path, **kwargs)
                except (httpx.TransportError, httpx.TimeoutException):
                    if attempt == _MAX_RETRIES - 1:
                        raise
                    delay = 2**attempt
                    logger.warning(
                        "Otter.ai: network error — retrying in %.1fs (%d/%d).",
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue

                if response.status_code == 429:
                    retry_after = int(response.headers.get("retry-after", 2**attempt))
                    time.sleep(retry_after)
                    continue
                if response.status_code >= 500 and attempt < _MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                if response.status_code in (401, 403):
                    raise PermissionError(
                        f"Otter.ai API: {response.status_code} — check your API key."
                    )
                response.raise_for_status()
                return response.json()
            raise RuntimeError(
                f"Otter.ai API: {method} {path} failed after {_MAX_RETRIES} retries."
            )

        client = _api_request

    @dlt.resource(
        name=OTTER_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
    )
    def otter_conversations() -> Iterator[dict[str, Any]]:
        import dlt as _runtime_dlt

        state = _runtime_dlt.current.resource_state()
        last_created = state.get("last_created_at")
        known_ids: set[str] = set(state.get("known_ids", []))

        if last_created:
            cursor_dt = datetime.fromisoformat(last_created).replace(tzinfo=UTC) - timedelta(
                minutes=_OVERLAP_WINDOW_MINUTES
            )
            cursor_iso = cursor_dt.isoformat()
        else:
            cursor_iso = None

        seen_ids: set[str] = set()
        max_created = last_created

        for conv in _iter_conversations(client, include_shared, channel_id):
            conv_id = conv.get("id")
            created_at = conv.get("created_at")

            if cursor_iso and created_at and created_at < cursor_iso:
                continue

            if conv_id:
                seen_ids.add(conv_id)
                known_ids.add(conv_id)

            if created_at and (max_created is None or created_at > max_created):
                max_created = created_at

            yield _conv_to_row(client, conv)

        # Deletion detection: known IDs not in current listing get tombstones
        # Only run on full syncs (first run) when we have a complete picture
        if not cursor_iso:
            for known_id in known_ids - seen_ids:
                logger.info(
                    "Otter.ai: conversation %s no longer in listing, tombstoning.", known_id
                )
                yield {"id": known_id, "_deleted": True}
                known_ids.discard(known_id)

        # Update state
        if max_created:
            state["last_created_at"] = max_created
        state["known_ids"] = sorted(known_ids)

        logger.info(
            "Otter.ai: synced %d conversation(s) (%d new/updated).",
            len(seen_ids),
            len(seen_ids),
        )

    @dlt.source(name=OTTER_SOURCE_NAME)
    def _otter() -> Any:
        return otter_conversations

    source = _otter()
    setattr(source, DOCUMENT_SOURCE_ATTR, OTTER_SOURCE_NAME)
    return source


def _is_transient(exc: Exception) -> bool:
    """True for network / timeout / 429 / 5xx errors worth retrying."""
    try:
        import httpx
    except ImportError:
        return True

    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if hasattr(exc, "response"):
        status = getattr(exc.response, "status_code", None)
        if status in (429, 500, 502, 503, 504):
            return True
    return False


def _is_gone(exc: Exception) -> bool:
    """True when a conversation is permanently gone (404) — safe to tombstone."""
    if hasattr(exc, "response"):
        status = getattr(exc.response, "status_code", None)
        return status == 404
    return False


def _iter_conversations(
    client: Any,
    include_shared: bool,
    channel_id: str | None,
) -> Iterable[dict[str, Any]]:
    """Yield Otter.ai conversations using cursor-based pagination."""
    params: dict[str, Any] = {
        "include_shared": str(include_shared).lower(),
        "limit": _PAGE_LIMIT,
    }
    if channel_id:
        params["channel_id"] = channel_id

    next_cursor: str | None = None
    while True:
        if next_cursor:
            params["cursor"] = next_cursor

        data = client("GET", "/conversations", params=params)
        conversations = data.get("data", [])
        meta = data.get("meta", {})

        yield from conversations

        if not meta.get("has_more"):
            break
        next_cursor = meta.get("next_cursor")
        if not next_cursor:
            break


def _conv_to_row(client: Any, conv: dict[str, Any]) -> dict[str, Any]:
    """Transform a raw Otter.ai conversation into a cognee document row."""
    conv_id = conv.get("id", "")
    title = conv.get("title", "Untitled meeting")
    owner = conv.get("owner", {}) if isinstance(conv.get("owner"), dict) else {}
    summary = conv.get("abstract_summary")

    # Build prose-friendly text body
    body_parts: list[str] = []
    body_parts.append(f"Otter.ai meeting transcript: {title}")
    body_parts.append(f"URL: {conv.get('url', '')}")
    body_parts.append(f"Created: {conv.get('created_at', 'unknown')}")

    if owner.get("name"):
        body_parts.append(f"Owner: {owner.get('name')} <{owner.get('email', '')}>")

    if summary:
        body_parts.append(f"Summary:\n{summary}")

    # Fetch full transcript
    try:
        detail = client("GET", f"/conversations/{conv_id}", params={"include": "transcript"})
        relationships = detail.get("data", {}).get("relationships", {})
        transcript = relationships.get("transcript", {})
        if isinstance(transcript, dict) and transcript.get("content"):
            body_parts.append(f"Transcript:\n{transcript['content']}")
    except Exception as exc:
        if _is_gone(exc):
            logger.warning("Conversation %s gone during transcript fetch, tombstoning.", conv_id)
            return {
                "id": conv_id,
                "title": title,
                "url": conv.get("url"),
                "created_at": conv.get("created_at"),
                "owner_name": owner.get("name") if owner else None,
                "owner_email": owner.get("email") if owner else None,
                "abstract_summary": summary,
                "text": "\n\n".join(body_parts),
                "raw": conv,
                "source": OTTER_SOURCE_NAME,
                "_deleted": True,
            }
        logger.warning("Failed to fetch transcript for conversation %s", conv_id)

    # Calendar guests
    guests = conv.get("calendar_guests", [])
    if guests:
        guest_names = [g.get("name", "") for g in guests if isinstance(g, dict)]
        body_parts.append(f"Participants: {', '.join(filter(None, guest_names))}")

    return {
        "id": conv_id,
        "title": title,
        "url": conv.get("url"),
        "created_at": conv.get("created_at"),
        "owner_name": owner.get("name") if owner else None,
        "owner_email": owner.get("email") if owner else None,
        "abstract_summary": summary,
        "text": "\n\n".join(body_parts),
        "raw": conv,
        "source": OTTER_SOURCE_NAME,
        "_deleted": False,
    }
