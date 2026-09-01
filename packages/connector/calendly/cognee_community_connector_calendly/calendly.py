"""Calendly connector for cognee — a ``dlt`` source that turns scheduled events into memory.

Pull Calendly scheduled events (invitee questions & answers + meeting notes) into
cognee, incrementally and with forget-on-delete — "ask my calendar". Built entirely
on the existing DLT ingestion subsystem; the source produced here is handed directly
to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_calendly import calendly_source

    await cognee.remember(
        calendly_source(),           # CALENDLY_API_TOKEN from env, or pass token=...
        dataset_name="calendly",
        write_disposition="merge",   # REQUIRED (see .. important:: below)
        max_rows_per_table=0,        # REQUIRED for a real account (see .. note:: below)
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**. The add pipeline defaults to
   ``"replace"`` (drop + reload the table each run); on the second, incremental
   sync that would wipe the whole synced history. Always pass ``"merge"``.

Design
------
* **Auth** — Calendly Personal Access Token (PAT), sent as ``Authorization: Bearer``.
  The connector only ever reads.
* **Primary key** — the scheduled-event UUID (from the event ``uri``). Combined with
  ``write_disposition="merge"`` this gives idempotent upserts.
* **Incremental cursor** — the event's ``updated_at`` (last-modified) timestamp,
  persisted in dlt's per-resource state. Each run lists the *current* active
  events (a cheap metadata sweep) and re-fetches only those whose ``updated_at``
  is newer than the stored cursor (plus anything new to the corpus). ``updated_at``
  — not ``start_time`` — is the correct delta signal: a meeting note added after
  an event ends bumps ``updated_at`` and is therefore caught, which a
  ``start_time`` window can never see.
* **Forget-on-delete** — Calendly has no delete feed, so each run compares the ids
  it saw on the previous run against the current active sweep. A canceled (or
  vanished) event drops out of the active listing and is emitted with the
  ``_deleted`` hard-delete marker; dlt removes those rows on ``merge`` and cognee's
  existing ``orphan_cleanup`` purges them from the graph + vector + relational
  stores.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows from the
   dlt destination (default 50). For a real account pass ``max_rows_per_table=0``
   (unlimited) so orphan-cleanup compares against the *whole* synced corpus rather
   than a truncated window.

Privacy
-------
This connector reads your scheduled events, invitee answers, and meeting notes. It
is **opt-in**: nothing is fetched until you construct a source and call ``remember``.
Keep your PAT private and use a dedicated dataset so you can ``cognee.forget`` it.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("calendly_connector")

CALENDLY_API_URL = "https://api.calendly.com"
# dlt resource / staging-table name for Calendly scheduled events.
CALENDLY_TABLE_NAME = "calendly_events"
CALENDLY_SOURCE_NAME = "calendly"

# Calendly pages up to 100 rows per request; one page per event is ~the whole event.
_PAGE_SIZE = 100

# Retry budget for rate-limited / transient Calendly API responses.
_MAX_RETRIES = 3

_EXTRA_HINT = (
    'The Calendly connector requires the "calendly" extra: pip install "cognee[calendly]" '
    "(provides dlt and httpx)."
)


def calendly_source(
    token: str | None = None,
    *,
    client: Any = None,
    invitee_email: str | None = None,
):
    """Create a dlt resource that yields Calendly scheduled events as documents.

    Args:
        token: Calendly Personal Access Token. Falls back to ``CALENDLY_API_TOKEN``.
        client: Pre-built :class:`CalendlyClient` (mainly a test-injection point);
            when omitted one is built from the token above.
        invitee_email: Restrict ingestion to events with an invitee matching this
            email (Calendly's ``invitee_email`` list filter). ``None`` = all events.

    Returns:
        A dlt resource (``calendly_events``) configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and an ``_deleted`` hard-delete column. Hand it
        to ``cognee.remember(...)`` with ``write_disposition="merge"``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    @dlt.resource(
        name=CALENDLY_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # `_deleted` is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the deletion
        # through cognee's orphan_cleanup (matching gmail.py / google_drive.py).
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def calendly_events():
        api = client or _build_client(token)
        # Only close the client when we built it ourselves; an injected client is
        # owned by the caller (tests pass a fake without a close()).
        close_on_exit = client is None
        try:
            resource_state = dlt.current.resource_state()
            user_uri = api.current_user()
            yield from _iter_rows(
                api, resource_state, user_uri=user_uri, invitee_email=invitee_email
            )
        finally:
            if close_on_exit:
                api.close()

    resource = calendly_events()
    # Opt into the document ingestion path (event → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, CALENDLY_SOURCE_NAME)
    return resource


def _build_client(token: str | None) -> "CalendlyClient":
    resolved_token = token or os.environ.get("CALENDLY_API_TOKEN")
    if not resolved_token:
        raise ValueError(
            "Calendly token required: pass token= or set CALENDLY_API_TOKEN."
        )
    return CalendlyClient(resolved_token)


# ---------------------------------------------------------------------------
# Calendly API client
# ---------------------------------------------------------------------------


class CalendlyClient:
    """Minimal Calendly API v2 client (Personal Access Token auth).

    Kept deliberately thin: three read-only methods covering the endpoints this
    connector needs. Tests inject a fake with the same surface.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = CALENDLY_API_URL,
        timeout: float = 30.0,
    ):
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def current_user(self) -> str:
        """Return the authenticated user's ``uri`` (used to scope event listings)."""
        payload = self._request("GET", "/users/me")
        return payload["resource"]["uri"]

    def list_events(
        self,
        *,
        user_uri: str,
        status: str | None = None,
        invitee_email: str | None = None,
        page_token: str | None = None,
        count: int = _PAGE_SIZE,
    ) -> tuple[list[dict], str | None]:
        """Return one page of scheduled events plus the next-page token.

        ``status`` is ``"active"`` or ``"canceled"``; ``invitee_email`` scopes to
        one invitee. All are passed through to Calendly unchanged.
        """
        params: dict[str, Any] = {"user": user_uri, "count": count}
        if status:
            params["status"] = status
        if invitee_email:
            params["invitee_email"] = invitee_email
        if page_token:
            params["page_token"] = page_token
        return self._get_page("/scheduled_events", params)

    def list_invitees(
        self,
        event_uuid: str,
        *,
        page_token: str | None = None,
        count: int = _PAGE_SIZE,
    ) -> tuple[list[dict], str | None]:
        """Return one page of invitees (with ``questions_and_answers``) for an event."""
        params: dict[str, Any] = {"count": count}
        if page_token:
            params["page_token"] = page_token
        return self._get_page(f"/scheduled_events/{event_uuid}/invitees", params)

    def _get_page(self, path: str, params: dict[str, Any]) -> tuple[list[dict], str | None]:
        payload = self._request("GET", path, params=params)
        pagination = payload.get("pagination") or {}
        return payload.get("collection") or [], pagination.get("next_page_token")

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> dict:
        """Call the Calendly API, retrying rate-limit / transient errors.

        Calendly rate-limits bursts aggressively, so a 429 or a 5xx is retried with
        backoff. Permanent errors (401/403/404) propagate immediately; under
        ``merge`` an aborted run leaves staging/memory untouched, which is the safe
        failure (no partial snapshot can drive deletions).
        """
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.request(method, path, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if attempt == _MAX_RETRIES - 1 or not _is_transient_status(exc):
                    raise
            except httpx.TransportError:
                if attempt == _MAX_RETRIES - 1:
                    raise
            time.sleep(2**attempt)
        raise AssertionError("unreachable")  # pragma: no cover - last attempt raises


def _is_transient_status(exc: httpx.HTTPStatusError) -> bool:
    return exc.response.status_code in (429, 500, 502, 503, 504)


# ---------------------------------------------------------------------------
# Sync strategy (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------


def _event_updated_at(event: dict) -> str:
    """Return the event's last-modified timestamp (``updated_at``) — the cursor.

    ``updated_at`` is the only monotonic signal Calendly exposes per event and is
    bumped by any change (reschedule, notes edit, cancellation), unlike
    ``start_time`` which is fixed at booking. RFC3339 UTC strings compare
    lexicographically because the API emits a single consistent format.
    """
    return event.get("updated_at") or ""


def _iter_rows(client, state: dict, *, user_uri: str, invitee_email: str | None = None):
    """Yield changed active events plus hard-delete tombstones for cancelations.

    Decoupled from dlt's resource-state machinery (which needs an active pipeline
    context) so it is directly unit-testable with a fake client and a plain dict
    standing in for dlt's resource state.

    One cheap metadata sweep of the *current* active events drives both sides,
    mirroring the Confluence connector:

    * **Change** — an event whose ``updated_at`` is newer than the stored cursor
      (``last_updated``) is re-fetched (invitees + notes) and re-emitted. An event
      absent from ``known_ids`` is fetched regardless of timestamp, so an event new
      to the corpus can't be lost to a cursor-boundary tie.
    * **Forget-on-delete** — an id that was known last run but no longer appears in
      the active sweep (canceled, or vanished from Calendly) is emitted with the
      ``_deleted`` hard-delete marker.

    The cursor is ``updated_at``, not ``start_time``: a meeting note added after an
    event ends bumps ``updated_at`` and is therefore picked up on the next run,
    which the old ``min_start_time`` window could never see. Unchanged events are
    skipped (their rows persist under ``merge``) and keep a stable content-hash
    ``data_id``, so they are not re-cognified.
    """
    known_ids: set[str] = set(state.get("known_ids", []))
    last_updated: str = state.get("last_updated", "")
    newest_updated = last_updated
    current_ids: set[str] = set()
    changed = 0

    for event in _iter_events(client, user_uri, status="active", invitee_email=invitee_email):
        event_id = _event_uuid(event.get("uri"))
        if not event_id:
            continue
        current_ids.add(event_id)

        updated_at = _event_updated_at(event)
        if event_id in known_ids and updated_at <= last_updated:
            continue
        if updated_at > newest_updated:
            newest_updated = updated_at

        invitees = list(_iter_invitees(client, event_id))
        yield _event_to_row(event, invitees)
        changed += 1

    # A canceled event drops out of the active listing; a vanished one disappears
    # entirely. Either way it is no longer in ``current_ids``, so the id difference
    # captures both. No separate ``status=canceled`` sweep is needed, and unlike
    # Confluence there is no "empty sweep" guard: the client raises on any HTTP
    # error (so a transient blip aborts the run and never reaches this point), and
    # a genuinely empty active listing simply means every event was canceled.
    deleted = known_ids - current_ids
    for event_id in sorted(deleted):
        yield {"id": event_id, "_deleted": True}

    state["known_ids"] = sorted(current_ids)
    state["last_updated"] = newest_updated
    logger.info("Calendly: synced %d changed event(s), %d deletion(s).", changed, len(deleted))


def _iter_events(
    client,
    user_uri: str,
    *,
    status: str,
    invitee_email: str | None = None,
):
    """Yield every scheduled event matching a status, following pagination."""
    page_token = None
    while True:
        events, page_token = client.list_events(
            user_uri=user_uri,
            status=status,
            invitee_email=invitee_email,
            page_token=page_token,
        )
        yield from events
        if not page_token:
            return


def _iter_invitees(client, event_uuid: str):
    """Yield every invitee (with questions & answers) for an event, following pagination."""
    page_token = None
    while True:
        invitees, page_token = client.list_invitees(event_uuid, page_token=page_token)
        yield from invitees
        if not page_token:
            return


# ---------------------------------------------------------------------------
# Event / invitee rendering
# ---------------------------------------------------------------------------


def _event_to_row(event: dict, invitees: list[dict]) -> dict:
    """Flatten a Calendly event + its invitees into a document row.

    Only identity/provenance + text are kept, so a metadata-only edit that bumps
    ``updated_at`` without changing the rendered text does not churn the content-hash
    data_id. The invitee questions & answers are the core context and are rendered
    in full into ``content``.
    """
    return {
        "id": _event_uuid(event.get("uri")),
        "title": event.get("name") or "",
        "content": _render_event(event, invitees),
        "url": event.get("uri"),
        "_deleted": False,
    }


def _render_event(event: dict, invitees: list[dict]) -> str:
    """Render a scheduled event to plain text, including meeting notes and invitees."""
    lines: list[str] = []

    name = event.get("name") or ""
    if name:
        lines.append(f"Event: {name}")
    lines.append(f"Status: {event.get('status') or 'unknown'}")

    start = event.get("start_time")
    if start:
        lines.append(f"Start: {start}")
    end = event.get("end_time")
    if end:
        lines.append(f"End: {end}")

    location = _render_location(event.get("location"))
    if location:
        lines.append(f"Location: {location}")

    notes = (event.get("meeting_notes_plain") or "").strip()
    if notes:
        lines.append("")
        lines.append("Meeting notes:")
        lines.append(notes)

    if invitees:
        lines.append("")
        lines.append("Invitees:")
        for invitee in invitees:
            lines.append(_render_invitee(invitee))

    return "\n".join(lines)


def _render_invitee(invitee: dict) -> str:
    """Render one invitee and their question/answer responses (the core context)."""
    name = invitee.get("name") or ""
    email = invitee.get("email") or ""
    label = f"{name} <{email}>" if name and email else (name or email or "unknown")

    lines = [f"- {label}"]
    for qa in invitee.get("questions_and_answers") or []:
        question = (qa.get("question") or "").strip()
        answer = (qa.get("answer") or "").strip()
        if question or answer:
            lines.append(f"  - Q: {question}")
            lines.append(f"    A: {answer}")
    return "\n".join(lines)


def _render_location(location: Any) -> str:
    """Render a Calendly location (``{type, location|join_url}``) to one line."""
    if not isinstance(location, dict):
        return ""
    loc_type = location.get("type") or ""
    loc_value = location.get("location") or location.get("join_url") or ""
    if loc_type and loc_value:
        return f"{loc_type}: {loc_value}"
    return loc_type or loc_value


def _event_uuid(uri: str | None) -> str:
    """Extract the scheduled-event UUID from its ``uri`` (last path segment)."""
    if not uri:
        return ""
    return uri.rstrip("/").split("/")[-1]
