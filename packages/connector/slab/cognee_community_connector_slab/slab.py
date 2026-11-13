"""DLT source for Slab knowledge base (posts, topics, and comments).

Extracts team wiki posts, associated topics, and threaded comments via Slab's
GraphQL API (https://api.slab.com/v1/graphql), converting posts into structured
documents for Cognee's cognify engine.

Architecture & Document-Mode:
- Sets DOCUMENT_SOURCE_ATTR = "slab" and PIPELINE_SCOPE_ATTR = "slab".
- Sets write_disposition="merge" with primary_key="id" and
  columns={"_deleted": {"data_type": "bool", "hard_delete": True}}.
- Posts are extracted and rendered into clean Markdown with frontmatter/metadata,
  code block preservation, and formatted comments.
- Incremental sync tracks updatedAt bookmarks via dlt resource state.
- Forget-on-delete tombstoning emits {"id": ..., "_deleted": True} when posts
  are removed or unshared upstream, safely pruning orphaned graph entities
  and vector index entries.
"""

import os
import time
from typing import Any, Generator, Iterable

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("slab_connector")

SLAB_SOURCE_NAME = "slab"
SLAB_TABLE_NAME = "slab_posts"
DEFAULT_BASE_URL = "https://api.slab.com/v1/graphql"

_MAX_RETRIES = 5
_INITIAL_RETRY_DELAY = 1.0

