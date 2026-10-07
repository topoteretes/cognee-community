"""DLT source for Zoho CRM (incremental sync + forget-on-delete).

Pulls Zoho CRM **records** (Leads, Contacts, Accounts, Deals, or any module you
choose), their **notes** and, optionally, **text attachments** into cognee as
documents::

    import cognee
    from cognee_community_connector_zoho_crm import zoho_crm_source

    await cognee.remember(
        zoho_crm_source(region="eu"),    # ZOHO_CLIENT_ID / _SECRET / _REFRESH_TOKEN from env
        dataset_name="zoho_crm",
        write_disposition="merge",       # REQUIRED (see .. important:: below)
    )

Rows are ingested as *normal documents*: the source declares
``cognee_document_source = "zoho_crm"``, so each record / note / attachment goes
through cognify entity extraction instead of the deterministic dlt-row path.

.. important::
   ``write_disposition="merge"`` is **mandatory**. Incremental runs only see what
   changed, so cognee's default ``"replace"`` would forget everything else.

Design (behaviour checked against a live Zoho CRM v8 account)
---------------------------------------------------------------
* **Auth (OAuth 2.0)** - a refresh token from a Zoho *Self Client* (or any
  OAuth client) is exchanged for an access token at the account's own data
  centre (``accounts.zoho.com`` / ``.eu`` / ``.in`` / ``.com.au`` / ``.jp`` /
  ``.com.cn`` / ``zohocloud.ca``). The API host is taken from the token response
  (``api_domain``), so EU / IN / AU accounts work without extra settings. Access
  tokens last one hour; a ``401`` triggers one refresh.
* **Fields** - Zoho requires ``fields`` on every list call and rejects more than
  50. The connector reads each module's field metadata, drops system, volatile
  and computed fields (``Modified_Time``, ``Last_Activity_Time``, sales-cycle
  durations, images, geo-coordinates, ...) and, by default, e-mail and phone
  fields, then asks for at most 50.
* **Incremental** - ``If-Modified-Since: <cursor>`` on every module, on
  ``Notes`` and on ``Attachments``. Zoho answers ``304`` when nothing changed.
  The filter is inclusive, so a boundary record can be read twice; ``merge`` makes
  that harmless. One cursor per module, the UTC time the last run *started*,
  kept in dlt resource state and only advanced after a fully successful run.
  Paging follows ``next_page_token`` (no 2,000-record ceiling).
* **Forget-on-delete** - Zoho's Deleted Records API (``/{module}/deleted``,
  ``type=all``: recycle bin and permanently deleted) is read on every incremental
  run; records, notes and attachments deleted since the cursor become
  ``{"id", "_deleted": True}`` tombstones. dlt removes them on ``merge`` and
  cognee's ``orphan_cleanup`` drops them from the graph.
* **Safe failure** - ``429``, ``5xx`` and network errors are retried with
  backoff; any other error aborts the run, so a partial read never moves a cursor
  or causes a false deletion.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("zoho_crm_connector")

ZOHO_SOURCE_NAME = "zoho_crm"
RECORDS_TABLE = "zoho_crm_records"
NOTES_TABLE = "zoho_crm_notes"
ATTACHMENTS_TABLE = "zoho_crm_attachments"
API_VERSION = "v8"

# Region -> accounts server. The API host comes from the token response.
ACCOUNTS_SERVERS = {
    "com": "https://accounts.zoho.com",
    "us": "https://accounts.zoho.com",
    "eu": "https://accounts.zoho.eu",
    "in": "https://accounts.zoho.in",
    "com.au": "https://accounts.zoho.com.au",
    "au": "https://accounts.zoho.com.au",
    "jp": "https://accounts.zoho.jp",
    "com.cn": "https://accounts.zoho.com.cn",
    "cn": "https://accounts.zoho.com.cn",
    "ca": "https://accounts.zohocloud.ca",
    "sa": "https://accounts.zoho.sa",
}
DEFAULT_MODULES = ("Leads", "Contacts", "Accounts", "Deals")
MAX_FIELDS = 50  # Zoho rejects more with LIMIT_EXCEEDED

_MAX_RETRIES = 5
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_DELETED_COLUMN = {"_deleted": {"data_type": "bool", "hard_delete": True}}

# Fields that change without the record's meaning changing (or are machine data);
# leaving them out keeps the content hash stable, so nothing is re-cognified.
_VOLATILE_FIELDS = frozenset(
    {
        "Modified_Time",
        "Modified_By",
        "Last_Activity_Time",
        "Change_Log_Time__s",
        "Locked__s",
        "Record_Status__s",
        "Last_Enriched_Time__s",
        "Enrich_Status__s",
        "Sales_Cycle_Duration",
        "Overall_Sales_Duration",
        "Lead_Conversion_Time",
        "Lead_Status_Modified_Time",
        "Latitude",
        "Longitude",
        "Coordinates",
        "Record_Image",
        "Unsubscribed_Mode",
        "Unsubscribed_Time",
        "Tag",
    }
)
_SKIPPED_TYPES = frozenset({"profileimage", "imageupload", "fileupload", "formula", "autonumber"})
_CONTACT_TYPES = frozenset({"email", "phone"})
_TITLE_FIELDS = (
    "Deal_Name",
    "Account_Name",
    "Full_Name",
    "Subject",
    "Name",
    "Product_Name",
    "Campaign_Name",
    "Solution_Title",
    "Vendor_Name",
    "Last_Name",
)
_TEXT_EXTENSIONS = (".txt", ".md", ".csv", ".json", ".log", ".html", ".htm", ".xml", ".tsv")

_EXTRA_HINT = (
    "The Zoho CRM connector requires dlt and httpx: pip install cognee-community-connector-zoho-crm"
)


@dataclass(frozen=True)
class _Options:
    modules: tuple[str, ...]
    redact_contact_details: bool
    modified_since: str | None
    attachment_max_bytes: int


def zoho_crm_source(
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
    region: str | None = None,
    modules: Iterable[str] | None = None,
    include_notes: bool = True,
    include_attachments: bool = False,
    attachment_max_bytes: int = 1_000_000,
    redact_contact_details: bool = True,
    modified_since: str | None = None,
    client: Any = None,
):
    """Create a dlt source with Zoho CRM records, notes and attachments.

    Args:
        client_id / client_secret / refresh_token: OAuth credentials. Fall back to
            ``ZOHO_CLIENT_ID`` / ``ZOHO_CLIENT_SECRET`` / ``ZOHO_REFRESH_TOKEN``.
        region: Data centre of the Zoho account: ``com`` (default), ``eu``,
            ``in``, ``com.au``, ``jp``, ``com.cn``, ``ca`` or ``sa``. Falls back to
            ``ZOHO_REGION``. Using the wrong one fails with ``invalid_client``.
        modules: Module API names to sync. Default: Leads, Contacts, Accounts, Deals.
        include_notes: Sync notes attached to records of those modules.
        include_attachments: Sync text-like attachments (``.txt``, ``.md``,
            ``.csv``, ...) of those modules. Off by default (downloads cost credits).
        attachment_max_bytes: Skip larger attachments.
        redact_contact_details: Leave out e-mail and phone fields (default).
        modified_since: ISO 8601 time; limits only the *first* run.
        client: ``httpx.Client``-like object (test-injection point) with
            ``get(url, params=, headers=)`` and ``post(url, params=)``.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    client_id = client_id or os.environ.get("ZOHO_CLIENT_ID")
    client_secret = client_secret or os.environ.get("ZOHO_CLIENT_SECRET")
    refresh_token = refresh_token or os.environ.get("ZOHO_REFRESH_TOKEN")
    region = (region or os.environ.get("ZOHO_REGION") or "com").lower().lstrip(".")
    if not (client_id and client_secret and refresh_token):
        raise ValueError(
            "Zoho CRM credentials required: client_id, client_secret and refresh_token "
            "(or ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN)."
        )
    if region not in ACCOUNTS_SERVERS:
        raise ValueError(f"Unknown Zoho region {region!r}. Use one of: {sorted(ACCOUNTS_SERVERS)}.")

    if client is None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc
        client = httpx.Client(timeout=60.0)

    options = _Options(
        modules=tuple(modules or DEFAULT_MODULES),
        redact_contact_details=redact_contact_details,
        modified_since=modified_since,
        attachment_max_bytes=attachment_max_bytes,
    )
    api = _Api(
        client, _Token(client, ACCOUNTS_SERVERS[region], client_id, client_secret, refresh_token)
    )

    resources = []

    @dlt.resource(
        name=RECORDS_TABLE, primary_key="id", write_disposition="merge", columns=_DELETED_COLUMN
    )
    def zoho_crm_records():
        yield from _sync_records(api, dlt.current.resource_state(), options)

    resources.append(zoho_crm_records)

    if include_notes:

        @dlt.resource(
            name=NOTES_TABLE, primary_key="id", write_disposition="merge", columns=_DELETED_COLUMN
        )
        def zoho_crm_notes():
            yield from _sync_notes(api, dlt.current.resource_state(), options)

        resources.append(zoho_crm_notes)

    if include_attachments:

        @dlt.resource(
            name=ATTACHMENTS_TABLE,
            primary_key="id",
            write_disposition="merge",
            columns=_DELETED_COLUMN,
        )
        def zoho_crm_attachments():
            yield from _sync_attachments(api, dlt.current.resource_state(), options)

        resources.append(zoho_crm_attachments)

    @dlt.source(name=ZOHO_SOURCE_NAME)
    def _zoho_crm():
        return resources

    source = _zoho_crm()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(source, DOCUMENT_SOURCE_ATTR, ZOHO_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# HTTP: OAuth refresh + retries
# ---------------------------------------------------------------------------


class _Token:
    """Access token from a refresh token, at the account's own data centre."""

    def __init__(self, http: Any, accounts: str, client_id: str, secret: str, refresh: str):
        self._http = http
        self._accounts = accounts
        self._params = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": secret,
            "refresh_token": refresh,
        }
        self.access_token: str | None = None
        self.api_domain: str | None = None

    def refresh(self) -> None:
        response = _with_retries(
            lambda: self._http.post(f"{self._accounts}/oauth/v2/token", params=self._params)
        )
        response.raise_for_status()
        payload = response.json()
        if "access_token" not in payload:
            # Zoho answers 200 with {"error": "invalid_client"} for a wrong region.
            raise PermissionError(
                f"Zoho refused the refresh token: {payload.get('error', payload)}. "
                "Check the credentials and the region (data centre) of the account."
            )
        self.access_token = payload["access_token"]
        self.api_domain = (payload.get("api_domain") or "").rstrip("/") or None
        if self.api_domain is None:
            raise PermissionError("Zoho token response had no api_domain.")
        logger.info("Zoho CRM: obtained an access token for %s.", self.api_domain)


