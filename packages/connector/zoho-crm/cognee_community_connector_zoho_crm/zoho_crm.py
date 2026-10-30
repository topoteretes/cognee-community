"""DLT source for Zoho CRM (incremental sync + forget-on-delete).

Pulls Zoho CRM **records** (Leads, Contacts, Accounts, Deals, or any module you
choose), their **notes** and, optionally, **text attachments** into cognee as
documents::

    import os

    import cognee
    from cognee_community_connector_zoho_crm import zoho_crm_source

    await cognee.remember(
        zoho_crm_source(
            resource_name="zoho_acme",  # one per Zoho org / scope, keep it fixed
            client_id=os.environ["ZOHO_CLIENT_ID"],
            client_secret=os.environ["ZOHO_CLIENT_SECRET"],
            refresh_token=os.environ["ZOHO_REFRESH_TOKEN"],
            region="eu",
        ),
        dataset_name="zoho_crm",
        primary_key="id",
        write_disposition="merge",  # REQUIRED (see .. important:: below)
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**. Incremental runs only see what
   changed, so cognee's default ``"replace"`` would forget everything else.

Design (behaviour checked against a live Zoho CRM v8 account, India DC)
-------------------------------------------------------------------------
* **Scope** - ``resource_name`` prefixes the tables and is published as the
  pipeline scope, so two Zoho orgs (or two configurations) in one dataset keep
  separate cursors and separate forget-on-delete bookkeeping.
* **Auth** - a refresh token is exchanged at the account's own data centre; the
  API host is taken from the token response (``api_domain``).
* **Fields** - ``fields`` is mandatory and capped at 50. Fields are chosen from
  the module's metadata *by type*: date-times (except ``Created_Time``), formula,
  auto-number, roll-up, image and file fields are left out, and so are visit
  tracking and other volatile fields, so a page view or a recalculation never
  changes a record's text. E-mail and phone fields are left out by default.
* **Incremental** - ``If-Modified-Since`` on every list call (Zoho answers ``304``
  when nothing changed; the filter is inclusive, ``merge`` makes repeats
  harmless). Cursors come from Zoho's own clock (the ``Date`` header of the run's
  first response) minus a 5-minute overlap, and all cursors move together, only
  after every module finished. Paging follows ``next_page_token`` and falls back
  to ``page`` numbers when a list does not return one.
* **Forget-on-delete** - the Deleted Records API (``/{module}/deleted``, sent with
  ``If-Modified-Since``) tombstones deleted records and notes, plus notes and
  attachments of a deleted parent. An attachment removed by itself is noticed
  only when its parent record is modified and re-listed. Every ``reconcile_every`` runs, the
  ids of each module are listed and every id that vanished is re-checked one by
  one before it is forgotten; this also catches converted leads, which leave
  ``Leads`` without showing up as deleted. A module removed from ``modules`` is
  forgotten with its notes and attachments.
* **Safe failure** - ``5xx`` and network errors are retried; a rate-limit wait
  longer than ``max_rate_limit_wait`` stops all interleaved resources before
  their next request. The stopped resource moves no cursor; a resource that
  already finished keeps its cursor. Attachment parent checkpoints are kept so
  a large fan-out can resume. Any other error aborts the run. Errors carry the
  status and Zoho's error code, never bodies.
"""

from __future__ import annotations

import email.utils
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("zoho_crm_connector")

ZOHO_SOURCE_NAME = "zoho_crm"
DEFAULT_RESOURCE_NAME = "zoho_crm"
API_VERSION = "v8"
CURSOR_OVERLAP = timedelta(minutes=5)
DEFAULT_MAX_RATE_LIMIT_WAIT = 120.0
MAX_VERIFY = 200

