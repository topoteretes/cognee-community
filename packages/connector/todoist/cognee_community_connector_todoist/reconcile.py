"""Reconciliation utilities for forget-on-delete pattern."""

from typing import Any, Dict, Iterator


def mark_deleted(items: Iterator[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """
    Add _deleted flag to items marked as deleted upstream.

    Cognee's orphan cleanup will remove items with _deleted=True from
    graph and vector stores on the next pipeline run.

    Args:
        items: Iterator of resource dicts with 'is_deleted' field

    Yields:
        Items with '_deleted' boolean column added
    """
    for item in items:
        is_deleted = item.pop("is_deleted", False)
        item["_deleted"] = bool(is_deleted)
        yield item


def filter_volatile_fields(item: Dict[str, Any], volatile_fields: list[str]) -> Dict[str, Any]:
    """
    Remove volatile fields that shouldn't affect content hash.

    Args:
        item: Resource dict
        volatile_fields: List of field names to exclude

    Returns:
        Dict with volatile fields removed
    """
    return {k: v for k, v in item.items() if k not in volatile_fields}


# Volatile fields per Todoist resource type
TASK_VOLATILE_FIELDS = [
    "child_order",
    "day_order",
    "order_key",
    "completed_count",
    "added_at",
    "added_by_uid",
    "assigned_by_uid",
    "user_id",
    "sync_id",
]

PROJECT_VOLATILE_FIELDS = [
    "child_order",
    "color",
    "view_style",
    "parent_id",
    "sync_id",
]

COMMENT_VOLATILE_FIELDS = [
    "posted_at",
    "posted_by_uid",
    "sync_id",
]