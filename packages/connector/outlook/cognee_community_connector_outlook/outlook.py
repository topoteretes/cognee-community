"""Outlook connector for cognee — a ``dlt`` source that turns mail and calendar into memory.

Sync selected Outlook mail folders and the primary calendar into cognee
through Microsoft Graph, incrementally and with forget-on-deletion.  The
resource produced here is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_outlook import outlook_source

    await cognee.remember(
        outlook_source(mail_folders=["inbox", "sentitems"], client_id=..., token_cache_path=...),
        dataset_name="my_outlook",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by message and event id
        max_rows_per_table=0,        # 0 = no row cap (mailboxes exceed the default 50)
    )

Microsoft Graph delta queries keep one ``deltaLink`` per mail folder and one for
the calendar window.  A message is forgotten once it is in none of the synced
folders, and a removed event is looked up by id first, since Graph also reports
events that only left the window.  Mail folders and the calendar fail
separately, and errors never delete.  The README explains the sync model, the
retry rules and the limitations.
"""

import re
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.sources.helpers.requests.session import Session
from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.paginators import BaseNextUrlPaginator
from requests import HTTPError, RequestException

from .graph_auth import (
    CALENDARS_READ,
    DEFAULT_AUTHORITY,
    MAIL_READ,
    MAIL_READ_BASIC,
    USER_READ,
    GraphAuth,
    GraphHostError,
    GraphLoginRequiredError,
    GraphTokenProvider,
)

logger = get_logger("outlook_connector")

# datetime.UTC only exists from Python 3.11.
UTC = timezone.utc
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
# cognee holds its dlt staging lock during a sync, so waits are kept short: a
# minute at most per wait, 3 attempts per request, and 2 minutes for every
# wait of one run together.  After that the request fails and the next run
# picks it up.
MAX_RETRY_WAIT_SECONDS = 60
MAX_REQUEST_ATTEMPTS = 3
WAIT_BUDGET_SECONDS = 120
RETRY_STATUSES = frozenset({429, *range(500, 600)})
RETRY_EXCEPTIONS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
# Runs an event lookup may fail before the connector stops asking.
MAX_LOOKUP_ATTEMPTS = 3
# Runs a forget may wait for a mail folder that keeps failing.
MAX_HELD_RUNS = 3
# dlt uses the resource name as a table name and a state key, so it has to be
# already normalized and short enough to be part of a file name.
_RESOURCE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
DEFAULT_MAIL_FOLDERS = ("inbox", "sentitems")
# Graph's well-known folder names, which it matches case-insensitively, with
# the name shown in a message's text.  Anything else is a folder id.
WELL_KNOWN_FOLDERS = {
    "archive": "Archive",
    "clutter": "Clutter",
    "conflicts": "Conflicts",
    "conversationhistory": "Conversation History",
    "deleteditems": "Deleted Items",
    "drafts": "Drafts",
    "inbox": "Inbox",
    "junkemail": "Junk Email",
    "localfailures": "Local Failures",
    "msgfolderroot": "Top of Information Store",
    "outbox": "Outbox",
    "recoverableitemsdeletions": "Recoverable Items Deletions",
    "scheduled": "Scheduled",
    "searchfolders": "Search Folders",
    "sentitems": "Sent Items",
    "serverfailures": "Server Failures",
    "syncissues": "Sync Issues",
}
# Moving a message to one of these is how Outlook deletes it.
UNSYNCABLE_FOLDERS = ("deleteditems", "junkemail", "recoverableitemsdeletions")
MAIL_FIELDS = [
    "subject",
    "from",
    "toRecipients",
    "ccRecipients",
    "sentDateTime",
    "receivedDateTime",
    "conversationId",
    "webLink",
    "hasAttachments",
]
# Error codes Graph uses when a delta token can no longer be used.
# Compared case-insensitively: Graph's docs and responses differ in casing.
RESYNC_ERROR_CODES = {"syncstatenotfound", "syncstateinvalid", "resyncrequired"}
# Row ids are prefixed so mail and events can never collide in one table.
MAIL_ID_PREFIX = "mail:"
EVENT_ID_PREFIX = "event:"
# Graph pages delta results 10 at a time by default.  50 keeps a 10,000
# message mailbox to 200 requests while a page of plain-text bodies stays small.
PAGE_SIZE = 50
PREFER_HEADERS = {
    "Prefer": ", ".join(
        [
            'IdType="ImmutableId"',
            'outlook.body-content-type="text"',
            # Event times come back in UTC and are rendered in the configured zone.
            'outlook.timezone="UTC"',
            f"odata.maxpagesize={PAGE_SIZE}",
        ]
    )
}


@dataclass(frozen=True, kw_only=True)
class _OutlookConfig:
    include_mail: bool
    mail_folders: tuple[str, ...]
    include_mail_bodies: bool
    mail_received_after: str | None
    include_calendar: bool
    calendar_days_back: int
    calendar_days_ahead: int
    calendar_window_refresh_days: int
    time_zone: str = "UTC"


class OutlookError(RuntimeError):
    """A Microsoft Graph request or delta round failed.

    The message holds the HTTP status at most, never a message, event or
    folder id, nor the request URL that requests puts in its own error text.
    """

    def __init__(self, message: str, status: int | None = None, connection: bool = False):
        super().__init__(message)
        self.status = status
        # Graph could not be reached at all, so every later call would fail too.
        self.connection = connection


