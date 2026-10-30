"""ServiceNow connector for cognee: a ``dlt`` source that turns IT history into memory.

One document per knowledge article, incident, problem and change request (and,
opt-in, their work notes and comments), synced incrementally with forget-on-delete.
Hand the source to :func:`cognee.remember` with ``write_disposition="merge"``::

    await cognee.remember(
        servicenow_source(
            instance_url="https://dev12345.service-now.com",
            username="cognee.integration",
            password=os.environ["SERVICENOW_PASSWORD"],
        ),
        dataset_name="servicenow",
        write_disposition="merge",
    )

The README describes what is stored, how changes and deletions are found, and what a
run does when ServiceNow fails part of the way through.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("servicenow_connector")

SERVICENOW_SOURCE_NAME = "servicenow"

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
_MAX_PAGES = 100_000
_FETCH_BATCH_SIZE = 100
_MAX_ATTEMPTS = 3
_MAX_RETRY_DELAY_SECONDS = 60.0
# Retries sleep inside cognee's staging lock, which every other dlt ingestion in
# the process waits on, so a run waits at most this long in total.
_WAIT_BUDGET_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 60

_STAT_KEYS = (
    "scanned",
    "skipped",
    "changed",
    "deleted",
    "failed",
    "failed_auth",
    "failed_rate_limit",
    "requests",
    "complete",
)

_TOMBSTONE_COLUMNS = {"_deleted": {"data_type": "bool", "hard_delete": True}}
# C0/C1 control characters (tab and newline kept) and lone surrogates: a NUL makes
# Postgres reject the load and a lone surrogate fails the dlt extract.
_UNSAFE_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")

_BLOCK_END_RE = re.compile(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|blockquote|pre)>")
_TAG_RE = re.compile(r"<[^>]+>")
# Non-breaking spaces (from &nbsp;) are folded into ordinary spaces too.
_SPACES_RE = re.compile(r"[ \t\r\f\v\xa0]+")


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def servicenow_source(
    instance_url: str,
    username: str | None = None,
    password: str | None = None,
    tables: Mapping[str, str] | None = None,
    include_journal: bool = False,
    service: Any = None,
    *,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    session: Any = None,
):
    """Create a dlt source that yields ServiceNow records as documents.

    Args:
        instance_url: e.g. ``https://dev12345.service-now.com``.
        username: Read-only integration user. Required unless ``service`` is given.
        password: Its password.
        tables: ``{table: encoded_query}`` to sync; ``""`` means every record.
            Defaults to :data:`DEFAULT_TABLES`. Supported tables: ``kb_knowledge``,
            ``incident``, ``problem``, ``change_request``.
        include_journal: Append work notes and comments to each record.
        service: Pre-built ``requests``-compatible session (tests, proxies). It is
            used as is and not closed; when omitted, each run opens and closes its own
            from the credentials above.
        resource_name: dlt resource (table) name, which also scopes the sync state.
            Defaults to ``servicenow_<host>`` from ``instance_url``, so two instances
            synced into one dataset keep apart.
        check_active: Optional host checkpoint, called before every request and before
            every row is published; raising from it stops the run.
        session: Alias of ``service``. Give at most one.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``. After a
        run, ``source.cognee_sync_stats`` holds counts only: ``scanned`` (records
        listed), ``skipped`` (unchanged), ``changed``, ``deleted``, ``failed``
        (listing rows without a sys_id), ``failed_auth`` and ``failed_rate_limit``
        (1 when the run stopped for that reason), ``requests``, and ``complete`` (1
        once the run finished).
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "The ServiceNow connector needs cognee's document sync cleanup (cognee>=1.6.3)."
        )
    base_url = str(instance_url or "").strip().rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ValueError(
            "instance_url must be the instance URL, e.g. https://dev12345.service-now.com."
        )
    if tables is not None and not isinstance(tables, Mapping):
        raise ValueError('tables must be a dict of {table: encoded_query}, e.g. {"incident": ""}.')
    scope = dict(DEFAULT_TABLES if tables is None else tables)
    unsupported = sorted(set(scope) - set(_TABLE_FIELDS))
    if unsupported:
        raise ValueError(f"Unsupported tables {unsupported}; choose from {sorted(_TABLE_FIELDS)}.")
    if not all(isinstance(query, str) for query in scope.values()):
        raise ValueError('Each table maps to an encoded query string ("" for every record).')
    name = resource_name if resource_name is not None else _default_resource_name(base_url)
    _check_resource_name(name)
    if service is not None and session is not None:
        raise ValueError("Pass service= or session=, not both.")
    client = service if service is not None else session
    # A value pasted from a password manager or a file often ends in a newline.
    user = (username or "").strip()
    secret = (password or "").rstrip("\r\n")
    if client is None and not (user and secret):
        raise ValueError("username and password are required (a read-only integration user).")
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns=_TOMBSTONE_COLUMNS,
    )
    def servicenow_records():
        http = client if client is not None else _make_session(user, secret)
        try:
            api = ServiceNowAPI(http, base_url, check_active=check_active, stats=stats)
            state = dlt.current.resource_state()
            yield from sync_records(
                api, scope, include_journal, state, stats=stats, check_active=check_active
            )
        finally:
            if client is None:
                http.close()

    @dlt.source(name=SERVICENOW_SOURCE_NAME)
    def _servicenow():
        return servicenow_records

    source = _servicenow()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, SERVICENOW_SOURCE_NAME)
    # The table name also scopes the dlt state (known records and their versions),
    # so the state and the table it describes always resolve to the same pipeline.
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    source.cognee_sync_stats = stats
    return source


