"""Calendly connector for cognee — a ``dlt`` source that turns your scheduled
meetings into memory ("ask my calendar").

Pull Calendly scheduled events (plus each invitee's questions/answers and
notes) into cognee, incrementally and with forget-on-deletion. Like the
sibling Confluence/Gmail/Google-Drive connectors this builds entirely on the
existing DLT ingestion subsystem; the resource produced here is handed
directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_calendly import calendly_source

    await cognee.remember(
        calendly_source(),
        dataset_name="my_calendar",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by event uuid
        max_rows_per_table=0,        # 0 = no row cap (see note below)
    )

Design
------
* **Auth** — a Calendly Personal Access Token (PAT), created at
  https://calendly.com/integrations/api_webhooks. Sent as a bearer token;
  the connector never writes to Calendly, only ``GET``.
* **Primary key** — the scheduled event's uuid (the last path segment of its
  ``uri``). With ``write_disposition="merge"`` this gives idempotent upserts.
* **Content** — each event becomes *one document*: the event's own fields
  (name, time, location) plus every invitee's status, their
  questions-and-answers, and free-text notes, all rendered to a single
  markdown blob. An event slot alone is nearly meaningless — the invitee's
  answers are what carry the actual context — so folding them into the same
  document lets cognee's entity extraction link them together, the same way
  ``resolve_dlt_sources`` links a Notion page's blocks into one document.
* **Incremental cursor** — a ``min_start_time`` window. Calendly's
  ``/scheduled_events`` list endpoint has no "changed since" filter, but it
  does accept ``min_start_time``, and every event (whatever its status) that
  starts on/after that floor is returned — including canceled ones. So the
  connector tracks a floor timestamp in dlt's per-resource state:

  - First run: floor = ``now - lookback_days`` (default 7), giving a
    recent-history backfill plus every future event.
  - Later runs: floor = ``now - recheck_window_days`` (default 2). Anything
    that starts in the future is always included (there is no upper bound),
    so newly booked meetings are never missed; only how far *back* each run
    re-checks shrinks to a rolling window.

  This means a change to an event more than ``recheck_window_days`` in the
  past (e.g. a very late cancellation) will not be picked up by a later run —
  documented in Limitations below, mirroring similar caveats on the Gmail
  (historyId) and Confluence (space sweep) connectors.
* **Forget-on-delete** — Calendly does not hard-delete scheduled events; the
  equivalent signal is ``status="canceled"``. A canceled event (or one whose
  every invitee canceled) is emitted with the ``_deleted`` hard-delete
  marker; dlt removes that row from its destination on ``merge`` and
  cognee's existing ``orphan_cleanup`` purges it from the graph, vector, and
  relational stores. A canceled *invitee* on an otherwise-active event is
  reflected the next time that event's row is re-synced (their status and
  answers are re-rendered, not dropped silently), since the row's ``id`` is
  the event's uuid, not the invitee's.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows
   from the dlt destination (default 50). For a real calendar pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against
   the *whole* synced corpus rather than a truncated window.

Limitations
-----------
* Only events that fall inside the current ``min_start_time`` window are
  re-checked for changes. An event more than ``recheck_window_days`` in the
  past that gets canceled or edited after that window has closed will not be
  forgotten/updated until (if ever) it is inside the window again. Widen
  ``recheck_window_days`` if your workflow needs a longer memory of
  after-the-fact cancellations.
* Calendly's REST API does not expose "notes" as a separate object; the
  connector treats every invitee ``questions_and_answers`` entry as
  free-text context (Calendly's own docs refer to the invitee's answer to
  the default first question — "Please share anything that will help
  prepare for our meeting" — as the invitee's *notes*), and renders all of
  it into the event's content.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("calendly_connector")

# Calendly REST API v2 lives at this base.
CALENDLY_API_BASE = "https://api.calendly.com"

CALENDLY_SOURCE_NAME = "calendly"
CALENDLY_TABLE_NAME = "calendly_events"

# Retry budget for rate-limited / transient Calendly API responses.
_MAX_RETRIES = 5
_PAGE_SIZE = 100

# The invitee Q&A entry Calendly's own UI/docs describe as the invitee's
# "notes" — the default first booking question. Matched case-insensitively
# and used only to label that answer distinctly in the rendered content;
# every other question/answer is still rendered too.
_NOTES_QUESTION_HINT = "share anything"

_EXTRA_HINT = (
    'The Calendly connector requires "requests": pip install "cognee[calendly]" '
    "(provides requests and dlt)."
)


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(token: str) -> Any:
    """Build a ``requests`` session authenticated with a Calendly Personal Access Token.

    ``requests`` is imported lazily so it stays an optional dependency
    (``pip install "cognee[calendly]"``).
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(_EXTRA_HINT) from exc

    session = requests.Session()
    session.headers.update(
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    )
    return session


