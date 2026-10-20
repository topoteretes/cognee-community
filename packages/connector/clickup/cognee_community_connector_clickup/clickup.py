"""DLT source for ClickUp tasks (full-snapshot sync + forget-on-delete).

Fetches ClickUp tasks across workspaces/spaces/lists and renders their details
(title, description, status, priority, assignees, custom fields, checklists) to
clean Markdown, then yields them as a dlt resource for cognee's ingestion pipeline.

Unlike the relational dlt path (SQL/CSV), ClickUp tasks are ingested as *normal
documents*: the source declares ``cognee_document_source = "clickup"``, so
``resolve_dlt_sources`` tags each item with ``external_metadata["source"] = "clickup"``.
``is_dlt_sourced`` returns False and each task flows through the standard cognify
entity-extraction pipeline — the right treatment for prose — instead of the
deterministic dlt-row schema-context path.

The source uses a full snapshot model: ``write_disposition="replace"`` rewrites
staging with exactly the tasks currently visible to the integration on each run.
Deletions propagate cleanly — a deleted or unassigned task drops out of the listing,
so it is absent from the snapshot and cognee's existing ``orphan_cleanup`` removes
it from the graph and vector stores. Unchanged tasks maintain a stable content hash
``data_id``, preventing unnecessary re-ingestion or re-cognification.
"""

import os
import time
from collections.abc import Iterator
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("clickup_connector")

CLICKUP_TABLE_NAME = "clickup_tasks"
CLICKUP_SOURCE_NAME = "clickup"
CLICKUP_BASE_URL = "https://api.clickup.com/api/v2"

# Retry budget for rate-limited / transient ClickUp API responses
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0