# Region -> (accounts server, CRM web app). The API host comes from the token.
REGIONS = {
    "com": ("https://accounts.zoho.com", "https://crm.zoho.com"),
    "eu": ("https://accounts.zoho.eu", "https://crm.zoho.eu"),
    "in": ("https://accounts.zoho.in", "https://crm.zoho.in"),
    "com.au": ("https://accounts.zoho.com.au", "https://crm.zoho.com.au"),
    "jp": ("https://accounts.zoho.jp", "https://crm.zoho.jp"),
    "com.cn": ("https://accounts.zoho.com.cn", "https://crm.zoho.com.cn"),
    "ca": ("https://accounts.zohocloud.ca", "https://crm.zohocloud.ca"),
    "sa": ("https://accounts.zoho.sa", "https://crm.zoho.sa"),
}
REGION_ALIASES = {"us": "com", "au": "com.au", "cn": "com.cn"}
DEFAULT_MODULES = ("Leads", "Contacts", "Accounts", "Deals")
MAX_FIELDS = 50  # Zoho rejects more with LIMIT_EXCEEDED

_UTC = timezone.utc
_MAX_RETRIES = 5
_SERVER_ERRORS = frozenset({500, 502, 503, 504})
_DELETED_COLUMN = {"_deleted": {"data_type": "bool", "hard_delete": True}}
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

# Types whose values are machine data or change on their own.
_SKIPPED_TYPES = frozenset(
    {
        "datetime",
        "formula",
        "autonumber",
        "rollup_summary",
        "profileimage",
        "imageupload",
        "fileupload",
        "multiselectlookup_count",
    }
)
_KEPT_DATETIMES = frozenset({"Created_Time"})
# Volatile fields that are not date-times (visit tracking, scores, durations).
_VOLATILE_FIELDS = frozenset(
    {
        "Modified_By",
        "Locked__s",
        "Record_Status__s",
        "Enrich_Status__s",
        "Sales_Cycle_Duration",
        "Overall_Sales_Duration",
        "Lead_Conversion_Time",
        "Days_Visited",
        "Number_Of_Chats",
        "Visitor_Score",
        "Average_Time_Spent_Minutes",
        "Referrer",
        "First_Visited_URL",
        "Latitude",
        "Longitude",
        "Coordinates",
        "Unsubscribed_Mode",
        "Tag",
    }
)
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


class RateLimitedError(Exception):
    """Zoho asked to wait longer than ``max_rate_limit_wait``."""


class _UnstableListingError(Exception):
    pass


class _RunStoppedError(Exception):
    """Another interleaved resource already stopped this extraction."""


@dataclass(frozen=True)
class _Options:
    modules: tuple[str, ...] = DEFAULT_MODULES
    redact_contact_details: bool = True
    modified_since: str | None = None
    attachment_max_bytes: int = 1_000_000
    reconcile: bool = True
    reconcile_every: int = 1
    web_base: str = "https://crm.zoho.com"