class _Api:
    """GET helper: base URL, auth header, one refresh on 401, retries.

    Returns ``None`` for ``204`` (no data) and ``304`` (not modified).
    """

    def __init__(self, http: Any, token: _Token):
        self._http = http
        self._token = token

    def get(self, path: str, params: dict | None = None, headers: dict | None = None) -> Any:
        response = self._send(path, params, headers)
        if response.status_code in (204, 304):
            return None
        return response.json()

    def get_bytes(self, path: str) -> bytes:
        return self._send(path, None, None).content

    def _send(self, path: str, params: dict | None, headers: dict | None) -> Any:
        if self._token.access_token is None:
            self._token.refresh()
        refreshed = False
        while True:
            url = f"{self._token.api_domain}/crm/{API_VERSION}/{path}"
            auth = {"Authorization": f"Zoho-oauthtoken {self._token.access_token}"}
            response = _with_retries(
                lambda url=url, auth=auth: self._http.get(
                    url, params=params, headers={**auth, **(headers or {})}
                )
            )
            if response.status_code == 401 and not refreshed:
                self._token.refresh()
                refreshed = True
                continue
            if response.status_code in (204, 304):
                return response
            if response.status_code in (401, 403):
                raise PermissionError(
                    f"Zoho CRM refused {path} (HTTP {response.status_code}): {_error(response)}"
                )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Zoho CRM error on {path} (HTTP {response.status_code}): {_error(response)}"
                )
            return response


