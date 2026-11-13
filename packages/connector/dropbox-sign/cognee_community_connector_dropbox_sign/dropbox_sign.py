"""Dropbox Sign connector for cognee: a ``dlt`` source that turns signature requests into memory.

Sync the signature requests of a Dropbox Sign (formerly HelloSign) account into
cognee, incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_dropbox_sign import dropbox_sign_source

    await cognee.remember(
        dropbox_sign_source(api_key=api_key),
        dataset_name="dropbox_sign",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``: one per signature request
  (``signature_request:<id>``: title, subject, status, dates, message, and every
  signer with their own status).
* **No form data.** A request's ``response_data`` (what signers typed into the
  form), ``custom_fields``, ``metadata``, attachments, file and signing URLs and
  signers' phone numbers are never read; documents are built from an allowlist of
  fields, and every text field is scrubbed anyway. Email addresses are left out
  unless ``include_emails=True``.
* **Incremental.** The listing carries every request whole, so one listing per run
  is enough: a request is emitted again only when its rendered document changes
  (a signer views, signs or declines it). Requests created before ``since_days``
  are skipped. The listing's order is not documented, so every page is read.
* **Forget-on-delete.** A request missing from a complete listing (canceled,
  removed, or outside a new selection) is emitted as a hard-delete tombstone.
* **A run is bounded.** A long rate limit, a spent retry wait budget or a spent
  request budget stops the run cleanly, keeps what it emitted, and reports why in
  ``cognee_sync_stats``.

Privacy
-------
Signature requests name people and the agreements they sign. Nothing is fetched
until you construct a source and call ``remember``, and anyone with read access to
the target dataset can read what was ingested.
"""

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_dropbox_sign.redact import scrub

logger = get_logger("dropbox_sign_connector")

DEFAULT_API_URL = "https://api.hellosign.com/v3"
# How far back requests are read, by creation time.
DEFAULT_SINCE_DAYS = 365
# Requests per run; a bigger account finishes over several runs.
DEFAULT_MAX_REQUESTS = 1000
# Total time one run may wait on retries before it stops cleanly.
DEFAULT_MAX_SLEEP = 300.0
# The largest page the listing allows; the API defaults to 20.
_PAGE_SIZE = 100
# Dropbox Sign usually answers in well under a second; only a stuck connection hits this.
_TIMEOUT = 30.0
# Attempts per request; backing off 1, 2, 4 and 8 seconds gives up after about 15.
_RETRIES = 5
# A longer wait for the rate limit to reset stops the run instead of waiting it out.
_MAX_RETRY_SLEEP = 60.0
# Waits are slept in steps this long, asking the host's check_active in between.
_WAIT_STEP = 1.0
_STATS = (
    "scanned",
    "emitted",
    "unchanged",
    "deleted",
    "skipped_test_mode",
    "redacted",
    "failed",
    "failed_auth",
    "failed_budget",
    "failed_rate_limit",
)


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only, never the key or bodies.
# ---------------------------------------------------------------------------
class DropboxSignSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class DropboxSignAuthError(DropboxSignSourceError):
    """Dropbox Sign rejected the API key (HTTP 401)."""


class DropboxSignRateLimitedError(DropboxSignSourceError):
    """Dropbox Sign kept answering HTTP 429, asked for a long wait, or the wait budget is spent."""


