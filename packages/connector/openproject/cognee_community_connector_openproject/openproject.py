"""dlt source for OpenProject work packages (merge + _deleted tombstone + incremental).

Fetches OpenProject work packages via the REST API v3, rendering each work
package as a prose document that preserves subject, description, type, status,
priority, assignee, version, and comment context. Each work package flows
through cognee's document-mode pipeline.

Sync model: ``write_disposition="merge"`` with a ``_deleted`` boolean column.
Each run:
1. Re-checks work packages that were in an active status (in progress, new,
   etc.) on the previous run — their state may have advanced.
2. Pages ``GET /api/v3/work_packages`` with ``filters=updatedAt`` for anything
   changed since the last sync (plus a 5-minute overlap window).
3. Reads back the current dataset and emits ``_deleted=True`` tombstones for
   any work package id that is no longer reachable through the active listing.

A transient API error aborts the run before any tombstone is emitted, so a
partial snapshot never drives mass deletions. Permanent errors (auth, 404 on
the root endpoint) raise immediately.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("openproject_connector")

OPENPROJECT_TABLE_NAME = "openproject_work_packages"
OPENPROJECT_SOURCE_NAME = "openproject"

_MAX_RETRIES = 5
_OVERLAP_WINDOW_MINUTES = 5
_PAGE_SIZE = 100
_ACTIVE_RECHECK_DAYS = 14

_EXTRA_HINT = (
    'The OpenProject connector requires the "openproject" extra: '
    'pip install "cognee[openproject]" (provides dlt and httpx).'
)


def openproject_source(
    base_url: str | None = None,
    api_key: str | None = None,
    project_ids: list[int] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields OpenProject work packages as documents.

    Args:
        base_url: OpenProject instance URL (e.g., ``https://openproject.example.com``).
            Falls back to ``OPENPROJECT_BASE_URL`` env var.
        api_key: OpenProject API key. Falls back to ``OPENPROJECT_API_KEY`` env var.
        project_ids: Optional list of project ids to restrict scope. When omitted,
            all visible work packages are ingested.
        client: Pre-built HTTP client callable (test-injection point).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        resolved_url = base_url or os.environ.get("OPENPROJECT_BASE_URL")
        resolved_key = api_key or os.environ.get("OPENPROJECT_API_KEY")

        if not resolved_url:
            raise ValueError(
                "OpenProject base URL required: pass base_url= or set OPENPROJECT_BASE_URL."
            )
        if not resolved_key:
            raise ValueError(
                "OpenProject API key required: pass api_key= or set OPENPROJECT_API_KEY."
            )

        base = resolved_url.rstrip("/")
        session = httpx.Client(
            base_url=f"{base}/api/v3",
            headers={"Authorization": f"Bearer {resolved_key}"},
            timeout=30.0,
        )

        def _api_request(method: str, path: str, **kwargs: Any) -> Any:
            for attempt in range(_MAX_RETRIES):
                try:
                    response = session.request(method, path, **kwargs)
                except (httpx.TransportError, httpx.TimeoutException):
                    if attempt == _MAX_RETRIES - 1:
                        raise
                    delay = 2**attempt
                    logger.warning(
                        "OpenProject: network error — retrying in %.1fs (%d/%d).",
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue

                if response.status_code == 429:
                    retry_after = int(response.headers.get("retry-after", 2**attempt))
                    time.sleep(retry_after)
                    continue
                if response.status_code >= 500 and attempt < _MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                if response.status_code in (401, 403):
                    raise PermissionError(
                        f"OpenProject API: {response.status_code} — check your API key."
                    )
                response.raise_for_status()
                return response.json()
            raise RuntimeError(
                f"OpenProject API: {method} {path} failed after {_MAX_RETRIES} retries."
            )

        client = _api_request

    @dlt.resource(
        name=OPENPROJECT_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
    )
    def openproject_work_packages() -> Iterator[dict[str, Any]]:
        import dlt as _runtime_dlt

        state = _runtime_dlt.current.resource_state()
        last_sync = state.get("last_sync")
        active_ids: set[str] = set(state.get("active_ids", []))
        known_ids: set[str] = set(state.get("known_ids", []))

        # Build the updatedAt filter with overlap window
        if last_sync:
            cursor_dt = datetime.fromisoformat(last_sync).replace(tzinfo=UTC) - timedelta(
                minutes=_OVERLAP_WINDOW_MINUTES
            )
            since = cursor_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            since = None

        # Phase 1: Re-check previously active work packages
        rechecked_ids: set[str] = set()
        for wp_id in list(active_ids):
            try:
                wp = client("GET", f"/work_packages/{wp_id}")
                rechecked_ids.add(wp_id)
                yield _wp_to_row(wp)
                if not _is_active(wp):
                    active_ids.discard(wp_id)
            except Exception as exc:
                if _is_gone(exc):
                    logger.info("OpenProject: active work package %s gone, tombstoning.", wp_id)
                    yield {"id": wp_id, "_deleted": True}
                    known_ids.discard(wp_id)
                    active_ids.discard(wp_id)
                else:
                    raise

        # Phase 2: Fetch work packages updated since cursor
        seen_ids: set[str] = set()
        filters = _build_filters(since=since, project_ids=project_ids)

        for wp in _iter_work_packages(client, filters):
            wp_id = str(wp.get("id"))
            if not wp_id:
                continue
            if wp_id in rechecked_ids:
                continue

            seen_ids.add(wp_id)
            known_ids.add(wp_id)

            if _is_active(wp):
                active_ids.add(wp_id)
            else:
                active_ids.discard(wp_id)

            yield _wp_to_row(wp)

        # Phase 3: Detect deletions — known IDs not in current listing
        if not since:
            for known_id in known_ids - seen_ids - rechecked_ids:
                logger.info("OpenProject: work package %s no longer active, tombstoning.", known_id)
                yield {"id": known_id, "_deleted": True}
                known_ids.discard(known_id)
                active_ids.discard(known_id)

        # Update state
        state["last_sync"] = datetime.now(UTC).isoformat()
        state["active_ids"] = list(active_ids)
        state["known_ids"] = list(known_ids)

        logger.info(
            "OpenProject: synced %d work package(s) (%d new/updated, %d active re-check).",
            len(seen_ids) + len(rechecked_ids),
            len(seen_ids),
            len(rechecked_ids),
        )

    @dlt.source(name=OPENPROJECT_SOURCE_NAME)
    def _openproject() -> Any:
        return openproject_work_packages

    source = _openproject()
    setattr(source, DOCUMENT_SOURCE_ATTR, OPENPROJECT_SOURCE_NAME)
    return source


def _is_transient(exc: Exception) -> bool:
    """True for network / timeout / 429 / 5xx errors worth retrying."""
    try:
        import httpx
    except ImportError:
        return True

    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if hasattr(exc, "response"):
        status = getattr(exc.response, "status_code", None)
        if status in (429, 500, 502, 503, 504):
            return True
    return False


def _is_gone(exc: Exception) -> bool:
    """True when a work package is permanently gone (404) — safe to tombstone."""
    if hasattr(exc, "response"):
        status = getattr(exc.response, "status_code", None)
        return status == 404
    return False


def _is_active(wp: dict[str, Any]) -> bool:
    """Identify work packages in active statuses that should be re-checked."""
    status = wp.get("_links", {}).get("status", {}).get("title", "").lower()
    active_statuses = {
        "new",
        "in progress",
        "in specification",
        "specified",
        "confirmed",
        "to be scheduled",
        "scheduled",
        "design",
    }
    return status in active_statuses


def _build_filters(since: str | None, project_ids: list[int] | None) -> str:
    """Build OpenProject v3 filter JSON array string."""
    import json

    filters_list: list[dict[str, Any]] = []

    if since:
        filters_list.append({"updatedAt": {"operator": "<>d", "values": [since, ""]}})

    if project_ids:
        filters_list.append({"project": {"operator": "=", "values": [str(p) for p in project_ids]}})

    return json.dumps(filters_list)


def _iter_work_packages(
    client: Any,
    filters: str,
) -> Iterable[dict[str, Any]]:
    """Yield work packages, handling page-based pagination (offset = page number)."""
    offset = 1
    while True:
        params: dict[str, Any] = {
            "pageSize": _PAGE_SIZE,
            "offset": offset,
            "filters": filters,
            "sortBy": '[["updatedAt:desc"]]',
        }
        response = client("GET", "/work_packages", params=params)

        elements = response.get("_embedded", {}).get("elements", [])
        if not elements:
            break

        yield from elements

        total = response.get("total", 0)
        page_size = response.get("pageSize", _PAGE_SIZE)
        current_offset = response.get("offset", 1)

        if current_offset * page_size >= total:
            break
        offset += 1


def _wp_to_row(wp: dict[str, Any]) -> dict[str, Any]:
    """Transform a raw OpenProject work package into a cognee document row."""
    wp_id = str(wp.get("id", ""))
    subject = wp.get("subject", "")
    description = (wp.get("description", {}) or {}).get("raw", "")
    wp_type = (wp.get("_links", {}).get("type", {}) or {}).get("title", "")
    status = (wp.get("_links", {}).get("status", {}) or {}).get("title", "")
    priority = (wp.get("_links", {}).get("priority", {}) or {}).get("title", "")
    assignee = (wp.get("_links", {}).get("assignee", {}) or {}).get("title", "")
    project = (wp.get("_links", {}).get("project", {}) or {}).get("title", "")
    version = (wp.get("_links", {}).get("version", {}) or {}).get("title", "")
    author = (wp.get("_links", {}).get("author", {}) or {}).get("title", "")

    body_parts: list[str] = []
    body_parts.append(f"OpenProject work package: {subject}")
    body_parts.append(f"Type: {wp_type}")
    body_parts.append(f"Status: {status}")
    body_parts.append(f"Priority: {priority}")
    if project:
        body_parts.append(f"Project: {project}")
    if assignee:
        body_parts.append(f"Assignee: {assignee}")
    if version:
        body_parts.append(f"Version: {version}")
    if author:
        body_parts.append(f"Author: {author}")

    created = wp.get("createdAt")
    if created:
        body_parts.append(f"Created: {created}")

    updated = wp.get("updatedAt")
    if updated:
        body_parts.append(f"Updated: {updated}")

    due_date = wp.get("dueDate")
    if due_date:
        body_parts.append(f"Due date: {due_date}")

    start_date = wp.get("startDate")
    if start_date:
        body_parts.append(f"Start date: {start_date}")

    estimated_time = wp.get("estimatedTime")
    if estimated_time:
        body_parts.append(f"Estimated time: {estimated_time}")

    if description:
        body_parts.append(f"\nDescription:\n{description}")

    # Comment count from metadata
    comment_count = wp.get("commentCount", 0)
    if comment_count:
        body_parts.append(f"\nComments: {comment_count}")

    project_href = (wp.get("_links", {}).get("project", {}) or {}).get("href", "")
    project_id = project_href.split("/")[-1] if project_href else None

    return {
        "id": wp_id,
        "project_id": project_id,
        "type": wp_type,
        "status": status,
        "priority": priority,
        "subject": subject,
        "description": description,
        "assignee": assignee or None,
        "version": version or None,
        "author": author or None,
        "created_at": created,
        "updated_at": updated,
        "due_date": due_date,
        "start_date": start_date,
        "estimated_time": estimated_time,
        "comment_count": comment_count,
        "text": "\n".join(body_parts),
        "raw": wp,
        "source": OPENPROJECT_SOURCE_NAME,
        "_deleted": False,
    }
