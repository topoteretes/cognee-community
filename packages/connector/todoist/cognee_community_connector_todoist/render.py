"""Render Todoist objects to Markdown for Cognee document ingestion."""

from todoist_api_python.models import Task, Project


def render_task(task: Task) -> str:
    """
    Render a Todoist task to Markdown.

    Excludes volatile fields that change without semantic meaning:
    - child_order, day_order, order_key, completed_count
    - Sync-only metadata: added_at, added_by_uid, assigned_by_uid
    """
    parts = []

    # Title with completion status
    status = "✅ " if task.checked else ""
    parts.append(f"# {status}{task.content}")

    # Description
    if task.description:
        parts.append(task.description)

    # Metadata (stable, useful for context)
    meta = []
    if task.priority and task.priority > 1:
        meta.append(f"Priority: {task.priority}")
    if task.due:
        meta.append(f"Due: {task.due}")
    if task.labels:
        meta.append(f"Labels: {', '.join(task.labels)}")
    if task.project_id:
        meta.append(f"Project ID: {task.project_id}")

    if meta:
        parts.append("**Metadata:** " + " | ".join(meta))

    # URL for reference
    parts.append(f"\n[View in Todoist](https://todoist.com/app/task/{task.id})")

    return "\n\n".join(parts)


def render_project(project: Project) -> str:
    """
    Render a Todoist project to Markdown.

    Excludes volatile: child_order, color (changes without semantic meaning).
    """
    parts = []

    parts.append(f"# Project: {project.name}")

    if project.comment_count and project.comment_count > 0:
        parts.append(f"Comments: {project.comment_count}")

    if project.is_favorite:
        parts.append("★ Favorite")

    parts.append(f"\n[View in Todoist](https://todoist.com/app/project/{project.id})")

    return "\n\n".join(parts)