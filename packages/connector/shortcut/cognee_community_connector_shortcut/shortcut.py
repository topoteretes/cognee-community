"""Shortcut connector for cognee: a ``dlt`` source that turns a Shortcut workspace into memory.

Pulls stories (with their comments), epics and iterations from Shortcut,
incrementally and with forget-on-delete. The source built here is handed
directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_shortcut import shortcut_source

    await cognee.remember(
        shortcut_source(),               # SHORTCUT_API_TOKEN from env
        dataset_name="shortcut",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by row id
        max_rows_per_table=0,        # 0 = no row cap
    )

Design
------
* **Auth**: an API token, sent as ``Shortcut-Token``. The connector only reads:
  ``GET`` requests plus ``POST /stories/search``, which is a query. A read-only
  token is enough.
* **Documents**: one per story (``story:<id>``), one per epic (``epic:<id>``)
  and one per iteration (``iteration:<id>``). The source declares
  ``cognee_document_source = "shortcut"``, so every row goes through normal
  cognify instead of the relational dlt path.
* **Listing stories**: ``POST /stories/search`` filters by date and has no
  pagination. No result cap was observed on a real workspace (2,609 stories in
  one response), but nothing documents that there is none, so a window that
  returns ``_WINDOW_CAP`` stories or more is not trusted: it is split in two
  and each part is asked for again (see ``_query_stories``).
* **Incremental sync**: an ``updated_at`` cursor kept in dlt resource state.
  Each run asks for stories updated since shortly before the cursor and
  fetches only those in full. Comments are part of the story and adding,
  editing or deleting one moves ``updated_at``.
* **Names a story borrows**: a story document shows its epic, iteration,
  workflow state, owners and labels by name. Renaming or deleting one of those
  leaves the story's ``updated_at`` untouched, so the cursor cannot see it.
  The connector therefore keeps a short fingerprint of those header lines per
  story and re-renders a story when its fingerprint changes.
* **Forget-on-delete**: every run lists all in-scope stories over
  ``created_at`` (the sweep) and compares the ids with those emitted by
  earlier runs. Ids that vanished are emitted with the ``_deleted``
  hard-delete marker; dlt removes those rows on ``merge`` and cognee's
  ``orphan_cleanup`` purges them from memory.
* **Safety**: every listing is finished before the first row is yielded, and
  state is written only after the last one. A listing that fails therefore
  deletes nothing and moves no cursor; the next run starts from the same point.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("shortcut_connector")

# dlt resource / staging-table name, and the document-source tag for its rows.
SHORTCUT_TABLE_NAME = "shortcut_documents"
SHORTCUT_SOURCE_NAME = "shortcut"

_API_BASE = "https://api.app.shortcut.com/api/v3"
# The story listing is one unpaginated response; 2,609 stories took up to 14 seconds.
_TIMEOUT_SECONDS = 120

# Retry budget for rate-limited / transient responses.
_MAX_RETRIES = 5
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)

# A story listing with this many results or more is treated as possibly
# truncated and split. Listings up to 2,609 stories were verified complete.
_WINDOW_CAP = 2500
# Shortcut timestamps and date filters have one-second resolution.
_SECOND = timedelta(seconds=1)
# The first window starts here and ends a day past the local clock, so a clock
# that runs behind Shortcut's cannot hide new stories.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CLOCK_MARGIN = timedelta(days=1)
# How far before the cursor the updated_at listing starts (see sync_stories).
_CURSOR_OVERLAP = timedelta(seconds=60)
# How long after a story's updated_at second a fetch must happen before that
# copy counts as final for the timestamp (see _is_settled).
_SETTLE_MARGIN = timedelta(seconds=2)


class ShortcutAPIError(RuntimeError):
    """A non-retryable (or retries-exhausted) error response from the Shortcut API."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Shortcut API returned {status}: {message}")
        self.status = status


