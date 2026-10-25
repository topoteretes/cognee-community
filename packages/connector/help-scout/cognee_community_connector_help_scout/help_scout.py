"""DLT source for Help Scout (incremental sync + forget-on-delete).

Pulls Help Scout **conversations with their threads** (Inbox API v2) and,
optionally, **Docs articles** (Docs API v1) into cognee as documents::

    import os

    import cognee
    from cognee_community_connector_help_scout import help_scout_source

    await cognee.remember(
        help_scout_source(
            resource_name="help_scout_acme",  # one per Help Scout account / scope
            app_id=os.environ["HELPSCOUT_APP_ID"],
            app_secret=os.environ["HELPSCOUT_APP_SECRET"],
        ),
        dataset_name="help_scout",
        primary_key="id",
        write_disposition="merge",  # REQUIRED (see .. important:: below)
    )

Rows are ingested as *normal documents*: the source declares
``cognee_document_source = "help_scout"``, so each conversation / article flows
through cognify entity extraction instead of the deterministic dlt-row path.

.. important::
   ``write_disposition="merge"`` is **mandatory**. Incremental runs only see what
   changed, so cognee's default ``"replace"`` would forget everything else.

Design
------
* **Scope** - ``resource_name`` prefixes both tables and is published as the
  pipeline scope, so two Help Scout accounts (or two configurations) in one
  dataset keep separate cursors and separate forget-on-delete bookkeeping.
* **Auth (OAuth 2.0)** - Inbox API: an app id + secret exchanged for a bearer
  token with the Client Credentials grant (renewed once on ``401``), or a ready
  ``access_token``. Docs API: a per-user Docs API key over HTTP Basic auth.
* **One document per conversation**, built only from fields that describe the
  conversation itself: subject, status, channel, customer, assignee, tags
  (sorted) and the published threads oldest-first. Inbox and collection *names*
  are left out on purpose: renaming an inbox would otherwise change the content
  hash of every conversation in it and re-extract them all.
* **Incremental** - ``status=all&modifiedSince=<cursor>`` sorted by
  ``modifiedAt``. The cursor comes from Help Scout's own clock (the ``Date``
  header of the run's first response) minus a 5-minute overlap. With no Date
  header it uses the local run-start time. Repeated ids, changing totals or an
  incomplete listing trigger a retry with the same cutoff. After three unstable
  passes the cursor stays put. Page-number APIs cannot provide snapshot isolation.
* **Configuration changes** - ``mailbox_ids``, ``include_notes`` and
  ``include_spam`` are stored next to the cursor. When they change, the next run
  does a full listing, so an inbox added later is backfilled and an inbox removed
  later is forgotten.
* **Rate-limit friendly thread loading** - changed conversations are listed with
  ``embed=threads``. The per-conversation threads endpoint is only called when
  the embedded copy can be incomplete: chat conversations (Help Scout truncates
  embedded chat threads) or fewer published threads than the conversation's
  ``threads`` count. A conversation deleted between those two calls (``404``)
  becomes a tombstone instead of failing the run.
* **Forget-on-delete** - conversations in ``state == "deleted"`` (or moved to
  spam, unless ``include_spam=True``) become ``{"id", "_deleted": True}``
  tombstones. Every ``reconcile_every`` runs, all conversation ids are listed
  (stable ``number`` order) to find conversations that simply vanished. Page
  paging can shift when something is deleted mid-listing, so the listing must be
  complete and consistent (same ``totalElements`` on every page, no repeats), and
  every candidate is re-checked one by one before it is forgotten. A short or
  shifting listing never turns into deletes. Articles get the same treatment.
* **Safe failure** - ``5xx`` and network errors are retried with backoff; a
  rate-limit wait longer than ``max_rate_limit_wait`` stops the run cleanly (what
  was read and confirmed tombstones are kept, and their identities are saved).
  The stopped resource advances no cursor; independent resources may finish.
  A per-id 403 keeps the document and is counted; other errors abort the run.
"""

from __future__ import annotations

import base64
import email.utils
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("help_scout_connector")

HELP_SCOUT_SOURCE_NAME = "help_scout"
DEFAULT_RESOURCE_NAME = "help_scout"

