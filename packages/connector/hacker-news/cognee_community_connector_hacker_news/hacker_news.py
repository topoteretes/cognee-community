"""Hacker News connector – reads stories from the HN Firebase API (no auth required)."""

import logging
import dlt
from dlt.sources.helpers import requests

logger = logging.getLogger(__name__)

HN_SOURCE_NAME = "hacker_news"
DOCUMENT_SOURCE_ATTR = "is_document_source"
HN_API_BASE = "https://hacker-news.firebaseio.com/v0"


def hacker_news_stories(story_type: str = "top", max_items: int = 200):
    """Returns a dlt source that yields Hacker News stories.

    Args:
        story_type: One of "top", "best", or "new".
        max_items: Maximum number of stories to fetch (default 200).
    """

    @dlt.resource(name="hn_stories", write_disposition="replace", primary_key="id")
    def _stories_resource():
        valid_types = {"top": "topstories", "best": "beststories", "new": "newstories"}
        endpoint = valid_types.get(story_type)
        if not endpoint:
            raise ValueError(f"story_type must be one of {list(valid_types.keys())}")

        # Fetch story IDs
        ids_url = f"{HN_API_BASE}/{endpoint}.json"
        resp = requests.get(ids_url)
        resp.raise_for_status()
        story_ids = resp.json()[:max_items]

        for sid in story_ids:
            item_url = f"{HN_API_BASE}/item/{sid}.json"
            item_resp = requests.get(item_url)
            item_resp.raise_for_status()
            item = item_resp.json()
            if item and item.get("type") == "story":
                yield _story_to_row(item)

    @dlt.source(name=HN_SOURCE_NAME)
    def _hn():
        return _stories_resource()

    source = _hn()
    setattr(source, DOCUMENT_SOURCE_ATTR, HN_SOURCE_NAME)
    return source


def _story_to_row(item: dict) -> dict:
    """Convert an HN API item into a flat document row."""
    title = item.get("title", "Untitled")
    url = item.get("url", "")
    score = item.get("score", 0)
    by = item.get("by", "unknown")
    time_val = item.get("time", 0)
    descendants = item.get("descendants", 0)
    text = item.get("text", "")  # for Ask HN / Show HN posts

    hn_url = f"https://news.ycombinator.com/item?id={item.get('id')}"

    content = (
        f"Title: {title}\n"
        f"Author: {by}\n"
        f"Score: {score}\n"
        f"Comments: {descendants}\n"
        f"URL: {url}\n"
        f"HN Link: {hn_url}\n"
    )
    if text:
        content += f"\nText:\n{text}"

    return {
        "id": f"hn_{item.get('id')}",
        "title": title,
        "content": content,
    }