class DropboxSignAPIError(DropboxSignSourceError):
    """Dropbox Sign answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _rate_limit_wait(response: Any, now: float) -> float | None:
    """Seconds until the rate limit resets. Dropbox Sign sends no Retry-After, only
    ``X-Ratelimit-Reset`` as a Unix time."""
    try:
        return max(float(response.headers.get("X-Ratelimit-Reset")) - now, 0.0)
    except (TypeError, ValueError):
        return None


class DropboxSignClient:
    """Minimal synchronous client for the Dropbox Sign API v3.

    ``get`` returns the decoded JSON body. Tests and hosts can pass any object
    with the same method to :func:`dropbox_sign_source` instead.
    """

    def __init__(
        self,
        api_key: str,
        api_url: str = DEFAULT_API_URL,
        *,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_sleep: float = DEFAULT_MAX_SLEEP,
        check_active: Callable[[], None] | None = None,
    ):
        api_key = (api_key or "").strip()
        # Reject early and without echoing the value: httpx would put an invalid
        # header value, key included, into its exception.
        if not api_key or not api_key.isascii() or not api_key.isprintable() or " " in api_key:
            raise ValueError("The Dropbox Sign API key is empty or has an invalid format")
        self.api_url = api_url.rstrip("/")
        # The key is the basic auth user name, with an empty password.
        self._auth = (api_key, "")
        # An injected client belongs to the caller; one made here is closed by close().
        self._http = http
        self._owned = http is None
        self._sleep = sleep
        self._clock = clock
        self._max_sleep = max_sleep
        self._slept = 0.0
        self._check_active = check_active

    def __repr__(self) -> str:
        return "DropboxSignClient(api_key=<redacted>)"

    def reset_sleep_budget(self) -> None:
        """Start a new run's retry wait budget."""
        self._slept = 0.0

    def close(self) -> None:
        """Close the HTTP client this object made; the next request opens a new one."""
        if self._owned and self._http is not None:
            self._http.close()
            self._http = None

    def _wait(self, delay: float) -> None:
        if self._slept + delay > self._max_sleep:
            raise DropboxSignRateLimitedError(
                "Dropbox Sign retries used up the wait budget of this run"
            )
        self._slept += delay
        # Slept in short steps, asking the host in between, so revoked access stops
        # a long wait within a second instead of at its end.
        left = delay
        while True:
            if self._check_active is not None:
                self._check_active()
            if left <= 0:
                return
            step = min(_WAIT_STEP, left)
            self._sleep(step)
            left -= step

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        import httpx

        if self._http is None:
            self._http = httpx.Client(timeout=_TIMEOUT)
        attempt = 0
        while True:
            last = attempt == _RETRIES - 1
            try:
                response = self._http.get(
                    self.api_url + path,
                    params=params,
                    auth=self._auth,
                    headers={"Accept": "application/json"},
                )
            except httpx.TransportError as exc:
                if last:
                    raise DropboxSignAPIError(
                        f"Dropbox Sign request failed: {type(exc).__name__}"
                    ) from None
                self._wait(float(2**attempt))
                attempt += 1
                continue
            status = response.status_code
            if status == 429:
                wait = _rate_limit_wait(response, self._clock())
                if last or (wait is not None and wait > _MAX_RETRY_SLEEP):
                    raise DropboxSignRateLimitedError("Dropbox Sign rate limit exceeded (HTTP 429)")
                self._wait(wait if wait is not None else float(2**attempt))
            elif status >= 500 and not last:
                self._wait(float(2**attempt))
            elif status == 401:
                raise DropboxSignAuthError("Dropbox Sign rejected the API key (HTTP 401)")
            elif status != 200:
                raise DropboxSignAPIError(
                    f"Dropbox Sign request failed: HTTP {status}", status=status
                )
            else:
                return response.json()
            attempt += 1


# ---------------------------------------------------------------------------
# Rendering: an allowlist of fields, deterministic.
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return str(value or "").strip()