def _request(session: Any, method: str, url: str, **kwargs) -> dict:
    """GET/etc a Calendly API URL, retrying rate-limit / transient errors and
    returning parsed JSON.

    Calendly rate-limits at roughly 100 requests/minute per token; a busy
    sync (many events x many invitees) can easily 429. Retry rate-limit
    (429) and server (5xx) responses with backoff honoring ``Retry-After``
    when present; anything else (4xx auth/not-found) propagates immediately.
    """
    for attempt in range(_MAX_RETRIES):
        response = session.request(method, url, **kwargs)
        if response.status_code < 400:
            return response.json()
        if response.status_code not in (429, 500, 502, 503, 504) or attempt == _MAX_RETRIES - 1:
            response.raise_for_status()
        delay = _retry_after(response.headers, attempt)
        logger.warning(
            "Calendly: %s %s -> %d — retrying in %.1fs (%d/%d).",
            method,
            url,
            response.status_code,
            delay,
            attempt + 1,
            _MAX_RETRIES,
        )
        time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover - loop always returns/raises


def _retry_after(headers, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = headers.get("Retry-After") if headers else None
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


def _paginate(session: Any, url: str, params: dict) -> Iterator[dict]:
    """Yield ``collection`` items across Calendly's cursor-based pagination.

    Unlike Confluence's ``_links.next`` (a ready-to-call URL), Calendly's
    ``pagination.next_page_token`` is a bare opaque cursor: per the API's own
    contract, the next page is fetched by calling the *same endpoint* with
    only ``page_token`` set (no other query params repeated) — so the first
    request sends the real filters and every later request drops them.
    """
    next_page_token: str | None = None
    first = True
    while first or next_page_token:
        request_params = dict(params) if first else {"page_token": next_page_token}
        data = _request(session, "GET", url, params=request_params)
        yield from data.get("collection", []) or []
        next_page_token = (data.get("pagination") or {}).get("next_page_token")
        first = False


def _current_user_uri(session: Any) -> str:
    """Return the authenticated user's resource ``uri`` (``GET /users/me``)."""
    data = _request(session, "GET", f"{CALENDLY_API_BASE}/users/me")
    return data["resource"]["uri"]


# ---------------------------------------------------------------------------
# Parsing / rendering
# ---------------------------------------------------------------------------
def _event_id(event: dict) -> str:
    """The event's uuid: the last path segment of its ``uri``."""
    return (event.get("uri") or "").rstrip("/").rsplit("/", 1)[-1]


def _invitee_id(invitee: dict) -> str:
    return (invitee.get("uri") or "").rstrip("/").rsplit("/", 1)[-1]


def _location_text(event: dict) -> str:
    """Render a scheduled event's ``location`` object as one plain-text line."""
    location = event.get("location") or {}
    kind = location.get("type", "")
    for key in ("location", "join_url", "data"):
        value = location.get(key)
        if isinstance(value, str) and value:
            return f"{kind}: {value}" if kind else value
    return kind or "Not specified"


def _render_qa(qa_list: list[dict] | None) -> list[str]:
    """Render an invitee's ``questions_and_answers`` to markdown lines.

    The entry Calendly's docs call the invitee's "notes" (the default first
    question, asking the invitee to share context ahead of the meeting) is
    labeled ``Notes`` instead of ``Q&A`` so it stands out — everything else
    is rendered as a normal question/answer pair. All entries are ingested
    either way; this only changes the label.
    """
    lines: list[str] = []
    for entry in sorted(qa_list or [], key=lambda e: e.get("position", 0)):
        question = (entry.get("question") or "").strip()
        answer = (entry.get("answer") or "").strip()
        if not answer:
            continue
        if _NOTES_QUESTION_HINT in question.lower():
            lines.append(f"  - Notes: {answer}")
        else:
            lines.append(f"  - Q: {question}\n    A: {answer}")
    return lines


def _render_invitee(invitee: dict) -> str:
    """Render one invitee (identity + status + Q&A/notes) as a markdown block."""
    name = invitee.get("name") or invitee.get("email") or "Unknown invitee"
    email = invitee.get("email") or ""
    status = invitee.get("status") or "unknown"
    header = f"- **{name}** <{email}> — {status}"
    if status == "canceled":
        reason = (invitee.get("cancellation") or {}).get("reason")
        if reason:
            header += f" ({reason})"

    lines = [header]
    lines.extend(_render_qa(invitee.get("questions_and_answers")))
    return "\n".join(lines)


def _event_to_row(event: dict, invitees: list[dict]) -> dict[str, Any]:
    """Flatten a scheduled event + its invitees into a single document row.

    ``content`` folds the event's own fields together with every invitee's
    status and questions/answers/notes into one markdown document, so
    cognify's entity extraction can link an invitee's answers to the event
    (and to each other) instead of ingesting a meaningless bare time slot.
    """
    name = event.get("name") or "(Untitled event)"
    lines = [
        f"# {name}",
        f"Status: {event.get('status', 'unknown')}",
        f"When: {event.get('start_time', '?')} – {event.get('end_time', '?')}",
        f"Location: {_location_text(event)}",
    ]
    if invitees:
        lines.append("\n## Invitees")
        lines.extend(_render_invitee(invitee) for invitee in invitees)

    return {
        "id": _event_id(event),
        "name": name,
        "status": event.get("status", "unknown"),
        "start_time": event.get("start_time", ""),
        "end_time": event.get("end_time", ""),
        "url": event.get("uri", ""),
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _deleted_row(event_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete an event by id."""
    return {"id": event_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Calendly API reads
# ---------------------------------------------------------------------------
def _list_scheduled_events(
    session: Any,
    *,
    user_uri: str | None,
    organization_uri: str | None,
    min_start_time: str,
) -> Iterator[dict]:
    """Yield scheduled events (any status) starting on/after ``min_start_time``."""
    params: dict[str, Any] = {
        "count": _PAGE_SIZE,
        "min_start_time": min_start_time,
        "sort": "start_time:asc",
    }
    if organization_uri:
        params["organization"] = organization_uri
    elif user_uri:
        params["user"] = user_uri
    yield from _paginate(session, f"{CALENDLY_API_BASE}/scheduled_events", params)


def _list_invitees(session: Any, event_uri: str) -> list[dict]:
    """Return every invitee (any status) for one scheduled event."""
    params = {"count": _PAGE_SIZE}
    return list(_paginate(session, f"{event_uri}/invitees", params))


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
@dataclass
class CalendlySyncConfig:
    user_uri: str | None = None
    organization_uri: str | None = None
    lookback_days: int = 7
    recheck_window_days: int = 2
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def sync_events(
    session: Any,
    state: dict,
    config: CalendlySyncConfig,
) -> Iterator[dict[str, Any]]:
    """Yield changed/new events since the ``min_start_time`` cursor, plus
    hard-delete markers for events (or fully-canceled event) found canceled.

    The floor is read from ``state['min_start_time']``; on the first run it
    defaults to ``now - lookback_days``, thereafter to
    ``now - recheck_window_days`` (see module docstring for the rationale).
    Every event at/after the floor is re-fetched (with fresh invitees), so an
    edited invitee answer or a newly added invitee is always reflected —
    there is no separate "unchanged, skip" fast path, mirroring Notion's
    stance that content correctness matters more than saving a request here.
    """
    floor = state.get("min_start_time")
    if not floor:
        floor = _iso(config.now - timedelta(days=config.lookback_days))

    active = 0
    canceled = 0
    for event in _list_scheduled_events(
        session,
        user_uri=config.user_uri,
        organization_uri=config.organization_uri,
        min_start_time=floor,
    ):
        event_id = _event_id(event)
        if event.get("status") == "canceled":
            canceled += 1
            yield _deleted_row(event_id)
            continue

        invitees = _list_invitees(session, event["uri"])
        active += 1
        yield _event_to_row(event, invitees)

    state["min_start_time"] = _iso(config.now - timedelta(days=config.recheck_window_days))
    logger.info("Calendly: %d active event(s), %d canceled event(s) forgotten.", active, canceled)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def calendly_source(
    *,
    token: str | None = None,
    user_uri: str | None = None,
    organization_uri: str | None = None,
    lookback_days: int = 7,
    recheck_window_days: int = 2,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields Calendly scheduled events for ``remember``.

    Args:
        token: Calendly Personal Access Token. Falls back to
            ``CALENDLY_API_TOKEN``. Ignored if ``session`` is provided.
        user_uri: Restrict to this user's events (``GET /users/me`` resource
            uri). Defaults to the token owner (resolved lazily on first
            sync). Ignored if ``organization_uri`` is set.
        organization_uri: Restrict to every event across this organization
            (requires an admin/owner token). Takes precedence over
            ``user_uri``.
        lookback_days: How far back the very first sync backfills (default 7).
        recheck_window_days: How far back each *later* sync re-checks for
            changes/cancellations (default 2). Future events are always
            included regardless of either setting.
        session: Pre-built ``requests`` session. Mainly an injection point
            for tests; when omitted one is built from ``token``.

    Returns:
        A ``dlt`` resource (``calendly_events``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an
        ``_deleted`` hard-delete column, tagged as a document source so its
        rendered content flows through normal cognify entity extraction.
        Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if session is None:
        resolved_token = token or os.environ.get("CALENDLY_API_TOKEN")
        if not resolved_token:
            raise ValueError(
                "Calendly Personal Access Token required: pass token= or set "
                "CALENDLY_API_TOKEN."
            )
        session = _make_session(resolved_token)

    @dlt.resource(
        name=CALENDLY_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def calendly_events():
        resolved_user_uri = user_uri
        if not organization_uri and not resolved_user_uri:
            resolved_user_uri = _current_user_uri(session)

        resource_state = dlt.current.resource_state()
        config = CalendlySyncConfig(
            user_uri=resolved_user_uri,
            organization_uri=organization_uri,
            lookback_days=lookback_days,
            recheck_window_days=recheck_window_days,
        )
        yield from sync_events(session, resource_state, config)

    resource = calendly_events()
    # Opt into the document ingestion path (event+invitee Q&A -> text document
    # -> cognify). resolve_dlt_sources reads this marker; it never imports
    # this connector. Sync stays incremental via write_disposition="merge"
    # (the min_start_time window + _deleted hard-delete above).
    setattr(resource, DOCUMENT_SOURCE_ATTR, CALENDLY_SOURCE_NAME)
    return resource
