"""Shortcut connector for cognee: a ``dlt`` source that turns a Shortcut workspace into memory.

One document per story (with its comments), epic and iteration, synced
incrementally with forget-on-delete. Hand the source to :func:`cognee.remember`::

    await cognee.remember(
        shortcut_source(),  # SHORTCUT_API_TOKEN from env
        dataset_name="shortcut",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

The README describes what is stored, how changes and deletions are found, and
what a run does when Shortcut fails part of the way through.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("shortcut_connector")

# Default dlt resource / staging-table name, and the document-source tag for its rows.
SHORTCUT_TABLE_NAME = "shortcut_documents"
SHORTCUT_SOURCE_NAME = "shortcut"

_API_BASE = "https://api.app.shortcut.com/api/v3"
# The story listing is one unpaginated response; 2,609 stories took up to 14 seconds.
_TIMEOUT_SECONDS = 120

# Five attempts per request, with 1, 2, 4, 8 s between them, or Shortcut's
# Retry-After on a 429.
_MAX_RETRIES = 5
# 429 is Shortcut's rate limit (200 requests a minute, per its docs); the 5xx
# answers are the usual temporary ones.
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
# The longest single wait, and the most a run waits in all. cognee holds its dlt
# staging lock during a sync, so a longer Retry-After, or a wait past the
# budget, stops the run cleanly instead (it resumes next run).
_MAX_WAIT_SECONDS = 60
_MAX_TOTAL_WAIT_SECONDS = 120
# Fresh item failures in a row (5xx or no answer, after retries) that mean
# Shortcut itself is down: the run stops cleanly. One or two can be a single
# broken item; three different items failing back to back are rarely a
# coincidence, and a stop costs nothing, as the next run resumes. Items already
# kept for a retry never count.
_OUTAGE_STREAK = 3
# A listed story or iteration that answers 403/404 at the same stamp is
# forgotten after 3 runs AND 24 hours, counting only runs that finished and
# showed access to work (see _Run.settle): a host on a fast cron cannot forget
# an item in minutes, and a weekly cron still needs three observations. A
# forgotten item is asked again when its stamp moves, or after another day.
_GIVE_UP_RUNS = 3
_GIVE_UP_AFTER = timedelta(hours=24)
_REASK_AFTER = timedelta(hours=24)
# Forgotten items asked again per run, oldest first, after everything else.
_MAX_REASKS = 10
# The run's clock is the later of Shortcut's Date header and the local clock.
# They differing by more than this is logged (a proxy may cache a stale Date).
_CLOCK_TOLERANCE = timedelta(seconds=60)

# A story listing with this many results or more is treated as possibly
# truncated and split. Listings up to 2,609 stories were verified complete.
_WINDOW_CAP = 2500
# Shortcut timestamps and date filters have one-second resolution.
_SECOND = timedelta(seconds=1)
# Where the first window starts.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_STAMP = "1970-01-01T00:00:00Z"
# How far before the cursor the updated_at listing starts (see _Run.list).
# The overlap only has to catch a story stamped at or just before the cursor
# that the previous listing could not see yet; a minute is a wide margin.
_CURSOR_OVERLAP = timedelta(seconds=60)
# How long after a story's updated_at second a fetch must happen before that
# copy counts as final for the timestamp (see _is_settled): the one second of
# timestamp resolution, plus one for any clock difference between Shortcut's
# servers.
_SETTLE_MARGIN = timedelta(seconds=2)

_STAT_KEYS = ("scanned", "skipped", "failed", "deleted", "kept", "given_up")


class ShortcutAPIError(RuntimeError):
    """An error answer from Shortcut (``status``), or no answer at all (``status`` 0).

    The message names only the status and the endpoint. Shortcut's own
    explanation is on ``detail``, with the token cut out; a response body never is.
    """

    def __init__(self, status: int, endpoint: str, detail: str = ""):
        answer = f"HTTP {status}" if status else "no answer"
        super().__init__(f"Shortcut {endpoint}: {answer}")
        self.status = status
        self.endpoint = endpoint
        self.detail = detail


class ShortcutResponseError(RuntimeError):
    """Shortcut answered with a body that is not JSON or not of the expected shape.

    Never read as an empty listing or a skippable item: it aborts the run.
    """


class ShortcutListingError(RuntimeError):
    """A story listing that cannot be shown to be complete (see ``_query_stories``)."""


class _CutShortError(Exception):
    """Internal: stop the run cleanly, keeping what was rendered so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _make_session(token: str) -> requests.Session:
    """A ``requests`` session that sends the token as ``Shortcut-Token``."""
    session = requests.Session()
    session.headers.update({"Shortcut-Token": token, "Accept": "application/json"})
    return session


def _check_token(token: str) -> None:
    """Refuse a token ``requests`` would reject, without ever echoing it.

    A pasted token often ends in a newline; requests then raises an error whose
    message holds the whole header, token included.
    """
    if any(char.isspace() or not char.isprintable() for char in token):
        raise ValueError("The Shortcut token contains whitespace or control characters")


class _Client:
    """One run's access to Shortcut: the session, the host's check, the waits, the clock."""

    def __init__(
        self, session: Any, check_active: Callable[[], None] | None = None, secret: str = ""
    ):
        self.session = session
        self.check_active = check_active
        # The token, only to cut it out of Shortcut's error messages.
        headers = getattr(session, "headers", None)
        self.secret = secret or (headers.get("Shortcut-Token", "") if headers else "")
        # Shortcut's clock at its first answer (the Date header), see _run_clock.
        self.server_now: datetime | None = None
        self.waited = 0.0  # seconds slept this run
        self.out_of_time = False  # a wait was refused for the run's budget

    def check(self) -> None:
        if self.check_active is not None:
            self.check_active()

    def can_wait(self, seconds: float) -> bool:
        """Whether a wait fits in the single-wait cap and the run's budget."""
        if seconds > _MAX_WAIT_SECONDS:
            return False
        if self.waited + seconds > _MAX_TOTAL_WAIT_SECONDS:
            self.out_of_time = True
            return False
        return True

    def wait(self, seconds: float) -> None:
        self.check()
        self.waited += seconds
        time.sleep(seconds)


