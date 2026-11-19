"""Smartsheet connector for cognee — a ``dlt`` source that turns sheets into memory.

Sync one or more Smartsheet sheets (selected directly, or discovered under
folders/workspaces, or every sheet the token can see) into cognee. The
resource produced here is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_smartsheet import smartsheet_source

    await cognee.remember(
        smartsheet_source(sheet_ids=[1234567890123456]),
        dataset_name="my_sheets",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Auth** — a personal access token (``Authorization: Bearer``), read-only
  from this connector's side (it only ever issues GET requests). Generating a
  token requires a paid Business/Enterprise/Advanced Work Management plan —
  the free trial and Pro plan cannot generate API tokens (confirmed,
  help.smartsheet.com/articles/529590). A token inherits *all* of the
  creating user's access and cannot be scoped down by the API itself
  (developers.smartsheet.com auth guide) — see the README for why a
  dedicated, narrowly-shared account is strongly recommended.
* **Hybrid document model** — one *sheet summary* document per sheet (name,
  columns, row counts, top-level row titles, sheet-level comments) plus one
  *row* document per row that actually carries content: free text, a
  contact/picklist value, a comment, or an attachment. A sheet with 10,000
  mostly-numeric rows does not become 10,000 near-empty documents; see
  ``_row_qualifies`` for the exact inclusion rule. This is a direct answer to
  the connector issue's warning that "rows are columnar, not documents."
* **Incremental sync** — a cheap per-sheet change marker
  (``GET /sheets/{id}/version``, confirmed to exist:
  developers.smartsheet.com/api/smartsheet/openapi/sheets/get-sheetversion)
  decides whether a sheet needs a full re-fetch. A sheet is re-fetched when
  it is new, its version changed, this is a full pass (first run, the
  resolved config changed, or every ``full_sync_every`` runs), or it is
  picked by a small rotating re-check (``recheck_sheets_per_run``) on
  incremental passes. **Smartsheet's own "Get sheet version" docs confirm
  that adding/modifying a discussion or comment DOES bump the version** —
  but row *deletion* specifically is not confirmed either way, which is
  exactly what the rotating re-check and periodic full pass guard against.
* **Forget-on-delete** — a re-fetched sheet's current document ids (summary +
  qualifying rows) are diffed against the ids stored from its last sync;
  anything missing is tombstoned (``_deleted: True``, dlt's ``hard_delete``).
  Sheets that vanish from the resolved selection are individually
  re-checked and tombstoned too. **Important, confirmed limitation**:
  Smartsheet returns the identical 404 (``errorCode`` 1006) for a sheet that
  was deleted AND for a sheet the token simply lost access to (unshared) —
  there is no API-level way to tell these apart (Smartsheet Community
  discussion 138910). This connector treats a 404 as "gone" either way,
  with a mass-delete ratio guard (``allow_mass_delete``) as the real safety
  net if that ambiguity ever means a permission change gets misread as mass
  deletion — see "How sync + forget-on-delete work" in the README.
* Attachments are metadata only by default (name, type, size); the
  short-lived ``urlExpiresInMillis`` download URL is never persisted.
  ``attachment_text=True`` opts into fetching plain-text content for small
  text/CSV/Markdown attachments specifically — see the README.
* **Pagination — two different, genuinely distinct mechanisms, verified
  against the current (2026-10-12) ``smartsheet/smartsheet-python-sdk``
  source, not assumed**: Smartsheet deprecated ``includeAll``/``page``/
  ``pageSize`` in favor of cursor pagination (``lastKey``/``maxItems``) on
  2026-06-03, but **only for a specific subset of endpoints** — ``GET
  /sights``, ``GET /workspaces`` (the top-level list), and the *new*
  ``GET /workspaces/{id}/children`` / ``GET /folders/{id}/children``
  endpoints (which replaced the old ``GET /workspaces/{id}`` / ``GET
  /folders/{id}``). List Sheets, Get Sheet's own row-paging, and the
  discussions/attachments listing endpoints were **not** part of that
  migration and still use the legacy ``page``/``pageSize``/``totalPages``
  contract. This connector therefore has two pagination helpers
  (``_paginate_cursor`` and ``_paginate_offset``), used for the right
  endpoints rather than one assumed-universal scheme.
* **Errors never guess** — every non-200 response raises, including the
  upstream ``errorCode``/``message``/``refId`` in the message; nothing is
  ever silently skipped.

Limitations
-----------
* Forget-on-delete relies on a 404 that is confirmed ambiguous between
  "deleted" and "access revoked" — see the README.
* Attachment content is only ever fetched for small, opt-in text-like
  attachments (``attachment_text=True``); everything else stays metadata
  only.
* Reports and dashboards are out of scope; only sheets are ingested.
"""

import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("smartsheet_connector")

SMARTSHEET_SOURCE_NAME = "smartsheet"
_DEFAULT_RESOURCE_NAME = "smartsheet_documents"
_DEFAULT_HOST = "https://api.smartsheet.com"
_API_PATH = "/2.0"

_EXTRA_HINT = (
    'The Smartsheet connector needs "requests": pip install requests, or install '
    '"cognee-community-connector-smartsheet", which declares it.'
)

# Confirmed against help.smartsheet.com/articles/529590 (trial FAQ: "You can't
# generate API access tokens") and developers.smartsheet.com/api/smartsheet/
# guides/getting-started ("Required plan: Business, Enterprise, or Advanced
# Work Management").
_TOKEN_HINT = (
    "Generate one at Account -> Personal Settings -> API Access (requires a paid "
    "Business, Enterprise, or Advanced Work Management plan -- the free trial and "
    "Pro plan cannot generate API tokens)."
)

# ---------------------------------------------------------------------------
# Column types and rendering rules
# ---------------------------------------------------------------------------

# Confirmed exact (smartsheet/models/enums/column_type.py, SDK commit a554eaf,
# 2026-10-12). There is no SYMBOL columnType -- Symbol is a decoration on a
# PICKLIST column (column.symbol), not a distinct type.
_COLUMN_TYPES = frozenset(
    {
        "TEXT_NUMBER",
        "DATE",
        "DATETIME",
        "CONTACT_LIST",
        "CHECKBOX",
        "PICKLIST",
        "DURATION",
        "PREDECESSOR",
        "ABSTRACT_DATETIME",
        "MULTI_CONTACT_LIST",
        "MULTI_PICKLIST",
    }
)
# Confirmed exact (smartsheet/models/enums/system_column_type.py).
_SYSTEM_COLUMN_TYPES = frozenset(
    {"AUTO_NUMBER", "MODIFIED_DATE", "MODIFIED_BY", "CREATED_DATE", "CREATED_BY"}
)
# Dates/checkboxes/durations/predecessors never carry free text worth
# graphing on their own -- never text-bearing regardless of content.
_NEVER_TEXT_BEARING_TYPES = frozenset(
    {"DATE", "DATETIME", "ABSTRACT_DATETIME", "CHECKBOX", "DURATION", "PREDECESSOR"}
)

_MAX_PARENT_DEPTH = 5
_MAX_CHILDREN_SHOWN = 5
_MAX_SUMMARY_ROWS = 50
_MAX_SUMMARY_OPTIONS = 10
_MAX_COMMENTS_PER_ROW = 20
_MAX_SHEET_COMMENTS = 20
_MAX_COMMENT_CHARS = 1000
_MAX_TITLE_CHARS = 120

# ---------------------------------------------------------------------------
# Pagination, retries, limits
# ---------------------------------------------------------------------------

# Legacy page/pageSize page size, for List Sheets and the discussions listing
# endpoint -- confirmed still in use post-migration (see module docstring).
_PAGE_SIZE = 100
# Get Sheet's own row-paging page size. The maximum accepted pageSize for
# this specific call was not found in STEP 0 research; 500 is a conservative
# default, not a confirmed API maximum -- see README "Limitations".
_SHEET_ROW_PAGE_SIZE = 500
# maxItems for the new cursor-paginated /workspaces/{id}/children and
# /folders/{id}/children endpoints.
_CHILDREN_PAGE_SIZE = 100
_MAX_PAGINATION_PAGES = 1000
_MAX_FOLDER_DEPTH = 10