class OutlookFolderNotFoundError(ValueError):
    """A folder in ``mail_folders`` that was never synced does not exist."""


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------
_sleep = time.sleep


def _retry_after(response: requests.Response) -> float | None:
    header = response.headers.get("Retry-After", "").strip()
    if header.isdigit():
        return float(header)
    try:
        moment = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


class _GraphSession(Session):
    """A requests session that retries 429, 5xx and dropped connections.

    Each wait follows ``Retry-After``, capped at a minute, and every wait of
    the session shares one budget.  A session lives for one run.  Once the
    attempts or the budget are used up, the last response or error is returned
    as it is and the caller decides what fails.
    """

    def __init__(self) -> None:
        super().__init__(raise_for_status=False)
        self.wait_left = float(WAIT_BUDGET_SECONDS)

    def send(self, request, **kwargs):
        attempt = 1
        while True:
            try:
                response = super().send(request, **kwargs)
            except HTTPError as exc:
                # dlt's paginate raises from a response hook, inside send.
                if not self._wait(attempt, exc.response):
                    raise
            except RETRY_EXCEPTIONS:
                if not self._wait(attempt, None):
                    raise
            else:
                if not self._wait(attempt, response):
                    return response
            attempt += 1

    def _wait(self, attempt: int, response: requests.Response | None) -> bool:
        """Sleep before the next attempt, or return False when there is none."""
        if response is not None and response.status_code not in RETRY_STATUSES:
            return False
        if attempt >= MAX_REQUEST_ATTEMPTS:
            return False
        seconds = _retry_after(response) if response is not None else None
        if seconds is None:
            seconds = 2.0 ** (attempt - 1)
        seconds = min(seconds, MAX_RETRY_WAIT_SECONDS)
        if seconds > self.wait_left:
            return False
        self.wait_left -= seconds
        if response is not None:
            response.close()
        _sleep(seconds)
        return True


def build_graph_client(auth: Any, base_url: str = GRAPH_BASE_URL) -> RESTClient:
    return RESTClient(base_url=base_url, auth=auth, session=_GraphSession())


def _check(check_active: Callable[[], None] | None) -> None:
    """The host's authorization checkpoint, run before every Graph request."""
    if check_active is not None:
        check_active()


