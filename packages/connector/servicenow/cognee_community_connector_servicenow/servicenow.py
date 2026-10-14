"""ServiceNow connector for cognee — a ``dlt`` source that turns IT history into memory.

Sync ServiceNow knowledge articles, incidents, problems and change requests (and,
opt-in, their work notes and comments) into cognee, incrementally and with
forget-on-delete::

    import cognee
    from cognee_community_connector_servicenow import servicenow_source

    await cognee.remember(
        servicenow_source(
            instance_url="https://dev12345.service-now.com",
            username="cognee.reader",
            password="...",
        ),
        dataset_name="servicenow",
        write_disposition="merge",   # REQUIRED (see .. important:: below)
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**. cognee applies the caller's
   write disposition to every table of the source; this source only emits
   records that changed and deletes by emitting ``_deleted`` tombstone rows, and
   both only work under ``merge``.

Design
------
* **Auth** — instance URL plus basic auth for a read-only integration user
  (``username``/``password``, or ``SERVICENOW_INSTANCE_URL``,
  ``SERVICENOW_USERNAME``, ``SERVICENOW_PASSWORD``). The connector only reads,
  through the Table API.
* **What is synced** — published ``kb_knowledge`` articles plus ``incident``,
  ``problem`` and ``change_request`` records, one document per record. Each table
  can be narrowed with an encoded query (e.g. ``{"incident": "active=true"}``).
  Reference fields are rendered by their display values (names, not sys_ids).
  Work notes and comments are opt-in (``include_journal=True``) because they
  often contain customer details.
* **Incremental sync without date queries** — each run lists every in-scope
  record's ``sys_id`` and raw ``sys_updated_on`` (two fields, UTC) and compares
  them with the previous run, kept in dlt's per-resource state. Only new or
  changed records are then fetched in full, in batches. This avoids encoded
  date filters, whose ``gs.dateGenerate`` values are interpreted in the
  integration user's time zone. Adding a work note or comment updates the parent
  record's ``sys_updated_on``, so journal changes are picked up too.
* **Forget-on-delete** — a record missing from the listing (deleted, or moved out
  of scope, such as a retired knowledge article or a record no longer matching
  the table's query) is emitted with the ``_deleted`` hard-delete marker. dlt
  drops it on ``merge`` and cognee's orphan cleanup removes it from the graph and
  vector stores. A table removed from ``tables`` has all its records forgotten.
  Any failed request aborts the run, so a broken listing never looks like a
  deletion.

Limitations
-----------
* The listing reads two fields of every in-scope record each run, and the state
  keeps one timestamp per record, so narrow very large tables with a query
  (e.g. ``active=true`` or a ``sys_created_on`` window).
* Journal entries are read from ``sys_journal_field``, which the integration user
  must be able to read when ``include_journal=True``.
"""

from __future__ import annotations

import html
import os
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

logger = get_logger("servicenow_connector")

SERVICENOW_SOURCE_NAME = "servicenow"
RECORDS_TABLE_NAME = "servicenow_records"

# Tables synced by default, with the query that defines "in scope" for each.
DEFAULT_TABLES: dict[str, str] = {
    "kb_knowledge": "workflow_state=published",
    "incident": "",
    "problem": "",
    "change_request": "",
}

# Fields rendered per table, in order: (field, label). Fields an instance does not
# have are simply absent from the response.
_TABLE_FIELDS: dict[str, list[tuple[str, str]]] = {
    "kb_knowledge": [
        ("kb_knowledge_base", "Knowledge base"),
        ("kb_category", "Category"),
        ("author", "Author"),
        ("text", "Article"),
    ],
    "incident": [
        ("state", "State"),
        ("priority", "Priority"),
        ("category", "Category"),
        ("assignment_group", "Assignment group"),
        ("assigned_to", "Assigned to"),
        ("description", "Description"),
        ("close_code", "Resolution code"),
        ("close_notes", "Resolution notes"),
    ],
    "problem": [
        ("state", "State"),
        ("priority", "Priority"),
        ("assignment_group", "Assignment group"),
        ("description", "Description"),
        ("cause_notes", "Cause"),
        ("fix_notes", "Fix"),
        ("close_notes", "Resolution notes"),
    ],
    "change_request": [
        ("type", "Type"),
        ("state", "State"),
        ("risk", "Risk"),
        ("assignment_group", "Assignment group"),
        ("description", "Description"),
        ("implementation_plan", "Implementation plan"),
        ("backout_plan", "Backout plan"),
        ("close_notes", "Close notes"),
    ],
}
_TABLE_LABELS = {
    "kb_knowledge": "knowledge article",
    "incident": "incident",
    "problem": "problem",
    "change_request": "change request",
}

_LIST_PAGE_SIZE = 1000
_FETCH_BATCH_SIZE = 100
_MAX_RETRIES = 5
_MAX_RETRY_DELAY_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 60