MAILBOX_API = "https://api.helpscout.net/v2"
TOKEN_URL = f"{MAILBOX_API}/oauth2/token"
DOCS_API = "https://docsapi.helpscout.net/v1"

CURSOR_OVERLAP = timedelta(minutes=5)
DEFAULT_MAX_RATE_LIMIT_WAIT = 120.0
# Re-check a bounded batch; remember checked IDs until the cycle completes.
MAX_VERIFY = 200

_UTC = timezone.utc
_MAX_RETRIES = 5
_SERVER_ERRORS = frozenset({500, 502, 503, 504})
_DELETED_COLUMN = {
    "_deleted": {"data_type": "bool", "hard_delete": True},
    "_sync_order": {"data_type": "bigint", "dedup_sort": "desc"},
}
_RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_THREAD_LABELS = {
    "customer": "customer",
    "message": "reply",
    "note": "internal note",
    "chat": "chat",
    "beaconchat": "chat",
    "phone": "phone",
    "forwardparent": "forward",
    "forwardchild": "forward",
}


class RateLimitedError(Exception):
    """Help Scout asked to wait longer than ``max_rate_limit_wait``."""


class _NotFoundError(Exception):
    pass


class _ForbiddenError(PermissionError):
    """An HTTP 403, distinguishable from invalid credentials (401)."""


class _UnstableListingError(Exception):
    pass


@dataclass(frozen=True)
class _Options:
    mailbox_ids: tuple[int, ...] = ()
    include_notes: bool = False
    include_spam: bool = False
    reconcile: bool = True
    reconcile_every: int = 1
    modified_since: str | None = None
    collection_ids: frozenset[str] = frozenset()
    article_status: str = "published"

    def conversation_config(self) -> dict:
        return {
            "mailbox_ids": sorted(self.mailbox_ids),
            "include_notes": self.include_notes,
            "include_spam": self.include_spam,
        }

    def article_config(self) -> dict:
        return {"collection_ids": sorted(self.collection_ids), "status": self.article_status}


