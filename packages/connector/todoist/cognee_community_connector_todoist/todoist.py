"""DLT source for Todoist (sync-token incremental sync + forget-on-delete).

Pulls Todoist projects, tasks, and comments into cognee incrementally — "ask
my todo list".  The source is a single dlt resource meant to be handed
directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_todoist import todoist_source

    await cognee.remember(
        todoist_source(token="..."),
        dataset_name="my_todoist",
        primary_key="id",
        write_disposition="merge",   # REQUIRED (see .. important:: below)
        max_rows_per_table=0,        # REQUIRED for a real account (see .. note:: below)
    )

.. important::
   ``write_disposition="merge"`` is **mandatory**.  The add pipeline defaults
   to ``"replace"`` (drop + reload the table each run); on the second,
   incremental sync that would wipe everything but the small delta.  Always
   pass ``"merge"``.

Design
------
* **Auth** — Todoist API token (read-only usage: the connector only issues
  GET/POST reads against the Sync API).  Pass ``token=`` or set
  ``TODOIST_API_TOKEN``.
* **Primary key** — the Todoist object ``id``.  Combined with
  ``write_disposition="merge"`` this gives idempotent upserts across all three
  entity kinds (projects / tasks / comments) in one staging table.
* **Incremental cursor** — Todoist Sync API's ``sync_token``.  The first run
  uses ``sync_token="*"`` (full backfill); every later run posts the token
  persisted in dlt's resource state and receives exactly what changed since.
* **Forget-on-delete** — deletions surface two ways and both are handled:
  (1) the Sync API returns tombstones for recently deleted objects (rows with
  ``is_deleted=1``), which are re-emitted as hard-delete markers; (2) the
  activities feed is polled for ``deleted`` events since the last sync, which
  covers objects that age out of the delta window.  Deleted rows are removed
  from the dlt destination on ``merge`` and cognee's existing
  ``orphan_cleanup`` then purges them from the graph, vector, and relational
  stores.  When a project is deleted, its tasks (tracked in resource state)
  are hard-deleted with it — Todoist does not reliably emit per-task events
  for a cascade.
* **Document mode** — the resource declares ``cognee_document_source =
  "todoist"`` (the same marker notion/google-drive use), so each row flows
  through the standard cognify entity-extraction pipeline as a normal text
  document instead of the deterministic dlt-row schema-context path.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows
   from the dlt destination (default 50).  For a real account pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.

Privacy
-------
This reads the content of your tasks and comments.  It is **opt-in**: nothing
is fetched until you construct a source and call ``remember``.  Use a
dedicated dataset so you can ``cognee.forget`` the whole thing in one call.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("todoist_connector")

# dlt resource / staging-table name for Todoist objects (all three kinds live
# in one table, discriminated by the ``kind`` column).
TODOIST_SOURCE_NAME = "todoist"
TODOIST_TABLE_NAME = "todoist"

_SYNC_URL = "https://api.todoist.com/sync/v9/sync"
_ACTIVITIES_URL = "https://api.todoist.com/sync/v9/activities"

# Retry budget for rate-limited / transient Todoist API responses.
_MAX_RETRIES = 5

# Page size for the activities (deletion-feed) endpoint.
_ACTIVITIES_PAGE = 100

_EXTRA_HINT = (
    'The Todoist connector requires the "todoist" extra: pip install "cognee[todoist]" '
    "(provides dlt and httpx)."
)

# Sync API resource types that map to the three entity kinds we ingest.
_SYNC_RESOURCE_TYPES = '["projects", "items", "notes"]'

# object_type (activities feed / sync payload) -> row ``kind``.
_OBJECT_KINDS = {"item": "task", "note": "comment", "project": "project"}


def todoist_source(
    token: str | None = None,
    client: Any = None,
):
    """Create a dlt resource that yields Todoist projects/tasks/comments.

    Args:
        token: Todoist API token. Falls back to ``TODOIST_API_TOKEN``.
        client: Pre-built client with ``sync()``/``deleted_events()``
            (mainly a test-injection point); when omitted a
            :class:`TodoistSyncClient` is built from the token above.

    Returns:
        A dlt resource suitable for ``cognee.remember(...)`` with
        ``write_disposition="merge"``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        resolved_token = token or os.environ.get("TODOIST_API_TOKEN")
        if not resolved_token:
            raise ValueError("Todoist API token required: pass token= or set TODOIST_API_TOKEN.")
        client = TodoistSyncClient(resolved_token)

    @dlt.resource(
        name=TODOIST_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker (matching gmail.py /
        # google_drive.py): rows where it is True are removed from the dlt
        # destination on merge, which propagates the deletion through
        # cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def todoist():
        resource_state = dlt.current.resource_state()
        yield from _sync(client, resource_state)

    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(todoist, DOCUMENT_SOURCE_ATTR, TODOIST_SOURCE_NAME)
    return todoist


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class TodoistSyncClient:
    """Minimal Todoist Sync API client over httpx, with retry/backoff.

    Exposes exactly the two operations the connector needs: ``sync`` (delta or
    full snapshot) and ``deleted_events`` (the activities deletion feed).  Any
    object with the same two methods can be injected in its place (tests).
    """

    def __init__(
        self,
        token: str,
        sync_url: str = _SYNC_URL,
        activities_url: str = _ACTIVITIES_URL,
        timeout: float = 30.0,
    ):
        self._token = token
        self._sync_url = sync_url
        self._activities_url = activities_url
        self._timeout = timeout

    def sync(self, sync_token: str) -> dict:
        """Fetch the current delta (or full snapshot when ``sync_token='*'``)."""
        return self._post(
            self._sync_url,
            data={"sync_token": sync_token, "resource_types": _SYNC_RESOURCE_TYPES},
        )

    def deleted_events(self, since: str | None = None) -> list[dict]:
        """Return all recorded ``deleted`` activities, optionally after ``since``.

        Paginates the whole deleted-events feed (the endpoint's default page is
        small) and filters by event date client-side, so callers get a
        stable, complete view regardless of server-side paging.
        """
        events: list[dict] = []
        offset = 0
        while True:
            batch = self._post(
                self._activities_url,
                data={
                    "object_event_type": "deleted",
                    "limit": _ACTIVITIES_PAGE,
                    "offset": offset,
                },
            ).get("events", [])
            events.extend(batch)
            if len(batch) < _ACTIVITIES_PAGE:
                break
            offset += _ACTIVITIES_PAGE
        if since is None:
            return events
        cutoff = _parse_ts(since)
        return [e for e in events if _parse_ts(e.get("event_date")) >= cutoff]

    def _post(self, url: str, data: dict) -> dict:
        """POST with Authorization header, retrying transient failures."""
        import httpx

        for attempt in range(_MAX_RETRIES):
            try:
                resp = httpx.post(
                    url,
                    data=data,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                    raise
                delay = _retry_after(getattr(exc, "response", None), attempt)
                logger.warning(
                    "Todoist: %s — retrying in %.1fs (%d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / timeout / network errors worth retrying."""
    import httpx

    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


def _retry_after(response: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = None
    if response is not None:
        header = response.headers.get("retry-after") or response.headers.get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


# ---------------------------------------------------------------------------
# Row builders (pure — unit-testable)
# ---------------------------------------------------------------------------


def _project_row(project: dict) -> dict:
    """Flatten a Sync-API project into a document row."""
    pid = project.get("id") or ""
    return {
        "id": pid,
        "kind": "project",
        "url": f"https://app.todoist.com/app/project/{pid}",
        "title": project.get("name") or "",
        "content": project.get("description") or project.get("name") or "",
        "_deleted": False,
    }


def _task_row(item: dict) -> dict:
    """Flatten a Sync-API item (task) into a document row.

    Volatile fields (due date, priority, labels, order) are deliberately left
    out of ``content`` so a metadata-only edit does not churn the content-hash
    data_id and trigger a pointless re-cognify.
    """
    tid = item.get("id") or ""
    name = item.get("content") or ""
    description = (item.get("description") or "").strip()
    content = f"{name}\n\n{description}" if description else name
    return {
        "id": tid,
        "kind": "task",
        "url": f"https://app.todoist.com/app/task/{tid}",
        "title": name,
        "content": content,
        "_deleted": False,
    }


def _note_row(note: dict) -> dict:
    """Flatten a Sync-API note (comment) into a document row."""
    nid = note.get("id") or ""
    if note.get("item_id"):
        parent = "task"
    elif note.get("project_id"):
        parent = "project"
    else:
        parent = "item"
    return {
        "id": nid,
        "kind": "comment",
        "url": "",
        "title": f"Comment on {parent}",
        "content": note.get("content") or "",
        "_deleted": False,
    }


def _tombstone_row(object_id: str, object_type: str | None = None) -> dict:
    """A hard-delete marker row for a deleted object."""
    return {"id": object_id, "kind": _OBJECT_KINDS.get(object_type or "", ""), "_deleted": True}


def _extract_deleted(events: list[dict]) -> Iterator[dict]:
    """Map activities ``deleted`` events to hard-delete marker rows.

    Only the three tracked object kinds are emitted; anything else (e.g.
    reminder or note-attachment deletions) is ignored.
    """
    for event in events:
        object_type = event.get("object_type")
        if event.get("event_type") != "deleted" or object_type not in _OBJECT_KINDS:
            continue
        object_id = event.get("object_id")
        if object_id:
            yield _tombstone_row(object_id, object_type)


# ---------------------------------------------------------------------------
# Sync strategies (pure given client + state dict — unit-testable)
# ---------------------------------------------------------------------------


def _sync(client: Any, state: dict) -> Iterator[dict]:
    """Run one sync pass against ``client``, recording cursors in ``state``.

    The first run (no ``sync_token`` in state) does a full backfill with
    ``sync_token="*"``; later runs receive exactly what changed.  Deletes come
    from both the delta payload's tombstones and the activities feed.  The
    sync_token and the sync-start wall clock are recorded *before* rows are
    consumed so no change falls into the gap between fetch and commit.
    """
    started_at = _utcnow_iso()
    delta = client.sync(state.get("sync_token") or "*")

    project_tasks: dict[str, list[str]] = state.setdefault("project_tasks", {})
    feed_deletions = list(_extract_deleted(client.deleted_events(state.get("last_sync_started"))))

    for project in delta.get("projects", []):
        if project.get("is_deleted"):
            # A cascade deletion: the project's tasks must go with it.
            for task_id in project_tasks.pop(project["id"], []):
                yield _tombstone_row(task_id, "item")
            yield _tombstone_row(project["id"], "project")
            continue
        yield _project_row(project)

    for item in delta.get("items", []):
        if item.get("is_deleted"):
            yield _tombstone_row(item["id"], "item")
            continue
        owner = project_tasks.setdefault(item.get("project_id") or "", [])
        if item["id"] not in owner:
            owner.append(item["id"])
        yield _task_row(item)

    for note in delta.get("notes", []):
        if note.get("is_deleted"):
            yield _tombstone_row(note["id"], "note")
            continue
        yield _note_row(note)

    for tomb in feed_deletions:
        # A deleted project cascades to its tasks (tracked in state) — Todoist
        # does not reliably emit per-task events for the cascade.
        if tomb["id"] in project_tasks:
            for task_id in project_tasks.pop(tomb["id"], []):
                yield _tombstone_row(task_id, "item")
            yield _tombstone_row(tomb["id"], "project")
        else:
            yield tomb

    state["sync_token"] = delta.get("sync_token")
    state["last_sync_started"] = started_at


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_ts(value: Any) -> datetime | None:
    """Parse a Todoist/ISO-8601 timestamp ('...Z' or '...+00:00') to UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