class _ODataNextLinkPaginator(BaseNextUrlPaginator):
    """Follows Graph's ``@odata.nextLink`` until the round's last page."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[str] = set()

    def update_state(self, response, data=None) -> None:
        link = response.json().get("@odata.nextLink")
        if link in self._seen:
            # The link carries the delta token, so it stays out of the message.
            raise OutlookError("Microsoft Graph returned the same page twice; stopping.")
        if link:
            self._seen.add(link)
        self._next_reference = link


class _DeltaRound:
    """The pages of one delta round; ``delta_link`` is set after the last one."""

    def __init__(
        self,
        client: RESTClient,
        url: str,
        params: dict | None,
        check_active: Callable[[], None] | None,
    ) -> None:
        self.client = client
        self.url = url
        self.params = params
        self.check_active = check_active
        self.delta_link: str | None = None

    def __iter__(self) -> Iterator[list[dict]]:
        _check(self.check_active)
        for page in self.client.paginate(
            self.url,
            params=self.params,
            headers=PREFER_HEADERS,
            paginator=_ODataNextLinkPaginator(),
            data_selector="value",
        ):
            self.delta_link = page.response.json().get("@odata.deltaLink") or self.delta_link
            yield list(page)
            # The paginator requests the next page when the loop continues.
            _check(self.check_active)
        if not self.delta_link:
            # Without it the next run could not resume, so treat it as a failure.
            raise OutlookError("Microsoft Graph delta round ended without a deltaLink.")


def _status(exc: HTTPError) -> int | None:
    return exc.response.status_code if exc.response is not None else None


def _error_code(exc: HTTPError) -> str | None:
    try:
        return exc.response.json()["error"]["code"]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _needs_resync(exc: HTTPError) -> bool:
    return _status(exc) == 410 or (_error_code(exc) or "").lower() in RESYNC_ERROR_CODES


# Lookup results besides the item itself and None for a confirmed 404.
_FAILED = object()  # this item could not be read
_SKIPPED = object()  # not asked: Graph failed on an earlier lookup this run


class _Lookups:
    """Looks single events up by id, one unreadable event at a time.

    A 4xx or a body that is not JSON fails only that event.  After the first
    5xx the remaining lookups wait for the next run, so a struggling Graph is
    not asked once per event.  A 401 and a 429 raise: every later request would
    fail the same way.
    """

    def __init__(self, client: RESTClient, check_active: Callable[[], None] | None) -> None:
        self.client = client
        self.check_active = check_active
        self.unavailable = False

    def get(self, path: str) -> Any:
        if self.unavailable:
            return _SKIPPED
        _check(self.check_active)
        response = self.client.get(path, headers=PREFER_HEADERS)
        status = response.status_code
        if status == 404:
            return None
        if status in (401, 429):
            response.raise_for_status()
        if status >= 500:
            self.unavailable = True
        if status >= 400:
            return _FAILED
        try:
            return response.json()
        except requests.exceptions.JSONDecodeError:
            return _FAILED


def _attempts(saved: Any) -> dict[str, int]:
    """Failed lookups per id; an older state saved a plain list of ids."""
    if isinstance(saved, dict):
        return {str(k): int(v) for k, v in saved.items()}
    # A listed id failed at least once.
    return dict.fromkeys(saved or [], 1)


def _least_tried_first(ids: set[str], attempts: dict[str, int]) -> list[str]:
    # An event that keeps failing goes last, so it cannot starve the others.
    return sorted(ids, key=lambda i: (attempts.get(i, 0), i))


# ---------------------------------------------------------------------------
# Sync state machine (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
_PART_STATS = {
    "mail": ("mail_changed", "mail_deleted", "mail_deferred", "mail_folders_failed"),
    "calendar": (
        "events_changed",
        "events_deleted",
        "events_left_window",
        "events_failed",
        "events_given_up",
    ),
}
# Counted as soon as a row is yielded, so they hold even when the part fails later.
_YIELDED_STATS = ("mail_changed", "events_changed")
_FAILURE_REASONS = ("auth", "rate_limit", "connection", "mail", "calendar")
# These end the run: the other part would fail the same way.
_RUN_ENDING = ("auth", "rate_limit", "connection")


def _iter_rows(
    client: RESTClient,
    config: _OutlookConfig,
    state: dict,
    stats: dict[str, int] | None = None,
    now: datetime | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield one row per new or changed message and event, plus tombstones.

    ``state`` is a plain dict standing in for dlt's resource state, so this is
    directly unit-testable.  Upserts are yielded page by page as Graph returns
    them; merge makes a repeated one harmless when a part fails and runs again.
    Tombstones and the new state only come once a part has finished.  Mail and
    calendar each keep their old state when they fail; the run raises only when
    neither finished.
    """
    if stats is None:
        stats = {}
    stats.clear()
    for keys in _PART_STATS.values():
        stats.update(dict.fromkeys(keys, 0))
    stats["failed"] = 0
    stats.update({f"failed_{reason}": 0 for reason in _FAILURE_REASONS})
    now = now or datetime.now(UTC)

    parts = [p for p in ("mail", "calendar") if getattr(config, f"include_{p}")]
    finished: dict[str, tuple[dict, list[str]]] = {}
    last_error: Exception | None = None
    for part in parts:
        part_stats = dict.fromkeys(_PART_STATS[part], 0)
        try:
            if part == "mail":
                new_state, deletes = yield from _sync_mail(
                    client, config, state.get("mail", {}), part_stats, check_active
                )
            else:
                new_state, deletes = yield from _sync_calendar(
                    client, config, state.get("calendar", {}), part_stats, now, check_active
                )
        except (RequestException, OutlookError, GraphLoginRequiredError) as exc:
            last_error, reason = _part_failure(exc)
            for key in _YIELDED_STATS:
                stats[key] += part_stats.get(key, 0)
            stats["failed"] += 1
            stats[f"failed_{reason or part}"] += 1
            logger.warning(
                "Outlook: the %s sync failed (HTTP %s); it keeps its state and is retried "
                "on the next run.",
                part,
                getattr(last_error, "status", None),
            )
            if reason in _RUN_ENDING:
                break
            continue
        stats.update(part_stats)
        finished[part] = (new_state, deletes)

    if last_error is not None and not finished:
        # Nothing finished, so there is no state to save and nothing is lost by raising.
        if isinstance(last_error, GraphLoginRequiredError):
            raise last_error
        raise OutlookError(f"Outlook sync failed: {last_error}", last_error.status) from None

    # Totals under the keys the other connectors publish, for host dashboards.
    # ``failed`` counts what the next run tries again: parts, folders and events.
    stats["scanned"] = stats["mail_changed"] + stats["events_changed"]
    stats["deleted"] = stats["mail_deleted"] + stats["events_deleted"]
    stats["failed"] += stats["mail_folders_failed"] + stats["events_failed"]

    for _state, deletes in finished.values():
        for row_id in deletes:
            yield {"id": row_id, "_deleted": True}

    for part, (new_state, _deletes) in finished.items():
        state[part] = new_state
    logger.info(
        "Outlook: synced %d changed, %d deleted, %d to retry.",
        stats["scanned"],
        stats["deleted"],
        stats["failed"],
    )


def _graph_error(exc: RequestException) -> OutlookError:
    """A clean error: requests puts the URL, with ids and the delta token, in its text."""
    if isinstance(exc, GraphHostError):
        return OutlookError("A Microsoft Graph page link pointed to another host; not followed.")
    if isinstance(exc, requests.exceptions.JSONDecodeError):
        return OutlookError("Microsoft Graph answered with something other than JSON.")
    response = getattr(exc, "response", None)
    status = response.status_code if response is not None else None
    if status is None:
        return OutlookError("Microsoft Graph could not be reached.", connection=True)
    return OutlookError(f"Microsoft Graph request failed: HTTP {status}", status)


