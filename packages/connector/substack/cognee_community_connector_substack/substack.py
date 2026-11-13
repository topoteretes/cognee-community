"""Substack connector – reads posts from a Substack publication's public API."""

import logging
import dlt
from dlt.sources.helpers import requests

logger = logging.getLogger(__name__)

SUBSTACK_SOURCE_NAME = "substack"
DOCUMENT_SOURCE_ATTR = "is_document_source"


def substack_posts(subdomain: str = dlt.config.value):
    """Returns a dlt source that yields Substack posts for a given publication.

    Args:
        subdomain: The Substack subdomain (e.g. "platformer" for platformer.substack.com).
    """

    @dlt.resource(name="substack_posts", write_disposition="replace", primary_key="id")
    def _posts_resource():
        if not subdomain:
            raise ValueError(
                "A Substack subdomain is required (e.g. 'platformer' for platformer.substack.com)"
            )

        base_url = f"https://{subdomain}.substack.com/api/v1/archive"
        offset = 0
        limit = 25

        while True:
            params = {"sort": "new", "offset": offset, "limit": limit}
            resp = requests.get(base_url, params=params)
            resp.raise_for_status()
            posts = resp.json()

            if not posts:
                break

            for post in posts:
                yield _post_to_row(post, subdomain)

            if len(posts) < limit:
                break
            offset += limit

    @dlt.source(name=SUBSTACK_SOURCE_NAME)
    def _substack():
        return _posts_resource()

    source = _substack()
    setattr(source, DOCUMENT_SOURCE_ATTR, SUBSTACK_SOURCE_NAME)
    return source


def _post_to_row(post: dict, subdomain: str) -> dict:
    """Convert a Substack API post object into a flat document row."""
    title = post.get("title", "Untitled")
    subtitle = post.get("subtitle", "")
    slug = post.get("slug", "")
    post_date = post.get("post_date", "")
    canonical_url = post.get("canonical_url", f"https://{subdomain}.substack.com/p/{slug}")
    description = post.get("description", "")
    # body_html can be very large; we take the truncated description for the content field
    # and let cognee's chunker handle it
    body = post.get("body_text") or post.get("truncated_body_text") or description

    content = (
        f"Title: {title}\n"
        f"Subtitle: {subtitle}\n"
        f"Date: {post_date}\n"
        f"URL: {canonical_url}\n\n"
        f"{body}"
    )

    return {
        "id": f"substack_{post.get('id')}",
        "title": title,
        "content": content,
    }