def help_scout_source(
    *,
    resource_name: str = DEFAULT_RESOURCE_NAME,
    app_id: str | None = None,
    app_secret: str | None = None,
    access_token: str | None = None,
    mailbox_ids: Iterable[int] | None = None,
    include_conversations: bool = True,
    include_notes: bool = False,
    include_spam: bool = False,
    reconcile: bool = True,
    reconcile_every: int = 1,
    modified_since: str | None = None,
    docs_api_key: str | None = None,
    include_articles: bool | None = None,
    collection_ids: Iterable[str] | None = None,
    article_status: str = "published",
    max_rate_limit_wait: float = DEFAULT_MAX_RATE_LIMIT_WAIT,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Create a dlt source with Help Scout conversations and/or Docs articles.

    All arguments are keyword-only. Credentials are taken only from the
    arguments (the example shows reading them from the environment).

    Args:
        resource_name: Stable name of this connection (lowercase letters, digits,
            ``_``). Prefixes the tables (``<name>_conversations``,
            ``<name>_articles``) and scopes the sync state, so use one name per
            Help Scout account and keep it fixed across runs.
        app_id / app_secret: OAuth app credentials (Client Credentials grant).
        access_token: A ready OAuth bearer token instead (cannot be renewed).
        mailbox_ids: Only these inboxes. Default: every inbox the token can see.
            Changing it triggers a full listing on the next run.
        include_conversations: Sync conversations (default ``True``).
        include_notes: Include internal notes in conversation documents.
        include_spam: Keep spam conversations (default: they are forgotten).
        reconcile: Look for conversations/articles that vanished without a change.
        reconcile_every: Reconcile on every N-th run (default every run). The
            reconcile lists every id (about one request per 25 conversations);
            raise this on large accounts to keep regular syncs short.
        modified_since: ISO 8601 time; limits only the *first* run.
        docs_api_key: Docs API key (enables articles).
        include_articles: Sync Docs articles. Default: ``True`` when a key is given.
        collection_ids: Only these Docs collections. Default: all.
        article_status: ``"published"`` (default) or ``"all"``.
        max_rate_limit_wait: Longest rate-limit wait (seconds) the connector will
            sleep; a longer one stops the run cleanly.
        check_active: Optional host authorization checkpoint, called before each
            request and around each row.
        service: Pre-built ``httpx.Client``-like object; ``client`` is an alias.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``. Its
        ``cognee_sync_stats`` dict holds integer counts for the last run.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The Help Scout connector requires dlt and httpx: "
            "pip install cognee-community-connector-help-scout"
        ) from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1 or not hasattr(
        dlt_utils, "PIPELINE_SCOPE_ATTR"
    ):
        raise RuntimeError(
            "The Help Scout connector needs a cognee build with table-scoped DLT "
            "document cleanup (cognee >= 1.6.1). Upgrade cognee before syncing, so a "
            "sync cannot delete another source's data."
        )
    if not isinstance(resource_name, str) or not _RESOURCE_NAME_RE.match(resource_name):
        raise ValueError(
            "resource_name must start with a lowercase letter and contain only "
            "lowercase letters, digits and '_' (max 41 characters)."
        )
    if service is not None and client is not None and service is not client:
        raise ValueError("Pass either service= or client=, not both.")
    http = service if service is not None else client
    if include_articles is None:
        include_articles = bool(docs_api_key)
    if include_conversations and not (access_token or (app_id and app_secret)):
        raise ValueError(
            "Help Scout credentials required: app_id= and app_secret=, or access_token=."
        )
    if include_articles and not docs_api_key:
        raise ValueError("Docs articles need docs_api_key=.")
    if not (include_conversations or include_articles):
        raise ValueError("Nothing to ingest: enable conversations and/or articles.")
    if not isinstance(reconcile_every, int) or reconcile_every < 1:
        raise ValueError("reconcile_every must be a positive integer.")
    if article_status not in ("published", "all"):
        raise ValueError('article_status must be "published" or "all".')
    mailboxes = tuple(sorted({int(m) for m in mailbox_ids or ()}))

    if http is None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a dependency
            raise ImportError("httpx is required") from exc
        http = httpx.Client(timeout=60.0)

    options = _Options(
        mailbox_ids=mailboxes,
        include_notes=include_notes,
        include_spam=include_spam,
        reconcile=reconcile,
        reconcile_every=reconcile_every,
        modified_since=modified_since,
        collection_ids=frozenset(collection_ids or ()),
        article_status=article_status,
    )
    stats: dict[str, int] = {}

    resources = []
    if include_conversations:
        token = _OAuthToken(http, app_id, app_secret, access_token)
        inbox_api = _Api(http, token, stats, check_active, max_rate_limit_wait, "Inbox API")

        @dlt.resource(
            name=f"{resource_name}_conversations",
            primary_key="id",
            write_disposition="merge",
            columns=_DELETED_COLUMN,
        )
        def help_scout_conversations():
            stats.clear()  # first resource of the run
            stats.update(failed_rate_limit=0, failed_auth=0)
            rows = _stop_cleanly(
                _sync_conversations(inbox_api, dlt.current.resource_state(), options, stats),
                stats,
            )
            yield from dlt_utils.guarded_rows(rows, check_active)

        resources.append(help_scout_conversations)

    if include_articles:
        docs_api = _Api(
            http, _DocsKey(docs_api_key), stats, check_active, max_rate_limit_wait, "Docs API"
        )

        @dlt.resource(
            name=f"{resource_name}_articles",
            primary_key="id",
            write_disposition="merge",
            columns=_DELETED_COLUMN,
        )
        def help_scout_articles():
            if not include_conversations:
                stats.clear()
            stats.setdefault("failed_rate_limit", 0)
            stats.setdefault("failed_auth", 0)
            rows = _stop_cleanly(
                _sync_articles(docs_api, dlt.current.resource_state(), options, stats), stats
            )
            yield from dlt_utils.guarded_rows(rows, check_active)

        resources.append(help_scout_articles)

    @dlt.source(name=resource_name)
    def _help_scout():
        return resources

    source = _help_scout()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(source, DOCUMENT_SOURCE_ATTR, HELP_SCOUT_SOURCE_NAME)
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    source.cognee_sync_stats = stats
    return source


def _count(stats: dict[str, int], key: str, amount: int = 1) -> None:
    stats[key] = stats.get(key, 0) + amount