def _part_failure(exc: Exception) -> tuple[Exception, str | None]:
    """The clean error for a failed part or folder, and whether it ends the run."""
    if isinstance(exc, GraphLoginRequiredError):
        return exc, "auth"
    error = exc if isinstance(exc, OutlookError) else _graph_error(exc)
    if error.connection:
        return error, "connection"
    if error.status == 401:
        return error, "auth"
    if error.status == 429:
        return error, "rate_limit"
    return error, None


# --- mail ------------------------------------------------------------------
def _sync_mail(
    client: RESTClient,
    config: _OutlookConfig,
    mail_state: dict,
    stats: dict[str, int],
    check_active: Callable[[], None] | None = None,
) -> Generator[dict, None, tuple[dict, list[str]]]:
    """One delta round per selected folder.

    Yields upserts page by page and returns the new mail state and the ids to
    forget.  A folder that fails keeps its old delta link while the others
    sync; the part only fails when every folder did.
    """
    options = {
        "bodies": config.include_mail_bodies,
        "received_after": config.mail_received_after,
    }
    old_links: dict[str, str] = dict(mail_state.get("delta_links", {}))
    # message id -> folders it is currently in, across the synced folders.
    members: dict[str, list[str]] = {k: list(v) for k, v in mail_state.get("members", {}).items()}
    # Messages that left their last folder while a move could still explain
    # it, with the runs they have waited so far.
    pending: dict[str, int] = dict(mail_state.get("pending", {}))
    synced = set(old_links) | {f for folders in members.values() for f in folders}
    if mail_state.get("options") != options:
        # $select and $filter live inside the deltaLink, so other options need
        # a fresh round of every folder.
        old_links = {}
    new_links: dict[str, str] = {}
    touched: set[str] = set()
    held: set[str] = set()
    emitted: set[str] = set()

    def leave(message_id: str, folder: str, hold: bool) -> None:
        folders = members.get(message_id)
        if not folders or folder not in folders:
            return
        folders.remove(folder)
        touched.add(message_id)
        if hold:
            held.add(message_id)

    def leave_folder(folder: str, hold: bool) -> None:
        for message_id, folders in list(members.items()):
            if folder in folders:
                leave(message_id, folder, hold)

    def read_round(
        folder: str, url: str, params: dict | None, hold: bool, fresh: bool
    ) -> Generator[dict, None, str | None]:
        listed: set[str] = set()
        delta = _DeltaRound(client, url, params, check_active)
        for page in delta:
            for item in page:
                message_id = item["id"]
                if "@removed" in item:
                    leave(message_id, folder, hold)
                    continue
                listed.add(message_id)
                if folder not in members.setdefault(message_id, []):
                    members[message_id].append(folder)
                if message_id not in emitted:
                    emitted.add(message_id)
                    stats["mail_changed"] += 1
                    yield _mail_row(item, folder, config.include_mail_bodies)
        if fresh:
            # A fresh round lists the whole folder: what it lacks has left it.
            for message_id, folders in list(members.items()):
                if folder in folders and message_id not in listed:
                    leave(message_id, folder, hold)
        return delta.delta_link

    def read_folder(folder: str, position: int) -> Generator[dict, None, str | None]:
        # A message that leaves a folder read after another one may have moved
        # into that earlier folder after it was read, so its forget waits a run.
        hold = position > 0
        link = old_links.get(folder)
        if link:
            try:
                return (yield from read_round(folder, link, None, hold, fresh=False))
            except HTTPError as exc:
                if not (_needs_resync(exc) or _status(exc) == 404):
                    raise
                logger.info("Outlook: a mail folder's delta token expired; listing it again.")
        try:
            return (
                yield from read_round(
                    folder,
                    f"/me/mailFolders/{folder}/messages/delta",
                    params,
                    hold,
                    fresh=True,
                )
            )
        except HTTPError as exc:
            if _status(exc) not in (400, 404):
                raise
            folder_status = _folder_status(client, folder, check_active)
            if folder not in synced and folder_status in (400, 404):
                raise OutlookFolderNotFoundError(_folder_not_found(folder, position)) from None
            if folder_status != 404:
                raise
        logger.warning("Outlook: a synced mail folder was not found; forgetting its mail.")
        leave_folder(folder, hold)
        return None

    # Folders dropped from the config: their mail is out of scope now.
    for folder in sorted(synced - set(config.mail_folders)):
        leave_folder(folder, hold=False)

    params = {"$select": ",".join(_mail_fields(config))}
    if config.mail_received_after:
        params["$filter"] = f"receivedDateTime ge {config.mail_received_after}"

    failed_error: Exception | None = None
    for position, folder in enumerate(config.mail_folders):
        try:
            link = yield from read_folder(folder, position)
        except (RequestException, OutlookError) as exc:
            error, reason = _part_failure(exc)
            if reason in _RUN_ENDING:
                raise
            failed_error = exc
            stats["mail_folders_failed"] += 1
            logger.warning(
                "Outlook: a mail folder failed (HTTP %s); it is retried on the next run.",
                getattr(error, "status", None),
            )
            if old_links.get(folder):
                new_links[folder] = old_links[folder]
            continue
        if link:
            new_links[folder] = link

    if failed_error is not None and stats["mail_folders_failed"] == len(config.mail_folders):
        raise failed_error

    deletes: list[str] = []
    next_pending: dict[str, int] = {}
    for message_id in sorted(touched | set(pending)):
        if members.get(message_id):
            continue
        members.pop(message_id, None)
        waited = pending.get(message_id, 0)
        if (message_id in held or stats["mail_folders_failed"]) and waited < MAX_HELD_RUNS:
            # A failed folder, or one read earlier, may hold the message now;
            # decide on the next run, but never wait on a broken folder forever.
            next_pending[message_id] = waited + 1
            continue
        # In none of the synced folders any more: deleted, moved to Deleted
        # Items or Junk, or moved to a folder that is not synced.
        deletes.append(MAIL_ID_PREFIX + message_id)
    stats["mail_deleted"] = len(deletes)
    stats["mail_deferred"] = len(next_pending)

    new_state = {
        "delta_links": new_links,
        "members": members,
        "pending": next_pending,
        "options": options,
    }
    return new_state, deletes


