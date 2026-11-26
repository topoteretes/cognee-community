"""Todoist DLT source — incremental sync via /sync endpoint with forget-on-delete."""

import dlt
from dlt.common import logger
from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth
from dlt.sources.helpers.rest_client.paginators import SinglePagePaginator

from .client import TodoistSyncClient, SyncResult
from .render import render_task, render_project
from .reconcile import mark_deleted

DOCUMENT_SOURCE_ATTR = "todoist"


@dlt.source(name="todoist")
def todoist_source(
    api_token: str = dlt.secrets.value,
    sync_token: str | None = dlt.state.value,
    resource_types: list[str] | None = None,
) -> dlt.Resources:
    """
    Todoist incremental sync source using the /sync endpoint.

    Args:
        api_token: Todoist API token (Bearer) — from TODOIST_API_KEY env or secrets
        sync_token: Persisted cursor for incremental sync (managed by dlt state)
        resource_types: Which resources to sync (default: items, projects, comments)

    Yields:
        DLT resources for tasks, projects, comments with merge write disposition.
    """
    if resource_types is None:
        resource_types = ["items", "projects", "comments"]

    client = TodoistSyncClient(api_token)

    # First sync: sync_token="" returns all active data + new sync_token
    # Subsequent: pass stored sync_token to get only changes
    logger.info(f"Syncing Todoist with sync_token={'<stored>' if sync_token else '<initial>'}")
    result: SyncResult = client.sync(sync_token or "", resource_types)

    # Persist new sync_token for next run
    dlt.state.set("sync_token", result.sync_token)

    # Yield resources with merge on prefixed IDs
    if "items" in resource_types:
        yield _tasks_resource(result)
    if "projects" in resource_types:
        yield _projects_resource(result)
    if "comments" in resource_types:
        yield _comments_resource(result)


def _tasks_resource(result: SyncResult) -> dlt.Resource:
    """Tasks (items) resource with merge on prefixed ID."""

    @dlt.resource(
        name="tasks",
        write_disposition="merge",
        primary_key="id",
        columns={
            "id": {"data_type": "text"},
            "content": {"data_type": "text"},
            "project_id": {"data_type": "text"},
            "source": {"data_type": "text"},
            "_deleted": {"data_type": "bool"},
        },
    )
    def _gen():
        for item in result.items:
            if item.is_deleted:
                # Hard delete: upstream deletion removes from Cognee
                yield {"id": f"task:{item.id}", "_deleted": True}
            else:
                yield {
                    "id": f"task:{item.id}",
                    "content": render_task(item),
                    "project_id": f"project:{item.project_id}" if item.project_id else None,
                    "source": DOCUMENT_SOURCE_ATTR,
                    "_deleted": False,
                }

    return _gen()


def _projects_resource(result: SyncResult) -> dlt.Resource:
    """Projects resource with merge on prefixed ID."""

    @dlt.resource(
        name="projects",
        write_disposition="merge",
        primary_key="id",
        columns={
            "id": {"data_type": "text"},
            "content": {"data_type": "text"},
            "source": {"data_type": "text"},
            "_deleted": {"data_type": "bool"},
        },
    )
    def _gen():
        for project in result.projects:
            if project.is_deleted:
                yield {"id": f"project:{project.id}", "_deleted": True}
            else:
                yield {
                    "id": f"project:{project.id}",
                    "content": render_project(project),
                    "source": DOCUMENT_SOURCE_ATTR,
                    "_deleted": False,
                }

    return _gen()


def _comments_resource(result: SyncResult) -> dlt.Resource:
    """Comments resource — attached to tasks, merge on prefixed ID."""

    @dlt.resource(
        name="comments",
        write_disposition="merge",
        primary_key="id",
        columns={
            "id": {"data_type": "text"},
            "content": {"data_type": "text"},
            "task_id": {"data_type": "text"},
            "source": {"data_type": "text"},
            "_deleted": {"data_type": "bool"},
        },
    )
    def _gen():
        for comment in result.comments:
            if comment.is_deleted:
                yield {"id": f"comment:{comment.id}", "_deleted": True}
            else:
                yield {
                    "id": f"comment:{comment.id}",
                    "content": comment.content,
                    "task_id": f"task:{comment.task_id}" if comment.task_id else None,
                    "source": DOCUMENT_SOURCE_ATTR,
                    "_deleted": False,
                }

    return _gen()