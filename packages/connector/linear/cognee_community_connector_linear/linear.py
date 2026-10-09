"""DLT source for Linear issues.

Fetches Linear issues via GraphQL and yields them as a dlt resource for cognee's ingestion pipeline.
"""

import os
import requests
from typing import Any, Iterator

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("linear_connector")

LINEAR_SOURCE_NAME = "linear"
LINEAR_TABLE_NAME = "linear_issues"
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

def linear_source(
    token: str | None = None,
    team_ids: list[str] | None = None,
):
    """Create a dlt source that yields Linear issues as markdown documents.

    Args:
        token: Linear API token. Falls back to ``LINEAR_API_KEY``.
        team_ids: Restrict ingestion to these team ids. When omitted, all issues are fetched.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as e:
        raise ImportError(
            "The Linear connector requires dlt: pip install dlt"
        ) from e

    token = token or os.environ.get("LINEAR_API_KEY")
    if not token:
        raise ValueError("Linear API token must be provided or set in LINEAR_API_KEY")

    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
    }

    @dlt.resource(name=LINEAR_TABLE_NAME, write_disposition="replace")
    def linear_issues() -> Iterator[dict]:
        has_next_page = True
        end_cursor = None
        count = 0

        # Optional team filter
        team_filter = ""
        if team_ids:
            # We can fetch issues for specific teams
            team_filter = f'team: {{ id: {{ in: {str(team_ids).replace("'", '"')} }} }}'

        while has_next_page:
            query = f"""
            query Issues($after: String) {{
                issues(first: 50, after: $after, filter: {{ {team_filter} }}) {{
                    pageInfo {{
                        hasNextPage
                        endCursor
                    }}
                    nodes {{
                        id
                        title
                        description
                        url
                        identifier
                    }}
                }}
            }}
            """
            
            response = requests.post(
                LINEAR_GRAPHQL_URL,
                headers=headers,
                json={"query": query, "variables": {"after": end_cursor}},
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            if "errors" in data:
                raise Exception(f"Linear GraphQL Error: {data['errors']}")
            
            issues_data = data.get("data", {}).get("issues", {})
            nodes = issues_data.get("nodes", [])
            
            for node in nodes:
                count += 1
                yield _issue_to_row(node)
                
            page_info = issues_data.get("pageInfo", {})
            has_next_page = page_info.get("hasNextPage", False)
            end_cursor = page_info.get("endCursor")

        logger.info("Linear: synced %d issue(s).", count)

    @dlt.source(name=LINEAR_SOURCE_NAME)
    def _linear():
        return linear_issues

    source = _linear()
    setattr(source, DOCUMENT_SOURCE_ATTR, LINEAR_SOURCE_NAME)
    return source

def _issue_to_row(issue: dict) -> dict:
    """Format a Linear issue into a document row."""
    identifier = issue.get("identifier", "")
    title = issue.get("title", "")
    desc = issue.get("description") or ""
    
    content = f"# {identifier}: {title}\n\n{desc}"
    
    return {
        "id": issue.get("id"),
        "url": issue.get("url"),
        "title": f"[{identifier}] {title}",
        "content": content,
    }