def _folder_status(client: RESTClient, folder: str, check_active: Callable[[], None] | None) -> int:
    """The HTTP status of the folder itself, to tell a missing folder from other errors."""
    _check(check_active)
    response = client.get(f"/me/mailFolders/{folder}", headers=PREFER_HEADERS)
    if response.status_code in (401, 429):
        response.raise_for_status()
    return response.status_code


def _folder_not_found(folder: str, position: int) -> str:
    # A name is the caller's own setting; a folder id stays out of the message.
    label = f'"{folder}"' if re.fullmatch(r"[a-z]{1,40}", folder) else f"number {position + 1}"
    return (
        f"Mail folder {label} in mail_folders was not found. Use a well-known name like "
        '"inbox" or "archive", or a folder id.'
    )


def _mail_fields(config: _OutlookConfig) -> list[str]:
    return [*MAIL_FIELDS, "body"] if config.include_mail_bodies else list(MAIL_FIELDS)


def _mail_row(item: dict, folder: str, include_body: bool = True) -> dict:
    subject = item.get("subject") or "(no subject)"
    lines = [
        f"Email: {subject}",
        f"From: {_person(item.get('from'))}",
        f"To: {_people(item.get('toRecipients'))}",
    ]
    if item.get("ccRecipients"):
        lines.append(f"Cc: {_people(item['ccRecipients'])}")
    lines += [
        f"Sent: {item.get('sentDateTime') or item.get('receivedDateTime') or 'unknown'}",
        f"Folder: {WELL_KNOWN_FOLDERS.get(folder, folder)}",
    ]
    if item.get("conversationId"):
        lines.append(f"Thread: {item['conversationId']}")
    if item.get("hasAttachments"):
        lines.append("Attachments: yes, not ingested")
    # Headers-only mode never renders a body, even if Graph sent one.
    body = ((item.get("body") or {}).get("content") or "").strip() if include_body else ""
    content = "\n".join(lines) + (f"\n\n{body}" if body else "")
    return {
        "id": MAIL_ID_PREFIX + item["id"],
        "title": subject,
        "content": content,
        "url": item.get("webLink"),
        "_deleted": False,
    }