def zoho_crm_source(
    *,
    resource_name: str = DEFAULT_RESOURCE_NAME,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    region: str = "com",
    modules: Iterable[str] | None = None,
    include_notes: bool = True,
    include_attachments: bool = False,
    attachment_max_bytes: int = 1_000_000,
    redact_contact_details: bool = True,
    reconcile: bool = True,
    reconcile_every: int = 1,
    modified_since: str | None = None,
    max_rate_limit_wait: float = DEFAULT_MAX_RATE_LIMIT_WAIT,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Create a dlt source with Zoho CRM records, notes and attachments.

    All arguments are keyword-only. Credentials are taken only from the
    arguments (the example shows reading them from the environment).

    Args:
        resource_name: Stable connection name (lowercase letters, digits, ``_``).
            Prefixes the tables and scopes the sync state; one per Zoho org.
        client_id / client_secret / refresh_token: OAuth credentials.
        region: Data centre: ``com`` (default), ``eu``, ``in``, ``com.au``,
            ``jp``, ``com.cn``, ``ca`` or ``sa``.
        modules: Module API names. Default: Leads, Contacts, Accounts, Deals.
            A module removed later is forgotten; one added later is backfilled.
        include_notes: Sync notes on records of those modules (needs an admin
            profile: ``GET /Notes`` is admin-only).
        include_attachments: Sync text-like attachments of those modules.
        attachment_max_bytes: Skip larger attachments.
        redact_contact_details: Leave out e-mail and phone fields (default).
        reconcile: List ids to catch records that vanished without a delete
            event (converted leads, records purged from the recycle bin).
        reconcile_every: Reconcile on every N-th run (default every run).
        modified_since: ISO 8601 time; limits only the *first* run.
        max_rate_limit_wait: Longest ``Retry-After`` (seconds) that is slept; a
            longer one stops the run cleanly.
        check_active: Optional host authorization checkpoint, called before each
            request and around each row.
        service: ``httpx.Client``-like object with ``get(url, params=, headers=)``
            and ``post(url, params=)``; ``client`` is an alias.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``. Its
        ``cognee_sync_stats`` dict holds integer counts for the last run.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1 or not hasattr(
        dlt_utils, "PIPELINE_SCOPE_ATTR"
    ):
        raise RuntimeError(
            "The Zoho CRM connector needs a cognee build with table-scoped DLT document "
            "cleanup and PIPELINE_SCOPE_ATTR (cognee >= 1.6.1). Upgrade cognee before "
            "syncing, so a sync cannot delete another source's data."
        )
    if not isinstance(resource_name, str) or not _RESOURCE_NAME_RE.match(resource_name):
        raise ValueError(
            "resource_name must start with a lowercase letter and contain only lowercase "
            "letters, digits and '_' (max 41 characters)."
        )
    if not (client_id and client_secret and refresh_token):
        raise ValueError("Zoho CRM needs client_id, client_secret and refresh_token.")
    region = REGION_ALIASES.get(region.lower().lstrip("."), region.lower().lstrip("."))
    if region not in REGIONS:
        raise ValueError(f"Unknown Zoho region {region!r}. Use one of: {sorted(REGIONS)}.")
    if service is not None and client is not None and service is not client:
        raise ValueError("Pass either service= or client=, not both.")
    if isinstance(modules, str):
        raise TypeError("modules must be an iterable of module names, not a string.")
    if not isinstance(reconcile_every, int) or reconcile_every < 1:
        raise ValueError("reconcile_every must be a positive integer.")
    http = service if service is not None else client
    if http is None:
        http = httpx.Client(timeout=60.0)

    accounts, web = REGIONS[region]
    options = _Options(
        modules=tuple(dict.fromkeys(modules or DEFAULT_MODULES)),
        redact_contact_details=redact_contact_details,
        modified_since=modified_since,
        attachment_max_bytes=attachment_max_bytes,
        reconcile=reconcile,
        reconcile_every=reconcile_every,
        web_base=web,
    )
    stats: dict[str, int] = {}
    token = _Token(http, accounts, client_id, client_secret, refresh_token)
    api = _Api(http, token, stats, check_active, max_rate_limit_wait)
    order = [
        "records",
        *(["notes"] if include_notes else []),
        *(["attachments"] if include_attachments else []),
    ]

    def run(kind: str, sync):
        if kind == order[0]:
            stats.clear()
            stats.update(failed_rate_limit=0, failed_auth=0)
            api.server_time = None
        elif stats.get("failed_rate_limit"):
            return  # this run already stopped on a long rate limit
        rows = _stop_cleanly(sync(api, dlt.current.resource_state(), options, stats), stats)
        yield from dlt_utils.guarded_rows(rows, check_active)

    resources = []

    @dlt.resource(
        name=f"{resource_name}_records",
        primary_key="id",
        write_disposition="merge",
        columns=_DELETED_COLUMN,
    )
    def zoho_crm_records():
        yield from run("records", _sync_records)

    resources.append(zoho_crm_records)

    if include_notes:

        @dlt.resource(
            name=f"{resource_name}_notes",
            primary_key="id",
            write_disposition="merge",
            columns=_DELETED_COLUMN,
        )
        def zoho_crm_notes():
            yield from run("notes", _sync_notes)

        resources.append(zoho_crm_notes)

    if include_attachments:

        @dlt.resource(
            name=f"{resource_name}_attachments",
            primary_key="id",
            write_disposition="merge",
            columns=_DELETED_COLUMN,
        )
        def zoho_crm_attachments():
            yield from run("attachments", _sync_attachments)

        resources.append(zoho_crm_attachments)

    @dlt.source(name=resource_name)
    def _zoho_crm():
        return resources

    source = _zoho_crm()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(source, DOCUMENT_SOURCE_ATTR, ZOHO_SOURCE_NAME)
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    source.cognee_sync_stats = stats
    return source


def _count(stats: dict[str, int], key: str, amount: int = 1) -> None:
    stats[key] = stats.get(key, 0) + amount


def _stop_cleanly(rows: Iterator[dict], stats: dict[str, int]) -> Iterator[dict]:
    """End cleanly on a long limit; cursors stay put, resume checkpoints may persist."""
    try:
        yield from rows
    except RateLimitedError as exc:
        _count(stats, "failed_rate_limit")
        logger.warning("Zoho CRM: %s Stopping this run; the next run continues.", exc)
    except _RunStoppedError:
        return  # another interleaved resource already hit a long rate limit


# ---------------------------------------------------------------------------
# HTTP: OAuth refresh, checkpoints, retries
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
        response = self._http.post(f"{self._accounts}/oauth/v2/token", params=self._params)
        if response.status_code >= 400:
            raise PermissionError(f"Zoho token request failed (HTTP {response.status_code}).")
        payload = response.json()
        if "access_token" not in payload:
            # Zoho answers 200 with {"error": "invalid_client"} for a wrong region.
            raise PermissionError(
                f"Zoho refused the refresh token ({payload.get('error', 'unknown error')}). "
                "Check the credentials and the region (data centre) of the account."
            )
        self.access_token = payload["access_token"]
        self.api_domain = (payload.get("api_domain") or "").rstrip("/") or None
        if self.api_domain is None:
            raise PermissionError("Zoho's token response had no api_domain.")


class _Api:
    """GET helper: base URL, auth, checkpoint, one refresh on 401, retries.

    Returns ``None`` for ``204`` (no data) and ``304`` (not modified).
    """

    def __init__(
        self,
        http: Any,
        token: _Token,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
        max_wait: float,
    ):
        self._http = http
        self._token = token
        self._stats = stats
        self._check_active = check_active
        self._max_wait = max_wait
        self.server_time: datetime | None = None

    def get(self, path: str, params: dict | None = None, headers: dict | None = None) -> Any:
        response = self._send(path, params, headers)
        if response.status_code in (204, 304):
            return None
        return response.json()

    def get_bytes(self, path: str) -> bytes:
        return self._send(path, None, None).content

    def _send(self, path: str, params: dict | None, headers: dict | None) -> Any:
        try:
            return self._send_once(path, params, headers)
        except PermissionError:
            _count(self._stats, "failed_auth")
            raise

    def _send_once(self, path: str, params: dict | None, headers: dict | None) -> Any:
        if self._stats.get("failed_rate_limit"):
            raise _RunStoppedError
        if self._token.access_token is None:
            self._token.refresh()
        refreshed = False
        attempt = 0
        while True:
            if self._check_active is not None:
                self._check_active()
            url = f"{self._token.api_domain}/crm/{API_VERSION}/{path}"
            auth = {"Authorization": f"Zoho-oauthtoken {self._token.access_token}"}
            try:
                response = self._http.get(url, params=params, headers={**auth, **(headers or {})})
            except httpx.TransportError as exc:
                if attempt >= _MAX_RETRIES - 1:
                    raise
                attempt += 1
                _sleep(float(2**attempt), f"network error ({type(exc).__name__})")
                continue
            self._note_server_time(response.headers)
            status = response.status_code
            if status == 401 and not refreshed:
                self._token.refresh()
                refreshed = True
                continue
            if status in (204, 304) or status < 400:
                return response
            if status == 429:
                wait = _retry_after(response.headers, attempt)
                if wait > self._max_wait or attempt >= _MAX_RETRIES - 1:
                    raise RateLimitedError(
                        f"rate limit: asked to wait {wait:.0f}s (limit {self._max_wait:.0f}s)."
                    )
                attempt += 1
                _sleep(wait, "HTTP 429")
                continue
            if status in _SERVER_ERRORS and attempt < _MAX_RETRIES - 1:
                attempt += 1
                _sleep(float(2**attempt), f"HTTP {status}")
                continue
            code = _error_code(response)
            if status in (401, 403):
                raise PermissionError(f"Zoho CRM refused the request (HTTP {status}, {code}).")
            raise RuntimeError(f"Zoho CRM error (HTTP {status}, {code}).")

    def _note_server_time(self, headers: Any) -> None:
        if self.server_time is not None or not headers:
            return
        value = headers.get("Date") or headers.get("date")
        if not value:
            return
        try:
            self.server_time = email.utils.parsedate_to_datetime(value).astimezone(_UTC)
        except (TypeError, ValueError):
            return


def _sleep(seconds: float, why: str) -> None:
    logger.warning("Zoho CRM: %s - retrying in %.1fs.", why, seconds)
    time.sleep(seconds)


def _error_code(response: Any) -> str:
    """Zoho's error code only (e.g. ``INVALID_DATA``); never the response body."""
    try:
        payload = response.json()
    except ValueError:
        return "no error code"
    code = payload.get("code") if isinstance(payload, dict) else None
    return str(code) if code else "no error code"


def _retry_after(headers: Any, attempt: int) -> float:
    value = headers.get("Retry-After") if headers else None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return float(2 ** (attempt + 1))


def _iso(value: datetime) -> str:
    return value.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_UTC)


