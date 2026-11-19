"""Flarum community forum data-source connector for Cognee.

Ingests discussions, thread categories, reply sequences, and user posts from Flarum forums
into Cognee memory for natural language community intelligence and semantic search.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Generator, List, Optional
import dlt
import httpx
from bs4 import BeautifulSoup

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

logger = logging.getLogger(__name__)

DEFAULT_FLARUM_BASE_URL = "http://localhost:8080"


def _html_to_clean_text(html_str: Optional[str]) -> str:
    """Strip HTML markup from Flarum post content into clean text."""
    if not html_str:
        return ""
    soup = BeautifulSoup(html_str, "html.parser")
    return soup.get_text(separator=" ", strip=True)


class FlarumClient:
    """Client for querying the Flarum JSON:API endpoints."""

    def __init__(
        self,
        base_url: str = DEFAULT_FLARUM_BASE_URL,
        token: Optional[str] = None,
        http_client: Optional[Any] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {
            "User-Agent": "CogneeCommunityFlarumConnector/0.1.0",
            "Accept": "application/vnd.api+json, application/json",
        }
        if token:
            headers["Authorization"] = f"Token {token}"

        self._http_client = http_client or httpx.Client(
            timeout=timeout,
            headers=headers,
        )

    def get_discussions(
        self,
        tag: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Retrieve forum discussions."""
        url = f"{self.base_url}/api/discussions"
        params: Dict[str, Any] = {"page[limit]": limit}
        if tag:
            params["filter[tag]"] = tag

        try:
            resp = self._http_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
        except Exception as exc:
            logger.error("Failed to fetch Flarum discussions: %s", exc)
            raise

    def get_discussion_posts(self, discussion_id: str) -> List[Dict[str, Any]]:
        """Retrieve chronological posts for a discussion."""
        url = f"{self.base_url}/api/posts"
        params = {"filter[discussion]": discussion_id, "sort": "createdAt"}
        try:
            resp = self._http_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
        except Exception as exc:
            logger.warning("Failed to fetch posts for discussion %s: %s", discussion_id, exc)
            return []


def _discussion_to_document(
    discussion: Dict[str, Any],
    posts: List[Dict[str, Any]],
    base_url: str,
) -> Optional[Dict[str, Any]]:
    """Convert a Flarum discussion and its posts into a structured Markdown document."""
    disc_id = discussion.get("id")
    attrs = discussion.get("attributes", {})
    title = attrs.get("title")
    if not disc_id or not title:
        return None

    slug = attrs.get("slug") or f"discussion-{disc_id}"
    created_at = attrs.get("createdAt", "Unknown date")
    last_posted_at = attrs.get("lastPostedAt", created_at)
    comment_count = attrs.get("commentCount", len(posts))

    url = f"{base_url}/d/{disc_id}-{slug}"

    post_sections = []
    for idx, post in enumerate(posts, 1):
        p_attrs = post.get("attributes", {})
        p_content = p_attrs.get("contentHtml") or p_attrs.get("content") or ""
        clean_text = _html_to_clean_text(p_content)
        p_time = p_attrs.get("createdAt", "")
        post_sections.append(f"### Post #{idx} ({p_time}):\n{clean_text}\n")

    posts_body = "\n".join(post_sections) if post_sections else "*(No public posts recorded)*"

    content = f"""# Forum Discussion: {title}
**Discussion ID:** `{disc_id}`
**URL:** [{title}]({url})
**Created:** {created_at} | **Last Activity:** {last_posted_at}
**Total Replies:** {comment_count}

## Discussion Thread:
{posts_body}

---
*Ingested from Flarum Community at `{base_url}` via Cognee community connector.*"""

    return {
        "id": f"flarum_disc_{disc_id}",
        "url": url,
        "title": f"Discussion: {title}",
        "content": content,
    }


@dlt.source(name="flarum_source")
def flarum_source(
    base_url: str = DEFAULT_FLARUM_BASE_URL,
    token: Optional[str] = None,
    tags: Optional[List[str]] = None,
    limit: int = 50,
    client: Optional[FlarumClient] = None,
) -> Any:
    """DLT data-source for ingesting Flarum forum discussions into Cognee."""
    flarum_client = client or FlarumClient(base_url=base_url, token=token)

    @dlt.resource(
        name="discussions",
        write_disposition="replace",
        primary_key="id",
    )
    def discussions() -> Generator[Dict[str, Any], None, None]:
        target_tags = tags or [None]

        for tag in target_tags:
            disc_list = flarum_client.get_discussions(tag=tag, limit=limit)
            for disc in disc_list:
                disc_id = disc.get("id")
                posts = []
                if disc_id:
                    posts = flarum_client.get_discussion_posts(discussion_id=disc_id)
                doc = _discussion_to_document(disc, posts=posts, base_url=base_url)
                if doc:
                    yield doc

    resource = discussions()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "flarum")
    return resource