def _as_client(client: Any) -> _Client:
    return client if isinstance(client, _Client) else _Client(client)


def _send(
    client: Any, method: str, path: str, *, params: dict | None = None, body: dict | None = None
) -> Any:
    """Call a Shortcut API path and return the successful response, retrying transient errors.

    Rate limits (429), server errors (5xx) and network failures are retried up
    to ``_MAX_RETRIES`` times; a wait over ``_MAX_WAIT_SECONDS``, or past the
    run's ``_MAX_TOTAL_WAIT_SECONDS``, is not taken. Every other error status
    raises :class:`ShortcutAPIError`. Both 200 and 201 are success: the story
    query answers 201.
    """
    client = _as_client(client)
    url = f"{_API_BASE}{path}"
    attempt = 0
    while True:
        last_attempt = attempt == _MAX_RETRIES - 1
        client.check()
        try:
            response = client.session.request(
                method, url, params=params, json=body, timeout=_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            # Only the type: the message of some requests errors holds a header.
            name = type(exc).__name__
            delay = float(2**attempt)
            if last_attempt or not client.can_wait(delay):
                raise ShortcutAPIError(0, path, name) from None
            logger.warning("Shortcut: %s on %s; retrying in %.0fs.", name, path, delay)
            client.wait(delay)
            attempt += 1
            continue

        status = response.status_code
        if status in (200, 201):
            if client.server_now is None:
                client.server_now = _server_time(response)
            return response
        if status in _TRANSIENT_STATUSES and not last_attempt:
            delay = _retry_delay(response.headers, attempt)
            if not client.can_wait(delay):
                raise ShortcutAPIError(status, path, f"asked to wait {delay:.0f}s")
            logger.warning("Shortcut: HTTP %d on %s; retrying in %.0fs.", status, path, delay)
            client.wait(delay)
            attempt += 1
            continue
        raise ShortcutAPIError(status, path, _redact(_error_detail(response), client.secret))


def _request(
    client: Any,
    method: str,
    path: str,
    shape: type,
    *,
    params: dict | None = None,
    body: dict | None = None,
) -> Any:
    """Call a Shortcut API path (see ``_send``) and return its JSON ``list`` or ``dict``."""
    return _json(_send(client, method, path, params=params, body=body), shape, path)


def _json(response: Any, shape: type, path: str) -> Any:
    """The JSON body of a success, which must be a ``list`` or a ``dict`` as expected.

    Anything else raises: an empty or odd body must never be read as an empty
    listing, which would look like deletions.
    """
    try:
        body = response.json()
    except ValueError:
        status = response.status_code
        raise ShortcutResponseError(f"Shortcut {path}: HTTP {status} without JSON") from None
    if not isinstance(body, shape):
        raise ShortcutResponseError(
            f"Shortcut {path}: a {type(body).__name__} where a {shape.__name__} was expected"
        )
    return body


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait before a retry: ``Retry-After`` when Shortcut sends it."""
    try:
        return float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt)


def _error_detail(response: Any) -> str:
    """Shortcut's ``message``, or nothing. Never the raw body."""
    try:
        message = response.json()["message"]
    except (ValueError, KeyError, TypeError):
        return ""
    return message[:200] if isinstance(message, str) else ""


def _redact(text: str, secret: str) -> str:
    """``text`` with the token cut out, in case Shortcut echoes it back.

    A token too short to replace safely (it could match ordinary words) drops
    the whole text when it occurs in it.
    """
    if not secret or secret not in text:
        return text
    return text.replace(secret, "[token]") if len(secret) >= 8 else ""


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
def _parse(stamp: str) -> datetime:
    """Parse a Shortcut timestamp such as ``2026-10-09T19:14:27Z``."""
    return datetime.fromisoformat(stamp)


def _stamp(moment: datetime) -> str:
    """Format a moment the way Shortcut does (UTC, whole seconds, ``Z``).

    The result can be sent as a date filter and compared as a string with the
    ``updated_at`` values Shortcut returns.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _server_time(response: Any) -> datetime | None:
    """Shortcut's clock when it answered, from the ``Date`` response header.

    ``None`` when the header is missing or unreadable. A zone of ``-0000``
    (unknown) is read as UTC, the zone HTTP dates are in.
    """
    try:
        moment = parsedate_to_datetime((response.headers or {}).get("Date"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _is_settled(updated_at: str, fetched_at: datetime | None) -> bool:
    """True when a story fetched at ``fetched_at`` is final for its ``updated_at``.

    Timestamps have one-second resolution, so a story edited twice within one
    second keeps the same ``updated_at``. A copy fetched while that second was
    still running may miss the second edit, and nothing would ever reveal it.
    Once Shortcut's own clock is past that second, no later edit can carry the
    same timestamp. The margin absorbs any difference between the server that
    stamps the ``Date`` header and the one that stamps ``updated_at``. Without
    a usable ``Date`` the answer is no, and the story is simply fetched again.
    """
    if fetched_at is None or not updated_at:
        return False
    return _parse(updated_at) <= fetched_at - _SETTLE_MARGIN


# ---------------------------------------------------------------------------
# Story listing over date windows
# ---------------------------------------------------------------------------
def _query_stories(
    session: Any, filters: dict, field: str, start: datetime, end: datetime
) -> list[dict]:
    """Return every story matching ``filters`` whose ``field`` lies in ``[start, end]``.

    ``field`` is ``"created_at"`` or ``"updated_at"``. ``POST /stories/search``
    cannot be paged, so the only way to make a listing smaller is to narrow its
    date window. A window that returns ``_WINDOW_CAP`` stories or more might
    have been cut off by the server, so its result is thrown away and the
    window is split in two. Never a partial list: if a window is already one
    second wide (the resolution of the filters) and still that full, there is
    nothing left to narrow and the listing fails with
    :class:`ShortcutListingError` instead of dropping stories.

    Shortcut includes both ends of a window, so the two parts of a split share
    the instant they were cut at. That leaves no gap for a story to fall into,
    and a story stamped exactly on the cut comes back from both parts; it is
    kept once.
    """
    query = {**filters, f"{field}_start": _stamp(start), f"{field}_end": _stamp(end)}
    stories = _request(session, "POST", "/stories/search", list, body=query)
    if len(stories) < _WINDOW_CAP:
        return stories
    if end - start <= _SECOND:
        raise ShortcutListingError(
            f"Shortcut returned {len(stories)} stories with {field} between {_stamp(start)} and "
            f"{_stamp(end)}. The connector does not trust a listing of {_WINDOW_CAP} stories or "
            f"more to be complete, and a one-second window cannot be narrowed further, so "
            f"nothing was synced. Narrow the selection with group_ids or epic_ids so that "
            f"fewer stories fall into that second."
        )
    middle = _split_point(stories, field, start, end)
    logger.info(
        "Shortcut: %d stories in one %s window; splitting at %s.",
        len(stories),
        field,
        _stamp(middle),
    )
    earlier = _query_stories(session, filters, field, start, middle)
    later = _query_stories(session, filters, field, middle, end)
    listed = {story["id"] for story in earlier}
    return earlier + [story for story in later if story["id"] not in listed]


def _split_point(stories: list[dict], field: str, start: datetime, end: datetime) -> datetime:
    """Pick where to cut ``[start, end]`` so both parts hold about half the stories.

    The cut is the median timestamp of the stories that came back, not the
    middle of the time range: the first window spans decades with all stories
    at its recent end, and halving the time would spend several full-size
    requests just to find them. The result is kept strictly inside the window
    so both parts are narrower than the original and the splitting must end.
    """
    stamps = sorted(_parse(story[field]) for story in stories)
    median = stamps[len(stamps) // 2]
    return min(max(median, start + _SECOND), end - _SECOND)


def _scope_filters(
    group_ids: list[str] | None, epic_ids: list[int] | None, include_archived: bool
) -> dict[str, Any]:
    """Build the ``/stories/search`` filters for the caller's selection.

    Filters combine with AND: with both teams and epics given, a story must be
    in one of the teams and in one of the epics. Leaving ``archived`` out
    returns archived and live stories alike.
    """
    filters: dict[str, Any] = {}
    if group_ids:
        filters["group_ids"] = list(group_ids)
    if epic_ids:
        filters["epic_ids"] = list(epic_ids)
    if not include_archived:
        filters["archived"] = False
    return filters


# ---------------------------------------------------------------------------
# Lookups: ids -> names
# ---------------------------------------------------------------------------
def _state_names(session: Any) -> dict[Any, str]:
    """Map every workflow state id to its name, across all workflows."""
    workflows = _request(session, "GET", "/workflows", list)
    return {
        state["id"]: state.get("name") or ""
        for workflow in workflows
        for state in workflow.get("states") or []
    }


def _member_names(session: Any) -> dict[Any, str]:
    """Map every member id to the member's display name."""
    members = _request(session, "GET", "/members", list)
    return {member["id"]: (member.get("profile") or {}).get("name") or "" for member in members}


def _in_scope(
    item: dict, group_ids: list[str] | None, ids: list[int] | None, include_archived: bool
) -> bool:
    """True when an epic or iteration belongs to the caller's selection."""
    if ids and item["id"] not in ids:
        return False
    if group_ids and not set(item.get("group_ids") or []) & set(group_ids):
        return False
    return include_archived or not item.get("archived")


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------
def _story_id(story_id: Any) -> str:
    return f"story:{story_id}"


def _epic_id(epic_id: Any) -> str:
    return f"epic:{epic_id}"


def _iteration_id(iteration_id: Any) -> str:
    return f"iteration:{iteration_id}"


def _header_lines(story: dict, names: dict[str, dict]) -> list[str]:
    """The ``Key: value`` lines at the top of a story document.

    Uses only fields that the slim story of a listing and the full story of
    ``GET /stories/{id}`` both carry. That is what lets ``_fingerprint`` tell,
    from a listing alone, whether a story's document would read differently.
    ``names`` maps ``state`` / ``member`` / ``epic`` / ``iteration`` ids to names.
    """
    lines = [f"Type: {story.get('story_type') or 'story'}"]
    state = names["state"].get(story.get("workflow_state_id"))
    if state:
        lines.append(f"State: {state}")
    if story.get("archived"):
        lines.append("Archived: yes")
    # An id that no longer resolves (deleted a moment ago) is left out, not guessed.
    owners = sorted(filter(None, (names["member"].get(o) for o in story.get("owner_ids") or [])))
    if owners:
        lines.append(f"Owners: {', '.join(owners)}")
    epic = names["epic"].get(story.get("epic_id"))
    if epic:
        lines.append(f"Epic: {epic}")
    iteration = names["iteration"].get(story.get("iteration_id"))
    if iteration:
        lines.append(f"Iteration: {iteration}")
    labels = sorted(label.get("name") or "" for label in story.get("labels") or [])
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")
    if story.get("deadline"):
        lines.append(f"Deadline: {story['deadline'][:10]}")
    return lines


def _iteration_mark(iteration: dict) -> str:
    """What the iteration list says about one iteration, to tell if it must be re-read.

    Editing an iteration moves its ``updated_at``. Its status is included as
    well because it follows the calendar: an iteration becomes ``started`` on
    its start date without anyone editing it.
    """
    return f"{iteration.get('updated_at') or ''} {iteration.get('status') or ''}"


def _fingerprint(lines: list[str]) -> str:
    """A short, stable hash of a story's header lines, small enough to keep in state."""
    return hashlib.sha1("\n".join(lines).encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def _render_story(story: dict, names: dict[str, dict]) -> str:
    """Render a full story and its comments as one text document.

    The text is deterministic for unchanged input and carries no timestamps or
    counters, so an unchanged story keeps its content hash and is not
    re-cognified. Owners and labels are sorted by name and comments by id, so
    the same story renders the same text whatever order its parts are listed
    in. The story name is not repeated here; it is the row's title.
    """
    sections = ["\n".join(_header_lines(story, names))]
    description = (story.get("description") or "").strip()
    if description:
        sections.append(description)
    # A deleted comment stays in the list with ``deleted: true`` and no text.
    # Comments come oldest first by id, whatever order they are listed in.
    comments = sorted(
        (comment for comment in story.get("comments") or [] if not comment.get("deleted")),
        key=lambda comment: comment.get("id") or 0,
    )
    if comments:
        lines = [_comment_line(comment, names["member"]) for comment in comments]
        sections.append("Comments:\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _comment_line(comment: dict, members: dict[Any, str]) -> str:
    author = members.get(comment.get("author_id")) or "Unknown"
    return f"- {author}: {(comment.get('text') or '').strip()}"


def _story_row(story: dict, names: dict[str, dict]) -> dict[str, Any]:
    """Flatten a fetched story into a dlt row."""
    return {
        "id": _story_id(story["id"]),
        "url": story.get("app_url") or "",
        "title": (story.get("name") or "").strip(),
        "content": _render_story(story, names),
        "_deleted": False,
    }


def _epic_row(epic: dict) -> dict[str, Any]:
    """Flatten an epic (state, deadline, description) into a dlt row."""
    lines = [f"State: {epic.get('state') or 'unknown'}"]
    if epic.get("archived"):
        lines.append("Archived: yes")
    if epic.get("deadline"):
        lines.append(f"Deadline: {epic['deadline'][:10]}")
    return _document_row(_epic_id(epic["id"]), epic, lines)


def _iteration_row(iteration: dict) -> dict[str, Any]:
    """Flatten an iteration (status, dates, description) into a dlt row."""
    lines = [f"Status: {iteration.get('status') or 'unknown'}"]
    if iteration.get("start_date"):
        lines.append(f"Start: {iteration['start_date']}")
    if iteration.get("end_date"):
        lines.append(f"End: {iteration['end_date']}")
    return _document_row(_iteration_id(iteration["id"]), iteration, lines)


def _document_row(row_id: str, item: dict, lines: list[str]) -> dict[str, Any]:
    """Build the row of an epic or iteration: header lines, then its description."""
    sections = ["\n".join(lines)]
    description = (item.get("description") or "").strip()
    if description:
        sections.append(description)
    return {
        "id": row_id,
        "url": item.get("app_url") or "",
        "title": (item.get("name") or "").strip(),
        "content": "\n\n".join(sections),
        "_deleted": False,
    }


def _deleted_row(row_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a document by id."""
    return {"id": row_id, "_deleted": True}


def _fetch(client: _Client, path: str) -> tuple[dict, datetime | None]:
    """GET one story or iteration in full, with Shortcut's clock at that moment."""
    response = _send(client, "GET", path)
    return _json(response, dict, path), _server_time(response)


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict, so it is unit-testable)
# ---------------------------------------------------------------------------
class _Run:
    """One sync of the selection; ``state`` changes only at the end.

    State: ``known_ids`` (row ids emitted and current), ``cursor`` (newest story
    ``updated_at`` seen), ``seen`` (stories near the cursor whose copy is final),
    ``fingerprints`` (story id -> hash of its header lines), ``iterations``
    (iteration id -> its mark when last rendered), ``retry`` (row ids kept after a
    failure), ``unreadable`` (row id -> ``{stamp, runs, since}`` of items answering
    403/404, ``since`` being the run clock at the first counted run) and
    ``given_up`` (row id -> ``{stamp, at}``: the stamp at which one was forgotten,
    and when it was last asked).
    """

    def __init__(self, client: _Client, state: dict, stats: dict[str, int]):
        self.client = client
        self.state = state
        self.stats = stats
        self.known_ids: set[str] = set(state.get("known_ids", []))
        self.fingerprints: dict[str, str] = dict(state.get("fingerprints", {}))
        self.seen: dict[str, str] = dict(state.get("seen", {}))
        self.marks: dict[str, str] = dict(state.get("iterations", {}))
        self.cursor: str = state.get("cursor", "")
        self.retry: set[str] = set(state.get("retry", []))
        self.unreadable: dict[str, dict] = {
            row_id: dict(entry) for row_id, entry in state.get("unreadable", {}).items()
        }
        # A ``given_up`` entry written as a bare stamp (by the previous version of
        # this connector) is due to be asked again.
        self.given_up: dict[str, dict] = {
            row_id: dict(entry) if isinstance(entry, dict) else {"stamp": entry, "at": _EPOCH_STAMP}
            for row_id, entry in state.get("given_up", {}).items()
        }
        # What a run that stops early commits instead: only finished runs count.
        self.unreadable_before = {row_id: dict(e) for row_id, e in self.unreadable.items()}
        self.kept: set[str] = set()
        self.rendered: set[str] = set()
        self.reached: set[str] = set()  # row ids whose fetch was attempted
        self.outage = 0
        self.unread: dict[str, list[str]] = {}  # kind -> row ids that answered 403/404
        self.counted: set[str] = set()  # row ids whose 403/404 counted in this run
        self.reasks: set[str] = set()  # forgotten row ids asked again this run
        self.reask_failed: set[str] = set()
        self.listed = False
        # Filled by list():
        self.now = _EPOCH  # the run clock (see _run_clock)
        self.names: dict[str, dict] = {}
        self.epics: list[dict] = []
        self.iterations: list[dict] = []
        self.swept: dict[Any, dict] = {}
        self.stamps: dict[Any, str] = {}  # story id -> updated_at in the cursor listing
        self.settled: set[Any] = set()
        self.prints: dict[str, str] = {}  # story id -> fingerprint of its header now
        self.order: list[Any] = []  # story ids to render: fresh, retries, then re-asks
        self.next_marks: dict[str, str] = {}
        self.stamp_of: dict[str, str] = {}  # row id -> its listing stamp

    # -- 1. List ---------------------------------------------------------------
    def list(
        self,
        group_ids: list[str] | None,
        epic_ids: list[int] | None,
        include_archived: bool,
        now: datetime | None,
    ) -> None:
        all_epics = _request(
            self.client, "GET", "/epics", list, params={"includes_description": "true"}
        )
        all_iterations = _request(self.client, "GET", "/iterations", list)
        # Names come from the whole workspace: an in-scope story may sit in an epic
        # or iteration that is not itself selected for ingestion.
        self.names = {
            "state": _state_names(self.client),
            "member": _member_names(self.client),
            "epic": {epic["id"]: (epic.get("name") or "").strip() for epic in all_epics},
            "iteration": {i["id"]: (i.get("name") or "").strip() for i in all_iterations},
        }
        self.epics = [e for e in all_epics if _in_scope(e, group_ids, epic_ids, include_archived)]

        self.now = (now or _run_clock(self.client)).replace(microsecond=0)
        end = self.now
        # Entries written before ``since`` existed start their 24 hours now; one
        # that never counted is dropped.
        for entries in (self.unreadable, self.unreadable_before):
            for row_id, entry in list(entries.items()):
                if "since" not in entry:
                    if entry.get("runs"):
                        entry["since"] = _stamp(self.now)
                    else:
                        del entries[row_id]
        filters = _scope_filters(group_ids, epic_ids, include_archived)
        # The sweep: every in-scope story that exists right now. created_at never
        # changes, so a story cannot move between windows while they are queried.
        self.swept = {
            story["id"]: story
            for story in _query_stories(self.client, filters, "created_at", _EPOCH, end)
        }
        iterations = [i for i in all_iterations if _in_scope(i, group_ids, None, include_archived)]
        if epic_ids:
            # Only the iterations the selected epics' stories are planned in: one
            # that no selected story sits in any more leaves the scope, as a story
            # leaving a selected epic does.
            used = {story.get("iteration_id") for story in self.swept.values()}
            iterations = [i for i in iterations if i["id"] in used]
        self.iterations = iterations
        # The listing starts a little before the cursor: a story can carry the
        # cursor's timestamp, or one just before it, and still not have been in
        # the previous listing. On a first sync the sweep already is that listing.
        if self.cursor:
            start = _parse(self.cursor) - _CURSOR_OVERLAP
            listed = _query_stories(self.client, filters, "updated_at", start, end)
        else:
            listed = list(self.swept.values())
        self.stamps = {story["id"]: story.get("updated_at") or "" for story in listed}

        # ``seen`` tells an unchanged story near the cursor, which is skipped, from
        # one that is new to the window or has moved, which is rendered.
        self.settled = {
            story_id
            for story_id, when in self.stamps.items()
            if self.seen.get(str(story_id)) == when
        }
        changed = self.stamps.keys() - self.settled
        # A story whose header would read differently (an epic was renamed, a label
        # deleted, ...) or that has no fingerprint yet is rendered whatever its
        # timestamp says.
        self.prints = {
            str(story_id): _fingerprint(_header_lines(story, self.names))
            for story_id, story in self.swept.items()
        }
        changed |= {
            story_id
            for story_id in self.swept
            if self.fingerprints.get(str(story_id)) != self.prints[str(story_id)]
        }
        # A forgotten item is asked again once its stamp moves, or after a day.
        stamp_of = {_story_id(i): s.get("updated_at") or "" for i, s in self.swept.items()}
        stamp_of |= {_iteration_id(i["id"]): _iteration_mark(i) for i in self.iterations}
        for row_id, entry in list(self.given_up.items()):
            if row_id not in stamp_of:
                del self.given_up[row_id]
            elif stamp_of[row_id] != entry["stamp"]:
                del self.given_up[row_id]
                kind, key = row_id.split(":", 1)
                (self.fingerprints if kind == "story" else self.marks).pop(key, None)
        due = sorted(
            (entry["at"], row_id)
            for row_id, entry in self.given_up.items()
            if _parse(entry["at"]) <= self.now - _REASK_AFTER
        )
        self.reasks = {row_id for _, row_id in due[:_MAX_REASKS]}
        self.stamp_of = stamp_of
        # Only stories in the sweep are rendered, so every emitted id can be deleted.
        changed &= self.swept.keys()
        changed = {i for i in changed if _story_id(i) not in self.given_up}
        self.retry &= set(stamp_of) - set(self.given_up)
        fresh = sorted(i for i in changed if _story_id(i) not in self.retry)
        retried = sorted(i for i in changed if _story_id(i) in self.retry)
        asked = sorted(i for i in self.swept if _story_id(i) in self.reasks)
        self.order = fresh + retried + asked
        self.stats["scanned"] = len(self.swept)
        self.stats["skipped"] = len(self.swept) - len(self.order)
        self.listed = True

    # -- 2. Render -------------------------------------------------------------
    def render(self) -> Iterator[dict[str, Any]]:
        for story_id in self.order:
            row_id = _story_id(story_id)
            self.reached.add(row_id)
            try:
                story, fetched_at = _fetch(self.client, f"/stories/{story_id}")
            except ShortcutAPIError as exc:
                self._failed(row_id, exc)
                # A fingerprint of "" matches nothing, so it is rendered next run.
                self.prints[str(story_id)] = ""
                continue
            self._succeeded(row_id)
            # Skip this story next run only if no later edit can share the
            # timestamp it was listed with (see _is_settled).
            if _is_settled(self.stamps.get(story_id, ""), fetched_at):
                self.settled.add(story_id)
            yield _story_row(story, self.names)
        self.settle("story")

        # The epic list carries everything an epic document shows, so epics cost
        # no request and are emitted every run; unchanged text keeps its hash.
        for epic in self.epics:
            yield _epic_row(epic)

        # The iteration list has no description, so an iteration costs one request,
        # made only when the list shows it is new or changed.
        listed = [i for i in self.iterations if _iteration_id(i["id"]) not in self.given_up]
        fresh = [i for i in listed if _iteration_id(i["id"]) not in self.retry]
        retried = [i for i in listed if _iteration_id(i["id"]) in self.retry]
        asked = [i for i in self.iterations if _iteration_id(i["id"]) in self.reasks]
        for item in fresh + retried + asked:
            row_id, key, mark = _iteration_id(item["id"]), str(item["id"]), _iteration_mark(item)
            if self.marks.get(key) == mark and row_id not in self.retry | self.reasks:
                self.next_marks[key] = mark
                continue
            self.reached.add(row_id)
            try:
                iteration, fetched_at = _fetch(self.client, f"/iterations/{item['id']}")
            except ShortcutAPIError as exc:
                self._failed(row_id, exc)
                continue
            self._succeeded(row_id)
            # Same one-second rule as for stories: remember it only once final.
            if _is_settled(item.get("updated_at") or "", fetched_at):
                self.next_marks[key] = mark
            yield _iteration_row(iteration)
        self.settle("iteration")

    def _succeeded(self, row_id: str) -> None:
        if row_id not in self.retry | self.reasks:
            self.outage = 0
        if self.given_up.pop(row_id, None) is not None:
            logger.info("Shortcut: %s is readable again.", row_id)
        self.unreadable.pop(row_id, None)
        self.rendered.add(row_id)

    def _failed(self, row_id: str, exc: ShortcutAPIError) -> None:
        """Keep an item that failed on its own, or stop the run if Shortcut is failing."""
        self.stats["failed"] += 1
        self.stop_if_systemic(exc)
        if exc.status == 401:
            raise exc  # nothing rendered yet: the token is bad
        if row_id in self.reasks:
            # Still unreadable: it stays forgotten and is asked again in a day.
            self.reask_failed.add(row_id)
            return
        self.kept.add(row_id)
        logger.warning("Shortcut: kept %s for a retry next run (%s).", row_id, exc)
        if exc.status in (403, 404):
            # Still listed, so not gone: hidden from this token, or a passing
            # failure. settle() decides which once the kind's fetches are done.
            self.unread.setdefault(row_id.split(":", 1)[0], []).append(row_id)
        elif row_id not in self.retry:
            self.outage += 1
            if self.outage >= _OUTAGE_STREAK:
                raise _CutShortError("unavailable")

    def settle(self, kind: str) -> None:
        """Count this run's 403/404s of one kind where access to that kind is shown to work.

        A 403 or 404 on a listed item means it is hidden from this token, or that
        Shortcut is failing or the token lost a permission; only the first is a
        reason to forget it. So it counts only if another item of the same kind
        was read in this run. In steady state the others are unchanged and not
        fetched, so one of them is fetched as a probe; if the probe fails too,
        nothing counts.
        """
        pending = self.unread.pop(kind, [])
        if not pending:
            return
        readable = any(row_id.startswith(f"{kind}:") for row_id in self.rendered)
        probe = None if readable else self._probe(kind)
        if readable or probe:
            for row_id in pending:
                self._count_unreadable(row_id)
        elif probe is False:
            self.stats["failed_permission"] = self.stats.get("failed_permission", 0) + len(pending)
            if self._probe_path(kind) is None:
                logger.warning(
                    "Shortcut: %s: every fetch answered 403/404 this run and there is no other "
                    "%s to check access with; nothing counts toward forgetting it.",
                    kind,
                    kind,
                )
            else:
                logger.warning(
                    "Shortcut: %s: every fetch answered 403/404 this run; "
                    "check the token's permissions.",
                    kind,
                )

    def _probe(self, kind: str) -> bool | None:
        """Fetch one unchanged item of ``kind``: True when it is readable.

        False when it answers 403/404 too, or there is no other item to try;
        None when Shortcut is failing (5xx, no answer), which proves nothing.
        """
        path = self._probe_path(kind)
        if path is None:
            return False
        self.stats[f"probe_{kind}"] = 1
        try:
            _fetch(self.client, path)
        except ShortcutAPIError as exc:
            self.stop_if_systemic(exc)
            if exc.status == 401:
                raise exc
            return False if exc.status in (403, 404) else None
        return True

    def _probe_path(self, kind: str) -> str | None:
        """The newest item of ``kind`` not fetched this run and not unreadable."""
        unusable = self.reached | self.unreadable.keys() | self.given_up.keys()
        if kind == "story":
            ids = [i for i in self.swept if _story_id(i) not in unusable]
            if not ids:
                return None
            best = max(ids, key=lambda i: (self.swept[i].get("updated_at") or "", i))
            return f"/stories/{best}"
        items = [i for i in self.iterations if _iteration_id(i["id"]) not in unusable]
        if not items:
            return None
        best = max(items, key=lambda i: (i.get("updated_at") or "", i["id"]))
        return f"/iterations/{best['id']}"

    def _count_unreadable(self, row_id: str) -> None:
        self.counted.add(row_id)
        entry = self.unreadable.get(row_id)
        stamp = self.stamp_of.get(row_id, "")
        if entry and entry["stamp"] == stamp:
            self.unreadable[row_id] = {**entry, "runs": entry["runs"] + 1}
        else:
            self.unreadable[row_id] = {"stamp": stamp, "runs": 1, "since": _stamp(self.now)}

    def stop_if_systemic(self, exc: ShortcutAPIError) -> None:
        if self.client.out_of_time:
            raise _CutShortError("rate_limit" if exc.status == 429 else "unavailable") from None
        if exc.status == 429:
            raise _CutShortError("rate_limit") from None
        if exc.status == 401 and self.rendered:
            raise _CutShortError("auth") from None

    # -- 3. Delete and 4. Commit ------------------------------------------------
    def finish(self) -> Iterator[dict[str, Any]]:
        current_ids = {_story_id(i) for i in self.swept}
        current_ids |= {_epic_id(e["id"]) for e in self.epics}
        current_ids |= {_iteration_id(i["id"]) for i in self.iterations}
        for row_id, entry in self.unreadable.items():
            # Only a run that counted this item's failure can give it up: one where
            # access was not shown to work (a lost permission) never does.
            if (
                row_id in self.counted
                and entry["runs"] >= _GIVE_UP_RUNS
                and _parse(entry["since"]) <= self.now - _GIVE_UP_AFTER
            ):
                logger.warning(
                    "Shortcut: %s answered HTTP 403/404 in %d runs since %s; its document is "
                    "forgotten until it changes or reads again.",
                    row_id,
                    entry["runs"],
                    entry["since"],
                )
                self.given_up[row_id] = {"stamp": entry["stamp"], "at": _stamp(self.now)}
                self.kept.discard(row_id)
                self.stats["given_up"] += 1
        for row_id in self.reask_failed:
            self.given_up[row_id]["at"] = _stamp(self.now)
        current_ids -= set(self.given_up)
        deleted = sorted(self.known_ids - current_ids)
        for row_id in deleted:
            yield _deleted_row(row_id)
        self.stats["deleted"] = len(deleted)

        newest = max([self.cursor, *self.stamps.values()])
        window_start = _stamp(_parse(newest) - _CURSOR_OVERLAP) if newest else ""
        prints = {
            key: value for key, value in self.prints.items() if _story_id(key) not in self.given_up
        }
        self.state["known_ids"] = sorted(current_ids)
        self.state["fingerprints"] = prints
        self.state["iterations"] = self.next_marks
        self.state["cursor"] = newest
        # Remember the settled stories that the next run's overlap will list again.
        self.state["seen"] = {
            str(story_id): when
            for story_id, when in self.stamps.items()
            if story_id in self.settled and when >= window_start
        }
        self._commit_items(self.kept, self.unreadable, self.given_up)

    def commit_progress(self) -> None:
        """After a clean stop: keep what was rendered, delete nothing, keep the cursor.

        The cursor stays, so the next run lists the same window again (one search),
        and the stories not rendered still differ from their fingerprint or ``seen``.
        Nothing this run saw counts toward giving an item up. A forgotten item that
        was read again, has changed or is gone is no longer forgotten (as in any
        run); nothing new is forgotten, and none is marked as asked.
        """
        if not self.listed:
            return  # stopped while listing: nothing moved
        for story_id in self.order:
            row_id = _story_id(story_id)
            if row_id in self.rendered:
                self.fingerprints[str(story_id)] = self.prints[str(story_id)]
                if story_id in self.settled and story_id in self.stamps:
                    self.seen[str(story_id)] = self.stamps[story_id]
        self.marks.update(self.next_marks)  # rendered and settled, or unchanged
        self.state["known_ids"] = sorted(self.known_ids | self.rendered)
        self.state["fingerprints"] = self.fingerprints
        self.state["iterations"] = self.marks
        self.state["cursor"] = self.cursor
        self.state["seen"] = self.seen
        unreached = set(self.retry) - self.reached
        self._commit_items(
            self.kept | unreached,
            {r: e for r, e in self.unreadable_before.items() if r not in self.rendered},
            self.given_up,
        )

    def _commit_items(self, retry: set[str], unreadable: dict, given_up: dict) -> None:
        self.state["retry"] = sorted(retry)
        self.state["unreadable"] = {
            row_id: entry for row_id, entry in sorted(unreadable.items()) if row_id in retry
        }
        self.state["given_up"] = dict(sorted(given_up.items()))
        self.stats["kept"] = len(retry)


def _run_clock(client: _Client) -> datetime:
    """The run's clock: the later of Shortcut's (its first answer's Date) and the local one.

    The sweep ends there, and the give-up times are read from it. The later of
    the two means neither a local clock that runs behind nor a stale Date (a
    caching proxy) ends the sweep early, which would make the newest stories look
    deleted. A clock that runs ahead only widens the windows.
    """
    local = _local_now()
    server = client.server_now
    if server is None:
        logger.warning("Shortcut: no Date header seen; the run uses the local clock.")
        return local
    if abs(server - local) > _CLOCK_TOLERANCE:
        logger.warning(
            "Shortcut: its Date header and the local clock differ by %.0fs; using the later one.",
            abs((server - local).total_seconds()),
        )
    return max(server, local)


def _local_now() -> datetime:
    return datetime.now(UTC)


def sync_stories(
    session: Any,
    state: dict,
    *,
    group_ids: list[str] | None = None,
    epic_ids: list[int] | None = None,
    include_archived: bool = True,
    now: datetime | None = None,
    check_active: Callable[[], None] | None = None,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed documents since the last run, plus hard-delete markers.

    1. *List.* Lookups, epics, iterations, every in-scope story (the sweep) and
       the stories updated since the cursor. A failure here propagates first.
    2. *Render.* Each changed story (fresh first, then retries, then forgotten
       ones due to be asked again), each epic, and each new or changed iteration.
    3. *Delete.* A hard-delete marker for every known id that is no longer current.
    4. *Commit.* Only now is ``state`` written.

    A run stopped cleanly (a ``401`` after progress, a rate limit, an outage,
    the wait budget spent) keeps what it rendered and its cursor, and deletes
    nothing. ``now`` (for tests) defaults to the run clock (see ``_run_clock``).
    """
    stats = {} if stats is None else stats
    for key in _STAT_KEYS:
        stats.setdefault(key, 0)
    client = session if isinstance(session, _Client) else _Client(session, check_active)
    run = _Run(client, state, stats)
    try:
        try:
            run.list(group_ids, epic_ids, include_archived, now)
            yield from run.render()
            yield from run.finish()
        except ShortcutAPIError as exc:
            run.stop_if_systemic(exc)  # also a rate limit while listing
            raise
    except _CutShortError as cut:
        run.commit_progress()
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Shortcut sync stopped early (%s); it resumes on the next run.", cut.reason)
        return
    logger.info(
        "Shortcut: %d item(s) rendered, %d epic(s), %d deletion(s); kept %d item(s) for retry.",
        len(run.rendered),
        len(run.epics),
        stats["deleted"],
        stats["kept"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def shortcut_source(
    token: str | None = None,
    group_ids: list[str] | None = None,
    epic_ids: list[int] | None = None,
    include_archived: bool = True,
    service: Any = None,
    *,
    resource_name: str = SHORTCUT_TABLE_NAME,
    check_active: Callable[[], None] | None = None,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields Shortcut documents for ``remember``.

    Args:
        token: Shortcut API token. Falls back to ``SHORTCUT_API_TOKEN``.
        group_ids: Only ingest stories, epics and iterations of these teams
            (Shortcut calls a team a "group" in its API; the ids are UUIDs).
        epic_ids: Only ingest these epics, the stories in them and the
            iterations those stories are planned in.
        include_archived: When ``False`` archived stories and epics are left
            out, and one is forgotten once it is archived.
        service: Pre-built ``requests``-like session, mainly for tests; when
            omitted one is built from the token for each run and closed after it.
            ``session`` is accepted as an alias.
        resource_name: dlt resource (table) name, which also scopes the sync
            state. Give each token or selection its own name in a shared dataset.
        check_active: Optional host authorization checkpoint, called before every
            request and wait and around every row; it stops the sync by raising.

    With no selection the whole workspace is ingested. ``group_ids`` and
    ``epic_ids`` combine with AND.

    Returns:
        A ``dlt`` resource with ``primary_key="id"``, ``write_disposition="merge"``
        and a ``_deleted`` hard-delete column. ``resource.cognee_sync_stats``
        holds the last run's counts.
    """
    if not (hasattr(dlt_utils, "PIPELINE_SCOPE_ATTR") and hasattr(dlt_utils, "guarded_rows")):
        raise RuntimeError(
            "The Shortcut connector needs cognee's dlt_utils.PIPELINE_SCOPE_ATTR and "
            "guarded_rows (cognee>=1.6.2)."
        )
    _check_resource_name(resource_name)
    if service is not None and session is not None:
        raise ValueError("Pass service= or session=, not both")
    service = service if service is not None else session

    if isinstance(group_ids, str):
        group_ids = [group_ids]
    if isinstance(epic_ids, int):
        epic_ids = [epic_ids]

    resolved_token = token or os.environ.get("SHORTCUT_API_TOKEN") or ""
    if service is None:
        if not resolved_token:
            raise ValueError("Shortcut API token required: pass token= or set SHORTCUT_API_TOKEN.")
        _check_token(resolved_token)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def shortcut_documents():
        stats.clear()
        http = service if service is not None else _make_session(resolved_token)
        try:
            rows = sync_stories(
                _Client(http, check_active, resolved_token),
                dlt.current.resource_state(),
                group_ids=group_ids,
                epic_ids=epic_ids,
                include_archived=include_archived,
                stats=stats,
            )
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if service is None:
                http.close()

    resource = shortcut_documents()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(resource, DOCUMENT_SOURCE_ATTR, SHORTCUT_SOURCE_NAME)
    # Give the sync state its own dlt pipeline, apart from other sources.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: an empty name would lose that
    scope, and two names dlt normalizes alike ("Foo-Bar", "foo_bar") would share
    a table.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > 64:
        raise ValueError("resource_name must be at most 64 characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")