_MAX_RETRIES = 5
_RATE_LIMIT_MAX_RETRIES = 5
# Smartsheet's own guidance (content-center "rate-limits-retry-logic-and-
# error-handling-with-smartsheet-api") recommends a flat minimum 60s sleep
# plus jitter on 429 when no Retry-After header is present; a core-API
# Retry-After header was not confirmed to exist (see README).
_RATE_LIMIT_COOLDOWN_SECONDS = 60

# Confirmed exact (developers.smartsheet.com/api/smartsheet/guides/basics/
# http-and-rest): "INTEGRATION-TYPE,SMAR-ORGANIZATION-NAME,INTEGRATOR-NAME".
# Optional; sent so Smartsheet can attribute traffic to this connector.
_INTEGRATION_SOURCE_HEADER = "smartsheet-integration-source"
_INTEGRATION_SOURCE_VALUE = "APPLICATION,cognee-community,smartsheet-connector"


# Bump whenever row/summary rendering changes materially enough that stored
# documents should be rebuilt on the next run even if a sheet's `version`
# did not change (cognee's content-hash-keyed data_id already makes
# re-emitting unchanged content a no-op downstream, so this is a courtesy
# "rebuild soon" nudge, not a correctness requirement).
_RENDER_VERSION = 2

# Attachment-text feature: types treated as "small enough and text enough to
# be worth reading", and caps so one sheet can't balloon a sync.
_TEXT_ATTACHMENT_MIME_TYPES = frozenset(
    {"text/plain", "text/csv", "text/markdown", "text/x-markdown"}
)
_TEXT_ATTACHMENT_EXTENSIONS = frozenset({".txt", ".csv", ".md", ".markdown"})
_MAX_TEXT_ATTACHMENTS_PER_ROW = 5
_MAX_ATTACHMENT_TEXT_CHARS = 2000

# Mass-delete guard thresholds (see `_check_mass_delete_guards`): refuse to
# tombstone more than half of previously-stored documents in one pass,
# unless there are fewer than this many candidates to begin with.
_MASS_DELETE_MIN_COUNT = 20
_MASS_DELETE_RATIO = 0.5


@dataclass(frozen=True)
class _Config:
    base_url: str
    region: str
    sheet_ids: tuple[int, ...] | None
    folder_ids: tuple[int, ...] | None
    workspace_ids: tuple[int, ...] | None
    all_sheets: bool
    include_columns: tuple[str, ...] | None
    exclude_columns: tuple[str, ...] | None
    include_comments: bool
    include_attachments: bool
    contact_include_email: bool
    include_all_rows: bool
    max_rows_per_sheet: int
    max_sheets: int | None
    timeout: float
    full_sync_every: int = 20
    recheck_sheets_per_run: int = 1
    allow_mass_delete: bool = False
    attachment_text: bool = False
    attachment_max_bytes: int = 100_000


