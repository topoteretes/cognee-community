"""DEV.to (Forem) data source connector for Cognee.

This module provides data extraction from DEV.to's REST API into Cognee's
memory layer, enabling AI agents to ingest, cognify, and query developer
tutorials, architecture articles, engineering blogs, and community knowledge.
"""

from typing import Any, Dict, Iterator, List, Optional
import os
import requests

try:
    import dlt
    HAS_DLT = True
except ImportError:
    HAS_DLT = False

DEVTO_BASE_URL = "https://dev.to/api"


def _get_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    """Build request headers including API key if provided."""
    headers = {
        "Accept": "application/vnd.forem.api-v1+json",
        "User-Agent": "cognee-community-connector-devto/0.1.0",
    }
    key = api_key or os.getenv("DEVTO_API_KEY")
    if key:
        headers["api-key"] = key
    return headers


def fetch_devto_articles(
    tag: Optional[str] = None,
    username: Optional[str] = None,
    per_page: int = 30,
    page: int = 1,
    top: Optional[int] = None,
    api_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch a list of articles from the DEV.to API.

    Args:
        tag: Optional tag/topic to filter by (e.g. 'python', 'ai', 'devops').
        username: Optional DEV.to username to fetch articles by author.
        per_page: Number of articles to retrieve (1-100, default: 30).
        page: Page number for pagination.
        top: Filter most popular articles within the last N days.
        api_key: Optional DEV.to API key for authenticated requests.

    Returns:
        List of article metadata dictionaries.
    """
    url = f"{DEVTO_BASE_URL}/articles"
    params: Dict[str, Any] = {
        "per_page": min(max(1, per_page), 100),
        "page": page,
    }
    if tag:
        params["tag"] = tag
    if username:
        params["username"] = username
    if top:
        params["top"] = top

    response = requests.get(
        url,
        headers=_get_headers(api_key),
        params=params,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def fetch_devto_article_by_id(
    article_id: int,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch complete article details including markdown body by article ID."""
    url = f"{DEVTO_BASE_URL}/articles/{article_id}"
    response = requests.get(
        url,
        headers=_get_headers(api_key),
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def format_article_to_document(article: Dict[str, Any]) -> str:
    """Format an article into a rich markdown document for Cognee's cognify step.

    Includes author attribution, publication date, tags, reactions, and content.
    """
    title = article.get("title", "Untitled Article")
    author_info = article.get("user") or {}
    author_name = author_info.get("name", "Unknown Author")
    author_user = author_info.get("username", "")
    url = article.get("url", "")
    published_at = article.get("published_at", "N/A")
    tags = article.get("tags") or article.get("tag_list") or []
    if isinstance(tags, list):
        tags_str = ", ".join(tags)
    else:
        tags_str = str(tags)
    reactions = article.get("positive_reactions_count", 0)
    comments = article.get("comments_count", 0)
    reading_time = article.get("reading_time_minutes", 0)
    description = article.get("description", "")
    body_markdown = article.get("body_markdown") or description

    return (
        f"# {title}\n\n"
        f"**Author:** {author_name} (@{author_user})\n"
        f"**Published:** {published_at}\n"
        f"**URL:** {url}\n"
        f"**Tags:** {tags_str}\n"
        f"**Reactions:** {reactions} | **Comments:** {comments} | **Reading Time:** {reading_time} min\n\n"
        f"## Summary\n{description}\n\n"
        f"## Content\n{body_markdown}\n"
    )


def fetch_devto_documents(
    tag: Optional[str] = None,
    username: Optional[str] = None,
    per_page: int = 15,
    fetch_full_content: bool = False,
    api_key: Optional[str] = None,
) -> List[str]:
    """Fetch articles and format them as markdown documents for `cognee.add()`.

    Args:
        tag: Optional topic tag to filter by.
        username: Optional author username.
        per_page: Number of articles to ingest.
        fetch_full_content: If True, fetches full markdown body for each article.
        api_key: Optional DEV.to API key.

    Returns:
        List of formatted markdown strings.
    """
    articles = fetch_devto_articles(
        tag=tag,
        username=username,
        per_page=per_page,
        api_key=api_key,
    )

    documents: List[str] = []
    for art in articles:
        if fetch_full_content and "id" in art:
            try:
                full_art = fetch_devto_article_by_id(art["id"], api_key=api_key)
                documents.append(format_article_to_document(full_art))
                continue
            except Exception:
                pass  # Fallback to summary article if full fetch fails
        documents.append(format_article_to_document(art))

    return documents


if HAS_DLT:
    @dlt.source(name="devto")
    def devto_source(
        tag: Optional[str] = None,
        username: Optional[str] = None,
        per_page: int = 30,
        fetch_full_content: bool = False,
        api_key: Optional[str] = None,
    ) -> Any:
        """DLT source for DEV.to articles.

        Compatible with `await cognee.remember(devto_source(...))`.
        """
        @dlt.resource(name="devto_articles", write_disposition="replace")
        def devto_articles_resource() -> Iterator[Dict[str, Any]]:
            articles = fetch_devto_articles(
                tag=tag,
                username=username,
                per_page=per_page,
                api_key=api_key,
            )
            for art in articles:
                if fetch_full_content and "id" in art:
                    try:
                        full_art = fetch_devto_article_by_id(art["id"], api_key=api_key)
                        full_art["formatted_document"] = format_article_to_document(full_art)
                        yield full_art
                        continue
                    except Exception:
                        pass
                art["formatted_document"] = format_article_to_document(art)
                yield art

        return devto_articles_resource
else:
    def devto_source(*args, **kwargs):  # type: ignore
        raise ImportError(
            "dlt is required to use devto_source(). "
            "Please install dlt: pip install dlt"
        )
