"""Airtable data-source connector for cognee.

Syncs Airtable base records (plus their comments and the table's field schema)
into cognee memory, incrementally and with forget-on-deletion — "ask my base".

Design, mirroring the Confluence / Google Drive connectors:

* **Primary key** — the Airtable record ``id``.  With
  ``write_disposition="merge"`` this gives idempotent upserts.
* **Incremental cursor** — the record's configured modified-time field
  (``lastModifiedTime`` by default).  Airtable does not expose a modified time on
  the record itself, so the table must carry a field with that value; see the
  README for the one-time setup.  The cursor is persisted in dlt's per-resource
  state, so re-running ``remember`` only re-ingests records that appeared or
  changed since the previous run.
* **Forget-on-delete** — a single listing sweep enumerates the *current* records
  and that set drives deletion detection; records that vanished upstream are
  emitted once as ``_deleted=True`` hard-delete markers, which dlt removes from
  the destination and cognee's ``orphan_cleanup`` then forgets from the graph and
  vector stores.

Only records that are new or newer than the stored cursor are emitted (and only
those have their comments fetched), so an unchanged base is a no-op.
"""

import json
import time
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger

logger = get_logger("airtable_connector")

# dlt resource name and the default modified-time field.
AIRTABLE_TABLE_NAME = "airtable_records"
DEFAULT_MODIFIED_FIELD = "lastModifiedTime"

_API_BASE = "https://api.airtable.com/v0"
_META_BASE = "https://api.airtable.com/v0/meta"
# Airtable caps pageSize at 100; asking for more is an error rather than a clamp.
_PAGE_SIZE = 100
# Retry budget for rate-limited / transient Airtable responses (5 req/s per base).
_MAX_RETRIES = 5

_EXTRA_HINT = (
    'The Airtable connector requires the "airtable" extra: pip install "cognee[airtable]" '
    "(provides dlt and requests)."
)