def _error(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        return ""
    return f"{payload.get('code')} {payload.get('message')} {payload.get('details') or ''}".strip()


def _with_retries(send) -> Any:
    """Call ``send()`` and retry 429/5xx/network errors with backoff."""
    import httpx

    for attempt in range(_MAX_RETRIES):
        try:
            response = send()
        except httpx.TransportError as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            delay = float(2**attempt)
            logger.warning("Zoho CRM: %s - retrying in %.1fs.", exc, delay)
            time.sleep(delay)
            continue
        if response.status_code in _TRANSIENT_STATUS and attempt < _MAX_RETRIES - 1:
            delay = _retry_after(response.headers, attempt)
            logger.warning(
                "Zoho CRM: HTTP %s - retrying in %.1fs (%d/%d).",
                response.status_code,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
            continue
        return response
    raise RuntimeError("Zoho CRM: retry loop exited unexpectedly.")  # pragma: no cover


def _retry_after(headers: Any, attempt: int) -> float:
    value = headers.get("Retry-After") if headers else None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return float(2**attempt)


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _since_header(cursor: str | None) -> dict:
    return {"If-Modified-Since": cursor} if cursor else {}


def _iter_pages(api: _Api, path: str, params: dict, headers: dict | None = None) -> Iterator[dict]:
    """Yield ``data`` rows across pages, following ``next_page_token``."""
    query = {**params, "per_page": 200}
    seen: set[str] = set()
    while True:
        payload = api.get(path, query, headers)
        if not payload:  # 204 no data / 304 not modified
            return
        yield from payload.get("data") or []
        info = payload.get("info") or {}
        token = info.get("next_page_token")
        if not info.get("more_records") or not token or token in seen:
            return
        seen.add(token)
        query = {**params, "per_page": 200, "page_token": token}


def _iter_deleted(api: _Api, module: str, since: str) -> Iterator[str]:
    """Ids of ``module`` records deleted at or after ``since``."""
    page = 1
    since_ts = _parse_time(since)
    while True:
        payload = api.get(f"{module}/deleted", {"type": "all", "page": page, "per_page": 200})
        if not payload:
            return
        for item in payload.get("data") or []:
            deleted_at = _parse_time(item.get("deleted_time"))
            if item.get("id") and (
                deleted_at is None or since_ts is None or deleted_at >= since_ts
            ):
                yield str(item["id"])
        if not (payload.get("info") or {}).get("more_records"):
            return
        page += 1


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Field:
    api_name: str
    label: str


def _module_fields(api: _Api, module: str, redact: bool) -> list[_Field]:
    """Fields worth reading for ``module`` (at most 50, ``id`` always first)."""
    payload = api.get("settings/fields", {"module": module}) or {}
    chosen: list[_Field] = []
    for field in payload.get("fields") or []:
        name = field.get("api_name") or ""
        data_type = field.get("data_type") or ""
        if (
            not name
            or name == "id"
            or name.startswith("$")
            or name in _VOLATILE_FIELDS
            or data_type in _SKIPPED_TYPES
            or (redact and data_type in _CONTACT_TYPES)
        ):
            continue
        chosen.append(_Field(name, field.get("field_label") or name.replace("_", " ")))
    if len(chosen) > MAX_FIELDS - 1:
        logger.warning(
            "Zoho CRM: %s has %d usable fields; reading the first %d (Zoho's limit is %d).",
            module,
            len(chosen),
            MAX_FIELDS - 1,
            MAX_FIELDS,
        )
        chosen = chosen[: MAX_FIELDS - 1]
    return [_Field("id", "ID"), *chosen]


def _sync_records(api: _Api, state: dict, options: _Options) -> Iterator[dict]:
    cursors: dict = state.setdefault("cursors", {})
    run_started = _utc_now_iso()
    changed = deleted = 0
    new_cursors = {}
    for module in options.modules:
        cursor = cursors.get(module) or options.modified_since
        fields = _module_fields(api, module, options.redact_contact_details)
        params = {"fields": ",".join(f.api_name for f in fields)}
        for record in _iter_pages(api, module, params, _since_header(cursor)):
            changed += 1
            yield _record_row(module, record, fields)
        if cursors.get(module):  # deletions only matter once something was loaded
            for record_id in _iter_deleted(api, module, cursors[module]):
                deleted += 1
                yield _tombstone(f"{module}:{record_id}")
        new_cursors[module] = run_started
    cursors.update(new_cursors)  # reached only when every module finished
    logger.info("Zoho CRM: %d record(s) synced, %d forgotten.", changed, deleted)


def _record_row(module: str, record: dict, fields: list[_Field]) -> dict:
    lines = [f"Module: {module}"]
    for field in fields:
        if field.api_name == "id":
            continue
        value = _render_value(record.get(field.api_name))
        if value:
            lines.append(f"{field.label}: {value}")
    return {
        "id": f"{module}:{record['id']}",
        "title": _record_title(record),
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _record_title(record: dict) -> str:
    for name in _TITLE_FIELDS:
        value = _render_value(record.get(name))
        if value:
            return value
    names = " ".join(p for p in (record.get("First_Name"), record.get("Last_Name")) if p)
    return names or str(record.get("id"))


def _render_value(value: Any) -> str:
    """Readable text for a Zoho field value. Lookups keep only the name."""
    if value is None or value == "" or value == []:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return str(value.get("name") or value.get("display_value") or "").strip()
    if isinstance(value, list):
        parts = [_render_value(v) for v in value]
        return ", ".join(p for p in parts if p)
    return str(value).strip()


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

_NOTE_FIELDS = "id,Note_Title,Note_Content,Parent_Id,Created_Time,Owner"


def _deleted_children(
    api: _Api, state: dict, own_module: str, modules: Iterable[str]
) -> Iterator[str]:
    """Ids of children deleted directly, or whose parent record was deleted.

    Zoho may remove a record's notes / attachments together with the record, so
    child -> parent ids are kept in state and the parents' deleted feeds are
    checked as well.
    """
    since = state["cursor"]
    gone = set(_iter_deleted(api, own_module, since))
    deleted_parents: set[str] = set()
    for module in modules:
        deleted_parents.update(_iter_deleted(api, module, since))
    parents: dict = state.get("parents") or {}
    gone.update(child for child, parent in parents.items() if parent in deleted_parents)
    for child in gone:
        parents.pop(child, None)
    return iter(sorted(gone))


def _sync_notes(api: _Api, state: dict, options: _Options) -> Iterator[dict]:
    cursor = state.get("cursor") or options.modified_since
    run_started = _utc_now_iso()
    wanted = set(options.modules)
    parents: dict = state.setdefault("parents", {})
    synced = deleted = 0
    for note in _iter_pages(api, "Notes", {"fields": _NOTE_FIELDS}, _since_header(cursor)):
        parent = note.get("Parent_Id") or {}
        parent_module = (parent.get("module") or {}).get("api_name") or note.get("$se_module")
        if parent_module not in wanted:
            continue
        synced += 1
        if parent.get("id"):
            parents[str(note["id"])] = str(parent["id"])
        yield _note_row(note, parent, parent_module)
    if state.get("cursor"):
        for note_id in _deleted_children(api, state, "Notes", options.modules):
            deleted += 1
            yield _tombstone(f"Notes:{note_id}")
    state["cursor"] = run_started
    logger.info("Zoho CRM: %d note(s) synced, %d forgotten.", synced, deleted)


def _note_row(note: dict, parent: dict, parent_module: str) -> dict:
    title = (note.get("Note_Title") or "").strip()
    parent_name = _render_value(parent)
    lines = (
        [
            f"Note on {parent_module[:-1] if parent_module.endswith('s') else parent_module}: "
            f"{parent_name}"
        ]
        if parent_name
        else []
    )
    for label, value in (
        ("Author", _render_value(note.get("Owner"))),
        ("Created", note.get("Created_Time")),
    ):
        if value:
            lines.append(f"{label}: {value}")
    content = (note.get("Note_Content") or "").strip()
    return {
        "id": f"Notes:{note['id']}",
        "title": title or (f"Note on {parent_name}" if parent_name else "Note"),
        "content": "\n".join(lines) + ("\n\n" + content if content else ""),
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Attachments (text-like files only)
# ---------------------------------------------------------------------------

_ATTACHMENT_FIELDS = "id,File_Name,Size,Parent_Id,Created_Time"


def _sync_attachments(api: _Api, state: dict, options: _Options) -> Iterator[dict]:
    cursor = state.get("cursor") or options.modified_since
    run_started = _utc_now_iso()
    wanted = set(options.modules)
    parents: dict = state.setdefault("parents", {})
    synced = skipped = deleted = 0
    for item in _iter_pages(
        api, "Attachments", {"fields": _ATTACHMENT_FIELDS}, _since_header(cursor)
    ):
        parent = item.get("Parent_Id") or {}
        parent_module = (parent.get("module") or {}).get("api_name") or item.get("$se_module")
        name = item.get("File_Name") or ""
        size = _to_int(item.get("Size"))
        if (
            parent_module not in wanted
            or not parent.get("id")
            or not name.lower().endswith(_TEXT_EXTENSIONS)
            or (size is not None and size > options.attachment_max_bytes)
        ):
            skipped += 1
            continue
        raw = api.get_bytes(f"{parent_module}/{parent['id']}/Attachments/{item['id']}")
        if len(raw) > options.attachment_max_bytes:
            skipped += 1
            continue
        synced += 1
        parents[str(item["id"])] = str(parent["id"])
        text = raw.decode("utf-8", errors="replace").strip()
        header = f"Attachment on {_render_value(parent) or parent_module}: {name}"
        yield {
            "id": f"Attachments:{item['id']}",
            "title": name,
            "content": f"{header}\n\n{text}",
            "_deleted": False,
        }
    if state.get("cursor"):
        for attachment_id in _deleted_children(api, state, "Attachments", options.modules):
            deleted += 1
            yield _tombstone(f"Attachments:{attachment_id}")
    state["cursor"] = run_started
    logger.info(
        "Zoho CRM: %d attachment(s) synced, %d skipped (type/size), %d forgotten.",
        synced,
        skipped,
        deleted,
    )


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tombstone(row_id: str) -> dict:
    return {"id": row_id, "_deleted": True}
