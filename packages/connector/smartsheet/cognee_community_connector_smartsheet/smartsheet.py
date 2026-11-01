"""Smartsheet connector for cognee — a ``dlt`` source that turns sheets into memory.

Sync Smartsheet sheets — rows, discussions, and attachment text — into cognee,
incrementally and with forget-on-deletion — "ask my sheets".  Like the sibling
Confluence connector this builds entirely on the existing DLT ingestion
subsystem; the source produced here is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_smartsheet import smartsheet_source

    await cognee.remember(
        smartsheet_source(token="…"),          # or SMARTSHEET_TOKEN
        dataset_name="my_sheets",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by row id
        max_rows_per_table=0,        # 0 = no row cap (see note below)
    )

Design
------
* **Auth** — a Smartsheet API access token, sent as ``Authorization: Bearer``
  on every request. The connector only issues ``GET`` requests.
* **Rows are columnar, not documents** (the issue's watch-out) — so each row
  is rendered into a real document: the row's *primary column* value becomes
  the title, and every non-empty cell becomes a ``Column: value`` line using
  the sheet's column titles (Smartsheet stores cells as ``columnId`` /
  ``displayValue`` pairs). The owning sheet's name prefixes the title so
  graph nodes from different sheets are self-describing. Discussion comments
  and attachment descriptions are folded into the row's document; plain-text
  and CSV attachments (below ``max_attachment_bytes``) have their text
  inlined, other file types are listed by name.
* **Primary key** — the Smartsheet row id. Combined with
  ``write_disposition="merge"`` this gives idempotent upserts, and cognee's
  content-hash ``data_id`` keeps unchanged documents from being
  re-cognified.
* **Incremental cursor** — two levels, mirroring Smartsheet's structure:
  the account sheet listing carries each sheet's ``modifiedAt``; a sheet whose
  ``modifiedAt`` did not advance since the last run is skipped without
  fetching rows, and inside a changed sheet only rows whose ``modifiedAt`` is
  newer than the stored per-row timestamp are re-emitted. (Smartsheet has no
  server-side ``modifiedSince`` filter on these list endpoints, so the
  timestamps are compared client-side; the result is the same.) The cursors
  live in dlt's per-resource state, so re-running ``remember`` resumes where
  it left off and re-embeds only the delta.
* **Forget-on-delete** — each run sweeps every successfully fetched sheet's
  row ids: rows that vanished (deleted upstream) are emitted with the
  ``_deleted`` hard-delete marker. Sheets removed from the account listing —
  or from ``sheet_ids`` when an explicit selection is given — tombstone all
  their rows. A sheet that fails to fetch is skipped for the run; its rows
  are never tombstoned on unseen evidence.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows
   from the dlt destination (default 50). For real sheets pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.

.. note::
   Attachment *bodies* are inlined only for ``text/plain`` and ``text/csv``
   attachments up to ``max_attachment_bytes``; other file types (Office, PDF,
   images) are recorded by name, type, and size. Discussions and attachments
   are fetched per emitted (new or changed) row only.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("smartsheet_connector")

# dlt resource / staging-table name, and the system_metadata["source"] tag
# stamped on every document this connector produces.
SMARTSHEET_SOURCE_NAME = "smartsheet"
SMARTSHEET_TABLE_NAME = "smartsheet_rows"

_API_BASE = "https://api.smartsheet.com/2.0"

_PAGE_SIZE = 100

# Attachment MIME types whose body is inlined into the row document, and the
# per-attachment cap (bodies are embedded in graph documents; anything larger
# would blow the chunking budget).
_INLINE_ATTACHMENT_TYPES = ("text/plain", "text/csv", "text/tab-separated-values")
_MAX_ATTACHMENT_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(token: str) -> Any:
    """Build a ``requests`` session authenticated with a Smartsheet API token.

    ``requests`` is imported lazily so it stays an optional dependency.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(
            'The Smartsheet connector requires "requests". Install the connector:\n'
            '    pip install "cognee-community-connector-smartsheet"'
        ) from exc

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    return session


def _api_get(session: Any, path: str, params: dict | None = None) -> Any:
    """GET a Smartsheet API path and return the decoded JSON."""
    response = session.get(f"{_API_BASE}{path}", params=params or {})
    response.raise_for_status()
    return response.json()