def airtable_source(
    base_id: str | None = None,
    table_ids: list[str] | None = None,
    *,
    token: str | None = None,
    modified_field: str = DEFAULT_MODIFIED_FIELD,
    include_comments: bool = True,
    include_schema: bool = True,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields Airtable records for ``remember``.

    Args:
        base_id: Airtable base id (``app...``). Falls back to ``AIRTABLE_BASE_ID``.
        table_ids: Restrict ingestion to these table ids or names. ``None`` syncs
            every table in the base.
        token: Airtable personal access token. Falls back to ``AIRTABLE_API_KEY``.
        modified_field: Field used as the incremental cursor. Requires a "Last
            modified time" field on each synced table (see README).
        include_comments: Fold each record's comments into its text.
        include_schema: Attach the table's field schema to each row.
        session: Pre-built ``requests`` session. Mainly an injection point for
            tests; when omitted one is built from ``token``.

    Returns:
        A ``dlt`` resource (``airtable_records``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    import os

    resolved_base = base_id or os.environ.get("AIRTABLE_BASE_ID")
    if session is None and not (token or os.environ.get("AIRTABLE_API_KEY")):
        raise ValueError(
            "Airtable personal access token required: pass token= or set AIRTABLE_API_KEY."
        )
    if not resolved_base:
        raise ValueError("Airtable base id required: pass base_id= or set AIRTABLE_BASE_ID.")

    @dlt.resource(
        name=AIRTABLE_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def airtable_records():
        client = session or _make_session(token)
        resource_state = dlt.current.resource_state()
        yield from sync_records(
            client,
            resolved_base,
            resource_state,
            table_ids=table_ids,
            modified_field=modified_field,
            include_comments=include_comments,
            include_schema=include_schema,
        )

    return airtable_records


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_records(
    session: Any,
    base_id: str,
    state: dict,
    *,
    table_ids: list[str] | None = None,
    modified_field: str = DEFAULT_MODIFIED_FIELD,
    include_comments: bool = True,
    include_schema: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield changed Airtable records since the last run, plus hard-delete markers.

    One listing sweep per table enumerates the *current* records (fields
    included): that set drives deletion detection, while records that are new or
    newer than the stored cursor have their comments fetched and are emitted.
    The cursor (``last_modified``) and the id set (``known_ids``) are advanced in
    ``state`` so the next run is a no-op when nothing changed.
    """
    known_ids: set[str] = set(state.get("known_ids", []))
    last_modified: str = state.get("last_modified", "")
    newest_modified = last_modified
    current_ids: set[str] = set()
    changed = 0

    # The meta API does double duty: it is the only way to enumerate the base's
    # tables, and it supplies the field schema. Fetch it when either is needed.
    schemas = _table_schemas(session, base_id) if (include_schema or not table_ids) else {}

    for table in _resolve_tables(session, base_id, table_ids, schemas):
        for record in _paginate_records(session, base_id, table["id"]):
            record_id = str(record.get("id") or "")
            if not record_id:
                continue
            current_ids.add(record_id)

            modified = _modified_at(record, modified_field)
            if not modified:
                # No usable cursor value: re-ingest rather than silently skip, so
                # an unconfigured field can never hide content changes. The README
                # asks for a populated "Last modified time" field.
                logger.warning(
                    "Airtable: record %s has no %r value; re-ingesting it every run.",
                    record_id,
                    modified_field,
                )
            # Skip only records we already ingested that have not changed. A
            # record absent from known_ids is emitted regardless of timestamp, so
            # records that are new to the corpus but carry an old modified time
            # (restored, moved between tables, or tied at the cursor boundary)
            # are not lost.
            elif record_id in known_ids and modified <= last_modified:
                continue
            if modified > newest_modified:
                newest_modified = modified

            comments = _record_comments(session, base_id, record_id) if include_comments else []
            yield _record_to_row(
                record,
                base_id=base_id,
                table_id=table["id"],
                comments=comments,
                modified=modified,
                schema=schemas.get(table["id"]) if include_schema else None,
            )
            changed += 1

    # Deletion detection relies on the sweep enumerating every current record. An
    # empty sweep while records were previously known almost always means a
    # transient/failed listing (network blip, revoked token mid-run, momentary
    # empty page) rather than a genuine wipe — treating it as "all deleted" would
    # purge the whole dataset and overwrite known_ids with [], making the loss
    # permanent. Skip deletion and preserve state in that case.
    if known_ids and not current_ids:
        logger.warning(
            "Airtable: sweep returned 0 records but %d were known; skipping deletion "
            "this run to avoid a mass forget-on-delete on a transient sweep.",
            len(known_ids),
        )
        state["last_modified"] = newest_modified
        logger.info("Airtable: %d changed record(s), 0 deletion(s).", changed)
        return

    deleted = known_ids - current_ids
    for record_id in sorted(deleted):
        yield _deleted_row(record_id)

    state["known_ids"] = sorted(current_ids)
    state["last_modified"] = newest_modified
    logger.info("Airtable: %d changed record(s), %d deletion(s).", changed, len(deleted))


# ---------------------------------------------------------------------------
# HTTP helpers (module-private)
# ---------------------------------------------------------------------------
def _make_session(token: str | None):
    """Build an authenticated ``requests`` session."""
    try:
        import requests
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    import os

    resolved = token or os.environ.get("AIRTABLE_API_KEY")
    if not resolved:
        raise ValueError(
            "Airtable personal access token required: pass token= or set AIRTABLE_API_KEY."
        )
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {resolved}"})
    return session


def _request(session: Any, url: str, params: dict | None = None) -> dict:
    """GET ``url``, retrying rate-limit / transient errors.

    Airtable allows ~5 requests/second per base and returns ``429`` with a
    ``Retry-After`` header when exceeded, so a wide table would otherwise abort
    the sync. Rate-limit (429) and server (5xx) responses are retried with
    backoff; permanent errors (auth, not-found, bad formula) and exhausted
    retries propagate so the caller can decide.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            response = session.get(url, params=params)
            status = response.status_code
            if status == 429 or status >= 500:
                if attempt == _MAX_RETRIES - 1:
                    response.raise_for_status()
                delay = _retry_after(response.headers, attempt)
                logger.warning(
                    "Airtable: HTTP %d from %s — retrying in %.1fs (%d/%d).",
                    status,
                    url,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except Exception:
            if attempt == _MAX_RETRIES - 1:
                raise
            # Network-level failures (connection reset, timeout) are retried too.
            delay = float(2**attempt)
            logger.warning(
                "Airtable: request to %s failed — retrying in %.1fs (%d/%d).",
                url,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable: retry loop always returns or raises")


def _retry_after(headers, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


def _table_schemas(session: Any, base_id: str) -> dict[str, Any]:
    """Map table id -> ``{field name: field type}`` for the base.

    Best-effort: the meta API needs the ``schema.bases:read`` scope, and a token
    scoped to record data alone would otherwise fail the whole sync. A failure
    here only drops the schema column.
    """
    try:
        payload = _request(session, f"{_META_BASE}/bases/{base_id}/tables")
    except Exception as exc:
        logger.warning("Airtable: could not read the base schema (%s); continuing without it.", exc)
        return {}

    schemas: dict[str, Any] = {}
    for table in payload.get("tables") or []:
        fields = table.get("fields") or []
        schemas[str(table.get("id"))] = {
            str(field.get("name")): str(field.get("type")) for field in fields if field
        }
    return schemas


def _resolve_tables(
    session: Any, base_id: str, table_ids: list[str] | None, schemas: dict[str, Any]
) -> list[dict[str, str]]:
    """Resolve the tables to sync to ``[{"id": ..., "name": ...}]``."""
    if table_ids:
        return [{"id": str(table_id), "name": str(table_id)} for table_id in table_ids]

    # Without an explicit scope, sync every table in the base. The meta API is the
    # only way to enumerate them; if it is unavailable we cannot guess, so say so
    # rather than silently syncing nothing.
    if not schemas:
        raise ValueError(
            "Could not enumerate the base's tables (the meta API needs the "
            "schema.bases:read scope). Pass table_ids=[...] to sync specific tables."
        )
    return [{"id": table_id, "name": table_id} for table_id in schemas]


def _paginate_records(session: Any, base_id: str, table_id: str) -> Iterator[dict]:
    """Yield every record of a table across Airtable's offset pagination."""
    offset = None
    while True:
        params: dict[str, Any] = {"pageSize": _PAGE_SIZE}
        if offset:
            params["offset"] = offset
        payload = _request(session, f"{_API_BASE}/{base_id}/{table_id}", params)
        yield from payload.get("records") or []
        offset = payload.get("offset")
        # Stop on the last page, or if Airtable reports an offset we cannot use.
        if not offset:
            return


def _record_comments(session: Any, base_id: str, record_id: str) -> list[str]:
    """Return the plain texts of a record's comments (best effort, paginated)."""
    texts: list[str] = []
    offset = None
    while True:
        params: dict[str, Any] = {"pageSize": _PAGE_SIZE}
        if offset:
            params["offset"] = offset
        try:
            payload = _request(session, f"{_API_BASE}/{base_id}/{record_id}/comments", params)
        except Exception as exc:
            # Comments are an enhancement, never a reason to drop the record.
            logger.warning("Airtable: could not read comments for %s (%s).", record_id, exc)
            return texts
        for comment in payload.get("comments") or []:
            text = (comment.get("text") or "").strip()
            if text:
                texts.append(text)
        offset = payload.get("offset")
        if not offset:
            return texts


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------
def _modified_at(record: dict, modified_field: str) -> str:
    """Read the incremental cursor value off a record, as a comparable string.

    Airtable's "Last modified time" field returns ISO-8601 UTC, which sorts
    lexicographically, so a plain string compare is a valid ordering here.
    """
    value = (record.get("fields") or {}).get(modified_field)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _record_to_row(
    record: dict,
    *,
    base_id: str,
    table_id: str,
    comments: list[str],
    modified: str,
    schema: dict[str, str] | None,
) -> dict[str, Any]:
    """Flatten an Airtable record (fields, comments, schema) into a document row."""
    record_id = str(record.get("id") or "")
    fields = record.get("fields") or {}
    return {
        "id": record_id,
        "url": f"https://airtable.com/{base_id}/{table_id}/{record_id}",
        "title": _record_title(fields),
        "content": _render_content(fields, comments),
        "schema": json.dumps(schema or {}, sort_keys=True),
        "last_modified": modified,
        "_deleted": False,
    }


def _deleted_row(record_id: str) -> dict[str, Any]:
    """Hard-delete marker for a record that no longer exists upstream."""
    return {"id": record_id, "_deleted": True}


def _record_title(fields: dict) -> str:
    """Pick a human title: the first string-ish field, else the record id."""
    for value in fields.values():
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:200]
    return ""


def _render_content(fields: dict, comments: list[str]) -> str:
    """Render record fields (and comments) as markdown."""
    lines: list[str] = []
    for name, value in fields.items():
        rendered = _render_value(value)
        if rendered:
            lines.append(f"**{name}**: {rendered}")
    if comments:
        lines.append("")
        lines.append("Comments:")
        lines.extend(f"- {comment}" for comment in comments)
    return "\n".join(lines)


def _render_value(value: Any) -> str:
    """Render a single Airtable field value as text."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        # Linked records / attachments / multiple selects are lists of
        # strings or objects; render each element on its own bullet, or inline.
        rendered = [part for part in (_render_value(item) for item in value) if part]
        if len(rendered) <= 1:
            return rendered[0] if rendered else ""
        return "; ".join(rendered)
    if isinstance(value, dict):
        # Attachments carry a filename and url; collaborators carry a name.
        for key in ("name", "filename", "text", "url"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
        return json.dumps(value, sort_keys=True)
    return str(value)