def _since_header(cursor: str | None) -> dict:
    return {"If-Modified-Since": cursor} if cursor else {}


def _new_cursor(api: _Api) -> str:
    return _iso((api.server_time or datetime.now(_UTC)) - CURSOR_OVERLAP)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _iter_pages(
    api: _Api, path: str, params: dict, headers: dict | None = None, seen: set | None = None
) -> Iterator[dict]:
    """Yield ``data`` rows across pages.

    Follows ``info.next_page_token`` when the list returns one (records, and
    Notes on a live v8 account); otherwise falls back to ``page`` numbers, which
    is how the v8 docs describe ``GET /Notes``. A repeated token or page raises,
    so a short listing never turns into deletes.
    """
    seen = seen if seen is not None else set()
    tokens: set[str] = set()
    query: dict[str, Any] = {**params, "per_page": 200}
    page = 1
    while True:
        payload = api.get(path, query, headers)
        if not payload:  # 204 no data / 304 not modified
            return
        for row in payload.get("data") or []:
            if row.get("id") in seen:
                raise _UnstableListingError(f"{path} returned a record twice while paging.")
            seen.add(row.get("id"))
            yield row
        info = payload.get("info") or {}
        if not info.get("more_records"):
            return
        token = info.get("next_page_token")
        if token:
            if token in tokens:
                raise _UnstableListingError(f"{path} repeated a page token.")
            tokens.add(token)
            query = {**params, "per_page": 200, "page_token": token}
        else:
            page += 1
            query = {**params, "per_page": 200, "page": page}