def _stop_cleanly(rows: Iterator[dict], stats: dict[str, int]) -> Iterator[dict]:
    """End cleanly on a long limit; retain yielded rows and their bookkeeping."""
    try:
        # A retry may emit a newer version or tombstone for an already-yielded
        # id. Make merge select the LAST observation, not an arbitrary row.
        for sequence, row in enumerate(rows):
            yield {**row, "_sync_order": sequence}
    except RateLimitedError as exc:
        _count(stats, "failed_rate_limit")
        logger.warning("Help Scout: %s Stopping this run; the next run continues.", exc)


# ---------------------------------------------------------------------------
# HTTP: auth, checkpoints, retries
# ---------------------------------------------------------------------------


class _OAuthToken:
    """Bearer token for the Inbox API (Client Credentials, renewed on 401)."""

    def __init__(self, http: Any, app_id: str | None, app_secret: str | None, token: str | None):
        self._http = http
        self._app_id = app_id
        self._app_secret = app_secret
        self._token = token

    def headers(self) -> dict:
        if self._token is None:
            self.renew()
        return {"Authorization": f"Bearer {self._token}"}

    def can_renew(self) -> bool:
        return bool(self._app_id and self._app_secret)

    def renew(self) -> None:
        if not self.can_renew():
            raise PermissionError("Help Scout rejected the access token (HTTP 401).")
        data = {
            "grant_type": "client_credentials",
            "client_id": self._app_id,
            "client_secret": self._app_secret,
        }
        response = self._http.post(TOKEN_URL, data=data)
        if response.status_code in (400, 401, 403):
            raise PermissionError(
                f"Help Scout rejected the app id/secret (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Help Scout token request failed (HTTP {response.status_code}).")
        self._token = response.json()["access_token"]


class _DocsKey:
    """HTTP Basic auth for the Docs API: the key is the user name, password ``X``."""

    def __init__(self, key: str):
        encoded = base64.b64encode(f"{key}:X".encode()).decode()
        self._headers = {"Authorization": f"Basic {encoded}"}

    def headers(self) -> dict:
        return self._headers

    def can_renew(self) -> bool:
        return False

    def renew(self) -> None:  # pragma: no cover - never called (can_renew is False)
        raise PermissionError("Docs API key rejected.")


class _Api:
    """GET JSON with auth, a host checkpoint, one re-auth on 401 and retries.

    Messages and logs carry the API name and status only, never URLs or ids.
    """

    def __init__(
        self,
        http: Any,
        auth: Any,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
        max_wait: float,
        name: str,
    ):
        self._http = http
        self._auth = auth
        self._stats = stats
        self._check_active = check_active
        self._max_wait = max_wait
        self.name = name
        self.server_time: datetime | None = None

    def get(self, url: str, params: dict | None = None) -> dict:
        try:
            return self._get(url, params)
        except PermissionError:
            _count(self._stats, "failed_auth")
            raise

    def _get(self, url: str, params: dict | None) -> dict:
        renewed = False
        for attempt in range(_MAX_RETRIES):
            if self._check_active is not None:
                self._check_active()
            try:
                response = self._http.get(url, params=params, headers=self._auth.headers())
            except PermissionError:
                raise
            except Exception as exc:
                if not _is_transport_error(exc) or attempt == _MAX_RETRIES - 1:
                    raise
                self._sleep(float(2**attempt), f"network error ({type(exc).__name__})")
                continue
            self._note_server_time(response.headers)
            status = response.status_code
            if status == 401 and not renewed and self._auth.can_renew():
                self._auth.renew()
                renewed = True
                continue
            if status == 403:
                raise _ForbiddenError(f"Help Scout {self.name} refused access (HTTP 403).")
            if status == 401:
                raise PermissionError(
                    f"Help Scout {self.name} refused the request (HTTP {status}). "
                    "Check the credentials and the app's permissions."
                )
            if status == 404:
                # Use a fixed resource label, never expose ids, URLs or query strings.
                kind = "resource"
                path = url.split("?", 1)[0].rstrip("/")
                for label in ("threads", "conversations", "articles", "collections"):
                    if f"/{label}" in path:
                        kind = label
                        break
                raise _NotFoundError(f"Help Scout {self.name}: {kind} not found (HTTP 404).")
            if status == 429:
                wait = _rate_limit_wait(response.headers, attempt)
                if wait > self._max_wait or attempt == _MAX_RETRIES - 1:
                    raise RateLimitedError(
                        f"{self.name} rate limit: asked to wait {wait:.0f}s "
                        f"(limit {self._max_wait:.0f}s)."
                    )
                self._sleep(wait, "HTTP 429")
                continue
            if status in _SERVER_ERRORS and attempt < _MAX_RETRIES - 1:
                self._sleep(float(2**attempt), f"HTTP {status}")
                continue
            if status >= 400:
                raise RuntimeError(f"Help Scout {self.name} error (HTTP {status}).")
            return response.json()
        raise RuntimeError(f"Help Scout {self.name}: retries exhausted.")  # pragma: no cover

    def _sleep(self, seconds: float, why: str) -> None:
        logger.warning("Help Scout %s: %s - retrying in %.1fs.", self.name, why, seconds)
        time.sleep(seconds)

    def _note_server_time(self, headers: Any) -> None:
        if self.server_time is not None or not headers:
            return
        value = headers.get("Date") or headers.get("date")
        if not value:
            return
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_UTC)
            self.server_time = parsed.astimezone(_UTC)
        except (TypeError, ValueError):
            return