class ShortcutListingError(RuntimeError):
    """A story listing that succeeded but cannot be shown to be complete."""


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(token: str) -> Any:
    """Build a ``requests`` session authenticated with a Shortcut API token."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a declared dependency
        raise ImportError(
            'The Shortcut connector requires "requests". Install the package:\n'
            "    pip install cognee-community-connector-shortcut"
        ) from exc

    session = requests.Session()
    session.headers.update({"Shortcut-Token": token, "Accept": "application/json"})
    return session


def _send(
    session: Any, method: str, path: str, *, params: dict | None = None, body: dict | None = None
) -> Any:
    """Call a Shortcut API path and return the successful response, retrying transient errors.

    Rate limits (429), server errors (5xx) and network failures are retried up
    to ``_MAX_RETRIES`` times. Any other error status raises
    :class:`ShortcutAPIError` so the caller decides what it means. Both 200 and
    201 are success: the story query answers 201.
    """
    url = f"{_API_BASE}{path}"
    for attempt in range(_MAX_RETRIES):
        last_attempt = attempt == _MAX_RETRIES - 1
        try:
            response = session.request(
                method, url, params=params, json=body, timeout=_TIMEOUT_SECONDS
            )
        except OSError as exc:
            # requests' connection and timeout errors all derive from OSError.
            if last_attempt:
                raise
            delay = float(2**attempt)
            logger.warning("Shortcut: %s; retrying in %.1fs.", exc, delay)
            time.sleep(delay)
            continue

        status = response.status_code
        if status in (200, 201):
            return response
        if status in _TRANSIENT_STATUSES and not last_attempt:
            delay = _retry_delay(response.headers, attempt)
            logger.warning("Shortcut: HTTP %d on %s; retrying in %.1fs.", status, path, delay)
            time.sleep(delay)
            continue
        raise ShortcutAPIError(status, _error_message(response))

    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


def _request(
    session: Any, method: str, path: str, *, params: dict | None = None, body: dict | None = None
) -> Any:
    """Call a Shortcut API path (see ``_send``) and return the decoded JSON body."""
    return _send(session, method, path, params=params, body=body).json()


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait before a retry: exactly ``Retry-After`` when Shortcut sends it."""
    try:
        return float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt)


def _error_message(response: Any) -> str:
    """Extract Shortcut's ``message``, falling back to the raw body."""
    try:
        return response.json()["message"]
    except (ValueError, KeyError, TypeError):
        return str(getattr(response, "text", ""))[:200]