def _list_sheets(session: Any) -> list[dict]:
    """List every sheet the token can see, across the account listing's pages."""
    sheets: list[dict] = []
    page = 1
    while True:
        data = _api_get(session, "/users/me/sheets", {"pageSize": _PAGE_SIZE, "page": page})
        batch = data.get("data") or []
        sheets.extend(item for item in batch if isinstance(item, dict) and item.get("id"))
        total_pages = data.get("totalPages") or 1
        if page >= total_pages or not batch:
            break
        page += 1
    return sheets


def _fetch_sheet_rows(session: Any, sheet_id: str) -> tuple[dict, list[dict], dict[str, str]]:
    """Fetch a sheet with all rows across pages and its column-title map.

    Returns ``(primary_column_ids, rows, column_titles)``. Get-sheet pages by
    ``pageSize``/``page``; the response carries ``totalRowCount`` and
    ``totalPages`` when paged.
    """
    columns: dict[str, str] = {}
    primary_ids: set[str] = set()
    rows: list[dict] = []
    page = 1
    while True:
        data = _api_get(session, f"/sheets/{sheet_id}", {"pageSize": _PAGE_SIZE, "page": page})
        for column in data.get("columns") or []:
            if column.get("id") is not None:
                columns[str(column["id"])] = column.get("title") or ""
                if column.get("primary"):
                    primary_ids.add(str(column["id"]))
        batch = [row for row in data.get("rows") or [] if isinstance(row, dict) and row.get("id")]
        rows.extend(batch)
        total_pages = data.get("totalPages") or 1
        if page >= total_pages or not batch:
            break
        page += 1
    return primary_ids, rows, columns


# ---------------------------------------------------------------------------
# Document rendering
# ---------------------------------------------------------------------------
def _cell_text(cell: dict) -> str:
    """A cell's display text: ``displayValue`` when present, else the raw value."""
    if cell.get("displayValue") is not None:
        return str(cell["displayValue"])
    value = cell.get("value")
    return "" if value is None else str(value)


def _row_title(row: dict, primary_ids: set[str], columns: dict[str, str]) -> str:
    """The row's primary-column value — Smartsheet's own notion of a row title."""
    for cell in row.get("cells") or []:
        if str(cell.get("columnId")) in primary_ids:
            text = _cell_text(cell).strip()
            if text:
                return text
    return ""


def _row_content(
    sheet_name: str,
    row: dict,
    columns: dict[str, str],
    comments: list[dict],
    attachments: list[dict],
) -> str:
    """Render a row as a readable document: cells, then comments, then attachments."""
    lines = [f"Sheet: {sheet_name}"]
    lines.append("")
    for cell in row.get("cells") or []:
        column_title = columns.get(str(cell.get("columnId")))
        text = _cell_text(cell)
        if column_title and text:
            lines.append(f"{column_title}: {text}")
    if comments:
        lines.append("")
        lines.append("## Comments")
        for comment in comments:
            author = ((comment.get("createdBy") or {}).get("email")) or "unknown"
            text = (comment.get("text") or "").strip()
            if text:
                lines.append(f"- **{author}**: {text}")
    if attachments:
        lines.append("")
        lines.append("## Attachments")
        for attachment in attachments:
            size = attachment.get("sizeInKB")
            size_note = f", {size} KB" if size is not None else ""
            lines.append(
                f"- {attachment.get('name') or 'attachment'}"
                f" ({attachment.get('mimeType') or 'unknown'}{size_note})"
            )
            body = attachment.get("_text_body")
            if body:
                lines.append("")
                lines.append(body)
    return "\n".join(lines).strip()


def _row_row(row_id: str, title: str, content: str, url: str) -> dict[str, Any]:
    return {"id": row_id, "title": title, "content": content, "url": url, "_deleted": False}