def _iter_deleted(api: _Api, module: str, since: str) -> Iterator[str]:
    """Ids of ``module`` records deleted at or after ``since``."""
    since_ts = _parse_time(since)
    page = 1
    while True:
        payload = api.get(
            f"{module}/deleted",
            {"type": "all", "page": page, "per_page": 200},
            _since_header(since),
        )
        if not payload:  # 204 nothing deleted / 304 nothing since the cursor
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


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Field:
    api_name: str
    label: str


def _module_fields(api: _Api, module: str, redact: bool) -> list[_Field]:
    """Fields worth reading for ``module`` (at most 50, ``id`` first)."""
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
            or (data_type in _SKIPPED_TYPES and name not in _KEPT_DATETIMES)
            or (redact and data_type in _CONTACT_TYPES)
            or field.get("visible") is False
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


@dataclass
class _Verified:
    """Outcome of one module's reconcile, updated as each id is confirmed so a
    clean rate-limit stop can still save what was learned."""

    gone: set[str] = dc_field(default_factory=set)
    live: set[str] = dc_field(default_factory=set)  # confirmed alive in this cycle
    backlog: int = 0  # candidates still unchecked after this run


def _sync_records(
    api: _Api, state: dict, options: _Options, stats: dict[str, int]
) -> Iterator[dict]:
    cursors: dict = dict(state.get("cursors") or {})
    known: dict[str, list[str]] = {m: list(v) for m, v in (state.get("known") or {}).items()}
    # Ids confirmed alive during the current backlog cycle, so they are not
    # re-checked until every other candidate has been seen.
    live: dict[str, set[str]] = {
        m: {str(i) for i in v}
        for m, v in (state.get("reconcile_live") or {}).items()
        if isinstance(v, list) and v
    }
    state.pop("reconcile_positions", None)  # older checkpoint format
    config = {"redact": options.redact_contact_details}
    if state.get("config") not in (None, config):
        cursors = {}  # rendering changed: re-read everything
    first_run = not cursors  # a full listing already reconciles
    if first_run:
        live = {}
    runs = int(state.get("runs_since_reconcile") or 0) + 1
    do_reconcile = options.reconcile and not first_run and runs >= options.reconcile_every

    def save_partial() -> None:
        # Never moves cursors: a stopped run is repeated from the old ones.
        state["known"] = known
        state["reconcile_live"] = {m: sorted(v) for m, v in live.items() if v}

    # A module dropped from `modules` is forgotten.
    for module in sorted(set(known) - set(options.modules)):
        for record_id in known.pop(module):
            _count(stats, "records_forgotten")
            yield _tombstone(module, record_id)
        cursors.pop(module, None)
        live.pop(module, None)

    backlog = 0
    for module in options.modules:
        ids = set(known.get(module) or [])
        gone: set[str] = set()
        verify = _Verified(live=live.get(module, set()))
        try:
            cursor = cursors.get(module) or options.modified_since
            fields = _module_fields(api, module, options.redact_contact_details)
            params = {"fields": ",".join(f.api_name for f in fields)}
            emitted: set[str] = set()
            for record in _iter_pages(api, module, params, _since_header(cursor)):
                record_id = str(record["id"])
                emitted.add(record_id)
                ids.add(record_id)
                _count(stats, "records_synced")
                yield _record_row(module, record, fields, options.web_base)
            if module in cursors:  # deletions only matter once something was loaded
                gone.update(i for i in _iter_deleted(api, module, cursors[module]) if i in ids)
                if do_reconcile:
                    _vanished(api, module, ids - emitted - gone, stats, verify)
                    gone.update(verify.gone)
                    backlog += verify.backlog
        except (RateLimitedError, _RunStoppedError):
            # Keep what this run already loaded or confirmed, so a rerun does not
            # repeat it; the cursors stay where they were.
            gone.update(verify.gone)
            for record_id in sorted(gone):
                _count(stats, "records_forgotten")
                yield _tombstone(module, record_id)
            known[module] = sorted(ids - gone)
            live[module] = verify.live
            save_partial()
            raise
        for record_id in sorted(gone):
            _count(stats, "records_forgotten")
            yield _tombstone(module, record_id)
        known[module] = sorted(ids - gone)
        live[module] = verify.live

    # Reached only when every module finished: all cursors move together.
    cursor_now = _new_cursor(api)
    state["cursors"] = dict.fromkeys(options.modules, cursor_now)
    save_partial()
    state["config"] = config
    # Unchecked candidates are verified next run, not after `reconcile_every`.
    state["runs_since_reconcile"] = (
        options.reconcile_every if backlog else (0 if (do_reconcile or first_run) else runs)
    )


