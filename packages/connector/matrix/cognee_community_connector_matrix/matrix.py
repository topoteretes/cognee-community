"""Matrix / Element connector for cognee — a ``dlt`` source over room messages.

Pull messages (and thread replies) from Matrix rooms the account has joined into
cognee, incrementally and with forget-on-redaction — "ask my team chat".  Works
against any homeserver that speaks the Matrix Client-Server API (matrix.org,
a self-hosted Synapse / Dendrite / Conduit, an Element-hosted server)::

    import cognee
    from cognee_community_connector_matrix import matrix_source

    await cognee.remember(
        matrix_source(
            homeserver="https://matrix.example.org",
            access_token="syt_…",
            room_ids=["!abc123:example.org"],
        ),
        dataset_name="team_chat",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Auth** — a user (or bot user) access token, sent as ``Authorization:
  Bearer``.  The connector only issues ``GET`` requests.
* **Primary key** — the Matrix ``event_id`` of the message.  Combined with
  ``write_disposition="merge"`` this gives idempotent upserts.
* **Incremental cursor** — the ``next_batch`` token returned by ``/sync``.  It
  is persisted in dlt's per-resource state; the next run calls
  ``/sync?since=<token>`` and only sees events that arrived after it.
* **Gaps** — ``/sync`` returns at most ``timeline.limit`` events per room.  When
  more arrived, the room's timeline is ``limited`` and carries ``prev_batch``;
  we then page ``/rooms/{id}/messages`` backwards until we reach the newest
  timestamp ingested on the previous run (or ``max_backfill_events`` on the
  first run).
* **Edits** — an ``m.replace`` relation updates the *original* event's row with
  ``m.new_content``, so the graph holds the current text, not every revision.
* **Forget-on-delete** — a redaction (``m.room.redaction``) emits the redacted
  event id with the ``_deleted`` hard-delete marker; dlt drops the row on
  ``merge`` and cognee's ``orphan_cleanup`` purges it from the graph + vector +
  relational stores.  Leaving a room (``rooms.leave``) forgets every message
  ingested from it when ``forget_on_leave=True`` (the default, explicit policy).

.. note::
   End-to-end encrypted rooms deliver ``m.room.encrypted`` events that cannot
   be read without the device keys.  They are skipped and counted in the log;
   use an unencrypted room or a bot account for the rooms you want in memory.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from cognee.shared.logging_utils import get_logger

logger = get_logger("matrix_connector")

_API = "/_matrix/client/v3"

# Message types that carry human-readable text worth remembering.
_TEXT_MSGTYPES = frozenset({"m.text", "m.notice", "m.emote"})


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------
def _make_session(access_token: str) -> Any:
    """Build a ``requests`` session that authenticates with a Matrix access token."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - depends on installed deps
        raise ImportError('The Matrix connector requires "requests": pip install requests') from exc

    session = requests.Session()
    session.headers.update(
        {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    )
    return session


def _api_get(session: Any, homeserver: str, path: str, params: dict | None = None) -> dict:
    response = session.get(f"{homeserver}{_API}{path}", params=params or {})
    response.raise_for_status()
    return response.json()


def _build_filter(room_ids: list[str] | None, timeline_limit: int) -> str:
    """Inline ``/sync`` filter: only message-ish timeline events, no presence noise."""
    room_filter: dict[str, Any] = {
        "timeline": {
            # m.room.member is not ingested, but Synapse omits a left room from
            # rooms.leave when the filter hides the leave event itself, which
            # would silently break forget-on-leave.
            "types": [
                "m.room.message",
                "m.room.redaction",
                "m.room.encrypted",
                "m.room.member",
            ],
            "limit": timeline_limit,
        },
        "state": {"types": ["m.room.name", "m.room.canonical_alias"]},
        "ephemeral": {"not_types": ["*"]},
        "include_leave": True,
        "account_data": {"not_types": ["*"]},
    }
    if room_ids:
        room_filter["rooms"] = list(room_ids)
    return json.dumps(
        {
            "room": room_filter,
            "presence": {"not_types": ["*"]},
            "account_data": {"not_types": ["*"]},
        },
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------
def _iso(ts_ms: int | None) -> str:
    if not ts_ms:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).isoformat()


def _redacts(event: dict) -> str | None:
    """Return the id a redaction targets (top-level before room v11, content after)."""
    return event.get("redacts") or (event.get("content") or {}).get("redacts")


def _relation(event: dict) -> dict:
    return (event.get("content") or {}).get("m.relates_to") or {}


def _room_name(state_events: list[dict]) -> str | None:
    name = alias = None
    for event in state_events:
        content = event.get("content") or {}
        if event.get("type") == "m.room.name" and content.get("name"):
            name = content["name"]
        elif event.get("type") == "m.room.canonical_alias" and content.get("alias"):
            alias = content["alias"]
    return name or alias


def _permalink(room_id: str, event_id: str) -> str:
    return f"https://matrix.to/#/{quote(room_id, safe='!:')}/{quote(event_id, safe='$:')}"


def _text(sender: str, room_name: str, when: str, content: dict) -> str:
    """Prefix the body with who/where/when so entity extraction has context."""
    return f"{sender} in {room_name} at {when}: {content.get('body') or ''}"


def _message_row(event: dict, room_id: str, room_name: str, content: dict) -> dict[str, Any]:
    relation = _relation(event)
    thread_root = relation.get("event_id") if relation.get("rel_type") == "m.thread" else None
    reply_to = (relation.get("m.in_reply_to") or {}).get("event_id")
    sender = event.get("sender") or ""
    when = _iso(event.get("origin_server_ts"))
    return {
        "id": event["event_id"],
        "room_id": room_id,
        "room_name": room_name,
        "sender": sender,
        "sent_at": when,
        "thread_root": thread_root,
        "reply_to": reply_to,
        "url": _permalink(room_id, event["event_id"]),
        "text": _text(sender, room_name, when, content),
        "_deleted": False,
    }


def _deleted_row(event_id: str) -> dict[str, Any]:
    return {"id": event_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Room timeline handling
# ---------------------------------------------------------------------------
def _backfill(
    session: Any,
    homeserver: str,
    room_id: str,
    from_token: str,
    *,
    stop_at_ts: int,
    known_ids: set[str],
    max_events: int,
    filter_json: str,
) -> list[dict]:
    """Page ``/messages`` backwards from ``from_token``; return events oldest-first."""
    events: list[dict] = []
    token: str | None = from_token
    path = f"/rooms/{quote(room_id, safe='')}/messages"
    room_event_filter = json.dumps(json.loads(filter_json)["room"]["timeline"])
    while token and len(events) < max_events:
        data = _api_get(
            session,
            homeserver,
            path,
            {"from": token, "dir": "b", "limit": 100, "filter": room_event_filter},
        )
        chunk = data.get("chunk") or []
        reached_known = False
        for event in chunk:  # newest-first
            # Stop at the first event we already ingested, or anything strictly
            # older than the newest one (strict, so same-millisecond events
            # are not lost; re-emitting a known message is an idempotent upsert).
            if event.get("event_id") in known_ids or (
                stop_at_ts and (event.get("origin_server_ts") or 0) < stop_at_ts
            ):
                reached_known = True
                break
            events.append(event)
            if len(events) >= max_events:
                break
        if reached_known or not chunk:
            break
        token = data.get("end")
    events.reverse()
    return events


def _apply_edit(
    event: dict,
    room_id: str,
    room_name: str,
    pending: dict[str, dict],
    stats: dict[str, int],
    *,
    known_ids: set[str],
    redacted_ids: set[str],
    room_seen_before: bool,
) -> None:
    """Normalize an ``m.replace`` onto the original event id (no resurrection)."""
    content = event.get("content") or {}
    relation = _relation(event)
    original_id = relation.get("event_id")
    new_content = content.get("m.new_content") or {}
    if not original_id or new_content.get("msgtype") not in _TEXT_MSGTYPES:
        return

    # A prior redaction wins: never bring a forgotten message back via an edit.
    if original_id in redacted_ids or (pending.get(original_id) or {}).get("_deleted"):
        return

    original = pending.get(original_id)
    # Spec: only the original author may edit. Without the original in this
    # batch we can still upsert when we previously ingested that id.
    if original and original.get("sender") != event.get("sender"):
        return
    if (
        not original
        and room_seen_before
        and original_id not in known_ids
        and original_id not in pending
    ):
        # Original was dropped from state (redacted/left) — do not resurrect.
        return

    row = _message_row(dict(event, event_id=original_id), room_id, room_name, new_content)
    if original:  # keep the original's place in time and in its thread
        row.update(
            sent_at=original["sent_at"],
            thread_root=original["thread_root"],
            reply_to=original["reply_to"],
        )
        row["text"] = _text(row["sender"], room_name, row["sent_at"], new_content)
    pending[original_id] = row
    stats["edited"] += 1


def _apply_timeline(
    events: list[dict],
    room_id: str,
    room_name: str,
    pending: dict[str, dict],
    stats: dict[str, int],
    *,
    known_ids: set[str] | None = None,
    redacted_ids: set[str] | None = None,
    room_seen_before: bool = False,
) -> int:
    """Fold timeline events (oldest-first) into ``pending``; return newest ts seen.

    Messages and redactions are applied first; ``m.replace`` edits run in a
    second pass so an edit that arrives before its original in the same batch
    still lands on the original event id without being overwritten.
    """
    known_ids = known_ids or set()
    redacted_ids = redacted_ids if redacted_ids is not None else set()
    newest = 0
    edits: list[dict] = []

    for event in events:
        newest = max(newest, event.get("origin_server_ts") or 0)
        etype = event.get("type")
        event_id = event.get("event_id")

        if etype == "m.room.encrypted":
            stats["encrypted"] += 1
            continue

        if etype == "m.room.redaction":
            target = _redacts(event)
            if target:
                pending[target] = _deleted_row(target)
                redacted_ids.add(target)
                stats["redacted"] += 1
            continue

        if etype != "m.room.message" or not event_id:
            continue

        content = event.get("content") or {}
        if (event.get("unsigned") or {}).get("redacted_because") or not content:
            # Already redacted before we saw it: make sure it is not in memory.
            pending[event_id] = _deleted_row(event_id)
            redacted_ids.add(event_id)
            stats["redacted"] += 1
            continue

        if _relation(event).get("rel_type") == "m.replace":
            edits.append(event)
            continue

        if content.get("msgtype") not in _TEXT_MSGTYPES:
            stats["skipped"] += 1
            continue

        pending[event_id] = _message_row(event, room_id, room_name, content)
        redacted_ids.discard(event_id)
        stats["messages"] += 1

    for event in edits:
        _apply_edit(
            event,
            room_id,
            room_name,
            pending,
            stats,
            known_ids=known_ids,
            redacted_ids=redacted_ids,
            room_seen_before=room_seen_before,
        )
    return newest


# ---------------------------------------------------------------------------
# Sync (pure given a session + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_messages(
    session: Any,
    homeserver: str,
    state: dict,
    *,
    room_ids: list[str] | None = None,
    timeline_limit: int = 100,
    max_backfill_events: int = 1000,
    forget_on_leave: bool = True,
) -> Iterator[dict[str, Any]]:
    """Yield new/edited messages since the last run, plus hard-delete markers.

    ``state`` keys (all JSON-serialisable, persisted by dlt):

    * ``since`` — the ``next_batch`` token of the previous ``/sync``.
    * ``room_last_ts`` — newest ``origin_server_ts`` ingested per room; bounds
      gap backfill so a ``limited`` timeline never re-reads old history.
    * ``room_event_ids`` — ids ingested per room, used to forget a room's
      messages once the account leaves it (when ``forget_on_leave`` is on).
    * ``redacted_ids`` — event ids we have already forgotten, so a later
      ``m.replace`` cannot resurrect them.

    Leaving a room is an access change, not proof the upstream messages were
    deleted. ``forget_on_leave=True`` (default) is the explicit policy that
    matches the issue's forget-on-delete acceptance criterion; set it to
    ``False`` to keep previously ingested rows after a leave.
    """
    filter_json = _build_filter(room_ids, timeline_limit)
    params: dict[str, Any] = {"filter": filter_json, "timeout": 0}
    since = state.get("since")
    if since:
        params["since"] = since

    data = _api_get(session, homeserver, "/sync", params)
    rooms = data.get("rooms") or {}
    room_last_ts: dict[str, int] = dict(state.get("room_last_ts") or {})
    room_event_ids: dict[str, list[str]] = {
        k: list(v) for k, v in (state.get("room_event_ids") or {}).items()
    }
    room_names: dict[str, str] = dict(state.get("room_names") or {})
    redacted_ids: set[str] = set(state.get("redacted_ids") or [])
    wanted = set(room_ids or [])
    stats = {"messages": 0, "edited": 0, "redacted": 0, "encrypted": 0, "skipped": 0}
    pending: dict[str, dict] = {}

    for room_id, room in (rooms.get("join") or {}).items():
        if wanted and room_id not in wanted:
            continue
        name = _room_name((room.get("state") or {}).get("events") or [])
        if not name:
            name = _room_name((room.get("timeline") or {}).get("events") or [])
        if name:
            room_names[room_id] = name
        room_name = room_names.get(room_id, room_id)

        timeline = room.get("timeline") or {}
        events = list(timeline.get("events") or [])
        if timeline.get("limited") and timeline.get("prev_batch"):
            events = (
                _backfill(
                    session,
                    homeserver,
                    room_id,
                    timeline["prev_batch"],
                    stop_at_ts=room_last_ts.get(room_id, 0),
                    known_ids=set(room_event_ids.get(room_id, [])),
                    max_events=max_backfill_events,
                    filter_json=filter_json,
                )
                + events
            )

        room_pending: dict[str, dict] = {}
        known = set(room_event_ids.get(room_id, []))
        newest = _apply_timeline(
            events,
            room_id,
            room_name,
            room_pending,
            stats,
            known_ids=known,
            redacted_ids=redacted_ids,
            room_seen_before=room_id in room_last_ts,
        )
        if newest:
            room_last_ts[room_id] = max(room_last_ts.get(room_id, 0), newest)

        ids = set(room_event_ids.get(room_id, []))
        for event_id, row in room_pending.items():
            if row.get("_deleted"):
                ids.discard(event_id)
                redacted_ids.add(event_id)
            else:
                ids.add(event_id)
                redacted_ids.discard(event_id)
        room_event_ids[room_id] = sorted(ids)
        pending.update(room_pending)

    forgotten = 0
    for room_id in rooms.get("leave") or {}:
        if wanted and room_id not in wanted:
            continue
        left_ids = room_event_ids.pop(room_id, [])
        room_last_ts.pop(room_id, None)
        if not forget_on_leave:
            continue
        for event_id in left_ids:
            pending[event_id] = _deleted_row(event_id)
            redacted_ids.add(event_id)
            forgotten += 1

    yield from pending.values()

    # Advance the cursor only after every row was handed to dlt.
    state["since"] = data.get("next_batch") or since
    state["room_last_ts"] = room_last_ts
    state["room_event_ids"] = room_event_ids
    state["room_names"] = room_names
    state["redacted_ids"] = sorted(redacted_ids)
    logger.info(
        "Matrix: %(messages)d new, %(edited)d edited, %(redacted)d redacted, "
        "%(encrypted)d encrypted (skipped), %(skipped)d non-text (skipped), "
        "%(forgotten)d forgotten from left rooms.",
        {**stats, "forgotten": forgotten},
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def matrix_source(
    *,
    homeserver: str,
    access_token: str | None = None,
    room_ids: list[str] | None = None,
    timeline_limit: int = 100,
    max_backfill_events: int = 1000,
    forget_on_leave: bool = True,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields Matrix room messages for ``remember``.

    Args:
        homeserver: Client-Server API base URL, e.g. ``https://matrix.org``.
        access_token: Access token of the (bot) user whose joined rooms to sync.
        room_ids: Restrict to these room ids (``!abc:server``). ``None`` syncs
            every joined room.
        timeline_limit: Events per room per ``/sync`` response.
        max_backfill_events: Cap on history fetched per room when a timeline is
            ``limited`` (first run, or a long gap between runs).
        forget_on_leave: When ``True`` (default), emit hard-delete markers for
            every message previously ingested from a room that appears under
            ``rooms.leave``. Leaving is an access change, not an upstream
            deletion — keep this explicit. Set ``False`` to retain rows.
        session: Pre-built ``requests`` session (test injection point).

    Returns:
        A ``dlt`` resource (``matrix_messages``) with ``primary_key="id"``,
        ``write_disposition="merge"`` and an ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError('The Matrix connector requires dlt: pip install "dlt"') from exc

    homeserver = homeserver.rstrip("/")
    if session is None and not access_token:
        raise ValueError("matrix_source requires an access_token (or an injected session).")

    @dlt.resource(
        name="matrix_messages",
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def matrix_messages():
        client = session or _make_session(access_token)
        yield from sync_messages(
            client,
            homeserver,
            dlt.current.resource_state(),
            room_ids=room_ids,
            timeline_limit=timeline_limit,
            max_backfill_events=max_backfill_events,
            forget_on_leave=forget_on_leave,
        )

    return matrix_messages