# --- calendar --------------------------------------------------------------
def _sync_calendar(
    client: RESTClient,
    config: _OutlookConfig,
    calendar_state: dict,
    stats: dict[str, int],
    now: datetime,
    check_active: Callable[[], None] | None = None,
) -> Generator[dict, None, tuple[dict, list[str]]]:
    """One delta round over the date window.

    Yields upserts page by page and returns the new calendar state and the
    ids to forget.
    """
    known: set[str] = set(calendar_state.get("known", []))
    # Failed lookups per id: a removal still to confirm, and a series master
    # still to fetch.  Delta rounds won't list them again.
    recheck = _attempts(calendar_state.get("recheck"))
    retry_masters = _attempts(calendar_state.get("retry_masters"))
    zone = _zone(config.time_zone)
    link = calendar_state.get("delta_link")
    window = {
        "days_back": config.calendar_days_back,
        "days_ahead": config.calendar_days_ahead,
    }
    opened_at = calendar_state.get("opened_at")
    snapshot = (
        not link
        or calendar_state.get("window") != window
        or opened_at is None
        or now - datetime.fromisoformat(opened_at)
        >= timedelta(days=config.calendar_window_refresh_days)
    )

    removed: set[str] = set()
    seen: set[str] = set()
    cancelled: set[str] = set()
    series_ids: set[str] = set()
    deletes: list[str] = []

    def forget(event_id: str) -> None:
        known.discard(event_id)
        deletes.append(EVENT_ID_PREFIX + event_id)

    def read_round(url: str, params: dict | None) -> Generator[dict, None, str | None]:
        delta = _DeltaRound(client, url, params, check_active)
        for page in delta:
            for item in page:
                event_id = item["id"]
                if "@removed" in item:
                    removed.add(event_id)
                    continue
                seen.add(event_id)
                removed.discard(event_id)
                if item.get("type") == "occurrence":
                    # Graph lists each occurrence of a recurring series with
                    # only its id and times; the series master carries the
                    # details and becomes the one document for the series.
                    if item.get("seriesMasterId"):
                        series_ids.add(item["seriesMasterId"])
                elif item.get("isCancelled"):
                    cancelled.add(event_id)
                else:
                    known.add(event_id)
                    stats["events_changed"] += 1
                    yield _event_row(item, zone)
        return delta.delta_link

    if not snapshot:
        try:
            link = yield from read_round(link, None)
        except HTTPError as exc:
            if not _needs_resync(exc):
                raise
            logger.info("Outlook: calendar delta token expired; resyncing.")
            snapshot = True
    if snapshot:
        # A calendarView window is fixed for the life of its deltaLink, so a
        # rolling window needs a fresh round now and then.
        start = now - timedelta(days=config.calendar_days_back)
        end = now + timedelta(days=config.calendar_days_ahead)
        link = yield from read_round(
            "/me/calendarView/delta", {"startDateTime": _iso(start), "endDateTime": _iso(end)}
        )
        opened_at = now.isoformat()

    for event_id in sorted(cancelled & known):
        forget(event_id)

    lookups = _Lookups(client, check_active)

    def lookup_failed(event_id: str, result: Any, attempts: dict[str, int]) -> int | None:
        """Attempts so far, or None once the event is given up."""
        tries = attempts.get(event_id, 0) + (result is _FAILED)
        if tries >= MAX_LOOKUP_ATTEMPTS:
            stats["events_given_up"] += 1
            logger.warning("Outlook: an event could not be read for 3 runs; giving up on it.")
            return None
        stats["events_failed"] += 1
        return tries

    # Occurrences prove their series is alive and changed: the master holds
    # the title, attendees and recurrence, and Graph may not list it in the
    # round (a series that started before the window), so fetch it by id.
    next_masters: dict[str, int] = {}
    for master_id in _least_tried_first((series_ids | set(retry_masters)) - seen, retry_masters):
        master = lookups.get(f"/me/events/{master_id}")
        if master is _FAILED or master is _SKIPPED:
            tries = lookup_failed(master_id, master, retry_masters)
            if tries is not None:
                next_masters[master_id] = tries
        elif master is None or master.get("isCancelled"):
            if master_id in known:
                forget(master_id)
        else:
            known.add(master_id)
            stats["events_changed"] += 1
            yield _event_row(master, zone)
    seen |= series_ids

    if snapshot:
        # Known events missing from the fresh listing were deleted or left
        # the window; the lookup below tells which.
        removed |= known - seen
    removed |= set(recheck)

    next_recheck: dict[str, int] = {}
    # Only events that were stored: a deleted series also reports every
    # occurrence, and none of those were ever ingested.
    for event_id in _least_tried_first((removed - seen) & known, recheck):
        # Graph also reports an event that left the window as removed, so
        # check it before forgetting anything.
        event = lookups.get(f"/me/events/{event_id}")
        if event is _FAILED or event is _SKIPPED:
            tries = lookup_failed(event_id, event, recheck)
            if tries is None:
                # Graph no longer lists it and it has not been readable for
                # several runs: forget it rather than keep it unchecked forever.
                forget(event_id)
            else:
                next_recheck[event_id] = tries
        elif event is None or event.get("isCancelled"):
            forget(event_id)
        else:
            known.discard(event_id)
            stats["events_left_window"] += 1
    stats["events_deleted"] = len(deletes)

    new_state = {
        "delta_link": link,
        "opened_at": opened_at,
        "window": window,
        "known": sorted(known),
        "recheck": next_recheck,
        "retry_masters": next_masters,
    }
    return new_state, deletes


def _event_row(item: dict, zone: tzinfo = UTC) -> dict:
    subject = item.get("subject") or "(no title)"
    lines = [f"Calendar event: {subject}"]
    if item.get("isAllDay"):
        lines.append(f"When: {_all_day(item)}")
    else:
        lines.append(f"When: {_span(item.get('start'), item.get('end'), zone)}")
    location = (item.get("location") or {}).get("displayName")
    if location:
        lines.append(f"Location: {location}")
    lines.append(f"Organizer: {_person(item.get('organizer'))}")
    # Graph's order is not stable between fetches, and the content hash decides
    # whether the event is re-cognified.
    attendees = sorted(item.get("attendees") or [], key=_attendee_key)
    if attendees:
        lines.append("Attendees: " + ", ".join(f"{_person(a)} ({_response(a)})" for a in attendees))
    if item.get("seriesMasterId"):
        lines.append(f"Series: {item['seriesMasterId']}")
    elif (item.get("recurrence") or {}).get("pattern"):
        pattern = item["recurrence"]["pattern"]
        span = item["recurrence"].get("range") or {}
        repeats = f"Repeats: {pattern.get('type')}"
        if span.get("startDate"):
            repeats += f" from {span['startDate']}"
        if span.get("type") == "endDate" and span.get("endDate"):
            repeats += f" until {span['endDate']}"
        lines.append(repeats)
    online = (item.get("onlineMeeting") or {}).get("joinUrl")
    if online:
        lines.append(f"Online meeting: {online}")
    body = ((item.get("body") or {}).get("content") or "").strip()
    content = "\n".join(lines) + (f"\n\n{body}" if body else "")
    return {
        "id": EVENT_ID_PREFIX + item["id"],
        "title": subject,
        "content": content,
        "url": item.get("webLink"),
        "_deleted": False,
    }


# --- formatting -------------------------------------------------------------
def _person(recipient: dict | None) -> str:
    address = (recipient or {}).get("emailAddress") or {}
    name, email = address.get("name"), address.get("address")
    if name and email and name != email:
        return f"{name} <{email}>"
    return email or name or "unknown"


