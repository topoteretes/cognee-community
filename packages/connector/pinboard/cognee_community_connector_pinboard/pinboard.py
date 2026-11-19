import os
import hashlib
from typing import List, Dict, Any, Optional
import requests

PINBOARD_API_URL = "https://api.pinboard.in/v1"

def fetch_pinboard_posts(
    auth_token: str,
    tag: Optional[str] = None,
    api_base_url: str = PINBOARD_API_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Fetches bookmarks from the Pinboard API v1.
    Auth token is expected in 'username:TOKEN' format.
    """
    http = session or requests.Session()
    params = {
        "auth_token": auth_token,
        "format": "json"
    }
    if tag:
        params["tag"] = tag

    url = f"{api_base_url.rstrip('/')}/posts/all"
    response = http.get(url, params=params, timeout=20)
    response.raise_for_status()

    data = response.json()
    if isinstance(data, list):
        return data
    return []

def format_pinboard_post_markdown(post: Dict[str, Any]) -> str:
    """
    Renders Pinboard bookmark metadata and notes into structured Markdown for Cognee.
    """
    href = post.get("href", "")
    description = post.get("description", "Untitled Bookmark").strip()
    extended = post.get("extended", "").strip()
    tags = post.get("tags", "").strip()
    time_created = post.get("time", "")
    shared = post.get("shared", "yes")
    toread = post.get("toread", "no")

    lines = [
        f"# {description}",
        f"- **Bookmark URL**: [{href}]({href})",
        f"- **Date Saved**: {time_created}",
        f"- **To Read Later**: {'Yes' if toread == 'yes' else 'No'}",
        f"- **Publicly Shared**: {'Yes' if shared == 'yes' else 'No'}",
    ]

    if tags:
        lines.append(f"- **Tags**: `{tags}`")

    if extended:
        lines.append("\n## Notes & Excerpt\n")
        lines.append(extended)

    return "\n".join(lines)

def pinboard_source(
    auth_token: Optional[str] = None,
    tag: Optional[str] = None,
    api_base_url: str = PINBOARD_API_URL,
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    Cognee data connector for Pinboard bookmarks and reading lists.
    Returns normalized document entities ready for `cognee.remember()`.
    """
    token = auth_token or os.getenv("PINBOARD_API_TOKEN", "")
    if not token:
        raise ValueError("Pinboard auth_token must be provided or set via PINBOARD_API_TOKEN (format: username:TOKEN).")

    raw_posts = fetch_pinboard_posts(
        auth_token=token,
        tag=tag,
        api_base_url=api_base_url,
        session=session
    )

    documents = []
    for post in raw_posts:
        href = post.get("href", "")
        description = post.get("description", "Untitled Bookmark")
        content_md = format_pinboard_post_markdown(post)

        # Stable deterministic hash of canonical URL
        doc_id = hashlib.sha256(f"pinboard:{href}".encode("utf-8")).hexdigest()

        documents.append({
            "id": doc_id,
            "title": f"Pinboard: {description}",
            "content": content_md,
            "metadata": {
                "source": "pinboard",
                "url": href,
                "tags": post.get("tags", "").split(),
                "time": post.get("time", ""),
                "toread": (post.get("toread") == "yes")
            }
        })

    return documents