def _is_transport_error(exc: Exception) -> bool:
    try:
        import httpx
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, httpx.TransportError)


def _rate_limit_wait(headers: Any, attempt: int) -> float:
    """Seconds to wait on 429.

    The Inbox API sends ``X-RateLimit-Retry-After`` (seconds). The Docs API sends
    ``X-RateLimit-Reset`` instead (seconds until the window resets, or an epoch
    time); ``Retry-After`` is accepted too.
    """
    headers = headers or {}
    for name in ("X-RateLimit-Retry-After", "Retry-After"):
        value = _header_number(headers, name)
        if value is not None:
            return max(value, 0.0)
    reset = _header_number(headers, "X-RateLimit-Reset")
    if reset is not None:
        if reset > 1_000_000_000:  # epoch seconds
            reset -= time.time()
        return max(reset, 0.0)
    return float(2 ** (attempt + 1))


def _header_number(headers: Any, name: str) -> float | None:
    value = headers.get(name) if hasattr(headers, "get") else None
    if value is None:
        value = headers.get(name.lower()) if hasattr(headers, "get") else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@dataclass
class _Listing:
    """What a paged listing looked like, to decide whether it can be trusted."""

    totals: set[int] = field(default_factory=set)
    repeats: int = 0
    seen: set = field(default_factory=set)

    def consistent(self) -> bool:
        return (
            self.repeats == 0
            and len(self.totals) <= 1
            and (not self.totals or next(iter(self.totals)) == len(self.seen))
        )


def _iter_hal(
    api: _Api, url: str, params: dict | None, key: str, listing: _Listing | None = None
) -> Iterator[dict]:
    """Yield ``_embedded[key]`` items, following HAL ``_links.next`` links.

    A repeated ``next`` link means the listing is broken; raising keeps a short
    listing from turning into mass deletes.
    """
    listing = listing if listing is not None else _Listing()
    links_seen: set[str] = set()
    next_url, next_params = url, params
    while True:
        payload = api.get(next_url, next_params)
        total = (payload.get("page") or {}).get("totalElements")
        if isinstance(total, int):
            listing.totals.add(total)
        for item in (payload.get("_embedded") or {}).get(key) or []:
            item_id = item.get("id")
            if item_id in listing.seen:
                listing.repeats += 1
                continue
            listing.seen.add(item_id)
            yield item
        href = ((payload.get("_links") or {}).get("next") or {}).get("href")
        if not href:
            return
        if href in links_seen:
            raise _UnstableListingError(f"{api.name} repeated a page link.")
        links_seen.add(href)
        next_url, next_params = href, None