def _vanished(
    api: _Api,
    module: str,
    candidates: set[str],
    stats: dict[str, int],
    out: _Verified,
) -> None:
    """Confirm one batch of ids missing from a full id listing.

    Ids already confirmed alive in this cycle (``out.live``) are skipped, so
    live records the listing never shows cannot starve the rest. The cycle ends,
    and ``out.live`` is cleared, once nothing is left to check.
    """
    if not candidates:
        out.live = set()
        return
    try:
        present = {
            str(r["id"])
            for r in _iter_pages(
                api, module, {"fields": "id", "sort_by": "id", "sort_order": "asc"}
            )
        }
    except _UnstableListingError as exc:
        _count(stats, "reconcile_skipped")
        logger.warning("Zoho CRM: %s Nothing is forgotten this run.", exc)
        return  # cycle memory is kept as it was
    missing = sorted(candidates - present)
    out.live &= set(missing)
    pending = [i for i in missing if i not in out.live]
    batch = pending[:MAX_VERIFY]
    out.backlog = len(pending) - len(batch)
    if out.backlog:
        logger.warning(
            "Zoho CRM: verifying a batch of %d of %d unchecked vanished %s records; "
            "the remaining %d continue next run.",
            len(batch),
            len(pending),
            module,
            out.backlog,
        )
    for record_id in batch:
        record = (
            api.get(f"{module}/{record_id}", {"fields": "id,Converted__s"})
            if module == "Leads"
            else api.get(f"{module}/{record_id}", {"fields": "id"})
        )
        data = (record or {}).get("data") or []
        if not data or data[0].get("Converted__s") is True:  # 204 = gone; converted lead
            out.gone.add(record_id)
        else:
            out.live.add(record_id)
    if not out.backlog:
        out.live = set()  # cycle complete; the next reconcile checks everything again