def _request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute an HTTP request with exponential backoff on 429 rate limits and 5xx errors."""
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.request(method, url, headers=headers, params=params, timeout=30.0)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", _BASE_BACKOFF * (2**attempt)))
                logger.warning("ClickUp rate limit hit, backing off for %.1f seconds", retry_after)
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if attempt == _MAX_RETRIES - 1:
                raise RuntimeError(f"ClickUp request failed after {_MAX_RETRIES} attempts: {exc}") from exc
            time.sleep(_BASE_BACKOFF * (2**attempt))
    raise RuntimeError(f"ClickUp request exceeded retry budget for {url}")


def _format_task_markdown(task: dict[str, Any]) -> str:
    """Render a ClickUp task deterministically to Markdown for entity extraction."""
    task_id = task.get("id", "")
    name = task.get("name", "").strip()
    status = (task.get("status") or {}).get("status", "open")
    priority = (task.get("priority") or {}).get("priority", "none")
    url = task.get("url", "")

    # Extract assignees deterministically (sorted by username/email)
    assignees_raw = task.get("assignees") or []
    assignee_names = sorted(
        [a.get("username") or a.get("email") or str(a.get("id")) for a in assignees_raw]
    )
    assignees_str = ", ".join(assignee_names) if assignee_names else "Unassigned"

    # Extract tags deterministically (sorted)
    tags_raw = task.get("tags") or []
    tag_names = sorted([t.get("name") for t in tags_raw if t.get("name")])
    tags_str = ", ".join(tag_names) if tag_names else "None"

    # Contextual container info
    list_info = task.get("list") or {}
    list_name = list_info.get("name", "General")
    space_info = task.get("space") or {}
    space_id = space_info.get("id", "")

    # Description (prose body)
    description = (task.get("text_content") or task.get("description") or "").strip()

    # Checklists
    checklists = task.get("checklists") or []
    checklist_lines = []
    for cl in checklists:
        cl_name = cl.get("name", "Checklist")
        checklist_lines.append(f"### Checklist: {cl_name}")
        for item in cl.get("items") or []:
            resolved = item.get("resolved", False)
            marker = "[x]" if resolved else "[ ]"
            checklist_lines.append(f"- {marker} {item.get('name', '')}")

    checklists_rendered = "\n".join(checklist_lines)

    doc_parts = [
        f"# ClickUp Task: {name}",
        f"- **Task ID**: {task_id}",
        f"- **Status**: {status}",
        f"- **Priority**: {priority}",
        f"- **Assignees**: {assignees_str}",
        f"- **Tags**: {tags_str}",
        f"- **List**: {list_name}",
    ]
    if space_id:
        doc_parts.append(f"- **Space ID**: {space_id}")
    if url:
        doc_parts.append(f"- **URL**: {url}")

    doc_parts.append("\n## Description")
    doc_parts.append(description if description else "_No description provided._")

    if checklists_rendered:
        doc_parts.append("\n## Checklists")
        doc_parts.append(checklists_rendered)

    return "\n".join(doc_parts)


def clickup_source(
    api_token: str | None = None,
    team_id: str | None = None,
    space_ids: list[str] | None = None,
    list_ids: list[str] | None = None,
    include_closed: bool = True,
    client: Any = None,
):
    """Create a dlt source that yields ClickUp tasks as markdown documents.

    Args:
        api_token: ClickUp personal API token. Falls back to ``CLICKUP_API_TOKEN``.
        team_id: ClickUp workspace/team ID. Auto-detected from account if omitted.
        space_ids: Restrict ingestion to tasks within these specific space IDs.
        list_ids: Restrict ingestion to tasks within these specific list IDs.
        include_closed: Whether to include closed/completed tasks (default: True).
        client: Pre-built ``httpx.Client`` (for test injection).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The ClickUp connector requires dlt: pip install "dlt[sqlalchemy] httpx"'
        ) from exc

    resolved_token = api_token or os.environ.get("CLICKUP_API_TOKEN")
    if not resolved_token:
        raise ValueError(
            "ClickUp API token required: pass api_token= or set CLICKUP_API_TOKEN."
        )

    http_client = client or httpx.Client(timeout=30.0)
    headers = {"Authorization": resolved_token}

    @dlt.resource(name=CLICKUP_TABLE_NAME, primary_key="id", write_disposition="replace")
    def clickup_tasks() -> Iterator[dict[str, Any]]:
        # Resolve Workspace (Team) ID if not provided
        active_team_id = team_id or os.environ.get("CLICKUP_TEAM_ID")
        if not active_team_id:
            teams_res = _request_with_retry(http_client, "GET", f"{CLICKUP_BASE_URL}/team", headers)
            teams = teams_res.get("teams") or []
            if not teams:
                raise RuntimeError("No ClickUp workspaces found for the provided API token.")
            active_team_id = str(teams[0]["id"])
            logger.info("Auto-detected ClickUp workspace team_id: %s (%s)", active_team_id, teams[0].get("name"))

        task_ids_seen = set()

        # If specific list_ids are provided, fetch per list
        if list_ids:
            for lid in list_ids:
                page = 0
                while True:
                    params = {
                        "include_closed": str(include_closed).lower(),
                        "subtasks": "true",
                        "page": page,
                    }
                    data = _request_with_retry(
                        http_client, "GET", f"{CLICKUP_BASE_URL}/list/{lid}/task", headers, params
                    )
                    tasks = data.get("tasks") or []
                    if not tasks:
                        break

                    for task in tasks:
                        tid = str(task["id"])
                        if tid in task_ids_seen:
                            continue
                        task_ids_seen.add(tid)
                        body = _format_task_markdown(task)
                        yield {
                            "id": tid,
                            "name": task.get("name", ""),
                            "status": (task.get("status") or {}).get("status", ""),
                            "url": task.get("url", ""),
                            "body": body,
                        }
                    page += 1
            return

        # Fetch workspace-wide tasks (filtered by space_ids if specified)
        page = 0
        while True:
            params = {
                "include_closed": str(include_closed).lower(),
                "subtasks": "true",
                "page": page,
            }
            if space_ids:
                params["space_ids[]"] = space_ids

            data = _request_with_retry(
                http_client, "GET", f"{CLICKUP_BASE_URL}/team/{active_team_id}/task", headers, params
            )
            tasks = data.get("tasks") or []
            if not tasks:
                break

            for task in tasks:
                tid = str(task["id"])
                if tid in task_ids_seen:
                    continue
                task_ids_seen.add(tid)
                body = _format_task_markdown(task)
                yield {
                    "id": tid,
                    "name": task.get("name", ""),
                    "status": (task.get("status") or {}).get("status", ""),
                    "url": task.get("url", ""),
                    "body": body,
                }
            page += 1

    # Declare document source attribute so cognee runs full entity extraction (document-mode)
    setattr(clickup_tasks, DOCUMENT_SOURCE_ATTR, CLICKUP_SOURCE_NAME)

    @dlt.source(name=CLICKUP_SOURCE_NAME)
    def source():
        return clickup_tasks

    src_instance = source()
    setattr(src_instance, DOCUMENT_SOURCE_ATTR, CLICKUP_SOURCE_NAME)
    return src_instance
