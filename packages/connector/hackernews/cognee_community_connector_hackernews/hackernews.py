"""Hacker News data-source connector for cognee.

Fetches stories and discussions from the Hacker News Firebase API
into structured cognee documents.
"""

import datetime
import html
import re
from collections.abc import Iterator
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("hackernews_connector")

HN_TABLE_NAME = "hackernews_stories"
HN_SOURCE_NAME = "hackernews"
HN_BASE_URL = "https://hacker-news.firebaseio.com/v0"

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(raw_html: str | None) -> str:
    if not raw_html:
        return ""
    unescaped = html.unescape(raw_html)
    # Replace <p> with newlines
    unescaped = re.sub(r"<\s*p\s*>", "\n\n", unescaped, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub(" ", unescaped)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def iter_hackernews_items(
    story_type: str = "top",
    limit: int = 30,
    include_top_comments: int = 3,
    item_ids: list[int] | None = None,
    fetch_fn: Any = None,
) -> Iterator[dict[str, Any]]:
    """Yield parsed Hacker News stories with metadata and top comments."""
    def _default_fetch(url: str) -> Any:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    fetcher = fetch_fn or _default_fetch

    target_ids: list[int] = []
    if item_ids:
        target_ids = item_ids[:limit]
    else:
        endpoint = f"{HN_BASE_URL}/{story_type}stories.json"
        try:
            ids = fetcher(endpoint) or []
            target_ids = ids[:limit]
        except Exception as exc:
            logger.error("HN: failed to fetch %s stories: %s", story_type, exc)
            return

    for item_id in target_ids:
        item_url = f"{HN_BASE_URL}/item/{item_id}.json"
        try:
            item = fetcher(item_url)
        except Exception as exc:
            logger.warning("HN: failed to fetch item %d: %s", item_id, exc)
            continue

        if not item or item.get("deleted") or item.get("dead"):
            continue

        title = item.get("title", "Untitled")
        author = item.get("by", "anonymous")
        score = item.get("score", 0)
        url = item.get("url") or f"https://news.ycombinator.com/item?id={item_id}"
        timestamp = item.get("time", 0)
        time_str = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc).isoformat() if timestamp else ""
        text = _clean_html(item.get("text"))
        comment_count = item.get("descendants", 0)
        kids = item.get("kids") or []

        # Optional top comments extraction
        top_comments: list[str] = []
        if include_top_comments > 0 and kids:
            for comment_id in kids[:include_top_comments]:
                c_url = f"{HN_BASE_URL}/item/{comment_id}.json"
                try:
                    c_data = fetcher(c_url)
                    if c_data and not c_data.get("deleted") and not c_data.get("dead"):
                        c_author = c_data.get("by", "anon")
                        c_text = _clean_html(c_data.get("text"))
                        if c_text:
                            top_comments.append(f"> **{c_author}:** {c_text}")
                except Exception:
                    pass

        lines = [
            f"# {title}",
            f"**Author:** {author} | **Score:** {score} | **Comments:** {comment_count}",
            f"**Date:** {time_str} | **URL:** {url}",
        ]
        if text:
            lines.append(f"\n{text}")
        if top_comments:
            lines.append("\n### Top Comments\n" + "\n\n".join(top_comments))

        content = "\n".join(lines)

        yield {
            "id": f"hn:{item_id}",
            "item_id": item_id,
            "title": title,
            "author": author,
            "score": score,
            "url": url,
            "time": time_str,
            "comments_count": comment_count,
            "content": content,
        }


def hackernews_source(
    story_type: str = "top",
    limit: int = 30,
    include_top_comments: int = 3,
    item_ids: list[int] | None = None,
    fetch_fn: Any = None,
):
    """Create a dlt source yielding Hacker News stories as documents."""
    import dlt

    @dlt.resource(name=HN_TABLE_NAME, primary_key="id", write_disposition="replace")
    def hn_stories():
        count = 0
        for story in iter_hackernews_items(
            story_type=story_type,
            limit=limit,
            include_top_comments=include_top_comments,
            item_ids=item_ids,
            fetch_fn=fetch_fn,
        ):
            count += 1
            yield story
        logger.info("Hacker News: synced %d story item(s).", count)

    @dlt.source(name=HN_SOURCE_NAME)
    def _hackernews():
        return hn_stories

    source = _hackernews()
    setattr(source, DOCUMENT_SOURCE_ATTR, HN_SOURCE_NAME)
    return source