def _record_row(module: str, record: dict, fields: list[_Field], web_base: str) -> dict:
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
        "url": f"{web_base}/crm/tab/{module}/{record['id']}",
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
    """Readable text for a Zoho field value. Lookups keep only the name;
    multi-select values are sorted so their order never changes the text."""
    if value is None or value == "" or value == []:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return str(value.get("name") or value.get("display_value") or "").strip()
    if isinstance(value, list):
        parts = sorted(p for p in (_render_value(v) for v in value) if p)
        return ", ".join(parts)
    return str(value).strip()


def _tombstone(module: str, record_id: str) -> dict:
    return {"id": f"{module}:{record_id}", "_deleted": True}


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

_NOTE_FIELDS = "id,Note_Title,Note_Content,Parent_Id,Created_Time,Owner"


def _parent_module(item: dict) -> str | None:
    parent = item.get("Parent_Id") or {}
    return (parent.get("module") or {}).get("api_name") or item.get("$se_module")


def _deleted_parents(api: _Api, modules: Iterable[str], since: str) -> set[str]:
    gone: set[str] = set()
    for module in modules:
        gone.update(_iter_deleted(api, module, since))
    return gone


def _sync_notes(api: _Api, state: dict, options: _Options, stats: dict[str, int]) -> Iterator[dict]:
    modules = sorted(options.modules)
    cursor = state.get("cursor")
    same_modules = state.get("modules") == modules
    since = cursor if same_modules else options.modified_since
    # note id -> [parent module, parent id]
    parents: dict[str, list[str]] = {k: list(v) for k, v in (state.get("parents") or {}).items()}
    gone: set[str] = set()

    # Notes of a module no longer selected are forgotten.
    for note_id, (module, _) in list(parents.items()):
        if module not in options.modules:
            gone.add(note_id)

    for note in _iter_pages(api, "Notes", {"fields": _NOTE_FIELDS}, _since_header(since)):
        module = _parent_module(note)
        if module not in options.modules:
            continue
        parent = note.get("Parent_Id") or {}
        parents[str(note["id"])] = [module, str(parent.get("id"))]
        _count(stats, "notes_synced")
        yield _note_row(note, parent, module, options.web_base)

    if cursor:
        gone.update(i for i in _iter_deleted(api, "Notes", cursor) if i in parents)
        dead = _deleted_parents(api, options.modules, cursor)
        gone.update(n for n, (_, parent_id) in parents.items() if parent_id in dead)
    for note_id in sorted(gone):
        parents.pop(note_id, None)
        _count(stats, "notes_forgotten")
        yield {"id": f"Notes:{note_id}", "_deleted": True}

    state["cursor"] = _new_cursor(api)
    state["modules"] = modules
    state["parents"] = parents


def _note_row(note: dict, parent: dict, parent_module: str, web_base: str) -> dict:
    title = (note.get("Note_Title") or "").strip()
    parent_name = _render_value(parent)
    kind = parent_module[:-1] if parent_module.endswith("s") else parent_module
    lines = [f"Note on {kind}: {parent_name}"] if parent_name else []
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
        "url": f"{web_base}/crm/tab/{parent_module}/{parent.get('id')}"
        if parent.get("id")
        else None,
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Attachments (text-like files; listed per record, as the v8 docs describe)
# ---------------------------------------------------------------------------

_ATTACHMENT_FIELDS = "id,File_Name,Size,Modified_Time"