def _epoch(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    return float(value)


def _when(value: Any) -> str:
    epoch = _epoch(value)
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _status(request: dict, now: datetime) -> str:
    if request.get("is_complete"):
        return "complete"
    if request.get("is_declined"):
        return "declined"
    if request.get("has_error"):
        return "error"
    expires = _epoch(request.get("expires_at"))
    if expires is not None and expires <= now.timestamp():
        return "expired"
    return "awaiting signatures"


def _signer(signature: dict, number: int, include_emails: bool) -> str:
    name = _text(signature.get("signer_name"))
    email = _text(signature.get("signer_email_address"))
    who = name or (email if include_emails else "") or f"Signer {number}"
    if include_emails and email and email != who:
        who = f"{who} <{email}>"
    role = _text(signature.get("signer_role"))
    if role:
        who = f"{who} ({role})"
    status = _text(signature.get("status_code")).replace("_", " ") or "unknown"
    line = f"- {who}: {status}"
    signed = _when(signature.get("signed_at"))
    if signed:
        line += f" on {signed}"
    viewed = _when(signature.get("last_viewed_at"))
    if viewed and not signed:
        line += f", last viewed {viewed}"
    reason = _text(signature.get("decline_reason"))
    if reason:
        line += f", reason: {reason}"
    reassigned = _text(signature.get("reassignment_reason"))
    if reassigned:
        line += f", reassigned: {reassigned}"
    return line


def render_request(request: dict, now: datetime, include_emails: bool = False) -> dict[str, Any]:
    """A signature request's document, built only from allowlisted fields: never
    ``response_data``, ``custom_fields``, ``metadata``, attachments, file or signing
    URLs, or phone numbers."""
    request_id = request["signature_request_id"]
    title = _text(request.get("title")) or _text(request.get("subject")) or request_id
    signatures = [s for s in request.get("signatures") or [] if isinstance(s, dict)]
    # Signer order when one is set, then the order the API answered.
    signatures = sorted(
        enumerate(signatures),
        key=lambda pair: (
            pair[1]["order"] if isinstance(pair[1].get("order"), int) else pair[0],
            pair[0],
        ),
    )
    fields = [
        ("Signature request", title),
        ("Subject", _text(request.get("subject"))),
        ("Status", _status(request, now)),
        ("Sent", _when(request.get("created_at"))),
        ("Expires", _when(request.get("expires_at"))),
        ("Requested by", _text(request.get("requester_email_address")) if include_emails else ""),
        ("Test mode", "yes" if request.get("test_mode") else ""),
    ]
    parts = [f"{label}: {value}" for label, value in fields if value]
    if signatures:
        parts.extend(["", "Signers:"])
        parts.extend(
            _signer(signature, number, include_emails)
            for number, (_index, signature) in enumerate(signatures, start=1)
        )
    message = _text(request.get("message"))
    if message:
        parts.extend(["", message])
    details = _text(request.get("details_url"))
    return {
        "id": f"signature_request:{request_id}",
        "title": f"Signature request: {title}",
        "content": "\n".join(parts),
        "url": details if details.startswith("https://") else "",
        "_deleted": False,
    }


def _scrubbed(row: dict[str, Any], stats: dict[str, int]) -> dict[str, Any]:
    for key in ("title", "content"):
        row[key], found = scrub(row[key])
        stats["redacted"] += found
    return row


def _digest(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _DropboxSignConfig:
    account_id: str | None = None
    query: str | None = None
    since_days: int = DEFAULT_SINCE_DAYS
    include_test_mode: bool = False
    include_emails: bool = False
    full_resync: bool = False
    max_requests: int = DEFAULT_MAX_REQUESTS


class _CutShortError(Exception):
    """Internal: stop the run here, keeping what was emitted so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Walker:
    """One run over the account's signature requests, writing progress into ``state``."""

    def __init__(
        self,
        client: Any,
        config: _DropboxSignConfig,
        state: dict,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
        now: datetime,
    ):
        self.client = client
        self.config = config
        self.state = state
        self.stats = stats
        self.check_active = check_active
        self.now = now
        self.known: dict[str, dict] = state.setdefault("requests", {})
        self.requests = 0
        # Set once the first listing page is in; the budget never stops a run before that.
        self.progressed = False
        self.floor = (now - timedelta(days=config.since_days)).timestamp()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.progressed and self.requests >= self.config.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.get(path, params)
        except DropboxSignRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except DropboxSignAuthError:
            # Revoked during the run: keep what is done. Before any progress it is a
            # wrong key, which raises.
            if self.progressed:
                raise _CutShortError("auth") from None
            raise

    def _check_account(self) -> None:
        """Bind this state to one account, so a second key never forgets the first's
        requests under a shared resource name."""
        body = self._get("/account")
        account = body.get("account") if isinstance(body, dict) else None
        account_id = account.get("account_id") if isinstance(account, dict) else None
        if not isinstance(account_id, str) or not account_id:
            raise DropboxSignAPIError("Dropbox Sign answered /account without an account id")
        known = self.state.get("account")
        if known and known != account_id:
            raise ValueError(
                "This sync was made with another Dropbox Sign account. Pass a "
                "resource_name of its own for each account."
            )
        self.state["account"] = account_id

    def _sync(self, request: dict, entry: dict | None) -> dict | None:
        row = _scrubbed(render_request(request, self.now, self.config.include_emails), self.stats)
        digest = _digest(row)
        request_id = request["signature_request_id"]
        if entry and entry.get("doc") == digest and not self.config.full_resync:
            self.stats["unchanged"] += 1
            return None
        self.known[request_id] = {
            "doc": digest,
            "created": _epoch(request.get("created_at")) or 0,
        }
        self.stats["emitted"] += 1
        return row

    def rows(self) -> Iterator[dict]:
        self._check_account()
        params: dict[str, Any] = {"page_size": _PAGE_SIZE}
        if self.config.account_id:
            params["account_id"] = self.config.account_id
        if self.config.query:
            params["query"] = self.config.query
        seen: set[str] = set()
        # Requests older than since_days: not read, and not forgotten either.
        aged: set[str] = set()
        page = 1
        while True:
            body = self._get("/signature_request/list", {**params, "page": page})
            items = body.get("signature_requests") if isinstance(body, dict) else None
            info = body.get("list_info") if isinstance(body, dict) else None
            # A 200 without the list must not read as "every request was removed".
            if not isinstance(items, list):
                raise DropboxSignAPIError("Dropbox Sign answered without a request list")
            for request in items:
                if not isinstance(request, dict) or not _text(request.get("signature_request_id")):
                    # An entry that cannot be told apart leaves the listing incomplete.
                    raise DropboxSignAPIError("Dropbox Sign listed a request without its id")
                if (_epoch(request.get("created_at")) or 0) < self.floor:
                    aged.add(request["signature_request_id"])
                    continue
                if request.get("test_mode") and not self.config.include_test_mode:
                    self.stats["skipped_test_mode"] += 1
                    continue
                request_id = request["signature_request_id"]
                seen.add(request_id)
                self.stats["scanned"] += 1
                row = self._sync(request, self.known.get(request_id))
                if row is not None:
                    yield row
            self.progressed = True
            pages = info.get("num_pages") if isinstance(info, dict) else None
            if not isinstance(pages, int) or page >= pages or not items:
                break
            page += 1
        yield from self._forget_unlisted(seen | aged)

    def _forget_unlisted(self, listed: set[str]) -> Iterator[dict]:
        """Requests that left the complete listing or the selection. One that only
        aged out of ``since_days`` is still listed, so it is kept as last synced."""
        gone = [request_id for request_id in self.known if request_id not in listed]
        if gone and not listed and not self.state.get("empty_listing"):
            # A listing that is empty as a whole is confirmed by the next run.
            self.state["empty_listing"] = True
            logger.warning(
                "Dropbox Sign listed no requests although %d were synced before; nothing "
                "is forgotten until the next run confirms it.",
                len(gone),
            )
            return
        self.state.pop("empty_listing", None)
        for request_id in gone:
            del self.known[request_id]
            self.stats["deleted"] += 1
            yield {"id": f"signature_request:{request_id}", "_deleted": True}


def _iter_rows(
    client: Any,
    config: _DropboxSignConfig,
    state: dict,
    stats: dict[str, int],
    check_active: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> Iterator[dict]:
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict.

    A rejected key raises :class:`DropboxSignAuthError`, which is never read as "gone".
    """
    for key in _STATS:
        stats.setdefault(key, 0)
    reset = getattr(client, "reset_sleep_budget", None)
    if reset is not None:
        reset()
    walker = _Walker(client, config, state, stats, check_active, now or _utcnow())
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        # Every cut leaves the sync incomplete, so failed is set; failed_<reason> says why.
        # Nothing is forgotten from an incomplete listing.
        stats["failed"] = 1
        stats[f"failed_{cut.reason}"] = 1
        logger.warning(
            "Dropbox Sign sync stopped early (%s); it continues on the next run.", cut.reason
        )
        return
    logger.info(
        "Dropbox Sign: %d request(s) listed, %d document(s) emitted, %d deleted.",
        stats["scanned"],
        stats["emitted"],
        stats["deleted"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
# dlt table names stay readable up to this length; the merged Asana connector uses it too.
_MAX_RESOURCE_NAME = 64


def _default_resource_name(config: _DropboxSignConfig) -> str:
    name = "dropbox_sign"
    if config.account_id or config.query or config.include_test_mode:
        # Two differently selected syncs into one dataset must not share state and cleanup.
        scope = [config.account_id, config.query, config.include_test_mode]
        name += "_" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()[:8]
    return name


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: an empty name would lose that scope,
    and two names dlt normalizes alike ("Foo-Bar", "foo_bar") would share a table.
    """
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > _MAX_RESOURCE_NAME:
        raise ValueError(f"resource_name must be at most {_MAX_RESOURCE_NAME} characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")


def dropbox_sign_source(
    *,
    api_key: str | None = None,
    account_id: str | None = None,
    query: str | None = None,
    since_days: int = DEFAULT_SINCE_DAYS,
    include_test_mode: bool = False,
    include_emails: bool = False,
    full_resync: bool = False,
    api_url: str = DEFAULT_API_URL,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per Dropbox Sign signature request.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        api_key: A Dropbox Sign API key (Settings, API).
        account_id: Whose requests to list: a team member's account id, or ``"all"``
            for the whole team. Defaults to the key's own account.
        query: A Dropbox Sign search query that narrows the listing (for example
            ``"title:NDA"``).
        since_days: Only requests created within this many days.
        include_test_mode: Also sync test-mode requests, which have no legal value.
        include_emails: Put signer and requester email addresses in the documents.
            Off by default: signers are named by name and role.
        full_resync: Emit every request again, within ``since_days``.
        api_url: API address.
        max_requests: Request budget of one run, checked once the first page is in.
            A spent budget sets ``failed`` and ``failed_budget``.
        resource_name: Staging table name, which scopes this sync's state and
            cleanup. Defaults to ``dropbox_sign`` (plus a hash of ``account_id``,
            ``query`` and ``include_test_mode`` when set). Give each Dropbox Sign
            account its own name. A name you pass must be lowercase snake_case of at
            most 64 characters.
        check_active: Optional host authorization checkpoint, called before each
            request and around each row, and in the client built here during every
            retry wait.
        service: Pre-built client (see :class:`DropboxSignClient`). Mainly an
            injection point for tests and hosts. ``client`` is accepted as an alias.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Dropbox Sign connector requires dlt: pip install "cognee[dlt]".'
        ) from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Dropbox Sign sync requires a cognee build with table-scoped DLT document cleanup."
        )
    if account_id is not None and (not isinstance(account_id, str) or not account_id.strip()):
        raise ValueError("account_id must be a non-empty string, or None")
    if query is not None and (not isinstance(query, str) or not query.strip()):
        raise ValueError("query must be a non-empty string, or None")
    if isinstance(since_days, bool) or not isinstance(since_days, int) or since_days < 1:
        raise ValueError("since_days must be a whole number of at least 1")
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
        raise ValueError("max_requests must be a whole number of at least 1")

    config = _DropboxSignConfig(
        account_id=account_id.strip() if account_id else None,
        query=query.strip() if query else None,
        since_days=since_days,
        include_test_mode=bool(include_test_mode),
        include_emails=bool(include_emails),
        full_resync=bool(full_resync),
        max_requests=max_requests,
    )
    name = resource_name
    if name is None:
        name = _default_resource_name(config)
    _check_resource_name(name)
    service = service or client
    owned = service is None
    if owned:
        if not api_key:
            raise ValueError(
                "A Dropbox Sign API key is required: pass api_key=, or a pre-built service."
            )
        service = DropboxSignClient(api_key, api_url, check_active=check_active)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def dropbox_sign_documents():
        stats.clear()
        state = dlt.current.resource_state()
        rows = _iter_rows(service, config, state, stats, check_active)
        try:
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            # Also on errors and when dlt stops early; an injected service is the caller's.
            if owned:
                service.close()

    resource = dropbox_sign_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "dropbox_sign")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
