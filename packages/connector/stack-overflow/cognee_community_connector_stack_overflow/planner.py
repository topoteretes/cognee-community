"""Quota planner — estimate API request cost BEFORE any ingestion.

Usage::

    from cognee_community_connector_stack_overflow import plan_stack_overflow_sync
    report = plan_stack_overflow_sync(tags=["polars", "duckdb"])
    print(report)
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cognee_community_connector_stack_overflow.client import StackOverflowClient


def plan_stack_overflow_sync(
    tags: list[str],
    max_questions_per_tag: int = 500,
    page_size: int = 100,
    client: "StackOverflowClient | None" = None,
    api_key: str | None = None,
) -> str:
    """Return a human-readable cost estimate without ingesting anything.

    Args:
        tags: Tags to sync (one stream per tag).
        max_questions_per_tag: Cap applied during actual sync.
        page_size: Questions per API page (max 100).
        client: Injected client for testing.
        api_key: Passed to a new client when *client* is None.

    Returns:
        A multi-line string report.
    """
    from cognee_community_connector_stack_overflow.client import StackOverflowClient

    if client is None:
        client = StackOverflowClient(api_key=api_key)

    lines = ["Stack Overflow Sync — Cost Estimate", "=" * 40]
    total_first = 0
    total_incr = 0

    for tag in tags:
        total_questions = client.estimate_page_count(tag)
        capped = min(total_questions, max_questions_per_tag)
        pages = math.ceil(capped / page_size)
        # Each page = 1 request (filter includes answers inline).
        first_sync_requests = pages
        # Incremental: only changed questions since last run (rough estimate: 5%).
        incr_requests = max(1, math.ceil(capped * 0.05 / page_size))
        total_first += first_sync_requests
        total_incr += incr_requests
        lines.append(
            f"  tag={tag!r}: ~{total_questions} questions "
            f"(capped at {capped}) → "
            f"{first_sync_requests} req first sync, ~{incr_requests} req/run after"
        )

    lines.append("-" * 40)
    lines.append(f"  TOTAL first sync : ~{total_first} requests")
    lines.append(f"  TOTAL incremental: ~{total_incr} requests/run")
    lines.append(
        f"  Daily quota      : 300 req (no key) / 10 000 req (with key)"
    )
    lines.append(
        "  NOTE: No data was ingested. Call stack_overflow_source(...) to ingest."
    )
    return "\n".join(lines)