def _response(attendee: dict) -> str:
    return (attendee.get("status") or {}).get("response") or "none"


def _attendee_key(attendee: dict) -> tuple[str, str, str]:
    # Every rendered part is in the key, so equal addresses still sort the same way.
    address = (attendee or {}).get("emailAddress") or {}
    return (
        (address.get("address") or "").lower(),
        address.get("name") or "",
        _response(attendee or {}),
    )


def _people(recipients: list[dict] | None) -> str:
    return ", ".join(_person(r) for r in recipients or []) or "none"


def _zone(name: str | None) -> tzinfo | None:
    """A time zone by IANA name, or None when Python does not know the name."""
    if not name:
        return None
    if name.upper() in ("UTC", "ETC/UTC"):
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _moment(value: dict | None) -> datetime | None:
    """A Graph dateTimeTimeZone as an aware datetime; times are asked for in UTC."""
    text = (value or {}).get("dateTime")
    if not text:
        return None
    try:
        # Graph sends seven fractional digits, which fromisoformat rejects before 3.11.
        moment = datetime.fromisoformat(text[:19])
    except ValueError:
        return None
    return moment.replace(tzinfo=_zone((value or {}).get("timeZone")) or UTC)


def _when(value: dict | None) -> str:
    value = value or {}
    return f"{value.get('dateTime', 'unknown')} {value.get('timeZone', '')}".strip()


def _span(start: dict | None, end: dict | None, zone: tzinfo) -> str:
    first, last = _moment(start), _moment(end)
    if first is None or last is None:
        return f"{_when(start)} to {_when(end)}"
    first, last = first.astimezone(zone), last.astimezone(zone)
    until = f"{last:%H:%M}" if last.date() == first.date() else f"{last:%Y-%m-%d %H:%M}"
    return f"{first:%Y-%m-%d %H:%M} to {until} ({zone})"


def _day(moment: datetime, original_zone: str | None) -> date:
    """The calendar day an all-day boundary falls on.

    An all-day event starts at midnight in its own zone, which UTC shows as
    another hour.  The event's zone gives the day when Python knows its name;
    otherwise the nearest midnight does, right for every zone within 12 hours
    of UTC.
    """
    zone = _zone(original_zone)
    if zone is not None:
        local = moment.astimezone(zone)
        if (local.hour, local.minute) == (0, 0):
            return local.date()
    return (moment.astimezone(UTC) + timedelta(hours=12)).date()


def _all_day(item: dict) -> str:
    """The span of an all-day event, with its last day inclusive.

    Graph ends an all-day event at midnight after its last day: a one-day event
    on 2026-11-08 ends on 2026-11-09, so a three-day one reads "2026-11-08 to
    2026-11-10", and a one-day one needs no "to".
    """
    start, end = _moment(item.get("start")), _moment(item.get("end"))
    if start is None or end is None:
        return f"all day, {_when(item.get('start'))}"
    first = _day(start, item.get("originalStartTimeZone"))
    last = _day(end, item.get("originalEndTimeZone")) - timedelta(days=1)
    if last <= first:
        return f"all day, {first}"
    return f"all day, {first} to {last}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_mail_folders(mail_folders: list[str] | str | None) -> tuple[str, ...]:
    """Folders from the argument, or Inbox and Sent Items, de-duplicated.

    Well-known names like ``Outbox`` are case-insensitive, so they are
    lowercased; folder ids are not.  Blank entries are dropped.
    """
    if mail_folders is None:
        mail_folders = list(DEFAULT_MAIL_FOLDERS)
    elif isinstance(mail_folders, str):
        mail_folders = [mail_folders]
    folders = []
    for folder in mail_folders:
        folder = folder.strip()
        if folder:
            folders.append(folder.lower() if folder.lower() in WELL_KNOWN_FOLDERS else folder)
    return tuple(dict.fromkeys(folders))