@dataclass(frozen=True)
class _ColumnInfo:
    id: int
    title: str
    type: str
    primary: bool
    is_system: bool
    included: bool


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def smartsheet_source(
    api_token: str | None = None,
    *,
    host: str | None = None,
    sheet_ids: list[int] | None = None,
    folder_ids: list[int] | None = None,
    workspace_ids: list[int] | None = None,
    all_sheets: bool = False,
    include_columns: list[str] | None = None,
    exclude_columns: list[str] | None = None,
    include_comments: bool = True,
    include_attachments: bool = True,
    contact_include_email: bool = False,
    include_all_rows: bool = False,
    max_rows_per_sheet: int = 2000,
    max_sheets: int | None = None,
    timeout: float = 60.0,
    full_sync_every: int = 20,
    recheck_sheets_per_run: int = 1,
    allow_mass_delete: bool = False,
    attachment_text: bool = False,
    attachment_max_bytes: int = 100_000,
    resource_name: str = _DEFAULT_RESOURCE_NAME,
    check_active: "Any" = None,
    session: Any = None,
):
    """Return a ``dlt`` resource yielding one document per sheet and per
    content-bearing row.

    Hand the result to ``cognee.remember(...)``/``cognee.add(...)`` with
    ``write_disposition="merge"``, ``primary_key="id"``, and
    ``max_rows_per_table=0``.

    Args:
        api_token: A Smartsheet personal access token. Falls back to
            ``SMARTSHEET_API_TOKEN``. Requires a paid Business, Enterprise,
            or Advanced Work Management plan -- the free trial and Pro plan
            cannot generate tokens.
        host: Regional API host, e.g. ``https://api.smartsheet.eu``. Falls
            back to ``SMARTSHEET_HOST``, then ``https://api.smartsheet.com``
            (US). Tokens are region-specific; the wrong host fails auth, it
            does not redirect.
        sheet_ids: Specific sheet ids to sync.
        folder_ids: Sync every sheet found in these folders, recursing into
            subfolders.
        workspace_ids: Sync every sheet found in these workspaces, recursing
            into their folders.
        all_sheets: Sync every sheet the token's user can see. Requires
            explicit opt-in; at least one of ``sheet_ids``, ``folder_ids``,
            ``workspace_ids``, or ``all_sheets=True`` is required.
        include_columns: Only these column titles (case-insensitive) become
            row content. Mutually exclusive with ``exclude_columns``.
        exclude_columns: These column titles (case-insensitive) are dropped
            from row content. Mutually exclusive with ``include_columns``.
        include_comments: Include row and sheet-level comments.
        include_attachments: Include attachment *metadata* (name, type,
            size) -- never the file content or the short-lived download URL.
        contact_include_email: Render a contact's email alongside their name.
            Off by default: emails are personal data, name alone is usually
            enough context.
        include_all_rows: Keep every row, including rows with no text-bearing
            cell, no comments, and no attachments (by default those are
            skipped as noise; see ``_row_qualifies``).
        max_rows_per_sheet: Refuse to sync a sheet with more rows than this,
            rather than silently truncating it (a partial listing must never
            be mistaken for "the rest were deleted" once Phase 2 adds
            forget-on-delete).
        max_sheets: Refuse to sync more than this many resolved sheets.
        timeout: Per-request timeout in seconds.
        full_sync_every: Force a full re-fetch of every resolved sheet every
            this many runs, even if a sheet's version looks unchanged (a
            periodic safety net; 0 disables it).
        recheck_sheets_per_run: On an incremental run, also force a full
            re-fetch of this many sheets on a rotating basis, regardless of
            their version -- a safety net for changes that might not bump
            `version` (e.g. row deletion is not confirmed to; see the
            README). 0 disables it.
        allow_mass_delete: Bypass both mass-delete guards (an empty resolved
            selection that would forget every previously-synced sheet, and
            tombstoning more than half of previously-stored documents in one
            pass). Off by default -- a 404 from Smartsheet is confirmed
            ambiguous between "deleted" and "access revoked" (see the
            README), so a sudden wave of tombstones is more likely a
            permissions problem than mass deletion.
        attachment_text: Fetch and ingest the plain-text content of small
            text/CSV/Markdown attachments (opt-in; everything else, and
            every attachment when this is False, stays metadata only).
        attachment_max_bytes: Per-attachment size cap for `attachment_text`;
            content beyond this is truncated, not skipped.
        resource_name: Stable dlt resource name. Give each account its own
            name when several sync into one dataset, so their state stays
            separate.
        check_active: Optional host authorization checkpoint during
            extraction (see ``cognee.tasks.ingestion.dlt_utils.guarded_rows``).
        session: A pre-built, already-authenticated session, mainly for
            tests.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Smartsheet sync requires a Cognee build with table-scoped DLT document "
            "cleanup. Upgrade Cognee before syncing to avoid deleting another source's data."
        )

    token = _resolve_api_token(api_token)
    base_url = _normalize_host(host)
    region = _region_key(base_url)

    sheet_ids_t = _validate_ids("sheet_ids", sheet_ids)
    folder_ids_t = _validate_ids("folder_ids", folder_ids)
    workspace_ids_t = _validate_ids("workspace_ids", workspace_ids)
    if not (sheet_ids_t or folder_ids_t or workspace_ids_t or all_sheets):
        raise ValueError(
            "Pass at least one of sheet_ids, folder_ids, workspace_ids, or all_sheets=True."
        )
    if include_columns and exclude_columns:
        raise ValueError("Pass only one of include_columns or exclude_columns, not both.")
    if max_rows_per_sheet < 1:
        raise ValueError("max_rows_per_sheet must be >= 1.")
    if max_sheets is not None and max_sheets < 1:
        raise ValueError("max_sheets must be >= 1 if set.")
    if timeout <= 0:
        raise ValueError("timeout must be > 0.")
    if full_sync_every < 0:
        raise ValueError("full_sync_every must be >= 0 (0 disables the periodic full pass).")
    if recheck_sheets_per_run < 0:
        raise ValueError("recheck_sheets_per_run must be >= 0 (0 disables the rotating re-check).")
    if attachment_max_bytes < 1:
        raise ValueError("attachment_max_bytes must be >= 1.")

    config = _Config(
        base_url=base_url,
        region=region,
        sheet_ids=sheet_ids_t,
        folder_ids=folder_ids_t,
        workspace_ids=workspace_ids_t,
        all_sheets=all_sheets,
        include_columns=tuple(c.strip().lower() for c in include_columns)
        if include_columns
        else None,
        exclude_columns=tuple(c.strip().lower() for c in exclude_columns)
        if exclude_columns
        else None,
        include_comments=include_comments,
        include_attachments=include_attachments,
        contact_include_email=contact_include_email,
        include_all_rows=include_all_rows,
        max_rows_per_sheet=max_rows_per_sheet,
        max_sheets=max_sheets,
        timeout=timeout,
        full_sync_every=full_sync_every,
        recheck_sheets_per_run=recheck_sheets_per_run,
        allow_mass_delete=allow_mass_delete,
        attachment_text=attachment_text,
        attachment_max_bytes=attachment_max_bytes,
    )

    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        write_disposition="merge",
        primary_key="id",
        # `_deleted` is a boolean hard-delete marker: rows where it is True
        # are removed from the dlt destination on merge, which propagates
        # the deletion through cognee's orphan_cleanup -- this is how
        # forget-on-delete reaches the graph.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def smartsheet_documents():
        active_session = session if session is not None else _make_session(token)
        yield from dlt_utils.guarded_rows(
            _iter_rows(active_session, config, dlt.current.resource_state(), stats),
            check_active,
        )

    resource = smartsheet_documents()
    # Opt into the document ingestion path: each sheet/row becomes a text
    # document that flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, SMARTSHEET_SOURCE_NAME)
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics contain counts only, never sheet/row content.
    resource.cognee_sync_stats = stats
    return resource


def _resolve_api_token(api_token: str | None) -> str:
    token = api_token or os.getenv("SMARTSHEET_API_TOKEN")
    if not token:
        raise ValueError(
            f"Smartsheet API token is missing. Pass api_token=... or set "
            f"SMARTSHEET_API_TOKEN. {_TOKEN_HINT}"
        )
    return token


def _normalize_host(host: str | None) -> str:
    host = host or os.getenv("SMARTSHEET_HOST") or _DEFAULT_HOST
    parsed = urlsplit(host)
    if parsed.scheme != "https":
        raise ValueError(f"host must use https://, got: {host!r}")
    if not parsed.hostname:
        raise ValueError(f"host must include a hostname, got: {host!r}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"host must not include credentials, a query, or a fragment: {host!r}")
    path = parsed.path.rstrip("/")
    if path not in ("", _API_PATH):
        raise ValueError(f"host must not include a path other than {_API_PATH}: {host!r}")
    return f"https://{parsed.netloc}{_API_PATH}"


# Confirmed exact hosts: developers.smartsheet.com/api/smartsheet/guides/
# basics/base-url and help.smartsheet.com/articles/2482446.
def _region_key(base_url: str) -> str:
    hostname = (urlsplit(base_url).hostname or "").lower()
    if hostname == "api.smartsheet.com":
        return "com"
    if hostname == "api.smartsheet.eu":
        return "eu"
    if hostname == "api.smartsheet.au":
        return "au"
    if hostname == "api.smartsheetgov.com":
        return "gov"
    return re.sub(r"[^a-z0-9]+", "-", hostname).strip("-") or "unknown"


def _validate_ids(name: str, ids: list[int] | None) -> tuple[int, ...] | None:
    if ids is None:
        return None
    cleaned = []
    for value in ids:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a list of positive integers, got {value!r}.")
        cleaned.append(value)
    if not cleaned:
        raise ValueError(f"{name} must not be empty if provided; omit it instead.")
    return tuple(dict.fromkeys(cleaned))


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
def _make_session(token: str) -> Any:
    try:
        import requests
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    session.headers["Accept"] = "application/json"
    session.headers[_INTEGRATION_SOURCE_HEADER] = _INTEGRATION_SOURCE_VALUE
    return session


def _sleep(seconds: float) -> None:
    """Thin wrapper around ``time.sleep`` so tests can monkeypatch it to run instantly."""
    time.sleep(seconds)


def _retry_delay(attempt: int) -> float:
    """Exponential backoff with jitter for transient 5xx retries."""
    return (2**attempt) + random.random()


class _SmartsheetAPIError(RuntimeError):
    """A non-2xx Smartsheet API response, carrying the parsed status/error code.

    Subclasses ``RuntimeError`` so every existing ``pytest.raises(RuntimeError,
    ...)`` still matches. Call sites that need to tell "not found" apart from
    other failures (forget-on-delete's 404 check) read ``.status_code``
    instead of parsing the message string.
    """

    def __init__(self, message: str, *, status_code: int, error_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


def _get_json(session: Any, url: str, params: dict, timeout: float) -> dict:
    """GET a Smartsheet API URL and return the parsed JSON body.

    401/403/404 raise with the upstream ``errorCode``/``message``/``refId``
    (confirmed error envelope shape, ``smartsheet/models/error_result.py``)
    folded into the message, never echoing the token. 429 waits a flat
    cooldown (``Retry-After`` is honored if present, though a core-API
    Retry-After header was not confirmed to exist -- see module docstring)
    and retries a bounded number of times. 5xx retries with exponential
    backoff and jitter. A connection/DNS/TLS failure raises hinting at the
    host/region. Nothing is ever swallowed.
    """
    server_error_attempts = 0
    rate_limit_attempts = 0
    while True:
        try:
            response = session.get(url, params=params, timeout=timeout)
        except Exception as exc:
            raise RuntimeError(
                f"Smartsheet API request to {url} failed to connect: {exc}. Check that `host` "
                "points at the correct region (api.smartsheet.com / .eu / .au / "
                "api.smartsheetgov.com)."
            ) from exc

        status = response.status_code
        if status == 200:
            return response.json()

        try:
            body = response.json()
        except ValueError:
            body = {}
        detail = (
            f"errorCode={body.get('errorCode')} message={body.get('message')!r} "
            f"refId={body.get('refId')}"
        )

        error_code = body.get("errorCode") if isinstance(body, dict) else None
        if status == 401:
            raise _SmartsheetAPIError(
                f"Smartsheet API authentication failed (401, {detail}). The token is invalid or "
                f"expired. {_TOKEN_HINT}",
                status_code=401,
                error_code=error_code,
            )
        if status == 403:
            raise _SmartsheetAPIError(
                f"Smartsheet API authorization failed (403, {detail}) for {url}. The token's "
                "user lacks access to this sheet, folder, or workspace.",
                status_code=403,
                error_code=error_code,
            )
        if status == 404:
            raise _SmartsheetAPIError(
                f"Smartsheet API resource not found (404, {detail}): {url}. Note: Smartsheet "
                "returns this same error whether the resource was deleted or the token simply "
                "lost access to it -- see the README.",
                status_code=404,
                error_code=error_code,
            )
        if status == 410:
            raise _SmartsheetAPIError(
                f"Smartsheet API resource gone (410, {detail}): {url}",
                status_code=410,
                error_code=error_code,
            )

        if status == 429:
            rate_limit_attempts += 1
            if rate_limit_attempts > _RATE_LIMIT_MAX_RETRIES:
                raise _SmartsheetAPIError(
                    f"Smartsheet API rate limit exceeded for {url} after "
                    f"{_RATE_LIMIT_MAX_RETRIES} retries ({detail}).",
                    status_code=429,
                    error_code=error_code,
                )
            retry_after = response.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after
                else _RATE_LIMIT_COOLDOWN_SECONDS + random.random()
            )
            logger.warning("Smartsheet: %s rate-limited (429), waiting %.1fs.", url, delay)
            _sleep(delay)
            continue

        if status in (500, 502, 503, 504):
            server_error_attempts += 1
            if server_error_attempts >= _MAX_RETRIES:
                raise _SmartsheetAPIError(
                    f"Smartsheet API request to {url} failed after {_MAX_RETRIES} attempts "
                    f"(last status {status}, {detail}).",
                    status_code=status,
                    error_code=error_code,
                )
            delay = _retry_delay(server_error_attempts - 1)
            logger.warning("Smartsheet: %s returned %d, retrying in %.1fs.", url, status, delay)
            _sleep(delay)
            continue

        raise _SmartsheetAPIError(
            f"Unexpected Smartsheet API response from {url}: status {status} ({detail}).",
            status_code=status,
            error_code=error_code,
        )


def _paginate_offset(session: Any, url: str, params: dict, timeout: float):
    """Legacy ``page``/``pageSize`` pagination.

    Confirmed still in use (NOT migrated to the newer ``lastKey``/``maxItems``
    cursor system -- see module docstring) for List Sheets and the
    discussions-listing endpoint, via ``smartsheet/smartsheet-python-sdk``
    source at commit ``a554eaf`` (2026-10-12): ``Sheets.list_sheets`` and
    ``Discussions.get_all_discussions`` still send only ``page``/
    ``pageSize``/``includeAll``.
    """
    params = dict(params or {})
    params["pageSize"] = _PAGE_SIZE
    page = 1
    for _page in range(_MAX_PAGINATION_PAGES):
        params["page"] = page
        payload = _get_json(session, url, params, timeout)
        items = payload.get("data") or []
        if not items:
            return
        yield from items
        total_pages = payload.get("totalPages")
        if isinstance(total_pages, int):
            if page >= total_pages:
                return
        elif len(items) < _PAGE_SIZE:
            return  # totalPages absent/malformed: a short page means "last page"
        page += 1
    raise RuntimeError(f"Smartsheet pagination exceeded {_MAX_PAGINATION_PAGES} pages at {url}.")


def _paginate_cursor(session: Any, url: str, timeout: float):
    """Cursor pagination (``lastKey``/``maxItems``).

    Confirmed current for exactly ``GET /workspaces/{id}/children`` and
    ``GET /folders/{id}/children`` (which replaced the old ``GET
    /workspaces/{id}`` / ``GET /folders/{id}``) -- see module docstring.
    Absence of ``lastKey`` in the response means no more pages.
    """
    params = {"childrenResourceTypes": "sheets,folders", "maxItems": _CHILDREN_PAGE_SIZE}
    last_key = None
    seen_keys: set[str] = set()
    for _page in range(_MAX_PAGINATION_PAGES):
        if last_key is not None:
            params["lastKey"] = last_key
        payload = _get_json(session, url, params, timeout)
        items = payload.get("data") or []
        yield from items
        next_key = payload.get("lastKey")
        if not next_key:
            return
        if next_key in seen_keys:
            raise RuntimeError(f"Smartsheet pagination returned a repeated cursor at {url}.")
        seen_keys.add(next_key)
        last_key = next_key
    raise RuntimeError(f"Smartsheet pagination exceeded {_MAX_PAGINATION_PAGES} pages at {url}.")


# ---------------------------------------------------------------------------
# Selection resolution
# ---------------------------------------------------------------------------
def _resolve_sheet_ids(session: Any, config: _Config) -> dict[int, str]:
    """Resolve the configured selection into {sheet_id: best-known name}.

    An explicit sheet id's real name is unknown until it is actually
    fetched (List Sheets is not called just to look up a name for an id the
    caller already gave us); its placeholder name here is only used for the
    one "resolved sheets" log line, never for rendering.
    """
    found: dict[int, str] = {}

    def add_sheet(sheet_id: int, name: str) -> None:
        found.setdefault(sheet_id, name)

    if config.all_sheets:
        url = f"{config.base_url}/sheets"
        for entry in _paginate_offset(session, url, {}, config.timeout):
            add_sheet(int(entry["id"]), entry.get("name") or str(entry["id"]))

    for sheet_id in config.sheet_ids or ():
        add_sheet(sheet_id, str(sheet_id))

    for folder_id in config.folder_ids or ():
        _collect_folder_children(session, config, folder_id, add_sheet, depth=0)

    for workspace_id in config.workspace_ids or ():
        _collect_workspace_children(session, config, workspace_id, add_sheet, depth=0)

    return dict(sorted(found.items()))


def _collect_folder_children(
    session: Any, config: _Config, folder_id: int, add_sheet, depth: int
) -> None:
    if depth > _MAX_FOLDER_DEPTH:
        raise RuntimeError(f"Folder {folder_id} nesting exceeds depth cap of {_MAX_FOLDER_DEPTH}.")
    url = f"{config.base_url}/folders/{folder_id}/children"
    for entry in _paginate_cursor(session, url, config.timeout):
        # Confirmed field: `resourceType`, lowercase values ("sheet", "folder", ...)
        # -- smartsheet/models/paginated_children_result.py, append_data().
        kind = str(entry.get("resourceType") or "").lower()
        if kind == "sheet":
            add_sheet(int(entry["id"]), entry.get("name") or str(entry["id"]))
        elif kind == "folder":
            _collect_folder_children(session, config, int(entry["id"]), add_sheet, depth + 1)


def _collect_workspace_children(
    session: Any, config: _Config, workspace_id: int, add_sheet, depth: int
) -> None:
    url = f"{config.base_url}/workspaces/{workspace_id}/children"
    for entry in _paginate_cursor(session, url, config.timeout):
        kind = str(entry.get("resourceType") or "").lower()
        if kind == "sheet":
            add_sheet(int(entry["id"]), entry.get("name") or str(entry["id"]))
        elif kind == "folder":
            _collect_folder_children(session, config, int(entry["id"]), add_sheet, depth + 1)


def _config_fingerprint(config: _Config) -> str:
    """A stable fingerprint of everything that affects *what* gets synced or
    *how* it is rendered. A changed fingerprint forces a full pass next run
    -- selection/rendering changes should not have to wait for a periodic
    full pass or a version bump to be reflected.
    """
    import hashlib

    relevant = (
        config.base_url,
        config.sheet_ids,
        config.folder_ids,
        config.workspace_ids,
        config.all_sheets,
        config.include_columns,
        config.exclude_columns,
        config.include_comments,
        config.include_attachments,
        config.contact_include_email,
        config.include_all_rows,
        config.attachment_text,
        config.attachment_max_bytes,
    )
    return hashlib.sha256(repr(relevant).encode()).hexdigest()


def _check_mass_delete_guards(
    config: _Config,
    removed_doc_ids: set[str],
    total_prior_docs: int,
    resolved_sheet_count: int,
    had_any_prior_sheets: bool,
) -> None:
    """Refuse to tombstone too much in one pass unless explicitly allowed.

    Smartsheet's 404 is confirmed ambiguous between "deleted" and "access
    revoked" (see the README), so a wave of tombstones is just as likely to
    be a permissions problem as real deletions. Two guards:

    1. The resolved selection came back empty, but there is prior state --
       this is almost always a selection/config mistake (e.g. an unset env
       var), not every sheet having been deleted at once.
    2. More than half of previously-stored documents (and at least
       ``_MASS_DELETE_MIN_COUNT``) would be tombstoned in one pass.
    """
    if not removed_doc_ids or config.allow_mass_delete:
        return
    if resolved_sheet_count == 0 and had_any_prior_sheets:
        raise RuntimeError(
            f"Smartsheet: the resolved selection is empty, which would forget all "
            f"{total_prior_docs} previously-synced document(s). This is usually a "
            "selection/config mistake (e.g. an unset sheet_ids/folder_ids/workspace_ids). "
            "Pass allow_mass_delete=True if this is intentional."
        )
    if len(removed_doc_ids) >= _MASS_DELETE_MIN_COUNT and len(removed_doc_ids) > (
        total_prior_docs * _MASS_DELETE_RATIO
    ):
        raise RuntimeError(
            f"Smartsheet: this pass would tombstone {len(removed_doc_ids)} of "
            f"{total_prior_docs} previously-synced document(s) (> "
            f"{_MASS_DELETE_RATIO:.0%}). Refusing in case this is a bulk access-loss event "
            "rather than real deletions -- Smartsheet returns the same 404 for both (see "
            "the README). Pass allow_mass_delete=True if this is intentional."
        )


# ---------------------------------------------------------------------------
# Sync state machine (pure given a session + state dict -- unit-testable)
# ---------------------------------------------------------------------------
def _iter_rows(session: Any, config: _Config, state: dict, stats: dict | None = None):
    """Yield one document per sheet summary and per qualifying row, plus
    tombstones for anything that should be forgotten.

    ``state`` is a plain dict standing in for dlt's resource state:
    ``{"render_version", "config_fingerprint", "run_count", "recheck_cursor",
    "sheets": {"<sheet_id>": {"version": int, "doc_ids": [str, ...]}}}``.
    Every API call and every tombstone/guard decision happens before the
    first yield, and ``state`` is only mutated after everything downstream
    of it has already been computed -- a failure partway through (a bad
    sheet id, a network error, a tripped guard) leaves both the emitted
    documents and the stored state exactly as they were before this call.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(sheets=0, rows=0, skipped_rows=0)

    prior_sheet_state: dict[str, dict] = dict(state.get("sheets") or {})
    run_count = state.get("run_count", 0)
    config_fp = _config_fingerprint(config)
    full_pass = (
        not state.get("initialized")
        or state.get("config_fingerprint") != config_fp
        or state.get("render_version") != _RENDER_VERSION
        or (
            config.full_sync_every > 0 and run_count > 0 and run_count % config.full_sync_every == 0
        )
    )

    sheet_ids = _resolve_sheet_ids(session, config)
    if config.max_sheets is not None and len(sheet_ids) > config.max_sheets:
        raise RuntimeError(
            f"Resolved {len(sheet_ids)} sheet(s), exceeding max_sheets={config.max_sheets}. "
            "Narrow your selection or raise max_sheets."
        )
    logger.info(
        "Smartsheet: resolved %d sheet(s) (full_pass=%s): %s",
        len(sheet_ids),
        full_pass,
        ", ".join(f"{sid}:{name}" for sid, name in sheet_ids.items()) or "(none)",
    )
    if not sheet_ids:
        logger.warning("Smartsheet: resolved selection is empty; nothing to sync this pass.")

    recheck_ids: set[int] = set()
    next_recheck_cursor = state.get("recheck_cursor", 0)
    if not full_pass and config.recheck_sheets_per_run > 0:
        known_ids = sorted(sid for sid in sheet_ids if str(sid) in prior_sheet_state)
        if known_ids:
            cursor = next_recheck_cursor % len(known_ids)
            rotated = known_ids[cursor:] + known_ids[:cursor]
            recheck_ids = set(rotated[: config.recheck_sheets_per_run])
            next_recheck_cursor = cursor + config.recheck_sheets_per_run

    documents: list[dict] = []
    new_sheet_state: dict[str, dict] = {}
    removed_doc_ids: set[str] = set()
    seen_sheet_ids: set[int] = set()

    for sheet_id in sheet_ids:
        key = str(sheet_id)
        seen_sheet_ids.add(sheet_id)
        prior = prior_sheet_state.get(key)
        force = full_pass or prior is None or sheet_id in recheck_ids

        known_version = None
        if not force:
            try:
                new_version = _fetch_sheet_version(session, config, sheet_id)
            except _SmartsheetAPIError as exc:
                if exc.status_code == 404:
                    removed_doc_ids.update(prior.get("doc_ids", []))
                    logger.warning("Smartsheet: sheet %d is gone (404); forgetting it.", sheet_id)
                    continue
                raise
            if new_version == prior["version"]:
                new_sheet_state[key] = prior
                continue
            known_version = new_version

        try:
            docs, version = _process_sheet(session, config, sheet_id, stats, known_version)
        except _SmartsheetAPIError as exc:
            if exc.status_code == 404:
                if prior is not None:
                    removed_doc_ids.update(prior.get("doc_ids", []))
                    logger.warning("Smartsheet: sheet %d is gone (404); forgetting it.", sheet_id)
                else:
                    logger.warning(
                        "Smartsheet: sheet %d returned 404 and was never previously synced; "
                        "skipping it this pass (check the id and the token's access).",
                        sheet_id,
                    )
                continue
            raise

        doc_ids = [doc["id"] for doc in docs]
        old_ids = set(prior.get("doc_ids", [])) if prior else set()
        removed_doc_ids.update(old_ids - set(doc_ids))
        documents.extend(docs)
        new_sheet_state[key] = {"version": version, "doc_ids": doc_ids}
        stats["sheets"] += 1

    # Sheets known from a prior run that are no longer in the resolved
    # selection at all (deleted, unshared, or simply scoped out) are
    # forgotten too -- no extra API call needed, this is a plain diff.
    for key, prior in prior_sheet_state.items():
        if int(key) in seen_sheet_ids:
            continue
        removed_doc_ids.update(prior.get("doc_ids", []))

    total_prior_docs = sum(len(v.get("doc_ids", [])) for v in prior_sheet_state.values())
    _check_mass_delete_guards(
        config,
        removed_doc_ids,
        total_prior_docs,
        resolved_sheet_count=len(sheet_ids),
        had_any_prior_sheets=bool(prior_sheet_state),
    )

    tombstones = [{"id": doc_id, "_deleted": True} for doc_id in sorted(removed_doc_ids)]
    stats["forgotten"] = len(tombstones)

    yield from tombstones
    yield from documents

    state["sheets"] = new_sheet_state
    state["run_count"] = run_count + 1
    state["recheck_cursor"] = next_recheck_cursor
    state["config_fingerprint"] = config_fp
    state["render_version"] = _RENDER_VERSION
    state["initialized"] = True
    logger.info("Smartsheet: sync stats %s", stats)


def _sheet_include_flags(config: _Config) -> list[str]:
    include_flags = ["columnType", "objectValue", "rowPermalink"]
    if config.include_comments:
        include_flags.append("discussions")
    if config.include_attachments:
        include_flags.append("attachments")
    return include_flags


def _sync_sheet(session: Any, config: _Config, sheet_id: int, stats: dict) -> list[dict]:
    sheet = _fetch_sheet(session, config, sheet_id, _sheet_include_flags(config))
    _enrich_sheet_attachments_with_text(session, config, sheet_id, sheet.get("rows") or [])

    sheet_comments: list[dict] = []
    if config.include_comments:
        sheet_comments = _fetch_sheet_level_comments(session, config, sheet_id)

    return _build_sheet_documents(config, sheet, sheet_id, sheet_comments, stats)


def _process_sheet(
    session: Any, config: _Config, sheet_id: int, stats: dict, known_version: int | None = None
) -> tuple[list[dict], int]:
    """Full fetch + render for one sheet, also surfacing its current
    ``version`` for the sync state.

    A thin sibling of ``_sync_sheet`` rather than a replacement for it:
    ``_sync_sheet``'s signature and return value stay exactly as Phase 1
    left them (the existing rendering tests call it directly), while this
    is what the Phase 2 incremental orchestration in ``_iter_rows`` calls.
    ``known_version`` lets a caller that already fetched the version (to
    decide whether to re-fetch at all) avoid asking twice.
    """
    sheet = _fetch_sheet(session, config, sheet_id, _sheet_include_flags(config))
    _enrich_sheet_attachments_with_text(session, config, sheet_id, sheet.get("rows") or [])

    sheet_comments: list[dict] = []
    if config.include_comments:
        sheet_comments = _fetch_sheet_level_comments(session, config, sheet_id)

    documents = _build_sheet_documents(config, sheet, sheet_id, sheet_comments, stats)
    version = (
        known_version
        if known_version is not None
        else _fetch_sheet_version(session, config, sheet_id)
    )
    return documents, version


def _build_sheet_documents(
    config: _Config, sheet: dict, sheet_id: int, sheet_comments: list[dict], stats: dict
) -> list[dict]:
    columns = sheet.get("columns") or []
    raw_rows = sheet.get("rows") or []
    column_index = _build_column_index(columns, config)

    parent_map, children_map = _build_hierarchy(raw_rows)
    rows_by_id = {int(row["id"]): row for row in raw_rows}

    documents = []
    kept = 0
    for row in sorted(raw_rows, key=lambda r: r.get("rowNumber", 0)):
        doc = _render_row(
            config, sheet, sheet_id, row, column_index, parent_map, children_map, rows_by_id
        )
        if doc is None:
            stats["skipped_rows"] = stats.get("skipped_rows", 0) + 1
            continue
        documents.append(doc)
        kept += 1

    documents.append(
        _render_sheet_summary(
            config, sheet, sheet_id, columns, column_index, raw_rows, sheet_comments, kept
        )
    )
    stats["rows"] = stats.get("rows", 0) + kept
    return documents


def _fetch_sheet(session: Any, config: _Config, sheet_id: int, include_flags: list[str]) -> dict:
    """Fetch a sheet in full via Get Sheet's own (legacy, not cursor) row-paging.

    Checks ``totalRowCount`` against ``max_rows_per_sheet`` as soon as the
    first page is in, before fetching the rest -- a huge sheet is refused
    cheaply, not partially fetched then discarded.
    """
    url = f"{config.base_url}/sheets/{sheet_id}"
    page = 1
    sheet: dict | None = None
    all_rows: list[dict] = []
    for _page in range(_MAX_PAGINATION_PAGES):
        params = {
            "include": ",".join(include_flags),
            "page": page,
            "pageSize": _SHEET_ROW_PAGE_SIZE,
        }
        payload = _get_json(session, url, params, config.timeout)
        if sheet is None:
            sheet = payload
            total_row_count = payload.get("totalRowCount", len(payload.get("rows") or []))
            if total_row_count > config.max_rows_per_sheet:
                raise RuntimeError(
                    f"Sheet {sheet_id} ({payload.get('name', sheet_id)!r}) has "
                    f"{total_row_count} rows, exceeding max_rows_per_sheet="
                    f"{config.max_rows_per_sheet}. Narrow your selection, raise "
                    "max_rows_per_sheet, or exclude this sheet."
                )
        rows = payload.get("rows") or []
        all_rows.extend(rows)
        total_pages = payload.get("totalPages")
        if not rows or not isinstance(total_pages, int) or page >= total_pages:
            break
        page += 1
    else:
        raise RuntimeError(
            f"Sheet {sheet_id} row pagination exceeded {_MAX_PAGINATION_PAGES} pages."
        )

    if sheet.get("totalRowCount") is not None and len(all_rows) != sheet["totalRowCount"]:
        raise RuntimeError(
            f"Sheet {sheet_id} ({sheet.get('name', sheet_id)!r}) reported "
            f"totalRowCount={sheet['totalRowCount']} but pagination returned "
            f"{len(all_rows)} row(s). Refusing to treat the difference as deleted rows."
        )
    sheet["rows"] = all_rows
    return sheet


def _fetch_sheet_version(session: Any, config: _Config, sheet_id: int) -> int:
    """Cheap per-sheet change marker (confirmed: ``GET /sheets/{id}/version``
    returns ``{"version": N}``). Used to skip a full re-fetch when nothing
    has changed; raises ``_SmartsheetAPIError`` (404 included) like any other
    Smartsheet call -- callers decide what a 404 here means.
    """
    url = f"{config.base_url}/sheets/{sheet_id}/version"
    payload = _get_json(session, url, {}, config.timeout)
    return int(payload["version"])


def _fetch_sheet_level_comments(session: Any, config: _Config, sheet_id: int) -> list[dict]:
    """Comments not tied to any row.

    Confirmed: ``GET /sheets/{id}/discussions`` returns both sheet- and
    row-level discussions together, distinguished by ``parentType``
    (``smartsheet/models/discussion.py``). Row-level ones are skipped here
    since they are already attached to their row via Get Sheet's
    ``include=discussions``.
    """
    url = f"{config.base_url}/sheets/{sheet_id}/discussions"
    comments: list[dict] = []
    for discussion in _paginate_offset(session, url, {}, config.timeout):
        if str(discussion.get("parentType") or "").upper() == "ROW":
            continue
        comments.extend(discussion.get("comments") or [])
    return comments


# ---------------------------------------------------------------------------
# Column classification
# ---------------------------------------------------------------------------
def _build_column_index(columns: list[dict], config: _Config) -> dict[int, _ColumnInfo]:
    index: dict[int, _ColumnInfo] = {}
    for col in columns:
        col_id = int(col["id"])
        title = col.get("title") or f"Column {col_id}"
        col_type = str(col.get("type") or "").upper()
        is_system = bool(col.get("systemColumnType"))
        title_key = title.strip().lower()
        if config.include_columns is not None:
            included = title_key in config.include_columns
        elif config.exclude_columns is not None:
            included = title_key not in config.exclude_columns
        else:
            included = True
        if is_system:
            included = False
        index[col_id] = _ColumnInfo(
            id=col_id,
            title=title,
            type=col_type,
            primary=bool(col.get("primary")),
            is_system=is_system,
            included=included,
        )
    return index


def _build_hierarchy(raw_rows: list[dict]) -> tuple[dict[int, int], dict[int, list[int]]]:
    parent_map: dict[int, int] = {}
    children_map: dict[int, list[int]] = {}
    for row in raw_rows:
        parent_id = row.get("parentId")
        if parent_id is None:
            continue
        row_id = int(row["id"])
        parent_map[row_id] = int(parent_id)
        children_map.setdefault(int(parent_id), []).append(row_id)
    for kids in children_map.values():
        kids.sort()
    return parent_map, children_map


# ---------------------------------------------------------------------------
# Cell rendering
# ---------------------------------------------------------------------------
# A TEXT_NUMBER cell whose display text is only digits/currency/percent/
# separators carries no free text worth graphing on its own.
_NUMERIC_RE = re.compile(r"^[\s$€£%,.\d+-]+$")


def _is_purely_numeric(text: str) -> bool:
    stripped = text.strip()
    return not stripped or bool(_NUMERIC_RE.fullmatch(stripped))


def _cell_is_text_bearing(cell: dict, column: _ColumnInfo) -> bool:
    if column.type in _NEVER_TEXT_BEARING_TYPES:
        return False
    if column.type in ("CONTACT_LIST", "MULTI_CONTACT_LIST"):
        return bool(_contact_entries(cell)) or bool(cell.get("displayValue"))
    if column.type == "MULTI_PICKLIST":
        return bool(_multi_picklist_values(cell))
    if column.type == "PICKLIST":
        return bool(cell.get("displayValue") or cell.get("value"))
    # TEXT_NUMBER and anything unrecognized: text-bearing only if non-empty
    # and not purely numeric.
    display = cell.get("displayValue") or cell.get("value")
    return bool(display) and not _is_purely_numeric(str(display))


def _contact_entries(cell: dict) -> list[dict]:
    """A CONTACT_LIST cell's objectValue is one contact dict; a
    MULTI_CONTACT_LIST cell's is ``{"values": [contact, ...]}`` (confirmed
    exact shapes, ``smartsheet/models/{contact_object_value,
    multi_contact_object_value}.py``). Handle both without needing to know
    which column type this cell belongs to.
    """
    object_value = cell.get("objectValue")
    if not isinstance(object_value, dict):
        return []
    values = object_value.get("values")
    entries = values if isinstance(values, list) else [object_value]
    return [e for e in entries if isinstance(e, dict) and (e.get("name") or e.get("email"))]


def _multi_picklist_values(cell: dict) -> list[str]:
    object_value = cell.get("objectValue")
    if not isinstance(object_value, dict):
        return []
    values = object_value.get("values")
    return [str(v) for v in values] if isinstance(values, list) else []


def _cell_text(cell: dict, column: _ColumnInfo, config: _Config) -> str | None:
    """Render one cell's value as text, or None if empty/unrenderable.

    Always prefers ``displayValue`` (the computed/formatted text) over
    ``value``, and never renders ``formula`` -- a formula string is
    meaningless to cognify, its computed result is what matters.
    """
    if column.type in ("CONTACT_LIST", "MULTI_CONTACT_LIST"):
        entries = _contact_entries(cell)
        if entries:
            names = []
            for entry in entries:
                name = entry.get("name") or entry.get("email")
                email = entry.get("email")
                if config.contact_include_email and email and email != name:
                    names.append(f"{name} <{email}>")
                else:
                    names.append(name)
            return ", ".join(names)
        display = cell.get("displayValue")
        return str(display) if display else None

    if column.type == "CHECKBOX":
        value = cell.get("value")
        return None if value is None else ("Yes" if value else "No")

    if column.type == "MULTI_PICKLIST":
        values = _multi_picklist_values(cell)
        if values:
            return ", ".join(values)
        display = cell.get("displayValue")
        return str(display) if display else None

    display = cell.get("displayValue")
    if display:
        return str(display)
    value = cell.get("value")
    return str(value) if value not in (None, "") else None


def _render_cell_link(cell: dict) -> str | None:
    """A short phrase for a cross-sheet cell link or an external hyperlink.

    Confirmed fields: ``linkInFromCell`` has ``sheetId``/``sheetName``;
    ``hyperlink`` has ``url``/``sheetId`` (``smartsheet/models/{cell_link,
    hyperlink}.py``).
    """
    link_in = cell.get("linkInFromCell")
    if isinstance(link_in, dict) and (link_in.get("sheetName") or link_in.get("sheetId")):
        target = link_in.get("sheetName") or f"sheet {link_in.get('sheetId')}"
        return f"linked from {target}"
    hyperlink = cell.get("hyperlink")
    if isinstance(hyperlink, dict):
        if hyperlink.get("url"):
            return f"link: {hyperlink['url']}"
        if hyperlink.get("sheetId"):
            return f"linked to sheet {hyperlink['sheetId']}"
    return None


# ---------------------------------------------------------------------------
# Row inclusion and rendering
# ---------------------------------------------------------------------------
def _row_qualifies(row: dict, column_index: dict[int, _ColumnInfo], config: _Config) -> bool:
    if config.include_all_rows:
        return True
    if config.include_comments and row.get("discussions"):
        return True
    if config.include_attachments and row.get("attachments"):
        return True
    for cell in row.get("cells") or []:
        column = column_index.get(int(cell.get("columnId", -1)))
        if column is None or column.is_system or not column.included:
            continue
        if _cell_is_text_bearing(cell, column):
            return True
    return False


def _primary_cell_text(row: dict, column_index: dict[int, _ColumnInfo]) -> str | None:
    for cell in row.get("cells") or []:
        column = column_index.get(int(cell.get("columnId", -1)))
        if column and column.primary:
            text = cell.get("displayValue") or cell.get("value")
            return str(text) if text else None
    return None


def _row_title(
    row_id: int, rows_by_id: dict[int, dict], column_index: dict[int, _ColumnInfo]
) -> str:
    row = rows_by_id.get(row_id)
    if row is None:
        return f"Row {row_id}"
    return _primary_cell_text(row, column_index) or f"Row {row.get('rowNumber', row_id)}"


def _render_parent_path(
    row_id: int, parent_map: dict[int, int], rows_by_id: dict[int, dict], column_index
) -> str:
    chain = []
    current = parent_map.get(row_id)
    depth = 0
    while current is not None and depth < _MAX_PARENT_DEPTH:
        chain.append(_row_title(current, rows_by_id, column_index))
        current = parent_map.get(current)
        depth += 1
    if not chain:
        return "Parent: none (top level)"
    return "Parent: " + " > ".join(reversed(chain))


def _render_children(
    row_id: int, children_map: dict[int, list[int]], rows_by_id: dict[int, dict], column_index
) -> str | None:
    kids = children_map.get(row_id) or []
    if not kids:
        return None
    shown = [_row_title(k, rows_by_id, column_index) for k in kids[:_MAX_CHILDREN_SHOWN]]
    text = ", ".join(shown)
    extra = len(kids) - len(shown)
    if extra > 0:
        text += f" (+{extra} more)"
    return f"Children: {len(kids)} subrow(s): {text}"


def _comment_author(comment: dict) -> str:
    created_by = comment.get("createdBy")
    if isinstance(created_by, dict):
        return created_by.get("name") or created_by.get("email") or "unknown"
    return "unknown"


def _render_comment_lines(comments: list[dict], cap: int) -> list[str]:
    ordered = sorted(comments, key=lambda c: c.get("createdAt") or "")
    shown = ordered[:cap]
    lines = []
    for comment in shown:
        author = _comment_author(comment)
        text = (comment.get("text") or "").strip()
        if len(text) > _MAX_COMMENT_CHARS:
            text = text[:_MAX_COMMENT_CHARS] + "..."
        date = (comment.get("createdAt") or "")[:10]
        lines.append(f"- {author}: {text} ({date})")
    extra = len(ordered) - len(shown)
    if extra > 0:
        lines.append(f"(+{extra} more)")
    return lines


def _render_attachment_lines(attachments: list[dict]) -> list[str]:
    lines = []
    for attachment in attachments:
        name = attachment.get("name") or "attachment"
        parts = [attachment.get("attachmentType") or "FILE"]
        if attachment.get("mimeType"):
            parts.append(attachment["mimeType"])
        if attachment.get("sizeInKb") is not None:
            parts.append(f"{attachment['sizeInKb']} KB")
        # Never the download URL: it is short-lived (urlExpiresInMillis) and
        # persisting it anywhere would be pointless and a minor leak surface.
        lines.append(f"- {name} ({', '.join(parts)})")
        text = attachment.get("_text")
        if text:
            trimmed = (
                text
                if len(text) <= _MAX_ATTACHMENT_TEXT_CHARS
                else text[:_MAX_ATTACHMENT_TEXT_CHARS] + "..."
            )
            lines.append(f"  Content: {trimmed}")
    return lines


# ---------------------------------------------------------------------------
# Attachment text (opt-in: attachment_text=True)
# ---------------------------------------------------------------------------
def _is_text_attachment(attachment: dict) -> bool:
    mime = str(attachment.get("mimeType") or "").lower()
    if mime in _TEXT_ATTACHMENT_MIME_TYPES:
        return True
    name = str(attachment.get("name") or "").lower()
    return any(name.endswith(ext) for ext in _TEXT_ATTACHMENT_EXTENSIONS)


def _download_attachment_bytes(session: Any, url: str, timeout: float) -> bytes | None:
    """GET an attachment's temporary pre-signed download URL.

    Never sends the Smartsheet bearer token here: this is a storage URL,
    not a Smartsheet API endpoint (confirmed via the actively-maintained
    SDK's own ``download_attachment``, which issues a bare ``requests.get``
    with no Authorization header at all). The caller's ``session`` is
    reused for testability, but its session-level ``Authorization`` header
    is explicitly suppressed for this one call.
    """
    response = session.get(url, timeout=timeout, headers={"Authorization": None})
    if response.status_code in (404, 410):
        return None
    response.raise_for_status()
    return response.content


def _fetch_attachment_text(
    session: Any, config: _Config, sheet_id: int, attachment: dict, max_bytes: int
) -> str | None:
    """Fetch and decode one attachment's content, or None if it has vanished.

    A 404/410 fetching the attachment's own detail, or downloading its
    pre-signed URL, is treated as "gone -- nothing to add" (attachments are
    deleted far more casually than sheets). Any other error is a real
    failure and propagates -- this never silently drops a row's content for
    an unrelated reason (a timeout, a 403, a 500).
    """
    attachment_id = attachment.get("id")
    url = f"{config.base_url}/sheets/{sheet_id}/attachments/{attachment_id}"
    try:
        detail = _get_json(session, url, {}, config.timeout)
    except _SmartsheetAPIError as exc:
        if exc.status_code in (404, 410):
            return None
        raise
    download_url = detail.get("url")
    if not download_url:
        return None
    content = _download_attachment_bytes(session, download_url, config.timeout)
    if content is None:
        return None
    if len(content) > max_bytes:
        content = content[:max_bytes]
    text = content.decode("utf-8", errors="replace").strip()
    return text or None


def _enrich_sheet_attachments_with_text(
    session: Any, config: _Config, sheet_id: int, raw_rows: list[dict]
) -> None:
    """Mutate each qualifying row's attachments in place, adding a ``_text``
    key for small text/CSV/Markdown attachments (up to
    ``_MAX_TEXT_ATTACHMENTS_PER_ROW`` per row). No-op unless both
    ``attachment_text`` and ``include_attachments`` are set.
    """
    if not (config.attachment_text and config.include_attachments):
        return
    for row in raw_rows:
        attachments = row.get("attachments") or []
        enriched = 0
        for attachment in attachments:
            if enriched >= _MAX_TEXT_ATTACHMENTS_PER_ROW:
                break
            if not _is_text_attachment(attachment):
                continue
            text = _fetch_attachment_text(
                session, config, sheet_id, attachment, config.attachment_max_bytes
            )
            if text:
                attachment["_text"] = text
                enriched += 1


def _row_doc_id(region: str, sheet_id: int, row_id: int) -> str:
    return f"smartsheet:{region}:{sheet_id}:row:{row_id}"


def _summary_doc_id(region: str, sheet_id: int) -> str:
    return f"smartsheet:{region}:{sheet_id}:summary"


def _render_row(
    config: _Config,
    sheet: dict,
    sheet_id: int,
    row: dict,
    column_index: dict[int, _ColumnInfo],
    parent_map: dict[int, int],
    children_map: dict[int, list[int]],
    rows_by_id: dict[int, dict],
) -> dict | None:
    if not _row_qualifies(row, column_index, config):
        return None

    row_id = int(row["id"])
    primary_value = _primary_cell_text(row, column_index)
    title = (primary_value or f"Row {row.get('rowNumber', row_id)}")[:_MAX_TITLE_CHARS]

    lines = [f"# [Row] {title}", "", f"Sheet: {sheet.get('name', sheet_id)}"]
    lines.append(_render_parent_path(row_id, parent_map, rows_by_id, column_index))
    children_line = _render_children(row_id, children_map, rows_by_id, column_index)
    if children_line:
        lines.append(children_line)
    lines.append("")

    for cell in sorted(row.get("cells") or [], key=lambda c: c.get("columnId", 0)):
        column = column_index.get(int(cell.get("columnId", -1)))
        if column is None or column.is_system or not column.included:
            continue
        text = _cell_text(cell, column, config)
        if not text:
            continue
        link_note = _render_cell_link(cell)
        if link_note:
            text = f"{text} ({link_note})"
        lines.append(f"{column.title}: {text}")

    if config.include_comments:
        comments = []
        for discussion in row.get("discussions") or []:
            comments.extend(discussion.get("comments") or [])
        comment_lines = _render_comment_lines(comments, _MAX_COMMENTS_PER_ROW)
        if comment_lines:
            lines.append("")
            lines.append("Comments:")
            lines.extend(comment_lines)

    if config.include_attachments:
        attachment_lines = _render_attachment_lines(row.get("attachments") or [])
        if attachment_lines:
            lines.append("")
            lines.append("Attachments:")
            lines.extend(attachment_lines)

    content = "\n".join(lines).rstrip() + "\n"
    permalink = row.get("permalink") or sheet.get("permalink")
    return {
        "id": _row_doc_id(config.region, sheet_id, row_id),
        "title": title,
        "content": content,
        "url": permalink,
        "_deleted": False,
    }


def _render_sheet_summary(
    config: _Config,
    sheet: dict,
    sheet_id: int,
    columns: list[dict],
    column_index: dict[int, _ColumnInfo],
    raw_rows: list[dict],
    sheet_comments: list[dict],
    kept_row_count: int,
) -> dict:
    name = sheet.get("name") or str(sheet_id)
    lines = [f"# Smartsheet sheet: {name}", "", f"Sheet: {name}", "", "Columns:"]

    for col in columns:
        title = col.get("title") or f"Column {col.get('id')}"
        col_type = col.get("type") or "UNKNOWN"
        line = f"- {title} ({col_type})"
        options = col.get("options")
        if isinstance(options, list) and options:
            shown = [str(o) for o in options[:_MAX_SUMMARY_OPTIONS]]
            extra = len(options) - len(shown)
            opts = ", ".join(shown) + (f" (+{extra} more)" if extra > 0 else "")
            line += f" -- options: {opts}"
        lines.append(line)

    total_rows = sheet.get("totalRowCount", len(raw_rows))
    lines.append("")
    lines.append(f"Total rows: {total_rows} ({kept_row_count} became separate documents)")

    top_level = sorted(
        (r for r in raw_rows if r.get("parentId") is None), key=lambda r: r.get("rowNumber", 0)
    )
    if top_level:
        shown = [
            _primary_cell_text(r, column_index) or f"Row {r.get('rowNumber')}"
            for r in top_level[:_MAX_SUMMARY_ROWS]
        ]
        lines.append("")
        lines.append("Top-level rows:")
        lines.extend(f"- {t}" for t in shown)
        extra = len(top_level) - len(shown)
        if extra > 0:
            lines.append(f"(+{extra} more)")

    if config.include_comments and sheet_comments:
        comment_lines = _render_comment_lines(sheet_comments, _MAX_SHEET_COMMENTS)
        if comment_lines:
            lines.append("")
            lines.append("Sheet comments:")
            lines.extend(comment_lines)

    content = "\n".join(lines).rstrip() + "\n"
    return {
        "id": _summary_doc_id(config.region, sheet_id),
        "title": f"Smartsheet sheet: {name}",
        "content": content,
        "url": sheet.get("permalink"),
        "_deleted": False,
    }
