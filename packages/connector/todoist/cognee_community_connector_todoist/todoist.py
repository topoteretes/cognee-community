"""Todoist connector for cognee: a ``dlt`` source that turns tasks and projects into memory.

One source is one Todoist workspace/account. It yields tasks, comments, and projects,
and is meant to be handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_todoist import todoist_tasks

    await cognee.remember(
        todoist_tasks(api_token="<your-api-token>"),
        dataset_name="todoist",
        primary_key="id",
        write_disposition="merge",   # REQUIRED
        max_rows_per_table=0,
    )
"""

import logging
import dlt
from dlt.sources.helpers import requests

logger = logging.getLogger(__name__)

TODOIST_SOURCE_NAME = "todoist_tasks"
DOCUMENT_SOURCE_ATTR = "is_document_source"

@dlt.source(name=TODOIST_SOURCE_NAME)
def todoist_tasks(api_token: str | None = dlt.secrets.value):
    """Yield Todoist tasks, projects, and comments as flat document rows."""
    if not api_token:
        raise ValueError("An API token is required for the Todoist connector.")

    # The sync API requires a cursor. We store it in dlt state.
    state = dlt.current.source_state()
    sync_token = state.setdefault("sync_token", "*")

    url = "https://api.todoist.com/sync/v9/sync"
    headers = {"Authorization": f"Bearer {api_token}"}

    # We request items, projects, and notes(comments)
    data = {
        "sync_token": sync_token,
        "resource_types": '["items", "projects", "notes"]'
    }

    response = requests.post(url, headers=headers, data=data)
    response.raise_for_status()
    result = response.json()

    # Update the sync token for the next run
    state["sync_token"] = result.get("sync_token", "*")

    # Process and yield projects
    for project in result.get("projects", []):
        if project.get("is_deleted"):
            continue
        yield _project_to_row(project)

    # Process and yield tasks (items)
    for item in result.get("items", []):
        if item.get("is_deleted"):
            continue
        yield _item_to_row(item)

    # Process and yield comments (notes)
    for note in result.get("notes", []):
        if note.get("is_deleted"):
            continue
        yield _note_to_row(note)

def _project_to_row(project: dict) -> dict:
    return {
        "id": f"project_{project.get('id')}",
        "title": project.get("name", "Untitled Project"),
        "content": f"Todoist Project: {project.get('name')}\nColor: {project.get('color')}\nURL: {project.get('url', '')}",
        "url": project.get("url", ""),
    }

def _item_to_row(item: dict) -> dict:
    title = item.get("content", "Untitled Task")
    description = item.get("description", "")
    content = f"Todoist Task: {title}\nDescription: {description}"
    return {
        "id": f"task_{item.get('id')}",
        "title": title,
        "content": content,
        "url": f"https://todoist.com/app/task/{item.get('id')}"
    }

def _note_to_row(note: dict) -> dict:
    content = note.get("content", "")
    return {
        "id": f"comment_{note.get('id')}",
        "title": f"Comment on task {note.get('item_id')}",
        "content": content,
        "url": f"https://todoist.com/app/task/{note.get('item_id')}"
    }

source = todoist_tasks()
setattr(source, DOCUMENT_SOURCE_ATTR, TODOIST_SOURCE_NAME)

