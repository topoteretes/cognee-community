"""Readwise highlight export connector for cognee.

The connector emits one document per Readwise book or article.  Each document
contains its source metadata, document note, summary, highlights, and highlight
notes.  The export endpoint's ``updatedAfter`` cursor keeps subsequent runs
incremental, while ``includeDeleted=true`` and dlt's hard-delete hint propagate
deleted books into cognee's existing orphan cleanup.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator, MutableMapping
from datetime import UTC, datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("readwise_connector")

READWISE_EXPORT_URL = "https://readwise.io/api/v2/export/"
READWISE_SOURCE_NAME = "readwise"
READWISE_TABLE_NAME = "readwise_documents"
_MAX_RETRIES = 5
_EXTRA_HINT = (
    'The Readwise connector requires dlt and requests: pip install "dlt[sqlalchemy]" requests.'
)


def readwise_source(
    token: str | None = None,
    book_ids: list[int | str] | None = None,
    session: Any = None,
    clock: Callable[[], datetime] | None = None,
):
    """Create an incremental dlt source for a Readwise highlight library.

    Args:
        token: Readwise access token. Falls back to ``READWISE_ACCESS_TOKEN``.
        book_ids: Optionally restrict the export to Readwise ``user_book_id`` values.
        session: Requests-compatible session, primarily for offline tests.
        clock: UTC-aware clock injection, primarily for deterministic tests.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = token or os.environ.get("READWISE_ACCESS_TOKEN")
    if not resolved_token:
        raise ValueError(
            "Readwise access token required: pass token= or set READWISE_ACCESS_TOKEN."
        )

    if session is None:
        try:
            import requests
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc
        session = requests.Session()

    @dlt.resource(
        name=READWISE_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def readwise_documents():
        state = dlt.current.resource_state()
        yield from _sync_rows(
            session,
            resolved_token,
            state,
            book_ids=book_ids,
            clock=clock,
        )

    @dlt.source(name=READWISE_SOURCE_NAME)
    def _readwise():
        return readwise_documents

    source = _readwise()
    setattr(source, DOCUMENT_SOURCE_ATTR, READWISE_SOURCE_NAME)
    return source


def _sync_rows(
    session: Any,
    token: str,
    state: MutableMapping[str, Any],
    *,
    book_ids: list[int | str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one sync batch and advance its cursor only after complete pagination."""
    sync_started_at = _isoformat_utc((clock or (lambda: datetime.now(UTC)))())
    updated_after = state.get("updated_after")
    count = 0

    for book in _iter_export(
        session,
        token,
        updated_after=updated_after,
        book_ids=book_ids,
    ):
        count += 1
        yield _book_to_row(book)

    state["updated_after"] = sync_started_at
    logger.info("Readwise: synced %d changed document(s).", count)


def _iter_export(
    session: Any,
    token: str,
    *,
    updated_after: str | None = None,
    book_ids: list[int | str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Page through Readwise's v2 export endpoint."""
    page_cursor = None
    seen_cursors: set[str] = set()

    while True:
        params: dict[str, str] = {"includeDeleted": "true"}
        if updated_after:
            params["updatedAfter"] = updated_after
        if book_ids:
            params["ids"] = ",".join(str(book_id) for book_id in book_ids)
        if page_cursor:
            params["pageCursor"] = page_cursor

        response = _request(
            session,
            headers={"Authorization": f"Token {token}"},
            params=params,
        )
        payload = response.json()
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise ValueError("Readwise export response field 'results' must be a list.")
        yield from results

        next_cursor = payload.get("nextPageCursor")
        if not next_cursor:
            return
        next_cursor = str(next_cursor)
        if next_cursor in seen_cursors:
            raise ValueError("Readwise export returned a repeated page cursor.")
        seen_cursors.add(next_cursor)
        page_cursor = next_cursor


def _request(session: Any, *, headers: dict[str, str], params: dict[str, str]):
    """GET one export page, retrying rate limits and transient server failures."""
    for attempt in range(_MAX_RETRIES):
        response = session.get(READWISE_EXPORT_URL, headers=headers, params=params, timeout=30)
        if response.status_code not in (429, 500, 502, 503, 504):
            response.raise_for_status()
            return response
        if attempt == _MAX_RETRIES - 1:
            response.raise_for_status()

        delay = _retry_after(response.headers, attempt)
        logger.warning(
            "Readwise: HTTP %d — retrying in %.1fs (%d/%d).",
            response.status_code,
            delay,
            attempt + 1,
            _MAX_RETRIES,
        )
        time.sleep(delay)

    raise RuntimeError("Readwise request retry loop exited unexpectedly.")


def _retry_after(headers: MutableMapping[str, str] | None, attempt: int) -> float:
    value = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(2**attempt)


def _book_to_row(book: dict[str, Any]) -> dict[str, Any]:
    """Convert one exported book and its highlights to a document row."""
    book_id = book.get("user_book_id")
    if book_id is None:
        raise ValueError("Readwise export item is missing 'user_book_id'.")

    if book.get("is_deleted"):
        return {"id": str(book_id), "_deleted": True}

    return {
        "id": str(book_id),
        "url": book.get("source_url") or book.get("readwise_url") or book.get("unique_url"),
        "title": book.get("title") or book.get("readable_title") or "Untitled",
        "content": _render_book(book),
        "_deleted": False,
    }


def _render_book(book: dict[str, Any]) -> str:
    """Render stable source context, highlights, and notes as markdown."""
    lines: list[str] = []
    author = _clean(book.get("author"))
    category = _clean(book.get("category"))
    source = _clean(book.get("source"))
    if author:
        lines.append(f"Author: {author}")
    if category:
        lines.append(f"Category: {category}")
    if source:
        lines.append(f"Source: {source}")

    summary = _clean(book.get("summary"))
    document_note = _clean(book.get("document_note"))
    if summary:
        lines.extend(["", "## Summary", summary])
    if document_note:
        lines.extend(["", "## Document note", document_note])

    highlights = [
        highlight
        for highlight in (book.get("highlights") or [])
        if isinstance(highlight, dict) and not highlight.get("is_deleted")
    ]
    highlights.sort(key=_highlight_sort_key)
    if highlights:
        lines.extend(["", "## Highlights"])
    for highlight in highlights:
        text = _clean(highlight.get("text"))
        note = _clean(highlight.get("note"))
        if not text and not note:
            continue
        if text:
            lines.append(f"- {text}")
        if note:
            lines.append(f"  Note: {note}")

    return "\n".join(lines).strip()


def _highlight_sort_key(highlight: dict[str, Any]) -> tuple[int, str]:
    location = highlight.get("location")
    try:
        location_number = int(location)
    except (TypeError, ValueError):
        location_number = 2**31 - 1
    return location_number, str(highlight.get("id") or "")


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _isoformat_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
