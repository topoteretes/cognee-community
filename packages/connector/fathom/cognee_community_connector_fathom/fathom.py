"""Fathom connector for cognee: a ``dlt`` resource that turns meeting notes into memory.

It ingests the meetings your Fathom API key can see: title, attendees, the default
summary, action items (with assignee and completion state) and, opt-in, highlights and
the full speaker-attributed transcript. Hand it to :func:`cognee.remember`::

    await cognee.remember(
        fathom_source(api_key="<fathom api key>"),
        dataset_name="meetings",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
    )

Design
------
* **One document per meeting.** Rows are flat ``{id, title, content, url,
  cognee_node_set, _deleted}``. Every column feeds the stored document identity, so
  nothing volatile is rendered and arrays the API may reorder (action items) are
  sorted: a meeting whose text did not change keeps its document id and is not
  re-cognified, even when it was fetched again.
* **Every run starts with an inventory.** Fathom has no change or delete feed, so one
  walk over ``GET /meetings`` lists every visible meeting. It asks for action items
  only, which Fathom does not count as a heavy request, and keeps ``recording_id``,
  ``created_at`` and a fingerprint of the action items.
* **Incremental fetch.** The heavy request (summary, transcript) is made only for
  meetings that need it: ones never ingested before, ones created within
  ``recheck_days`` (a summary can still be processing and action items get ticked off
  right after the call) and ones whose action-item fingerprint changed, however old.
  They are fetched with ``created_after`` / ``created_before`` windows planned from the
  inventory, so a handful of old meetings never drags the whole history in.
* **Forget-on-delete.** A meeting known from earlier runs but absent from the inventory
  is emitted with the ``_deleted`` hard-delete marker; dlt removes the row on ``merge``
  and cognee's ``orphan_cleanup`` purges it from the graph, vector and relational
  stores. Fathom pages are page-number cursors, so a meeting deleted mid-walk can shift
  the pages and hide a live one: a second walk must confirm every absence before a
  tombstone is written. A run that would forget more than ``max_delete_fraction`` of
  what it knows forgets nothing and says so in ``cognee_sync_stats``, because losing
  access (a team change, a deactivated owner) looks exactly like deleting everything.
* **All or nothing.** The sync state (``{recording_id: action-item fingerprint}``) is
  written only after the last page of the last pass. Any API error raises, ``dlt``
  rolls the state back and memory is left as it was. A listing that failed halfway can
  therefore never be read as "everything was deleted".
* **Privacy.** E-mail addresses are never rendered. Attendees appear as ``Name
  (company.com)``. Provenance is the meeting's ``url``, which needs a Fathom login; the
  public ``share_url`` is not stored.
"""

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("fathom_connector")

BASE_URL = "https://api.fathom.ai/external/v1"
SOURCE_NAME = "fathom"
DEFAULT_RESOURCE_NAME = "fathom_meetings"
# Fathom returns 10 meetings per page and has no parameter to change that.
PAGE_SIZE = 10
# bump when the rendering changes: it makes the next run re-render every meeting
_RENDER_VERSION = 1
_STATE_VERSION = 1
# (connect, read) seconds; Fathom documents no expected latency
_TIMEOUT = (10, 60)
# attempts per request after an HTTP 429, honouring Retry-After
_RATE_LIMIT_RETRIES = 6
# attempts per request after a 5xx or a network error, backing off 1s, 2s, 4s
_SERVER_RETRIES = 3
# Retry-After is clamped to this many seconds: the limit windows are 60s long
_MAX_WAIT = 120.0
# below this many tombstones the delete guard does not apply: a small account that
# really lost its last few meetings should be able to forget them
_GUARD_MIN_DELETES = 5
_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FathomError(RuntimeError):
    """Base class of the errors this source raises."""


class FathomAPIError(FathomError):
    """Fathom answered with an error or an unexpected payload, or could not be reached."""


class FathomAuthError(FathomError):
    """Fathom rejected the API key (HTTP 401 or 403)."""