def _iter_docs(
    api: _Api, url: str, key: str, params: dict, listing: _Listing | None = None
) -> Iterator[dict]:
    """Yield items of a Docs API list (``{key: {page, pages, count, items}}``)."""
    listing = listing if listing is not None else _Listing()
    page = 1
    while True:
        block = api.get(url, {**params, "page": page}).get(key) or {}
        items = block.get("items") or []
        pages = int(block.get("pages") or 1)
        if isinstance(block.get("count"), int):
            listing.totals.add(block["count"])
        if not items and page < pages:
            raise _UnstableListingError(f"{api.name} returned an empty page in the middle.")
        for item in items:
            if item.get("id") in listing.seen:
                listing.repeats += 1
                continue
            listing.seen.add(item.get("id"))
            yield item
        if page >= pages:
            return
        page += 1


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def _sync_conversations(
    api: _Api, state: dict, options: _Options, stats: dict[str, int]
) -> Iterator[dict]:
    run_started = datetime.now(_UTC)
    api.server_time = None
    url = f"{MAILBOX_API}/conversations"
    config = options.conversation_config()
    config_changed = "config" in state and state["config"] != config
    cursor = None if config_changed else (state.get("modified_since") or options.modified_since)
    full_listing = cursor is None
    known = {int(i) for i in state.get("known_ids") or []}
    checked = (
        set(state.get("reconcile_checked") or [])
        if state.get("reconcile_config") == config
        else set()
    )
    runs = int(state.get("runs_since_reconcile") or 0) + 1
    do_reconcile = (
        config_changed
        or state.get("reconcile_pending", False)
        or (options.reconcile and (full_listing or runs >= options.reconcile_every))
    )
    base: dict[str, Any] = {"status": "all"}
    if options.mailbox_ids:
        base["mailbox"] = ",".join(str(m) for m in options.mailbox_ids)
    changed_params = {**base, "embed": "threads", "sortField": "modifiedAt", "sortOrder": "asc"}
    if cursor:
        changed_params["modifiedSince"] = cursor

    gone: set[int] = set()
    stable = False
    scope_complete = not config_changed
    try:
        # Retry from page 1 with the SAME cutoff. Reprocess repeated ids across
        # passes: their content may itself have changed while pages shifted.
        for _pass in range(3):
            listing = _Listing()
            try:
                for conversation in _iter_hal(api, url, changed_params, "conversations", listing):
                    conversation_id = conversation.get("id")
                    if conversation_id is None:
                        continue
                    if _excluded(conversation, options):
                        known.discard(conversation_id)
                        gone.add(conversation_id)
                        yield _tombstone(conversation_id)
                        continue
                    try:
                        threads, extra = _threads_for(api, conversation)
                    except _NotFoundError:
                        known.discard(conversation_id)
                        gone.add(conversation_id)
                        yield _tombstone(conversation_id)
                        continue
                    _count(stats, "conversations_thread_requests", extra)
                    known.add(conversation_id)
                    _count(stats, "conversations_synced")
                    yield _conversation_row(conversation, threads, options)
            except _UnstableListingError:
                continue
            if listing.consistent():
                stable = True
                break
        if not stable:
            logger.warning("Help Scout: the listing kept moving; the cursor stays where it was.")

        if do_reconcile:
            # Persist the intent even if a limit interrupts listing/verification.
            state["reconcile_pending"] = True
            if full_listing and stable and listing.consistent():
                present: set[int] | None = {int(i) for i in listing.seen if i is not None}
            else:
                present = _list_present_ids(api, url, base)
            if present is None:
                _count(stats, "conversations_reconcile_skipped")
            else:
                missing = known - present - gone
                checked.intersection_update(missing)
                candidates = sorted(missing - checked)
                for conversation_id in _confirm_gone(
                    api, candidates, options, stats, MAX_VERIFY, checked
                ):
                    known.discard(conversation_id)
                    gone.add(conversation_id)
                    yield _tombstone(conversation_id)
                pending = bool(set(candidates) - checked - gone)
                state["reconcile_pending"] = pending
                if not pending:
                    checked.clear()
                    runs = 0
                    scope_complete = True

        state["runs_since_reconcile"] = runs
        if stable:
            # A missing Date header must use the START, not the end, of a long run.
            now = api.server_time or run_started
            state["modified_since"] = _iso(now - CURSOR_OVERLAP)
            if scope_complete:
                state["config"] = config
    finally:
        # dlt commits yielded rows on a clean rate stop. Save their identities
        # too, including confirmed tombstones; other exceptions roll back dlt.
        state["known_ids"] = sorted(known)
        state["reconcile_checked"] = sorted(checked)
        state["reconcile_config"] = config
        _count(stats, "conversations_forgotten", len(gone))