def _normalize_received_after(value: str | None) -> str | None:
    """An ISO date or time as Graph's filter expects it, in UTC."""
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError(
            'mail_received_after must be an ISO date or time, like "2026-01-01T00:00:00Z".'
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return _iso(moment)


def _whole_days(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def outlook_source(
    mail_folders: list[str] | str | None = None,
    *,
    include_mail: bool = True,
    include_calendar: bool = True,
    include_mail_bodies: bool = True,
    mail_received_after: str | None = None,
    calendar_days_back: int = 180,
    calendar_days_ahead: int = 180,
    calendar_window_refresh_days: int = 7,
    time_zone: str = "UTC",
    client_id: str | None = None,
    authority: str | None = None,
    token_cache_path: str | None = None,
    account: str | None = None,
    access_token: str | None = None,
    resource_name: str = "outlook_items",
    check_active: Callable[[], None] | None = None,
    token_provider: GraphTokenProvider | None = None,
    base_url: str = GRAPH_BASE_URL,
    service: RESTClient | None = None,
):
    """Return a ``dlt`` resource yielding Outlook mail and calendar documents.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.  Settings are taken as arguments only; the example
    reads them from ``OUTLOOK_*`` environment variables.  Building the source
    never touches the network.

    Args:
        mail_folders: Folders to sync, as well-known names like ``inbox``,
            ``sentitems`` and ``archive``, or folder ids.  Defaults to Inbox and
            Sent Items.  Deleted Items and Junk Email are refused: moving a
            message there is how Outlook deletes it.
        include_mail: Sync mail.  Turning it off keeps already-synced mail.
        include_calendar: Sync the primary calendar.
        include_mail_bodies: Ingest message bodies.  When False only headers
            are read, which needs only the ``Mail.ReadBasic`` permission.
        mail_received_after: Only mail received on or after this ISO date or
            time, like ``2026-01-01T00:00:00Z``.
        calendar_days_back / calendar_days_ahead: The calendar window, in days.
        calendar_window_refresh_days: How often the window moves forward, in days.
        time_zone: IANA zone event times are written in, like ``Asia/Kolkata``.
        client_id: Entra app (client) id.
        authority: Sign-in authority.  Defaults to ``common``, which accepts
            personal and work or school accounts.
        token_cache_path: Private file for MSAL's token cache, filled once by
            ``examples/login.py``.  A sync never logs in interactively.
        account: The address to sync when the token cache holds several.
        access_token: A ready-made Graph token for quick tries.  It is never
            refreshed.
        resource_name: Stable dlt resource name: lowercase letters, digits and
            underscores, up to 64 characters.  Give each mailbox its own name
            when several sync into one dataset, so their state stays separate.
        check_active: Optional host authorization checkpoint, asked before
            every Graph request.
        token_provider: A pre-built ``GraphTokenProvider``, for hosts and tests.
        base_url: Graph endpoint, for tests and national clouds.  The token is
            only ever sent to this host.
        service: A pre-built Graph ``RESTClient``, for hosts and tests.  When
            given, the auth arguments above are not used and it is not closed.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Outlook sync requires a Cognee build with table-scoped DLT document cleanup. "
            "Upgrade Cognee before syncing to avoid deleting another source's data."
        )
    if not isinstance(resource_name, str) or not _RESOURCE_NAME.fullmatch(resource_name):
        # An empty name would silently drop the pipeline scope, and a name dlt
        # normalizes differently would share a table with another mailbox.
        raise ValueError(
            "resource_name must be 1 to 64 lowercase letters, digits or underscores, "
            'starting with a letter, like "work_outlook".'
        )
    if not (include_mail or include_calendar):
        raise ValueError("Nothing to sync: include_mail and include_calendar are both False.")

    mail_folders = _normalize_mail_folders(mail_folders)
    if include_mail and not mail_folders:
        # An empty list would forget every synced message, so refuse it.
        raise ValueError(
            "mail_folders is empty. Pass folders like ['inbox'], or include_mail=False."
        )
    if any(folder in UNSYNCABLE_FOLDERS for folder in mail_folders):
        raise ValueError(
            "Deleted Items and Junk Email cannot be synced: moving mail there is how "
            "Outlook deletes it, so it is forgotten instead."
        )
    if not (_whole_days(calendar_days_back) and _whole_days(calendar_days_ahead)) or (
        calendar_days_back < 0
        or calendar_days_ahead < 0
        or calendar_days_back + calendar_days_ahead == 0
    ):
        raise ValueError(
            "calendar_days_back and calendar_days_ahead must be whole days, 0 or more, "
            "and not both 0."
        )
    if not _whole_days(calendar_window_refresh_days) or calendar_window_refresh_days < 1:
        raise ValueError("calendar_window_refresh_days must be a whole number of days, 1 or more.")
    if _zone(time_zone) is None:
        raise ValueError('time_zone must be an IANA zone name, like "Europe/Berlin" or "UTC".')

    config = _OutlookConfig(
        include_mail=include_mail,
        mail_folders=mail_folders,
        include_mail_bodies=include_mail_bodies,
        mail_received_after=_normalize_received_after(mail_received_after),
        include_calendar=include_calendar,
        calendar_days_back=calendar_days_back,
        calendar_days_ahead=calendar_days_ahead,
        calendar_window_refresh_days=calendar_window_refresh_days,
        time_zone=time_zone,
    )

    provider = None
    if service is None:
        scopes = [USER_READ]
        if include_mail:
            scopes.append(MAIL_READ if include_mail_bodies else MAIL_READ_BASIC)
        if include_calendar:
            scopes.append(CALENDARS_READ)
        provider = token_provider or GraphTokenProvider(
            client_id=client_id,
            scopes=scopes,
            authority=authority or DEFAULT_AUTHORITY,
            token_cache_path=token_cache_path,
            access_token=access_token,
            account=account,
        )

    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        write_disposition="merge",
        primary_key="id",
        # `_deleted` is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def outlook_items():
        # A client per run: its retry wait budget is per run, and it is closed
        # when the run ends.
        client = (
            service
            if service is not None
            else build_graph_client(GraphAuth(provider, base_url), base_url)
        )
        try:
            rows = _iter_rows(
                client, config, dlt.current.resource_state(), stats, check_active=check_active
            )
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if service is None:
                client.session.close()

    resource = outlook_items()
    # Opt into the document ingestion path: each message and event becomes a
    # text document that flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, "outlook")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics contain counts only, never subjects or content.
    resource.cognee_sync_stats = stats
    return resource