class FathomRateLimitedError(FathomError):
    """Fathom kept answering HTTP 429 after every retry."""


class FathomConfigError(FathomError, ValueError):
    """The source was built or run with a configuration that would lose data."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait after a 429: the ``Retry-After`` header, else exponential backoff."""
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = float(2**attempt)
    return max(1.0, min(seconds, _MAX_WAIT))


class FathomClient:
    """Minimal synchronous client for ``GET /external/v1/meetings``.

    The key lives only in the session headers. Errors carry the endpoint and the HTTP
    status, never the key, a query string or a response body.
    """

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = BASE_URL,
    ):
        key = (api_key or "").strip()
        # reject early and without echoing the key: requests would put it in its exception
        if not key or not key.isascii() or not key.isprintable() or " " in key:
            raise ValueError(
                "Fathom API key required: pass api_key= or set FATHOM_API_KEY "
                "(User Settings > API Access in Fathom)."
            )
        self._session = session or requests.Session()
        self._session.headers.update({"X-Api-Key": key, "Accept": "application/json"})
        self._sleep = sleep
        self._base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"FathomClient(base_url={self._base_url!r})"

    def _get(self, path: str, params: dict[str, Any]) -> dict:
        """GET ``path`` and return its JSON object, retrying 429s and transient failures."""
        url = f"{self._base_url}{path}"
        rate_limited = 0
        failures = 0
        while True:
            try:
                response = self._session.get(url, params=params, timeout=_TIMEOUT)
            except requests.RequestException as exc:
                failures += 1
                if failures > _SERVER_RETRIES:
                    raise FathomAPIError(
                        f"Fathom {path} could not be reached ({type(exc).__name__})."
                    ) from None
                self._sleep(float(2 ** (failures - 1)))
                continue

            status = response.status_code
            if status == 429:
                rate_limited += 1
                if rate_limited > _RATE_LIMIT_RETRIES:
                    raise FathomRateLimitedError(
                        f"Fathom {path} is still rate limited after {_RATE_LIMIT_RETRIES} "
                        "retries; run the sync again later."
                    )
                wait = _retry_after(response.headers, rate_limited)
                logger.warning("Fathom: rate limited, waiting %.0fs.", wait)
                self._sleep(wait)
                continue
            if status >= 500:
                failures += 1
                if failures > _SERVER_RETRIES:
                    raise FathomAPIError(f"Fathom {path} failed (HTTP {status}).")
                self._sleep(float(2 ** (failures - 1)))
                continue
            if status in (401, 403):
                raise FathomAuthError(
                    f"Fathom rejected the API key (HTTP {status}). Check the key in "
                    "User Settings > API Access and that its owner is still active."
                )
            if status >= 400:
                raise FathomAPIError(f"Fathom {path} failed (HTTP {status}).")
            try:
                payload = response.json()
            except ValueError:
                raise FathomAPIError(f"Fathom {path} did not return JSON.") from None
            if not isinstance(payload, dict):
                raise FathomAPIError(f"Fathom {path} returned an unexpected payload.")
            return payload

    def list_meetings(self, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """Yield every meeting matching ``params``, following ``next_cursor`` to the end."""
        base = dict(params or {})
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page_params = {**base, "cursor": cursor} if cursor else base
            payload = self._get("/meetings", page_params)
            items = payload.get("items")
            if not isinstance(items, list):
                raise FathomAPIError("Fathom /meetings returned no 'items' list.")
            for item in items:
                if not isinstance(item, dict):
                    raise FathomAPIError("Fathom /meetings returned a malformed meeting.")
                yield item
            cursor = payload.get("next_cursor")
            if not cursor:
                return
            # a cursor that does not advance would page forever
            if not isinstance(cursor, str) or cursor in seen:
                raise FathomAPIError("Fathom /meetings returned a pagination cursor that repeats.")
            seen.add(cursor)


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------
def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (``...Z`` or with an offset) into an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(
        timezone.utc
    )


def _iso(moment: datetime) -> str:
    """Format a datetime the way the ``created_after`` filter documents it."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _assignee_name(item: dict) -> str:
    assignee = item.get("assignee")
    return _text(assignee.get("name")) if isinstance(assignee, dict) else ""


def _sorted_action_items(items: Any) -> list[dict]:
    """Action items in a stable order, so a reshuffled array does not change the document."""
    clean = [item for item in (items or []) if isinstance(item, dict)]
    return sorted(
        clean,
        key=lambda item: (
            _text(item.get("recording_timestamp")),
            _text(item.get("description")),
            _assignee_name(item),
        ),
    )


def action_signature(items: Any) -> str:
    """Short fingerprint of the action items: description, assignee and completion."""
    normalized = [
        [
            _text(item.get("recording_timestamp")),
            _text(item.get("description")),
            _assignee_name(item),
            bool(item.get("completed")),
        ]
        for item in _sorted_action_items(items)
    ]
    return hashlib.sha256(json.dumps(normalized).encode()).hexdigest()[:16]


def _summary_text(meeting: dict) -> str:
    summary = meeting.get("default_summary")
    return _text(summary.get("markdown_formatted")) if isinstance(summary, dict) else ""


def _attendees(meeting: dict) -> str:
    """``Name (company.com)`` per invitee. E-mail addresses are never rendered."""
    names: list[str] = []
    for invitee in meeting.get("calendar_invitees") or []:
        if not isinstance(invitee, dict):
            continue
        name = _text(invitee.get("name")) or _text(invitee.get("matched_speaker_display_name"))
        domain = _text(invitee.get("email_domain"))
        notes = [
            part for part in (domain, "external" if invitee.get("is_external") else "") if part
        ]
        label = name or "Unnamed attendee"
        names.append(f"{label} ({', '.join(notes)})" if notes else label)
    return ", ".join(names)


def _duration_minutes(meeting: dict) -> int | None:
    start = _parse_ts(meeting.get("recording_start_time"))
    end = _parse_ts(meeting.get("recording_end_time"))
    if start is None or end is None or end <= start:
        return None
    return max(1, round((end - start).total_seconds() / 60))


@dataclass(frozen=True)
class Options:
    """What a source ingests. Frozen: it is part of the stored sync state's identity."""

    include_summary: bool = True
    include_action_items: bool = True
    include_transcript: bool = False
    include_highlights: bool = False
    recheck_days: int = 14
    filters: dict[str, Any] = field(default_factory=dict)
    since: datetime | None = None
    max_delete_fraction: float = 0.5

    @property
    def fingerprint(self) -> str:
        """Identity of what each document contains; a change re-renders every meeting."""
        wanted = [
            self.include_summary,
            self.include_action_items,
            self.include_transcript,
            self.include_highlights,
            _RENDER_VERSION,
        ]
        return hashlib.sha256(json.dumps(wanted).encode()).hexdigest()[:16]

    def detail_params(self) -> dict[str, Any]:
        """Query flags of the detail request. Summary and transcript are Fathom's heavy ones."""
        flags = {
            "include_summary": self.include_summary,
            "include_action_items": self.include_action_items,
            "include_transcript": self.include_transcript,
            "include_highlights": self.include_highlights,
        }
        return {name: "true" for name, enabled in flags.items() if enabled}


def render_meeting(meeting: dict, options: Options) -> str:
    """Render one meeting as markdown. Only the selected sections are included."""
    lines: list[str] = []

    started = (
        _parse_ts(meeting.get("recording_start_time"))
        or _parse_ts(meeting.get("scheduled_start_time"))
        or _parse_ts(meeting.get("created_at"))
    )
    if started is not None:
        when = started.strftime("Recorded: %Y-%m-%d %H:%M UTC")
        minutes = _duration_minutes(meeting)
        lines.append(f"{when} ({minutes} min)" if minutes else when)
    if _text(meeting.get("meeting_type")):
        lines.append(f"Meeting type: {_text(meeting.get('meeting_type'))}")
    event = _text(meeting.get("meeting_title"))
    if event and event.casefold() != _text(meeting.get("title")).casefold():
        lines.append(f"Calendar event: {event}")
    recorder = meeting.get("recorded_by")
    if isinstance(recorder, dict) and _text(recorder.get("name")):
        team = _text(recorder.get("team"))
        lines.append(
            f"Recorded by: {_text(recorder.get('name'))}" + (f" ({team} team)" if team else "")
        )
    attendees = _attendees(meeting)
    if attendees:
        lines.append(f"Attendees: {attendees}")

    sections: list[str] = []
    if options.include_summary:
        summary = _summary_text(meeting)
        if summary:
            # Fathom's markdown usually opens with its own "## Summary" heading
            sections.append(summary if summary.startswith("#") else f"## Summary\n\n{summary}")
    if options.include_action_items:
        action_lines = []
        for item in _sorted_action_items(meeting.get("action_items")):
            description = _text(item.get("description"))
            if not description:
                continue
            mark = "x" if item.get("completed") else " "
            owner = _assignee_name(item)
            action_lines.append(f"- [{mark}] {description}" + (f" ({owner})" if owner else ""))
        if action_lines:
            sections.append("## Action items\n\n" + "\n".join(action_lines))
    if options.include_highlights:
        highlights = [h for h in (meeting.get("highlights") or []) if isinstance(h, dict)]
        highlights.sort(key=lambda h: (h.get("start_time") or 0, _text(h.get("summary"))))
        highlight_lines = []
        for highlight in highlights:
            kind = _text(highlight.get("type"))
            summary = _text(highlight.get("summary"))
            quote = _text(highlight.get("text"))
            head = ": ".join(part for part in (kind, summary) if part)
            if quote:
                head = f'{head} - "{quote}"' if head else f'"{quote}"'
            if head:
                highlight_lines.append(f"- {head}")
        if highlight_lines:
            sections.append("## Highlights\n\n" + "\n".join(highlight_lines))
    if options.include_transcript:
        transcript_lines = []
        for turn in meeting.get("transcript") or []:
            if not isinstance(turn, dict) or not _text(turn.get("text")):
                continue
            speaker = turn.get("speaker")
            name = _text(speaker.get("display_name")) if isinstance(speaker, dict) else ""
            stamp = _text(turn.get("timestamp"))
            prefix = f"[{stamp}] " if stamp else ""
            transcript_lines.append(f"{prefix}{name or 'Unknown speaker'}: {_text(turn['text'])}")
        if transcript_lines:
            sections.append("## Transcript\n\n" + "\n".join(transcript_lines))

    return "\n\n".join(["\n".join(lines), *sections] if lines else sections).strip()


def _node_sets(meeting: dict) -> list[str]:
    """Groups ``recall`` can be scoped to. cognee namespaces each as ``fathom:<name>``."""
    names = []
    kind = {"only_internal": "internal", "one_or_more_external": "external"}.get(
        meeting.get("calendar_invitees_domains_type") or ""
    )
    if kind:
        names.append(kind)
    if _text(meeting.get("meeting_type")):
        names.append(f"type:{_text(meeting.get('meeting_type'))}")
    recorder = meeting.get("recorded_by")
    if isinstance(recorder, dict) and _text(recorder.get("team")):
        names.append(f"team:{_text(recorder.get('team'))}")
    return names


def meeting_row(meeting: dict, options: Options) -> dict[str, Any]:
    """Flatten a meeting into a document row. The column set is frozen on purpose."""
    recording_id = str(meeting["recording_id"])
    return {
        "id": recording_id,
        "title": _text(meeting.get("title"))
        or _text(meeting.get("meeting_title"))
        or f"Fathom meeting {recording_id}",
        "content": render_meeting(meeting, options),
        "url": meeting.get("url"),
        "cognee_node_set": _node_sets(meeting),
        # hard-delete marker (always False for live meetings)
        "_deleted": False,
    }


def _deleted_row(recording_id: str) -> dict[str, Any]:
    return {"id": recording_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict, so unit tests need no dlt)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Listed:
    created_at: datetime
    signature: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _recording_id(meeting: dict) -> str:
    recording_id = meeting.get("recording_id")
    if recording_id is None or recording_id == "":
        # without an id the meeting could neither be keyed nor forgotten later
        raise FathomAPIError("Fathom /meetings returned a meeting without a recording_id.")
    return str(recording_id)


def _filter_params(options: Options) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if options.filters.get("teams"):
        params["teams[]"] = list(options.filters["teams"])
    if options.filters.get("recorded_by"):
        params["recorded_by[]"] = list(options.filters["recorded_by"])
    if options.since is not None:
        params["created_after"] = _iso(options.since)
    return params


def _list_inventory(client: Any, options: Options, *, with_actions: bool) -> dict[str, _Listed]:
    """Walk every page of ``/meetings`` without the heavy flags: id, creation time, actions."""
    params = _filter_params(options)
    if with_actions:
        params["include_action_items"] = "true"
    inventory: dict[str, _Listed] = {}
    for meeting in client.list_meetings(params):
        created = _parse_ts(meeting.get("created_at"))
        if created is None:
            raise FathomAPIError("Fathom /meetings returned a meeting without a created_at.")
        inventory[_recording_id(meeting)] = _Listed(
            created, action_signature(meeting.get("action_items")) if with_actions else ""
        )
    return inventory


def plan_windows(
    inventory: dict[str, _Listed], targets: set[str]
) -> list[tuple[datetime | None, datetime | None]]:
    """Plan ``(created_after, created_before)`` windows that cover every target.

    Meetings are laid out by creation time and a window runs from its first to its last
    target. Two targets share a window unless a full page of other meetings (the cost of
    a separate request) lies between them, so one old meeting whose action item was
    ticked off costs one request instead of the history since it. ``None`` means the
    window is open on that side.
    """
    order = sorted(inventory, key=lambda rid: (inventory[rid].created_at, rid))
    spans: list[tuple[int, int]] = []
    start = last = -1
    gap = 0
    for index, recording_id in enumerate(order):
        if recording_id in targets:
            if start < 0:
                start = index
            last = index
            gap = 0
        elif start >= 0:
            gap += 1
            if gap >= PAGE_SIZE:
                spans.append((start, last))
                start = -1
    if start >= 0:
        spans.append((start, last))

    margin = timedelta(seconds=1)
    windows: list[tuple[datetime | None, datetime | None]] = []
    for first, final in spans:
        after = None if first == 0 else inventory[order[first]].created_at - margin
        before = None if final == len(order) - 1 else inventory[order[final]].created_at + margin
        windows.append((after, before))
    return windows


def _has_content(meeting: dict, options: Options) -> bool:
    """Whether a meeting has anything yet in the sections the source is asked for."""
    return bool(
        (options.include_summary and _summary_text(meeting))
        or (options.include_action_items and meeting.get("action_items"))
        or (options.include_transcript and meeting.get("transcript"))
        or (options.include_highlights and meeting.get("highlights"))
    )


def sync_meetings(
    client: Any, state: dict, stats: dict[str, int], options: Options
) -> Iterator[dict[str, Any]]:
    """Yield the meetings that changed since the last run, plus hard-delete markers.

    ``state`` is only written once every pass finished, so an exception anywhere leaves
    it, and therefore the next run, untouched.
    """
    stats.update(listed=0, fetched=0, deferred=0, deleted=0, failed=0)
    now = _now()
    known: dict[str, str] = dict(state.get("meetings") or {})
    config_changed = state.get("config") != options.fingerprint
    window_start = now - timedelta(days=options.recheck_days)

    def young(created_at: datetime) -> bool:
        return options.recheck_days > 0 and created_at >= window_start

    # --- pass 1: the inventory -------------------------------------------------
    inventory = _list_inventory(client, options, with_actions=options.include_action_items)
    stats["listed"] = len(inventory)

    targets = {
        recording_id
        for recording_id, listed in inventory.items()
        if config_changed
        or recording_id not in known
        or young(listed.created_at)
        or (options.include_action_items and known[recording_id] != listed.signature)
    }

    # --- pass 2: details for the targets only ----------------------------------
    updated = dict(known)
    handled: set[str] = set()
    deferred: set[str] = set()
    for after, before in plan_windows(inventory, targets):
        params = {**_filter_params(options), **options.detail_params()}
        if after is not None:
            params["created_after"] = _iso(max(after, options.since) if options.since else after)
        if before is not None:
            params["created_before"] = _iso(before)
        for meeting in client.list_meetings(params):
            recording_id = _recording_id(meeting)
            if recording_id in handled:
                continue
            # a meeting the inventory has not seen (it finished meanwhile) is new
            if recording_id in inventory and recording_id not in targets:
                continue
            created = _parse_ts(meeting.get("created_at"))
            if created is None:
                created = inventory[recording_id].created_at if recording_id in inventory else now
            if recording_id not in known and young(created) and not _has_content(meeting, options):
                # Fathom is still processing it: ingesting the empty shell now would
                # cognify it twice. It stays a target until it has content or ages out.
                deferred.add(recording_id)
                continue
            handled.add(recording_id)
            updated[recording_id] = (
                action_signature(meeting.get("action_items"))
                if options.include_action_items
                else ""
            )
            yield meeting_row(meeting, options)
    stats["fetched"] = len(handled)
    stats["deferred"] = len(deferred)
    missing = targets - handled - deferred

    # --- pass 3: forget what is gone -------------------------------------------
    gone = set(known) - set(inventory)
    if gone:
        # Pages are page-number cursors: a meeting deleted mid-walk shifts the pages and
        # can hide a live one. Only a second walk that also misses it counts as gone.
        gone -= set(_list_inventory(client, options, with_actions=False))
    if len(gone) > _GUARD_MIN_DELETES and len(gone) / len(known) > options.max_delete_fraction:
        # losing access (a team change, a deactivated owner) looks like a mass delete
        logger.warning(
            "Fathom: %d of %d known meetings are gone, more than max_delete_fraction=%s; "
            "forgetting nothing this run.",
            len(gone),
            len(known),
            options.max_delete_fraction,
        )
        stats["failed"] = 1
        stats["failed_delete_guard"] = len(gone)
        gone = set()
    for recording_id in sorted(gone):
        updated.pop(recording_id, None)
        yield _deleted_row(recording_id)
    stats["deleted"] = len(gone)

    # --- commit, only now ------------------------------------------------------
    state["v"] = _STATE_VERSION
    state["meetings"] = updated
    # keep asking for a full re-render until every meeting made it into the new format
    if not (config_changed and missing):
        state["config"] = options.fingerprint
    logger.info(
        "Fathom: %d listed, %d fetched, %d deferred, %d missing, %d deleted.",
        stats["listed"],
        stats["fetched"],
        stats["deferred"],
        len(missing),
        stats["deleted"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _as_list(value: str | list[str] | tuple[str, ...] | None, name: str) -> list[str]:
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return [item.strip() for item in items]


def _require_merge(resource_name: str) -> None:
    """Refuse a run whose write disposition is not ``merge``.

    ``cognee.remember`` defaults to ``replace``, which rewrites the table with only the
    rows this run yields. This source yields only the meetings that changed, so every
    other meeting would be forgotten on the second sync.
    """
    import dlt

    try:
        disposition = dlt.current.source().resources[resource_name].write_disposition
    except Exception:  # not running inside a dlt pipeline, nothing to enforce
        return
    if isinstance(disposition, dict):
        disposition = disposition.get("disposition")
    if disposition != "merge":
        raise FathomConfigError(
            "The Fathom source needs write_disposition='merge'. Call cognee.remember("
            "fathom_source(...), primary_key='id', write_disposition='merge'): with "
            f"'{disposition}' every meeting that did not change would be forgotten."
        )


def fathom_source(
    api_key: str | None = None,
    *,
    include_summary: bool = True,
    include_action_items: bool = True,
    include_transcript: bool = False,
    include_highlights: bool = False,
    recheck_days: int = 14,
    teams: list[str] | None = None,
    recorded_by: list[str] | None = None,
    since: datetime | str | None = None,
    max_delete_fraction: float = 0.5,
    resource_name: str = DEFAULT_RESOURCE_NAME,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields Fathom meetings for ``remember``.

    Args:
        api_key: Fathom API key (User Settings > API Access). Falls back to
            ``FATHOM_API_KEY``.
        include_summary: Add Fathom's default summary.
        include_action_items: Add action items with assignee and completion state. They
            also drive change detection for old meetings, at no heavy-request cost.
        include_transcript: Add the full speaker-attributed transcript. Off by default: it
            makes a meeting many times larger than its summary.
        include_highlights: Add Fathom's highlights.
        recheck_days: Meetings created within this many days are fetched again on every
            run, to catch a summary that was still processing and action items ticked off
            after the call. ``0`` turns the window off; action-item changes are still
            detected on any meeting.
        teams: Only meetings of these Fathom teams.
        recorded_by: Only meetings recorded by these e-mail addresses. Without ``teams``
            or ``recorded_by`` Fathom leaves out calls recorded by users outside your org.
        since: Ignore meetings created before this date (a ``datetime`` or ISO string).
            Handy to backfill a large account in slices.
        max_delete_fraction: A run that would forget more than this share of the known
            meetings (and more than 5) forgets nothing and reports ``failed_delete_guard``
            in ``cognee_sync_stats``. Pass ``1.0`` to allow forgetting everything.
        resource_name: Stable name of this connection. It names the staging table and
            scopes the sync state and the cleanup of deleted meetings, so keep it fixed
            across runs and give every Fathom API key its own name when they share a
            dataset: two keys see different meetings and would forget each other's.
        client: Pre-built :class:`FathomClient`, mainly an injection point for tests.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``, ``write_disposition=
        "merge"`` and a ``_deleted`` hard-delete column. ``cognee.remember`` must be
        called with ``primary_key="id", write_disposition="merge"``.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError("The Fathom source requires cognee>=1.6.3 (per-row node sets).")
    if not isinstance(resource_name, str) or not _ID_RE.match(resource_name):
        raise ValueError("resource_name must be an identifier: letters, digits and underscores")
    if not any((include_summary, include_action_items, include_transcript, include_highlights)):
        raise ValueError("select at least one of summary, action items, transcript, highlights")
    if not isinstance(recheck_days, int) or isinstance(recheck_days, bool) or recheck_days < 0:
        raise ValueError("recheck_days must be a non-negative integer")
    if not 0 <= max_delete_fraction <= 1:
        raise ValueError("max_delete_fraction must be between 0 and 1")
    if isinstance(since, str):
        parsed_since = _parse_ts(since)
        if parsed_since is None:
            raise ValueError("since must be a datetime or an ISO-8601 string")
        since = parsed_since
    elif since is not None:
        since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)

    # built here so a malformed key fails at construction and the closure holds no raw key
    fathom = (
        client
        if client is not None
        else FathomClient(api_key or os.environ.get("FATHOM_API_KEY", ""))
    )
    options = Options(
        include_summary=include_summary,
        include_action_items=include_action_items,
        include_transcript=include_transcript,
        include_highlights=include_highlights,
        recheck_days=recheck_days,
        filters={
            "teams": _as_list(teams, "teams"),
            "recorded_by": _as_list(recorded_by, "recorded_by"),
        },
        since=since,
        max_delete_fraction=float(max_delete_fraction),
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def fathom_meetings():
        stats.clear()
        _require_merge(resource_name)
        yield from sync_meetings(fathom, dlt.current.resource_state(), stats, options)

    resource = fathom_meetings()
    setattr(resource, DOCUMENT_SOURCE_ATTR, SOURCE_NAME)
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