def _sync_attachments(
    api: _Api, state: dict, options: _Options, stats: dict[str, int]
) -> Iterator[dict]:
    modules = sorted(options.modules)
    cursor = state.get("cursor")
    same_modules = state.get("modules") == modules
    since = cursor if same_modules else options.modified_since
    # attachment id -> [parent module, parent id]
    owners: dict[str, list[str]] = {k: list(v) for k, v in (state.get("owners") or {}).items()}
    gone: set[str] = set()
    for att_id, (module, _) in list(owners.items()):
        if module not in options.modules:
            gone.add(att_id)

    # A clean rate-limit stop commits dlt state. Checkpoint completed parents so
    # a large first run resumes its N+1 attachment fan-out instead of starting
    # from the first parent forever. The record list is cheap and is repeated;
    # only per-parent attachment calls are skipped. The checkpoint dict is shared
    # with `state`, so it is updated in place (no per-parent copy or sort); dlt
    # reads state only after the resource stops or finishes.
    progress = state.get("progress") or {}
    if (
        progress.get("modules") != modules
        or progress.get("since") != since
        or not isinstance(progress.get("processed"), dict)
        or any(not isinstance(items, dict) for items in progress.get("processed", {}).values())
    ):
        progress = {"modules": modules, "since": since, "processed": {}}
    processed: dict[str, dict[str, Any]] = progress["processed"]
    state["owners"] = owners
    state["progress"] = progress
    # (module, parent id) -> attachment ids, so removals need no scan of `owners`.
    by_parent: dict[tuple[str, str], set[str]] = {}
    for att_id, (m, r) in owners.items():
        by_parent.setdefault((m, r), set()).add(att_id)

    for module in options.modules:
        done = processed.setdefault(module, {})
        # Modified_Time makes a resume checkpoint safe when a completed parent
        # changes between the stopped run and the resumed run.
        for record in _iter_pages(
            api, module, {"fields": "id,Modified_Time"}, _since_header(since)
        ):
            record_id = str(record["id"])
            modified = record.get("Modified_Time")
            if record_id in done and done[record_id] == modified:
                continue
            listed: set[str] = set()
            path = f"{module}/{record_id}/Attachments"
            for item in _iter_pages(api, path, {"fields": _ATTACHMENT_FIELDS}):
                att_id = str(item["id"])
                listed.add(att_id)
                name = item.get("File_Name") or ""
                size = _to_int(item.get("Size"))
                if not name.lower().endswith(_TEXT_EXTENSIONS) or (
                    size is not None and size > options.attachment_max_bytes
                ):
                    _count(stats, "attachments_skipped")
                    continue
                raw = api.get_bytes(f"{path}/{att_id}")
                if len(raw) > options.attachment_max_bytes:
                    _count(stats, "attachments_skipped")
                    continue
                owners[att_id] = [module, record_id]
                by_parent.setdefault((module, record_id), set()).add(att_id)
                _count(stats, "attachments_synced")
                text = raw.decode("utf-8", errors="replace").strip()
                yield {
                    "id": f"Attachments:{att_id}",
                    "title": name,
                    "content": f"Attachment on {module[:-1] if module.endswith('s') else module} "
                    f"{record_id}: {name}\n\n{text}",
                    "url": f"{options.web_base}/crm/tab/{module}/{record_id}",
                    "_deleted": False,
                }
            # Attachment removals are observable only when the parent is listed.
            siblings = by_parent.get((module, record_id), set())
            for att_id in sorted(siblings - listed):
                siblings.discard(att_id)
                owners.pop(att_id, None)
                _count(stats, "attachments_forgotten")
                yield {"id": f"Attachments:{att_id}", "_deleted": True}
            done[record_id] = modified  # after the parent's rows were yielded

    if cursor:
        dead = _deleted_parents(api, options.modules, cursor)
        gone.update(a for a, (_, parent_id) in owners.items() if parent_id in dead)
    for att_id in sorted(gone):
        owners.pop(att_id, None)
        _count(stats, "attachments_forgotten")
        yield {"id": f"Attachments:{att_id}", "_deleted": True}

    state["cursor"] = _new_cursor(api)
    state["modules"] = modules
    state["owners"] = owners
    state.pop("progress", None)


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