_EXTRA_HINT = (
    'The ServiceNow connector requires requests and dlt: pip install requests "dlt[sqlalchemy]"'
)
_TOMBSTONE_COLUMNS = {"_deleted": {"data_type": "bool", "hard_delete": True}}

_BLOCK_END_RE = re.compile(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|blockquote|pre)>")
_TAG_RE = re.compile(r"<[^>]+>")
# Non-breaking spaces (from &nbsp;) are folded into ordinary spaces too.
_SPACES_RE = re.compile(r"[ \t\r\f\v\xa0]+")


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def servicenow_source(
    instance_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    tables: Mapping[str, str] | None = None,
    include_journal: bool = False,
    session: Any = None,
):
    """Create a dlt source that yields ServiceNow records as documents.

    Args:
        instance_url: e.g. ``https://dev12345.service-now.com``. Falls back to
            ``SERVICENOW_INSTANCE_URL``.
        username: Read-only integration user. Falls back to ``SERVICENOW_USERNAME``.
        password: Its password. Falls back to ``SERVICENOW_PASSWORD``.
        tables: ``{table: encoded_query}`` to sync; ``""`` means every record.
            Defaults to :data:`DEFAULT_TABLES`. Supported tables: ``kb_knowledge``,
            ``incident``, ``problem``, ``change_request``.
        include_journal: Append work notes and comments to each record.
        session: Pre-built ``requests`` session (mainly a test-injection point);
            when omitted one is built from the credentials above.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    base_url = (instance_url or os.environ.get("SERVICENOW_INSTANCE_URL") or "").rstrip("/")
    if not base_url:
        raise ValueError(
            "ServiceNow instance URL required: pass instance_url= or set SERVICENOW_INSTANCE_URL."
        )
    scope = dict(DEFAULT_TABLES if tables is None else tables)
    unsupported = sorted(set(scope) - set(_TABLE_FIELDS))
    if unsupported:
        raise ValueError(f"Unsupported tables {unsupported}; choose from {sorted(_TABLE_FIELDS)}.")
    if session is None:
        user = username or os.environ.get("SERVICENOW_USERNAME")
        secret = password or os.environ.get("SERVICENOW_PASSWORD")
        if not (user and secret):
            raise ValueError(
                "ServiceNow credentials required: pass username= and password= or set "
                "SERVICENOW_USERNAME and SERVICENOW_PASSWORD."
            )
        session = _make_session(user, secret)
    api = ServiceNowAPI(session, base_url)

    @dlt.resource(
        name=RECORDS_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def servicenow_records():
        yield from sync_records(api, scope, include_journal, dlt.current.resource_state())

    @dlt.source(name=SERVICENOW_SOURCE_NAME)
    def _servicenow():
        return servicenow_records

    source = _servicenow()
    # Opt into the document ingestion path (row → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, SERVICENOW_SOURCE_NAME)
    # One dlt state (known records and their versions) per instance and cognee
    # dataset, so syncing other sources or datasets on the machine can't reset it.
    setattr(source, PIPELINE_SCOPE_ATTR, f"{SERVICENOW_SOURCE_NAME}:{base_url}")
    return source


def _make_session(username: str, password: str) -> Any:
    try:
        import requests
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    session = requests.Session()
    session.auth = (username, password)
    session.headers.update({"Accept": "application/json"})
    return session


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class ServiceNowAPI:
    """Thin read-only wrapper over the Table API: retries and offset paging."""

    def __init__(self, session: Any, base_url: str):
        self.session = session
        self.base_url = base_url.rstrip("/")

    def request(self, path: str, params: dict) -> dict:
        """GET a Table API path, retrying 429/5xx and network errors; others raise."""
        import requests

        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            try:
                response = self.session.request(
                    "GET", url, params=params, timeout=_REQUEST_TIMEOUT_SECONDS
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                reason, delay = str(exc), min(2.0**attempt, _MAX_RETRY_DELAY_SECONDS)
            else:
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt == _MAX_RETRIES - 1:
                    response.raise_for_status()
                    return response.json()
                reason = f"HTTP {response.status_code}"
                delay = _retry_delay(response.headers, attempt)
            logger.warning("ServiceNow: %s on %s — retrying in %.1fs.", reason, path, delay)
            time.sleep(delay)
            attempt += 1

    def table(self, table: str, params: dict, page_size: int = _LIST_PAGE_SIZE) -> Iterator[dict]:
        """Yield every record of ``/api/now/table/{table}`` across offset pages."""
        offset = 0
        while True:
            page = self.request(
                f"/api/now/table/{table}",
                {**params, "sysparm_limit": page_size, "sysparm_offset": offset},
            )
            records = page.get("result") or []
            yield from records
            if len(records) < page_size:
                return
            offset += page_size


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait: the Retry-After header, else exponential backoff."""
    retry_after = (headers or {}).get("Retry-After")
    if retry_after is not None:
        try:
            return min(float(retry_after), _MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    return min(2.0**attempt, _MAX_RETRY_DELAY_SECONDS)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def sync_records(
    api: ServiceNowAPI,
    scope: Mapping[str, str],
    include_journal: bool,
    state: dict,
) -> Iterator[dict]:
    """Yield new/changed records of every scoped table, then tombstones for vanished ones."""
    tables_state = state.setdefault("tables", {})
    for table in sorted(set(tables_state) - set(scope)):
        # The table was removed from the configuration: forget what it synced.
        for sys_id in sorted(tables_state.pop(table)):
            yield {"id": _record_id(table, sys_id), "_deleted": True}
    for table, query in scope.items():
        yield from _sync_table(
            api, table, query, include_journal, tables_state.setdefault(table, {})
        )


def _sync_table(
    api: ServiceNowAPI,
    table: str,
    query: str,
    include_journal: bool,
    known: dict,
) -> Iterator[dict]:
    # 1. Cheap listing: every in-scope sys_id with its raw (UTC) sys_updated_on.
    listing_query = f"{query}^ORDERBYsys_id" if query else "ORDERBYsys_id"
    current = {
        record["sys_id"]: record["sys_updated_on"]
        for record in api.table(
            table,
            {"sysparm_query": listing_query, "sysparm_fields": "sys_id,sys_updated_on"},
        )
    }
    changed = sorted(sys_id for sys_id, updated in current.items() if known.get(sys_id) != updated)

    # 2. Fetch only new/changed records, in batches, with display values.
    fields = ",".join(
        ["sys_id", "number", "short_description"] + [f for f, _ in _TABLE_FIELDS[table]]
    )
    for start in range(0, len(changed), _FETCH_BATCH_SIZE):
        batch = changed[start : start + _FETCH_BATCH_SIZE]
        records = list(
            api.table(
                table,
                {
                    "sysparm_query": f"sys_idIN{','.join(batch)}",
                    "sysparm_fields": fields,
                    "sysparm_display_value": "true",
                    "sysparm_exclude_reference_link": "true",
                },
            )
        )
        journal = _journal_entries(api, table, batch) if include_journal else {}
        for record in records:
            yield record_to_row(api.base_url, table, record, journal.get(record["sys_id"], []))

    # 3. Anything known before but no longer listed is gone (or out of scope).
    deleted = sorted(set(known) - set(current))
    for sys_id in deleted:
        yield {"id": _record_id(table, sys_id), "_deleted": True}

    known.clear()
    known.update(current)
    logger.info(
        "ServiceNow: %s — %d changed record(s), %d deletion(s).", table, len(changed), len(deleted)
    )


def _journal_entries(
    api: ServiceNowAPI, table: str, sys_ids: Sequence[str]
) -> dict[str, list[str]]:
    """Work notes and comments for ``sys_ids``, oldest first, as rendered lines."""
    entries: dict[str, list[str]] = {}
    for entry in api.table(
        "sys_journal_field",
        {
            "sysparm_query": (
                f"name={table}^elementINwork_notes,comments^element_idIN{','.join(sys_ids)}"
                "^ORDERBYsys_created_on"
            ),
            "sysparm_fields": "element_id,element,value,sys_created_by,sys_created_on",
        },
    ):
        kind = "Work note" if entry.get("element") == "work_notes" else "Comment"
        text = html_to_text(entry.get("value"))
        if text:
            line = (
                f"{kind} by {entry.get('sys_created_by')} on {entry.get('sys_created_on')}: {text}"
            )
            entries.setdefault(entry["element_id"], []).append(line)
    return entries


def record_to_row(base_url: str, table: str, record: Mapping[str, Any], journal: list[str]) -> dict:
    """Render one record (display values) as a document row."""
    sys_id = record["sys_id"]
    number = record.get("number") or sys_id
    summary = (record.get("short_description") or "").strip()
    lines = [f"ServiceNow {_TABLE_LABELS[table]} {number}." + (f" {summary}" if summary else "")]
    for field, label in _TABLE_FIELDS[table]:
        value = html_to_text(record.get(field))
        if value:
            lines.append(f"{label}: {value}")
    lines.extend(journal)
    return {
        "id": _record_id(table, sys_id),
        "title": f"{number}: {summary}" if summary else number,
        "content": "\n".join(lines),
        "url": f"{base_url}/nav_to.do?uri={table}.do?sys_id={sys_id}",
        "_deleted": False,
    }


def html_to_text(value: Any) -> str:
    """Render ServiceNow HTML (knowledge articles, notes) as plain text."""
    if not value:
        return ""
    text = _BLOCK_END_RE.sub("\n", str(value))
    text = html.unescape(_TAG_RE.sub("", text))
    lines = (_SPACES_RE.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _record_id(table: str, sys_id: str) -> str:
    return f"{table}:{sys_id}"