def _default_resource_name(base_url: str) -> str:
    """``servicenow_<host>`` (with port and path), e.g. ``servicenow_dev12345_service_now_com``."""
    parts = urlsplit(base_url)
    raw = f"{parts.hostname or ''}_{parts.port or ''}{parts.path}".lower()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return f"{SERVICENOW_SOURCE_NAME}_{slug}"[:64].rstrip("_")


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: two names dlt normalizes alike
    ("ServiceNow-Prod", "servicenow_prod") would share a table.
    """
    if not isinstance(name, str) or not name or len(name) > 64:
        raise ValueError("resource_name must be a non-empty string of at most 64 characters.")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _).")


class ServiceNowSyncError(RuntimeError):
    """The run cannot be trusted (stuck paging, an emptied table, a bad response) and stops."""


def _make_session(username: str, password: str) -> Any:
    session = requests.Session()
    session.auth = (username, password)
    session.headers.update({"Accept": "application/json"})
    return session


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class ServiceNowAPI:
    """Thin read-only wrapper over the Table API: retries and offset paging."""

    def __init__(
        self,
        session: Any,
        base_url: str,
        check_active: Callable[[], None] | None = None,
        stats: dict | None = None,
    ):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.check_active = check_active
        self.stats = stats if stats is not None else {}
        self.waited = 0.0  # seconds slept on retries this run

    def request(self, path: str, params: dict) -> dict:
        """GET a Table API path, retrying 429/5xx and network errors within the wait budget."""
        url = f"{self.base_url}{path}"
        for attempt in range(_MAX_ATTEMPTS):
            if self.check_active is not None:
                self.check_active()
            self.stats["requests"] = self.stats.get("requests", 0) + 1
            failure: Exception | None = None
            try:
                response = self.session.request(
                    "GET", url, params=params, timeout=_REQUEST_TIMEOUT_SECONDS
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                failure, reason = exc, type(exc).__name__
                delay = min(2.0**attempt, _MAX_RETRY_DELAY_SECONDS)
            else:
                status = response.status_code
                if status != 429 and status < 500:
                    if status in (401, 403):
                        self.stats["failed_auth"] = 1
                    return _parse(response, path)
                reason, delay = f"HTTP {status}", _retry_delay(response.headers, attempt)
            if attempt == _MAX_ATTEMPTS - 1 or self.waited + delay > _WAIT_BUDGET_SECONDS:
                break
            logger.warning("ServiceNow: %s on %s; retrying in %.1fs.", reason, path, delay)
            self.waited += delay
            time.sleep(delay)
        if failure is not None:
            raise failure
        if response.status_code == 429:
            self.stats["failed_rate_limit"] = 1
            raise ServiceNowSyncError(
                f"ServiceNow is rate-limiting {path} (HTTP 429). The run stopped before "
                "changing anything; the next run starts over."
            )
        raise ServiceNowSyncError(
            f"ServiceNow failed {path} with HTTP {response.status_code}. The run stopped "
            "before changing anything; the next run starts over."
        )

    def table(self, table: str, params: dict, page_size: int = _LIST_PAGE_SIZE) -> Iterator[dict]:
        """Yield every record of ``/api/now/table/{table}`` across offset pages."""
        offset = 0
        previous_first = None
        for _ in range(_MAX_PAGES):
            page = self.request(
                f"/api/now/table/{table}",
                {**params, "sysparm_limit": page_size, "sysparm_offset": offset},
            )
            records = page.get("result") or []
            first = records[0].get("sys_id") if records else None
            if first is not None and first == previous_first:
                # An instance that ignores the offset would page forever; a listing
                # that cannot move forward must not look finished.
                raise ServiceNowSyncError(f"ServiceNow returned the same page of {table} twice.")
            previous_first = first
            yield from records
            if len(records) < page_size:
                return
            offset += page_size
        raise ServiceNowSyncError(f"ServiceNow listing of {table} exceeded {_MAX_PAGES} pages.")


def _parse(response: Any, path: str) -> dict:
    """Raise on a failed response with a hint for the common causes; else decode JSON."""
    if response.status_code == 401:
        raise requests.HTTPError(
            "ServiceNow rejected the credentials (HTTP 401). Check the username and "
            "password; on newer instances the integration user also needs the "
            "snc_basic_auth_api_access role and Internal Integration User ticked.",
            response=response,
        )
    if response.status_code == 403:
        raise requests.HTTPError(
            f"ServiceNow refused {path} (HTTP 403). The integration user needs read access "
            "to this table (for example the itil role for incidents, or knowledge for "
            "articles).",
            response=response,
        )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        raise ServiceNowSyncError(
            f"ServiceNow answered {path} with a web page instead of JSON (HTTP "
            f"{response.status_code}). A Personal Developer Instance does this while it is "
            "hibernating: wake it at developer.servicenow.com and run the sync again."
        ) from None


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
    stats: dict | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield new/changed records of every scoped table, then tombstones for vanished ones."""
    if stats is None:
        stats = api.stats
    api.stats = stats  # requests are counted in the same dictionary
    stats.clear()
    stats.update(dict.fromkeys(_STAT_KEYS, 0))

    tables_state = state.setdefault("tables", {})
    queries = state.setdefault("queries", {})
    known_before = sum(len(tables_state.get(table, {})) for table in scope)
    same_selection = all(queries.get(table) == query for table, query in scope.items())
    for table in sorted(set(tables_state) - set(scope)):
        # The table was removed from the configuration: forget what it synced.
        queries.pop(table, None)
        for sys_id in sorted(tables_state.pop(table)):
            stats["deleted"] += 1
            yield _checked({"id": _record_id(table, sys_id), "_deleted": True}, check_active)
    for table, query in scope.items():
        yield from _sync_table(
            api,
            table,
            query,
            include_journal,
            tables_state.setdefault(table, {}),
            stats=stats,
            check_active=check_active,
        )
        queries[table] = query
    if stats["scanned"] == 0 and known_before and same_selection:
        # ACLs hide rows silently: a user who lost a role or whose credentials changed
        # gets empty 200s, not errors. Every table empty with the same queries as last
        # time is far likelier that than an emptied instance, so fail the run: dlt keeps
        # nothing from a failed run, so the tombstones above are discarded too.
        raise ServiceNowSyncError(
            f"ServiceNow listed no records in any table for the same queries as the last "
            f"run, which synced {known_before}. Refusing to forget them; check the "
            "integration user's roles. If the tables really are empty now, remove them "
            "from tables= to forget their records."
        )
    stats["complete"] = 1