def _list_present_ids(api: _Api, url: str, base: dict) -> set[int] | None:
    """All conversation ids in scope, or ``None`` when the listing can't be trusted."""
    listing = _Listing()
    params = {**base, "sortField": "number", "sortOrder": "asc"}
    try:
        for _ in _iter_hal(api, url, params, "conversations", listing):
            pass
    except _UnstableListingError as exc:
        logger.warning("Help Scout: %s No deletes inferred from this listing.", exc)
        return None
    if not listing.consistent():
        logger.warning(
            "Help Scout: the id listing changed; no deletes inferred from this listing."
        )
        return None
    return {int(i) for i in listing.seen if i is not None}


def _confirm_gone(
    api: _Api,
    candidates: list[int],
    options: _Options,
    stats: dict[str, int],
    limit: int | None = MAX_VERIFY,
    checked: set[int] | None = None,
) -> Iterator[int]:
    """Verify a bounded batch. A forbidden GET alone is not proof of deletion."""
    checked = checked if checked is not None else set()
    for conversation_id in candidates[:limit]:
        try:
            conversation = api.get(f"{MAILBOX_API}/conversations/{conversation_id}")
        except _NotFoundError:
            checked.add(conversation_id)
            yield conversation_id
            continue
        except _ForbiddenError:
            # Retain inaccessible documents. A 403 could mean missing permission,
            # not deletion; listing-level 403 and every 401 still abort the run.
            _count(stats, "conversations_access_denied")
            checked.add(conversation_id)
            continue
        checked.add(conversation_id)
        if _excluded(conversation, options) or (
            options.mailbox_ids and conversation.get("mailboxId") not in options.mailbox_ids
        ):
            yield conversation_id


def _excluded(conversation: dict, options: _Options) -> bool:
    if conversation.get("state") in ("deleted", "draft"):
        return True
    return conversation.get("status") == "spam" and not options.include_spam


def _threads_for(api: _Api, conversation: dict) -> tuple[list[dict], int]:
    """Return the conversation's threads and how many extra requests it took."""
    embedded = (conversation.get("_embedded") or {}).get("threads")
    if embedded is not None and not _needs_full_threads(conversation, embedded):
        return embedded, 0
    url = f"{MAILBOX_API}/conversations/{conversation['id']}/threads"
    return list(_iter_hal(api, url, None, "threads")), 1


def _needs_full_threads(conversation: dict, threads: list[dict]) -> bool:
    """True when the embedded thread list may be incomplete.

    Help Scout truncates chat threads embedded in list responses, and the
    conversation's ``threads`` field counts its published non-note threads, so a
    shorter embedded list means something is missing.
    """
    if conversation.get("type") == "chat":
        return True
    if any(t.get("type") in ("chat", "beaconchat") for t in threads):
        return True
    published = [
        t for t in threads if t.get("type") != "note" and t.get("state", "published") == "published"
    ]
    expected = conversation.get("threads")
    return isinstance(expected, int) and len(published) < expected


def _conversation_row(conversation: dict, threads: list[dict], options: _Options) -> dict:
    """Flatten a conversation and its threads into a document row.

    Only fields that describe the conversation itself are used, in a fixed order,
    so the content hash changes only when the conversation does.
    """
    number = conversation.get("number")
    subject = (conversation.get("subject") or "").strip()
    tags = sorted({t.get("tag", "") for t in conversation.get("tags") or [] if t.get("tag")})
    header = []
    for label, value in (
        ("Status", conversation.get("status")),
        ("Channel", conversation.get("type")),
        ("Customer", _person(conversation.get("primaryCustomer"))),
        ("Assignee", _person(conversation.get("assignee"))),
        ("Tags", ", ".join(tags)),
        ("Created", conversation.get("createdAt")),
        ("Closed", conversation.get("closedAt")),
    ):
        if value:
            header.append(f"{label}: {value}")

    lines = []
    for thread in sorted(threads, key=lambda t: (t.get("createdAt") or "", t.get("id") or 0)):
        thread_type = thread.get("type")
        if thread_type == "lineitem":
            continue
        if thread_type == "note" and not options.include_notes:
            continue
        if thread.get("state", "published") != "published":
            continue
        text = _html_to_text(thread.get("body"))
        if not text:
            continue
        who = _person(thread.get("createdBy")) or thread_type or "unknown"
        label = _THREAD_LABELS.get(thread_type, thread_type or "message")
        lines.append(f"[{label}] {who} ({thread.get('createdAt') or 'unknown time'}): {text}")

    parts = ["\n".join(header)]
    if lines:
        parts.append("Thread:\n" + "\n\n".join(lines))
    web = ((conversation.get("_links") or {}).get("web") or {}).get("href")
    return {
        "id": f"conversation:{conversation['id']}",
        "title": f"#{number} {subject}".strip() if number is not None else subject,
        "content": "\n\n".join(p for p in parts if p),
        "url": web or None,
        "_deleted": False,
    }


