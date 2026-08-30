"""DLT source for Hacker News (full-snapshot sync + forget-on-delete).

Uses the public Algolia HN Search API (no auth) to pull stories and comments
for tracked topics, then yields them as markdown documents for cognee.

Like the Notion connector, items are ingested as *normal documents*: the source
declares ``cognee_document_source = "hacker_news"``. A full snapshot
(``write_disposition="replace"``) means items that drop out of the search
results — deleted or no longer matching — are forgotten on the next run.
Unchanged ``objectID`` values keep a stable row id, so they are not
re-ingested.

The official Firebase API is a poor fit for topic filtering; Algolia is the
path the issue asked for. ``since`` is applied as ``created_at_i>=`` so a
re-sync can walk only newer hits. Prefer omitting it when you want
forget-on-delete over the full tracked set.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("hacker_news_connector")

HN_TABLE_NAME = "hacker_news_items"
HN_SOURCE_NAME = "hacker_news"
ALGOLIA_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
DEFAULT_TAGS = "(story,comment)"
DEFAULT_HITS_PER_PAGE = 50
DEFAULT_MAX_PAGES = 5
_PAGE_DELAY_SECONDS = 0.2
_HTML_TAG = re.compile(r"<[^>]+>")

_EXTRA_HINT = (
    "The Hacker News connector requires dlt: "
    'pip install "cognee-community-connector-hacker-news".'
)


def hacker_news_source(
    queries: Iterable[str],
    tags: str = DEFAULT_TAGS,
    since: datetime | int | str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    hits_per_page: int = DEFAULT_HITS_PER_PAGE,
    fetch: Callable[[str], dict[str, Any]] | None = None,
):
    """Create a dlt source that yields HN stories/comments as documents.

    Args:
        queries: Topic strings to search (Algolia ``query=``). At least one.
        tags: Algolia tag filter. Default ``(story,comment)`` covers threads.
        since: Optional cursor. Hits with ``created_at_i`` before this are
            omitted. Pass a datetime, unix seconds, or ISO string.
        max_pages: Safety cap on Algolia pages per query.
        hits_per_page: Page size (Algolia max is 1000; 50 is enough).
        fetch: Test injection. When omitted, the public Algolia HTTP API is used.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    topics = [q.strip() for q in queries if q and q.strip()]
    if not topics:
        raise ValueError("hacker_news_source requires at least one search query.")
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1.")

    since_unix = _parse_since(since)
    get_page = fetch or _default_fetch

    @dlt.resource(name=HN_TABLE_NAME, primary_key="id", write_disposition="replace")
    def hacker_news_items():
        count = 0
        seen_ids: set[str] = set()
        for query in topics:
            for hit in _iter_hits(get_page, query, tags, max_pages, hits_per_page, since_unix):
                row = _hit_to_row(hit)
                if row is None or row["id"] in seen_ids:
                    continue
                seen_ids.add(row["id"])
                count += 1
                yield row
        logger.info("Hacker News: synced %d item(s) for %d quer(y/ies).", count, len(topics))

    @dlt.source(name=HN_SOURCE_NAME)
    def _hacker_news():
        return hacker_news_items

    source = _hacker_news()
    setattr(source, DOCUMENT_SOURCE_ATTR, HN_SOURCE_NAME)
    return source


def _default_fetch(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "cognee-community-connector-hacker-news/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Hacker News Algolia request failed: {url}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected Algolia payload for {url}")
    return payload


def _iter_hits(
    fetch: Callable[[str], dict[str, Any]],
    query: str,
    tags: str,
    max_pages: int,
    hits_per_page: int,
    since_unix: int | None,
):
    for page in range(max_pages):
        url = _search_url(query, tags, page, hits_per_page, since_unix)
        payload = fetch(url)
        hits = payload.get("hits") or []
        yield from hits
        nb_pages = int(payload.get("nbPages") or 0)
        if page + 1 >= nb_pages or not hits:
            return
        if fetch is _default_fetch:
            time.sleep(_PAGE_DELAY_SECONDS)


def _search_url(
    query: str,
    tags: str,
    page: int,
    hits_per_page: int,
    since_unix: int | None,
) -> str:
    params: dict[str, str] = {
        "query": query,
        "tags": tags,
        "page": str(page),
        "hitsPerPage": str(hits_per_page),
    }
    if since_unix is not None:
        params["numericFilters"] = f"created_at_i>={since_unix}"
    return f"{ALGOLIA_SEARCH_URL}?{urllib.parse.urlencode(params)}"


def _parse_since(since: datetime | int | str | None) -> int | None:
    if since is None:
        return None
    if isinstance(since, int):
        return since
    if isinstance(since, float):
        return int(since)
    if isinstance(since, datetime):
        value = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    if isinstance(since, str):
        text = since.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Could not parse since= {since!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    raise ValueError(f"Could not parse since= {since!r}")


def _hit_to_row(hit: dict[str, Any]) -> dict[str, str] | None:
    object_id = str(hit.get("objectID") or "").strip()
    if not object_id:
        return None
    title = _plain(hit.get("title")) or f"HN item {object_id}"
    content = _plain(hit.get("story_text")) or _plain(hit.get("comment_text"))
    url = _plain(hit.get("url")) or f"https://news.ycombinator.com/item?id={object_id}"
    created = hit.get("created_at") or ""
    author = _plain(hit.get("author"))
    return {
        "id": object_id,
        "url": url,
        "title": title,
        "content": content,
        "published": str(created),
        "author": author,
    }


def _plain(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return _HTML_TAG.sub("", text).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