def _sync_table(
    api: ServiceNowAPI,
    table: str,
    query: str,
    include_journal: bool,
    known: dict,
    stats: dict | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    stats = {} if stats is None else stats
    # 1. Cheap listing: every in-scope sys_id with its version. sys_updated_on (raw,
    # UTC) has one-second precision, so sys_mod_count, which every update bumps,
    # catches a second edit within the same second as the last sync.
    listing_query = f"{query}^ORDERBYsys_id" if query else "ORDERBYsys_id"
    current = {}
    for record in api.table(
        table,
        {
            "sysparm_query": listing_query,
            "sysparm_fields": "sys_id,sys_updated_on,sys_mod_count",
        },
    ):
        if not record.get("sys_id"):
            stats["failed"] = stats.get("failed", 0) + 1
            continue
        current[record["sys_id"]] = (
            f"{record.get('sys_updated_on', '')}/{record.get('sys_mod_count', '')}"
        )
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
            row = record_to_row(api.base_url, table, record, journal.get(record["sys_id"], []))
            yield _checked(row, check_active)

    # 3. Anything known before but no longer listed is gone (or out of scope).
    deleted = sorted(set(known) - set(current))
    for sys_id in deleted:
        yield _checked({"id": _record_id(table, sys_id), "_deleted": True}, check_active)

    known.clear()
    known.update(current)
    for key, value in (
        ("scanned", len(current)),
        ("skipped", len(current) - len(changed)),
        ("changed", len(changed)),
        ("deleted", len(deleted)),
    ):
        stats[key] = stats.get(key, 0) + value
    logger.info(
        "ServiceNow: %s — %d changed record(s), %d deletion(s).", table, len(changed), len(deleted)
    )


def _checked(row: dict, check_active: Callable[[], None] | None) -> dict:
    """Ask the host checkpoint before a row is published."""
    if check_active is not None:
        check_active()
    return row


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


def _clean(text: str) -> str:
    return _UNSAFE_CHARS.sub("", text)


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
        "title": _clean(f"{number}: {summary}" if summary else number),
        "content": _clean("\n".join(lines)),
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