def _person(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    name = " ".join(p for p in (value.get("first"), value.get("last")) if p).strip()
    return name or value.get("name") or ""


def _tombstone(conversation_id: Any) -> dict:
    return {"id": f"conversation:{conversation_id}", "_deleted": True}


# ---------------------------------------------------------------------------
# Docs articles
# ---------------------------------------------------------------------------


def _sync_articles(
    api: _Api, state: dict, options: _Options, stats: dict[str, int]
) -> Iterator[dict]:
    config = options.article_config()
    known: dict[str, str] = (
        {} if state.get("config") not in (None, config) else dict(state.get("versions") or {})
    )
    previously: set[str] = set(state.get("versions") or {})
    present: dict[str, str] = {}
    trusted = True

    for collection in _iter_docs(api, f"{DOCS_API}/collections", "collections", {}):
        collection_id = collection.get("id")
        if not collection_id or (
            options.collection_ids and collection_id not in options.collection_ids
        ):
            continue
        listing = _Listing()
        url = f"{DOCS_API}/collections/{collection_id}/articles"
        params = {"status": options.article_status, "sort": "createdAt", "order": "asc"}
        try:
            refs = list(_iter_docs(api, url, "articles", params, listing))
        except _UnstableListingError as exc:
            logger.warning("Help Scout: %s Nothing is forgotten this run.", exc)
            trusted = False
            continue
        if not listing.consistent():
            trusted = False
        for ref in refs:
            article_id = ref.get("id")
            if not article_id:
                continue
            marker = f"{ref.get('updatedAt')}|{ref.get('lastPublishedAt')}|{ref.get('status')}"
            present[article_id] = marker
            if known.get(article_id) == marker:
                continue
            try:
                payload = api.get(f"{DOCS_API}/articles/{article_id}")
            except _NotFoundError:
                present.pop(article_id, None)
                continue
            article = payload.get("article")
            if not article or not article.get("id"):
                raise RuntimeError("Help Scout Docs API returned an empty article.")
            _count(stats, "articles_synced")
            yield _article_row(article)

    forgotten = 0
    if trusted:
        for article_id in sorted(previously - set(present)):
            if _article_really_gone(api, article_id, options):
                forgotten += 1
                yield {"id": f"article:{article_id}", "_deleted": True}
            else:
                present[article_id] = (state.get("versions") or {}).get(article_id, "")
    else:
        for article_id in previously - set(present):
            present[article_id] = (state.get("versions") or {}).get(article_id, "")
    _count(stats, "articles_forgotten", forgotten)
    state["versions"] = present
    state["config"] = config


def _article_really_gone(api: _Api, article_id: str, options: _Options) -> bool:
    try:
        article = (api.get(f"{DOCS_API}/articles/{article_id}").get("article")) or {}
    except _NotFoundError:
        return True
    if options.collection_ids and article.get("collectionId") not in options.collection_ids:
        return True
    return options.article_status == "published" and article.get("status") != "published"


def _article_row(article: dict) -> dict:
    header = [f"URL: {article['publicUrl']}"] if article.get("publicUrl") else []
    body = _html_to_text(article.get("text"))
    return {
        "id": f"article:{article.get('id')}",
        "title": (article.get("name") or "").strip(),
        "content": "\n\n".join(p for p in ("\n".join(header), body) if p),
        "url": article.get("publicUrl") or None,
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(self._skip - 1, 0)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _html_to_text(value: Any) -> str:
    """Thread bodies and articles are HTML; keep the text and line breaks."""
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(str(value))
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line).strip()
