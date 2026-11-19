"""Todoist connector for cognee: a ``dlt`` source that turns tasks and projects into memory."""

import logging
import dlt
from dlt.sources.helpers import requests

logger = logging.getLogger(__name__)

TODOIST_SOURCE_NAME = "todoist_tasks"
DOCUMENT_SOURCE_ATTR = "is_document_source"

def todoist_tasks(api_token: str | None = dlt.secrets.value):
    """Returns the Todoist dlt source."""
    @dlt.resource(name="todoist_items")
    def _todoist_tasks_resource():
        if not api_token:
            raise ValueError("An API token is required for the Todoist connector.")

        state = dlt.current.source_state()
        sync_token = state.setdefault("sync_token", "*")

        url = "https://api.todoist.com/sync/v9/sync"
        headers = {"Authorization": f"Bearer {api_token}"}

        data = {
            "sync_token": sync_token,
            "resource_types": '["items", "projects", "notes"]'
        }

        response = requests.post(url, headers=headers, data=data)
        response.raise_for_status()
        result = response.json()

        state["sync_token"] = result.get("sync_token", "*")

        for project in result.get("projects", []):
            if project.get("is_deleted"):
                continue
            yield _project_to_row(project)

        for item in result.get("items", []):
            if item.get("is_deleted"):
                continue
            yield _item_to_row(item)

        for note in result.get("notes", []):
            if note.get("is_deleted"):
                continue
            yield _note_to_row(note)

    @dlt.source(name=TODOIST_SOURCE_NAME)
    def _todoist():
        return _todoist_tasks_resource()

    source = _todoist()
    setattr(source, DOCUMENT_SOURCE_ATTR, TODOIST_SOURCE_NAME)
    return source

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
