"""DLT source for Linear issues, engineering tasks, and team roadmaps.

Full snapshot + forget-on-delete.
Fetches issues, statuses, assignees, team context, priorities, and descriptions from the Linear
GraphQL API, rendering them into clean Markdown documents. Yields them as a dlt resource for
cognee's ingestion pipeline.

Unlike relational dlt paths, Linear issues are ingested as *normal documents*:
the source declares ``cognee_document_source = "linear"``, so ``resolve_dlt_sources``
tags each item with ``external_metadata["source"] = "linear"``. This ensures each issue flows
through cognee's standard cognify entity-extraction pipeline — extracting entities,
relationships, assignees, and concepts for agent reasoning — instead of the flat dlt-row path.

The source uses a full snapshot synchronization model: ``write_disposition="replace"``
rewrites staging on each run with the issues currently active in Linear. Deletions
propagate cleanly — a removed or canceled issue drops out of the snapshot, so
cognee's existing ``orphan_cleanup`` removes it from the graph and vector stores.
Unchanged items preserve stable content-hash ``data_id`` keys, preventing redundant re-ingestion.
"""

import os
import time
from collections.abc import Iterator
from typing import Any

import httpx

try:
    from cognee.shared.logging_utils import get_logger
except ImportError:
    import logging

    def get_logger(name: str):
        return logging.getLogger(name)


try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

logger = get_logger("linear_connector")

LINEAR_TABLE_NAME = "linear_documents"
LINEAR_SOURCE_NAME = "linear"
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# Retry budget for rate-limited (429) / transient Linear API responses
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0

ISSUES_QUERY = """
query GetIssues($first: Int!, $after: String, $filter: IssueFilter) {
  issues(first: $first, after: $after, filter: $filter) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      identifier
      title
      description
      priority
      priorityLabel
      url
      createdAt
      updatedAt
      state {
        name
        type
      }
      assignee {
        name
        email
      }
      team {
        name
        key
      }
      labels {
        nodes {
          name
        }
      }
    }
  }
}
"""


def _request_with_retry(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    json_payload: dict[str, Any],
) -> dict[str, Any]:
    """Execute a GraphQL request with exponential backoff on 429 rate limits and 5xx errors."""
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.post(url, headers=headers, json=json_payload, timeout=30.0)
            if response.status_code == 429:
                header_backoff = response.headers.get("Retry-After")
                retry_after = (
                    float(header_backoff) if header_backoff else _BASE_BACKOFF * (2**attempt)
                )
                logger.warning("Linear rate limit reached, waiting %.1f seconds", retry_after)
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            res_data = response.json()
            if "errors" in res_data:
                err_msg = res_data["errors"][0].get("message", "Unknown GraphQL error")
                raise RuntimeError(f"Linear GraphQL error: {err_msg}")
            return res_data
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if attempt == _MAX_RETRIES - 1:
                msg = f"Linear request failed after {_MAX_RETRIES} attempts: {exc}"
                raise RuntimeError(msg) from exc
            time.sleep(_BASE_BACKOFF * (2**attempt))
    raise RuntimeError(f"Linear request exceeded retry budget for {url}")


def _format_issue_markdown(issue: dict[str, Any]) -> str:
    """Render a Linear issue deterministically into Markdown for entity extraction."""
    identifier = issue.get("identifier", "").strip()
    title = issue.get("title", "").strip()
    state_data = issue.get("state") or {}
    state_name = state_data.get("name", "Unknown")
    state_type = state_data.get("type", "")

    status_str = f"{state_name} ({state_type})" if state_type else state_name
    priority_label = issue.get("priorityLabel", "None")
    priority_num = issue.get("priority", 0)

    assignee = issue.get("assignee")
    if assignee:
        assignee_str = f"{assignee.get('name', '')} ({assignee.get('email', '')})".strip()
    else:
        assignee_str = "Unassigned"

    team = issue.get("team") or {}
    team_str = f"{team.get('name', '')} ({team.get('key', '')})".strip()

    labels_nodes = (issue.get("labels") or {}).get("nodes") or []
    labels_list = sorted([lbl.get("name", "") for lbl in labels_nodes if lbl.get("name")])
    labels_str = ", ".join(labels_list) if labels_list else "None"

    url = issue.get("url", "")
    description = (issue.get("description") or "").strip()

    header = f"[{identifier}] {title}" if identifier else title

    doc_parts = [
        f"# Linear Issue: {header}",
        "",
        f"- **Identifier**: {identifier}",
        f"- **Title**: {title}",
        f"- **Status**: {status_str}",
        f"- **Priority**: {priority_label} (Level {priority_num})",
        f"- **Team**: {team_str or 'None'}",
        f"- **Assignee**: {assignee_str}",
        f"- **Labels**: {labels_str}",
        f"- **URL**: {url}",
        "",
        "## Description",
        description if description else "_No description provided._",
    ]
    return "\n".join(doc_parts)


def linear_source(
    api_key: str | None = None,
    team_keys: list[str] | None = None,
    include_completed: bool = True,
    client: Any = None,
):
    """Create a dlt source yielding Linear engineering issues as structured markdown documents.

    Args:
        api_key: Linear personal API key. Falls back to ``LINEAR_API_KEY``.
        team_keys: Restrict sync to specific team keys (e.g., ['ENG', 'OPS']).
        include_completed: Include completed/archived issues in snapshot (default: True).
        client: Pre-built ``httpx.Client`` (primarily for test mock injection).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Linear connector requires dlt: pip install "dlt[sqlalchemy] httpx"'
        ) from exc

    resolved_key = api_key or os.environ.get("LINEAR_API_KEY")
    if not resolved_key:
        raise ValueError("Linear API key required: pass api_key= or set LINEAR_API_KEY.")

    http_client = client or httpx.Client(timeout=30.0)
    headers = {
        "Authorization": resolved_key,
        "Content-Type": "application/json",
    }

    @dlt.resource(name=LINEAR_TABLE_NAME, primary_key="id", write_disposition="replace")
    def linear_issues() -> Iterator[dict[str, Any]]:
        has_next_page = True
        after_cursor = None

        issue_filter: dict[str, Any] = {}
        if team_keys:
            issue_filter["team"] = {"key": {"in": team_keys}}
        if not include_completed:
            issue_filter["state"] = {"type": {"nin": ["completed", "canceled"]}}

        while has_next_page:
            variables = {
                "first": 50,
                "after": after_cursor,
                "filter": issue_filter if issue_filter else None,
            }

            data = _request_with_retry(
                http_client,
                LINEAR_GRAPHQL_URL,
                headers,
                {"query": ISSUES_QUERY, "variables": variables},
            )

            issues_data = (data.get("data") or {}).get("issues") or {}
            nodes = issues_data.get("nodes") or []
            page_info = issues_data.get("pageInfo") or {}

            for issue in nodes:
                issue_id = str(issue["id"])
                doc_text = _format_issue_markdown(issue)
                yield {
                    "id": issue_id,
                    "identifier": issue.get("identifier", ""),
                    "title": issue.get("title", ""),
                    "priority": issue.get("priority", 0),
                    "url": issue.get("url", ""),
                    "body": doc_text,
                }

            has_next_page = page_info.get("hasNextPage", False)
            after_cursor = page_info.get("endCursor")

    @dlt.source(name=LINEAR_SOURCE_NAME)
    def source():
        return [linear_issues]

    src = source()
    setattr(src, DOCUMENT_SOURCE_ATTR, "linear")
    return src