def _deleted_row(row_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a row document."""
    return {"id": str(row_id), "_deleted": True}


# ---------------------------------------------------------------------------
# Per-row details (discussions + attachments), fetched for emitted rows only
# ---------------------------------------------------------------------------
def _fetch_row_comments(session: Any, sheet_id: str, row_id: str) -> list[dict]:
    """A row's discussion comments. A failure here degrades to no comments."""
    try:
        discussions = _api_get(
            session, f"/sheets/{sheet_id}/rows/{row_id}/discussions", {"include": "comments"}
        )
    except Exception as exc:
        logger.warning("Smartsheet: skipping discussions of row %s: %s", row_id, exc)
        return []
    comments: list[dict] = []
    for discussion in discussions or []:
        if isinstance(discussion, dict):
            comments.extend(
                comment for comment in discussion.get("comments") or [] if isinstance(comment, dict)
            )
    return comments


def _fetch_row_attachments(session: Any, sheet_id: str, row_id: str) -> list[dict]:
    """A row's attachments; text/plain / text/csv bodies are inlined (capped).

    A failure here degrades to no attachments — the row document still
    carries the cells.
    """
    try:
        attachments = _api_get(session, f"/sheets/{sheet_id}/rows/{row_id}/attachments")
    except Exception as exc:
        logger.warning("Smartsheet: skipping attachments of row %s: %s", row_id, exc)
        return []

    enriched: list[dict] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict) or not attachment.get("id"):
            continue
        entry = dict(attachment)
        mime = (attachment.get("mimeType") or "").lower()
        if mime in _INLINE_ATTACHMENT_TYPES and attachment.get("url"):
            try:
                body = session.get(attachment["url"])
                body.raise_for_status()
                text = body.text
                if len(text.encode("utf-8")) <= _MAX_ATTACHMENT_BYTES:
                    entry["_text_body"] = text
            except Exception as exc:
                logger.warning(
                    "Smartsheet: skipping body of attachment %s: %s", attachment["id"], exc
                )
        enriched.append(entry)
    return enriched


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_sheets(
    session: Any,
    state: dict,
    *,
    sheet_ids: list[str] | None = None,
    include_comments: bool = True,
    include_attachments: bool = True,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield new/changed row documents plus hard-delete markers.

    The account listing's per-sheet ``modifiedAt`` skips unchanged sheets
    without fetching rows; a changed sheet is swept in full and only rows
    with a newer ``modifiedAt`` (or never seen) are re-emitted. The row-id
    sweep drives deletion detection. All state is advanced in ``state`` so
    the next run is a no-op when nothing changed. A sheet whose fetch fails
    is skipped for the run — its rows are neither emitted nor tombstoned on
    that evidence.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(scanned_sheets=0, skipped_sheets=0, unchanged_sheets=0, emitted=0, deleted=0)

    known_sheets: dict[str, dict] = dict(state.get("sheets", {}))
    known_rows: dict[str, dict] = dict(state.get("rows", {}))

    try:
        listing = _list_sheets(session)
    except Exception as exc:
        logger.warning("Smartsheet: skipping sync (sheet listing failed): %s", exc)
        state.update(sheets=known_sheets, rows=known_rows)
        return

    visible = {str(sheet["id"]): sheet for sheet in listing}
    wanted = [str(sid) for sid in sheet_ids] if sheet_ids else sorted(visible)
    synced: set[str] = set()
    skipped: set[str] = set()

    for sheet_id in wanted:
        sheet_meta = visible.get(sheet_id)
        if sheet_meta is None:
            if sheet_id in known_sheets:
                # Explicitly selected but no longer visible to the token:
                # deleted upstream or de-shared. Forget its rows.
                logger.warning(
                    "Smartsheet: sheet %s is no longer visible; forgetting its rows.", sheet_id
                )
                for row_id, meta in list(known_rows.items()):
                    if meta.get("sheet") == sheet_id:
                        known_rows.pop(row_id, None)
                        stats["deleted"] += 1
                        yield _deleted_row(row_id)
                known_sheets.pop(sheet_id, None)
            else:
                logger.warning("Smartsheet: sheet %s not found, skipping.", sheet_id)
            continue

        previous_modified = known_sheets.get(sheet_id, {}).get("modified_at") or ""
        if previous_modified and (sheet_meta.get("modifiedAt") or "") <= previous_modified:
            stats["unchanged_sheets"] += 1
            synced.add(sheet_id)
            continue

        try:
            primary_ids, rows, columns = _fetch_sheet_rows(session, sheet_id)
        except Exception as exc:
            stats["skipped_sheets"] += 1
            skipped.add(sheet_id)
            logger.warning("Smartsheet: skipping sheet %s (fetch failed): %s", sheet_id, exc)
            continue

        synced.add(sheet_id)
        sheet_rows = {
            row_id: meta for row_id, meta in known_rows.items() if meta.get("sheet") == sheet_id
        }
        present_ids: set[str] = set()
        sheet_name = sheet_meta.get("name") or sheet_id
        permalink = sheet_meta.get("permalink") or ""
        for row in rows:
            row_id = str(row["id"])
            present_ids.add(row_id)
            row_modified = row.get("modifiedAt") or ""
            previous = known_rows.get(row_id)
            if (
                previous is not None
                and row_modified
                and row_modified <= previous.get("modified_at", "")
            ):
                continue  # unchanged since the last run

            comments = _fetch_row_comments(session, sheet_id, row_id) if include_comments else []
            attachments = (
                _fetch_row_attachments(session, sheet_id, row_id) if include_attachments else []
            )
            content = _row_content(sheet_name, row, columns, comments, attachments)
            title = _row_title(row, primary_ids, columns) or f"Row {row_id}"
            stats["emitted"] += 1
            yield _row_row(row_id, f"{sheet_name}: {title}"[:120], content, permalink)
            known_rows[row_id] = {"modified_at": row_modified, "sheet": sheet_id}

        # Deletion detection: a known row absent from the full row sweep is
        # gone upstream — emit the hard-delete marker so dlt's merge drops it
        # and cognee's orphan_cleanup forgets it from memory.
        for row_id in sorted(set(sheet_rows) - present_ids):
            known_rows.pop(row_id, None)
            stats["deleted"] += 1
            yield _deleted_row(row_id)

        known_sheets[sheet_id] = {"modified_at": sheet_meta.get("modifiedAt") or ""}
        stats["scanned_sheets"] += 1

    # Sheets dropped from an explicit selection (or vanished from the listing
    # while not skipped for a transient failure): their rows are no longer
    # wanted.
    dropped = {
        sheet_id
        for sheet_id in set(known_sheets) | {m.get("sheet") for m in known_rows.values()}
        if sheet_id not in synced and sheet_id not in skipped and sheet_id not in visible
    }
    for row_id, meta in list(known_rows.items()):
        if meta.get("sheet") in dropped:
            known_rows.pop(row_id, None)
            stats["deleted"] += 1
            yield _deleted_row(row_id)
    known_sheets = {sid: s for sid, s in known_sheets.items() if sid not in dropped}

    state["sheets"] = known_sheets
    state["rows"] = known_rows
    logger.info(
        "Smartsheet: %d sheet(s) scanned, %d row(s) emitted, %d deletion(s), %d sheet(s) skipped, "
        "%d unchanged, %d sheet(s) failed.",
        stats["scanned_sheets"],
        stats["emitted"],
        stats["deleted"],
        stats["skipped_sheets"],
        stats["unchanged_sheets"],
        len(skipped),
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def smartsheet_source(
    *,
    token: str | None = None,
    sheet_ids: list[str] | None = None,
    include_comments: bool = True,
    include_attachments: bool = True,
    resource_name: str = SMARTSHEET_TABLE_NAME,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields Smartsheet row documents for ``remember``.

    Args:
        token: Smartsheet API access token. Falls back to ``SMARTSHEET_TOKEN``.
        sheet_ids: Restrict to these sheet ids (from the sheet URL). ``None``
            syncs every sheet the token can see.
        include_comments: Fold row discussions into the row documents.
        include_attachments: Record row attachments; text/plain and text/csv
            bodies up to ``max_attachment_bytes`` are inlined.
        resource_name: Stable dlt resource name. dlt state is keyed per
            resource name, so hosts syncing several Smartsheet accounts into
            one dataset should give each its own name.
        session: Pre-built ``requests`` session. Mainly an injection point for
            tests; when omitted one is built from ``token``.

    Returns:
        A ``dlt`` resource (``smartsheet_rows``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The Smartsheet connector requires dlt. Install it with the connector:\n"
            '    pip install "cognee-community-connector-smartsheet"'
        ) from exc

    token = token or os.environ.get("SMARTSHEET_TOKEN")
    if session is None and not token:
        raise ValueError("smartsheet_source requires token (or an injected session).")

    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker (matching gmail/confluence):
        # rows where it is True are removed from the dlt destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def smartsheet_rows():
        client = session or _make_session(token)
        resource_state = dlt.current.resource_state()
        yield from sync_sheets(
            client,
            resource_state,
            sheet_ids=sheet_ids,
            include_comments=include_comments,
            include_attachments=include_attachments,
            stats=stats,
        )

    resource = smartsheet_rows()
    # Opt into the document ingestion path: each row document (id/title/
    # content/url) becomes a text document that flows through normal cognify
    # (LLM graph extraction). resolve_dlt_sources reads this marker; it never
    # imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, SMARTSHEET_SOURCE_NAME)
    # Host-readable diagnostics contain counts only, never sheet or row content.
    resource.cognee_sync_stats = stats
    return resource