def _is_gone(exc: Exception) -> bool:
    """True when a single item is deleted or no longer visible to the token."""
    return isinstance(exc, ShortcutAPIError) and exc.status in (403, 404)


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

    ``None`` when the header is missing or unreadable.
    """
    try:
        return parsedate_to_datetime((response.headers or {}).get("Date"))
    except (TypeError, ValueError):
        return None


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
    stories = _request(session, "POST", "/stories/search", body=query)
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
    workflows = _request(session, "GET", "/workflows")
    return {
        state["id"]: state.get("name") or ""
        for workflow in workflows
        for state in workflow.get("states") or []
    }


def _member_names(session: Any) -> dict[Any, str]:
    """Map every member id to the member's display name."""
    members = _request(session, "GET", "/members")
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
    re-cognified. The story name is not repeated here; it is the row's title.
    """
    sections = ["\n".join(_header_lines(story, names))]
    description = (story.get("description") or "").strip()
    if description:
        sections.append(description)
    # A deleted comment stays in the list with ``deleted: true`` and no text.
    comments = [comment for comment in story.get("comments") or [] if not comment.get("deleted")]
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


def _fetch(session: Any, path: str) -> tuple[dict, datetime | None] | None:
    """GET one story or iteration in full, with Shortcut's clock at that moment.

    Returns ``None`` when it is gone (deleted between the listing and this
    fetch) so the caller can let it be forgotten. Other errors propagate.
    """
    try:
        response = _send(session, "GET", path)
        return response.json(), _server_time(response)
    except ShortcutAPIError as exc:
        if _is_gone(exc):
            logger.warning("Shortcut: %s is gone, skipping: %s", path, exc)
            return None
        raise


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict, so it is unit-testable)
# ---------------------------------------------------------------------------
def sync_stories(
    session: Any,
    state: dict,
    *,
    group_ids: list[str] | None = None,
    epic_ids: list[int] | None = None,
    include_archived: bool = True,
    now: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed documents since the last run, plus hard-delete markers.

    ``state`` holds ``known_ids`` (every row id emitted so far), ``cursor``
    (the newest story ``updated_at`` seen), ``seen`` (stories close to that
    cursor whose rendered copy is known to be final), ``fingerprints``
    (story id -> hash of the header lines its document shows) and
    ``iterations`` (iteration id -> its ``_iteration_mark`` when last rendered).

    The work is split so that a failure can never be mistaken for a deletion:

    1. *List.* Read the lookups, the epics and the iterations, list every
       in-scope story (the sweep), and list the stories updated since the
       cursor. Any error here propagates before a single row is yielded.
    2. *Render.* Fetch and yield each changed story once, then each epic and
       each new or changed iteration.
    3. *Delete.* Yield a hard-delete marker for every known id that is no
       longer current.
    4. *Commit.* Only now write the new id set, cursor, fingerprints and marks.
    """
    known_ids: set[str] = set(state.get("known_ids", []))
    fingerprints: dict[str, str] = state.get("fingerprints", {})
    seen: dict[str, str] = state.get("seen", {})
    iteration_marks: dict[str, str] = state.get("iterations", {})
    cursor: str = state.get("cursor", "")
    end = (now or datetime.now(UTC)).replace(microsecond=0) + _CLOCK_MARGIN

    # --- 1. List ------------------------------------------------------------
    all_epics = _request(session, "GET", "/epics", params={"includes_description": "true"})
    all_iterations = _request(session, "GET", "/iterations")
    # Names come from the whole workspace: an in-scope story may sit in an epic
    # or iteration that is not itself selected for ingestion.
    names = {
        "state": _state_names(session),
        "member": _member_names(session),
        "epic": {epic["id"]: (epic.get("name") or "").strip() for epic in all_epics},
        "iteration": {item["id"]: (item.get("name") or "").strip() for item in all_iterations},
    }
    epics = [e for e in all_epics if _in_scope(e, group_ids, epic_ids, include_archived)]
    iterations = [i for i in all_iterations if _in_scope(i, group_ids, None, include_archived)]

    filters = _scope_filters(group_ids, epic_ids, include_archived)
    # The sweep: every in-scope story that exists right now. created_at never
    # changes, so a story cannot move between windows while they are queried.
    swept = {
        story["id"]: story for story in _query_stories(session, filters, "created_at", _EPOCH, end)
    }
    # The listing starts a little before the cursor: a story can carry the
    # cursor's timestamp, or one just before it, and still not have been in the
    # previous listing. On a first sync the sweep already is that listing.
    if cursor:
        listed = _query_stories(
            session, filters, "updated_at", _parse(cursor) - _CURSOR_OVERLAP, end
        )
    else:
        listed = list(swept.values())
    stamps = {story["id"]: story.get("updated_at") or "" for story in listed}

    # ``seen`` is what earlier runs rendered inside the overlap (id ->
    # updated_at). It tells an unchanged story, which is skipped, from one that
    # is new to the window or has moved, which is rendered.
    settled = {story_id for story_id, when in stamps.items() if seen.get(str(story_id)) == when}
    changed = stamps.keys() - settled
    # A story whose header would read differently (an epic was renamed, a label
    # deleted, ...) or that has no fingerprint yet (new to the corpus) is
    # rendered whatever its timestamp says.
    current_prints = {
        str(story_id): _fingerprint(_header_lines(story, names))
        for story_id, story in swept.items()
    }
    changed |= {
        story_id
        for story_id in swept
        if fingerprints.get(str(story_id)) != current_prints[str(story_id)]
    }
    # Only stories in the sweep are rendered, so every emitted id ends up in
    # known_ids and can be deleted by a later run.
    changed &= swept.keys()

    # --- 2. Render ----------------------------------------------------------
    current_ids = {_story_id(story_id) for story_id in swept}
    rendered = 0
    for story_id in sorted(changed):
        fetched = _fetch(session, f"/stories/{story_id}")
        if fetched is None:
            # Deleted after the sweep saw it: let the deletion step forget it.
            current_ids.discard(_story_id(story_id))
            del current_prints[str(story_id)]
            continue
        story, fetched_at = fetched
        # Skip this story next run only if no later edit can share the
        # timestamp it was listed with (see _is_settled).
        if _is_settled(stamps.get(story_id, ""), fetched_at):
            settled.add(story_id)
        rendered += 1
        yield _story_row(story, names)

    # The epic list carries everything an epic document shows, so epics cost
    # no extra request and are emitted every run; unchanged text keeps its
    # content hash downstream.
    for epic in epics:
        current_ids.add(_epic_id(epic["id"]))
        yield _epic_row(epic)

    # The iteration list has no description, so an iteration costs one request.
    # It is only fetched when the list shows it is new or has changed; an
    # unchanged one keeps its row, and its id stays current so it is not deleted.
    current_marks: dict[str, str] = {}
    for item in iterations:
        key, mark = str(item["id"]), _iteration_mark(item)
        if iteration_marks.get(key) == mark:
            current_ids.add(_iteration_id(item["id"]))
            current_marks[key] = mark
            continue
        fetched = _fetch(session, f"/iterations/{item['id']}")
        if fetched is None:
            continue
        iteration, fetched_at = fetched
        current_ids.add(_iteration_id(item["id"]))
        # Same one-second rule as for stories: remember it only once final.
        if _is_settled(item.get("updated_at") or "", fetched_at):
            current_marks[key] = mark
        yield _iteration_row(iteration)

    # --- 3. Delete ----------------------------------------------------------
    deleted = known_ids - current_ids
    for row_id in sorted(deleted):
        yield _deleted_row(row_id)

    # --- 4. Commit ----------------------------------------------------------
    newest = max([cursor, *stamps.values()])
    window_start = _stamp(_parse(newest) - _CURSOR_OVERLAP) if newest else ""
    state["known_ids"] = sorted(current_ids)
    state["fingerprints"] = current_prints
    state["iterations"] = current_marks
    state["cursor"] = newest
    # Remember the settled stories that the next run's overlap will list again.
    state["seen"] = {
        str(story_id): when
        for story_id, when in stamps.items()
        if story_id in settled and when >= window_start
    }
    logger.info(
        "Shortcut: %d story(ies) rendered, %d epic(s), %d iteration(s), %d deletion(s).",
        rendered,
        len(epics),
        len(iterations),
        len(deleted),
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def shortcut_source(
    token: str | None = None,
    group_ids: list[str] | None = None,
    epic_ids: list[int] | None = None,
    include_archived: bool = True,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields Shortcut documents for ``remember``.

    Args:
        token: Shortcut API token. Falls back to ``SHORTCUT_API_TOKEN``.
        group_ids: Only ingest stories, epics and iterations of these teams
            (Shortcut calls a team a "group" in its API; the ids are UUIDs).
        epic_ids: Only ingest these epics and the stories in them.
        include_archived: When ``False`` archived stories and epics are left
            out, and one is forgotten once it is archived.
        session: Pre-built ``requests`` session. Mainly an injection point for
            tests; when omitted one is built from the token.

    With no selection the whole workspace is ingested. ``group_ids`` and
    ``epic_ids`` combine with AND.

    Returns:
        A ``dlt`` resource (``shortcut_documents``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - dlt is a declared dependency
        raise ImportError(
            'The Shortcut connector requires "dlt". Install the package:\n'
            "    pip install cognee-community-connector-shortcut"
        ) from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "The Shortcut connector requires a cognee build with table-scoped dlt document "
            "cleanup (cognee>=1.6). Upgrade cognee so upstream deletions are forgotten."
        )

    if isinstance(group_ids, str):
        group_ids = [group_ids]
    if isinstance(epic_ids, int):
        epic_ids = [epic_ids]

    resolved_token = token or os.environ.get("SHORTCUT_API_TOKEN")
    if session is None and not resolved_token:
        raise ValueError("Shortcut API token required: pass token= or set SHORTCUT_API_TOKEN.")

    @dlt.resource(
        name=SHORTCUT_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def shortcut_documents():
        yield from sync_stories(
            session or _make_session(resolved_token),
            dlt.current.resource_state(),
            group_ids=group_ids,
            epic_ids=epic_ids,
            include_archived=include_archived,
        )

    resource = shortcut_documents()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, SHORTCUT_SOURCE_NAME)
    # Give the incremental state its own dlt pipeline per dataset, so syncing
    # other dlt sources (or another dataset) cannot disturb the cursor.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, SHORTCUT_TABLE_NAME)
    return resource