# GraphQL query for organization posts
ORGANIZATION_POSTS_QUERY = """
query GetOrganizationPosts($first: Int, $after: String) {
  organization {
    posts(first: $first, after: $after) {
      edges {
        node {
          id
          title
          content
          insertedAt
          updatedAt
          owner {
            id
            name
            email
          }
          topics {
            id
            name
          }
          comments {
            id
            content
            insertedAt
            author {
              id
              name
            }
          }
        }
        cursor
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

# GraphQL query for posts belonging to a specific topic
TOPIC_POSTS_QUERY = """
query GetTopicPosts($topicId: ID!, $first: Int, $after: String) {
  topic(id: $topicId) {
    id
    name
    description
    posts(first: $first, after: $after) {
      edges {
        node {
          id
          title
          content
          insertedAt
          updatedAt
          owner {
            id
            name
            email
          }
          topics {
            id
            name
          }
          comments {
            id
            content
            insertedAt
            author {
              id
              name
            }
          }
        }
        cursor
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""


class SlabAPIError(RuntimeError):
    """Raised when Slab GraphQL returns errors or unhandled HTTP failures."""


def _is_transient(exc: Exception) -> bool:
    """Classify transient network or rate-limiting errors for retry."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code in {429, 500, 502, 503, 504}
    return False


def _execute_graphql(
    client: httpx.Client,
    base_url: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a GraphQL query against Slab with exponential backoff retries."""
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.post(base_url, json=payload)
            response.raise_for_status()
            data = response.json()

            errors = data.get("errors")
            resp_data = data.get("data")

            # Partial error handling: if data is returned alongside non-fatal errors
            if errors and resp_data:
                logger.warning("Slab GraphQL returned partial errors: %s", errors)
                return resp_data
            elif errors and not resp_data:
                raise SlabAPIError(f"Slab GraphQL execution errors: {errors}")

            if resp_data is None:
                raise SlabAPIError(f"Empty GraphQL data received from Slab: {data}")

            return resp_data
        except Exception as exc:
            last_error = exc
            if not _is_transient(exc) or attempt == _MAX_RETRIES - 1:
                raise
            sleep_sec = _INITIAL_RETRY_DELAY * (2**attempt)
            logger.warning(
                "Transient error contacting Slab GraphQL (attempt %d/%d): %s. Retrying in %.1fs...",
                attempt + 1,
                _MAX_RETRIES,
                exc,
                sleep_sec,
            )
            time.sleep(sleep_sec)

    if last_error:
        raise last_error
    raise SlabAPIError("GraphQL execution failed after retries.")


def _post_to_row(post: dict[str, Any]) -> dict[str, Any]:
    """Render a Slab post and its comments into a clean Markdown document row."""
    post_id = str(post.get("id", ""))
    title = (post.get("title") or "Untitled Post").strip()
    raw_content = post.get("content") or ""

    owner = post.get("owner") or {}
    author_name = (owner.get("name") or "Team Member").strip()

    updated_at = post.get("updatedAt") or post.get("insertedAt") or ""
    topics = post.get("topics") or []
    topic_names = [t.get("name", "").strip() for t in topics if t.get("name")]

    lines = [
        f"# {title}",
        "",
        f"**Post ID:** {post_id}",
        f"**Author:** {author_name}",
        f"**Updated At:** {updated_at}",
    ]

    if topic_names:
        lines.append(f"**Topics:** {', '.join(topic_names)}")

    lines.append("\n---\n")

    cleaned_content = raw_content.strip()
    if cleaned_content:
        lines.append(cleaned_content)
    else:
        lines.append("*(No content)*")

    # Render threaded comments
    comments = post.get("comments") or []
    valid_comments = []
    for comment in comments:
        c_text = (comment.get("content") or "").strip()
        if not c_text:
            continue
        c_author = comment.get("author") or {}
        c_name = (c_author.get("name") or "Team Member").strip()
        c_date = comment.get("insertedAt") or ""
        valid_comments.append((c_name, c_date, c_text))

    if valid_comments:
        lines.append("\n\n---\n### Comments\n")
        for c_name, c_date, c_text in valid_comments:
            if c_date:
                lines.append(f"- **{c_name}** ({c_date}): {c_text}")
            else:
                lines.append(f"- **{c_name}**: {c_text}")

    document_content = "\n".join(lines)
    canonical_id = f"slab_post_{post_id}"

    return {
        "id": canonical_id,
        "title": title,
        "content": document_content,
        "url": f"https://slab.com/posts/{post_id}",
        "updated_at": updated_at,
        "topics": topic_names,
        "author": author_name,
    }


def _iter_slab_posts(
    client: httpx.Client,
    base_url: str,
    topic_ids: list[str] | None = None,
    page_size: int = 50,
) -> Generator[dict[str, Any], None, None]:
    """Iterate through Slab posts using Relay cursor pagination.

    Deduplicates posts across queried topics to prevent duplicate document rows.
    """
    seen_post_ids: set[str] = set()

    if topic_ids:
        for topic_id in topic_ids:
            cursor: str | None = None
            has_next_page = True

            while has_next_page:
                variables = {"topicId": topic_id, "first": page_size, "after": cursor}
                data = _execute_graphql(client, base_url, TOPIC_POSTS_QUERY, variables)
                topic_data = data.get("topic")
                if not topic_data:
                    break

                posts_conn = topic_data.get("posts") or {}
                edges = posts_conn.get("edges") or []
                if not edges:
                    break

                for edge in edges:
                    node = edge.get("node")
                    if not node:
                        continue
                    post_id = str(node.get("id", ""))
                    if post_id in seen_post_ids:
                        continue
                    seen_post_ids.add(post_id)
                    yield node

                page_info = posts_conn.get("pageInfo") or {}
                has_next_page = bool(page_info.get("hasNextPage", False))
                cursor = page_info.get("endCursor")
                if not cursor:
                    break
    else:
        cursor = None
        has_next_page = True

        while has_next_page:
            variables = {"first": page_size, "after": cursor}
            data = _execute_graphql(client, base_url, ORGANIZATION_POSTS_QUERY, variables)
            org_data = data.get("organization") or {}
            posts_conn = org_data.get("posts") or {}
            edges = posts_conn.get("edges") or []
            if not edges:
                break

            for edge in edges:
                node = edge.get("node")
                if not node:
                    continue
                post_id = str(node.get("id", ""))
                if post_id in seen_post_ids:
                    continue
                seen_post_ids.add(post_id)
                yield node

            page_info = posts_conn.get("pageInfo") or {}
            has_next_page = bool(page_info.get("hasNextPage", False))
            cursor = page_info.get("endCursor")
            if not cursor:
                break


def _iter_slab_rows(
    client: httpx.Client,
    base_url: str,
    topic_ids: list[str] | None = None,
    incremental: bool = True,
    resource_state: dict[str, Any] | None = None,
) -> Generator[dict[str, Any], None, None]:
    """Fetch Slab posts, apply incremental filters, and emit tombstone rows."""
    state = resource_state if resource_state is not None else {}
    seen_ids: list[str] = state.get("slab_seen_ids", [])
    seen_ids_set: set[str] = set(seen_ids)
    last_cursor: str = state.get("slab_post_cursor", "")

    current_run_ids: set[str] = set()
    latest_timestamp = last_cursor

    for post in _iter_slab_posts(client, base_url, topic_ids=topic_ids):
        row = _post_to_row(post)
        row_id = row["id"]
        current_run_ids.add(row_id)
        post_updated_at = row.get("updated_at") or ""

        if post_updated_at > latest_timestamp:
            latest_timestamp = post_updated_at

        if incremental and last_cursor and post_updated_at <= last_cursor:
            continue

        yield row

    # Forget-on-delete tombstoning
    # Missing upstream posts are tombstoned to remove them from Cognee graph & vector index
    vanished_ids = seen_ids_set - current_run_ids
    for vanished_id in sorted(vanished_ids):
        yield {"id": vanished_id, "_deleted": True}

    # Update state only upon complete, error-free sync
    state["slab_seen_ids"] = sorted(current_run_ids)
    if latest_timestamp:
        state["slab_post_cursor"] = latest_timestamp


def slab_source(
    api_token: str | None = None,
    topic_ids: list[str] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    incremental: bool = True,
    client: httpx.Client | None = None,
) -> Any:
    """Create a DLT resource for Slab posts compatible with Cognee document-mode.

    Args:
        api_token: Slab API token. Defaults to SLAB_API_TOKEN environment variable.
        topic_ids: Optional list of topic IDs to filter posts.
        base_url: Slab GraphQL endpoint URL (defaults to https://api.slab.com/v1/graphql).
        incremental: Whether to use incremental syncing via post updatedAt timestamps.
        client: Optional pre-configured httpx.Client (useful for testing and mocks).

    Returns:
        dlt.resource yielding Slab post document rows.
    """
    token = api_token or os.environ.get("SLAB_API_TOKEN")
    if not token and client is None:
        raise ValueError(
            "Slab API token is required: provide api_token or set SLAB_API_TOKEN in environment."
        )

    headers = {
        "Authorization": token.strip() if token else "",
        "Content-Type": "application/json",
        "User-Agent": "cognee-community-connector-slab/0.1.0",
    }

    try:
        import dlt
    except ImportError as err:
        raise ImportError("The Slab connector requires dlt: pip install dlt[sqlalchemy]") from err

    @dlt.resource(
        name=SLAB_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def slab_resource_gen() -> Iterable[dict[str, Any]]:
        state = dlt.current.resource_state()
        if client is not None:
            yield from _iter_slab_rows(
                client=client,
                base_url=base_url,
                topic_ids=topic_ids,
                incremental=incremental,
                resource_state=state,
            )
        else:
            with httpx.Client(headers=headers, timeout=30.0) as http_client:
                yield from _iter_slab_rows(
                    client=http_client,
                    base_url=base_url,
                    topic_ids=topic_ids,
                    incremental=incremental,
                    resource_state=state,
                )

    res = slab_resource_gen()
    # Mark for Cognee document-mode ingestion and pipeline scoping
    setattr(res, DOCUMENT_SOURCE_ATTR, SLAB_SOURCE_NAME)
    setattr(res, dlt_utils.PIPELINE_SCOPE_ATTR, SLAB_SOURCE_NAME)
    return res
