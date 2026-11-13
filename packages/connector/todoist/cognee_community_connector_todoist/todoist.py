"""Todoist Sync API source for cognee.

The source keeps a Todoist ``sync_token`` in DLT resource state and emits only
changed projects, tasks, and comments. Todoist deletion flags become DLT hard
delete markers, which cognee's existing orphan cleanup propagates to memory.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("todoist_connector")

_SYNC_URL = "https://api.todoist.com/api/v1/sync"
_RESOURCE_TYPES = {
    "projects": "projects",
    "tasks": "items",
    "comments": "notes",
}


def _post_sync(
    token: str,
    sync_token: str,
    resource_types: list[str],
) -> dict[str, Any]:
    """Read selected resources from Todoist's Sync API."""
    body = urlencode(
        {
            "sync_token": sync_token,
            "resource_types": json.dumps(resource_types),
        }
    ).encode("utf-8")
    request = Request(
        _SYNC_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        try:
            error = json.load(exc)
        except (ValueError, OSError):
            error = {}
        if not isinstance(error, dict):
            error = {}
        message = error.get("error") or error.get("error_tag") or "request failed"
        raise RuntimeError(f"Todoist Sync API returned HTTP {exc.code}: {message}") from exc
    except ValueError as exc:
        raise RuntimeError("Todoist Sync API returned invalid JSON.") from exc

    if not isinstance(result, dict):
        raise RuntimeError("Todoist Sync API response must be a JSON object.")
    if not isinstance(result.get("sync_token"), str) or not result["sync_token"]:
        raise RuntimeError("Todoist Sync API response is missing a valid sync_token.")
    return result


def _record_to_row(resource_type: str, record: dict[str, Any]) -> dict[str, Any]:
    """Map one Todoist resource to a stable, searchable DLT row."""
    if not isinstance(record, dict):
        raise ValueError(f"Todoist {resource_type} record must be an object.")
    record_id = record.get("id")
    if not isinstance(record_id, (str, int)):
        raise ValueError(f"Todoist {resource_type} record is missing a valid id.")

    id_prefix = "comment" if resource_type == "comments" else resource_type[:-1]
    row_id = f"{id_prefix}:{record_id}"
    if record.get("is_deleted"):
        return {"id": row_id, "_deleted": True}

    if resource_type == "projects":
        return {
            "id": row_id,
            "type": "project",
            "todoist_id": str(record_id),
            "name": record.get("name", ""),
            "description": record.get("description", ""),
            "parent_id": record.get("parent_id"),
            "is_archived": record.get("is_archived", False),
            "_deleted": False,
        }
    if resource_type == "tasks":
        return {
            "id": row_id,
            "type": "task",
            "todoist_id": str(record_id),
            "content": record.get("content", ""),
            "description": record.get("description", ""),
            "project_id": record.get("project_id"),
            "section_id": record.get("section_id"),
            "parent_id": record.get("parent_id"),
            "checked": record.get("checked", False),
            "due": _due_text(record.get("due")),
            "labels": ", ".join(record.get("labels", []) or []),
            "priority": record.get("priority"),
            "_deleted": False,
        }
    return {
        "id": row_id,
        "type": "comment",
        "todoist_id": str(record_id),
        "content": record.get("content", ""),
        "task_id": record.get("item_id"),
        "project_id": record.get("project_id"),
        "posted_at": record.get("posted_at"),
        "_deleted": False,
    }


def _due_text(due: Any) -> str:
    """Flatten Todoist's due-date object into a searchable string."""
    if not isinstance(due, dict):
        return ""
    return due.get("string") or due.get("date") or ""


def _response_rows(response: dict[str, Any], resource_types: list[str]) -> list[dict[str, Any]]:
    """Validate and flatten the resource arrays returned by a sync response."""
    rows = []
    for resource_type in resource_types:
        response_fields = {
            "projects": ("projects",),
            "tasks": ("items", "tasks"),
        }.get(resource_type)
        if resource_type == "comments":
            # Task comments use the legacy Sync field name "notes".
            response_fields = ("notes", "project_notes")
        if response_fields is None:
            raise ValueError(f"Unsupported Todoist resource type: {resource_type}")
        for field in response_fields:
            records = response.get(field, [])
            if not isinstance(records, list):
                raise ValueError(f"Todoist Sync API field {field!r} must be an array.")
            rows.extend(_record_to_row(resource_type, record) for record in records)
    return rows


def todoist_source(
    token: str | None = None,
    *,
    include_projects: bool = True,
    include_tasks: bool = True,
    include_comments: bool = True,
):
    """Return a DLT resource that syncs selected Todoist records into cognee.

    Args:
        token: Todoist API token. Falls back to ``TODOIST_API_TOKEN``.
        include_projects: Include project names, descriptions, and hierarchy.
        include_tasks: Include task content, descriptions, and project context.
        include_comments: Include task and project comments.

    The returned resource uses DLT ``merge`` with an ``id`` primary key.
    Pass ``write_disposition="merge"`` and ``max_rows_per_table=0`` to
    ``cognee.remember`` so incremental records and hard deletions are handled
    by cognee's standard DLT ingestion path.
    """
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - optional installation
        raise ImportError(
            "The Todoist connector requires dlt. Install it with "
            '`pip install "cognee-community-connector-todoist"`.'
        ) from exc

    resolved_token = token or os.environ.get("TODOIST_API_TOKEN")
    if not resolved_token:
        raise ValueError("Todoist API token required: pass token= or set TODOIST_API_TOKEN.")

    selected_types = [
        resource_type
        for resource_type, enabled in (
            ("projects", include_projects),
            ("tasks", include_tasks),
            ("comments", include_comments),
        )
        if enabled
    ]
    if not selected_types:
        raise ValueError("Select at least one of projects, tasks, or comments to sync.")
    resource_types = [_RESOURCE_TYPES[resource_type] for resource_type in selected_types]

    @dlt.resource(
        name="todoist_records",
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def todoist_records() -> Iterator[dict[str, Any]]:
        state = dlt.current.resource_state()
        sync_token = state.get("sync_token", "*")
        previous_types = state.get("resource_types")
        if previous_types is not None and previous_types != resource_types:
            raise ValueError(
                "Todoist resource selection changed for this saved sync state. "
                "Use a fresh DLT pipeline state to change selected resources."
            )
        if sync_token != "*" and previous_types is None:
            raise ValueError(
                "Saved Todoist sync state has no resource selection. "
                "Use a fresh DLT pipeline state to avoid skipping existing resources."
            )

        response = _post_sync(resolved_token, sync_token, resource_types)
        rows = _response_rows(response, selected_types)
        yield from rows
        # Update only after the whole response has been emitted. If the request
        # or mapping fails, the previous cursor remains available for retry.
        state["sync_token"] = response["sync_token"]
        state["resource_types"] = resource_types
        logger.info("Todoist: synced %d changed record(s).", len(rows))

    return todoist_records
