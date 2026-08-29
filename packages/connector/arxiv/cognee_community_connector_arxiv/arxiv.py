"""DLT source for arXiv papers (full-snapshot sync + forget-on-delete).

Fetches arXiv paper metadata and abstracts via the public arXiv API, renders
them as markdown documents, and yields them as a dlt resource for cognee's
ingestion pipeline.

Like the Notion connector, papers are ingested as *normal documents*: the source
declares ``cognee_document_source = "arxiv"``, so ``resolve_dlt_sources`` routes
each paper through the standard cognify entity-extraction pipeline.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging
with exactly the papers matching the current search query. Papers that no longer
appear in the query results (e.g. withdrawn, cross-listed away) drop out of the
snapshot, and cognee's ``orphan_cleanup`` removes them from the graph and vector
stores. Unchanged papers keep a stable content-hash ``data_id``, so they are not
re-ingested or re-cognified.

arXiv API rate limit: ~1 request per 3 seconds. The connector enforces a 3.5 s
delay between paginated requests to stay safely under the limit.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import feedparser
import httpx

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("arxiv_connector")

ARXIV_TABLE_NAME = "arxiv_papers"
ARXIV_SOURCE_NAME = "arxiv"
_ARXIV_API_BASE = "https://export.arxiv.org/api/query"
# arXiv enforces ~1 request per 3 seconds. We use 3.5 s to stay safely under.
_ARXIV_RATE_DELAY = 3.5
# Max results per page (arXiv API cap).
_ARXIV_MAX_RESULTS = 100
# Lookback window for incremental sync: if no start_date is given, papers from
# the last 30 days are fetched (avoids pulling the entire arXiv on first sync).
_DEFAULT_LOOKBACK_DAYS = 30

_EXTRA_HINT = (
    'The arXiv connector requires the "arxiv" extra: '
    'pip install "cognee[arxiv]" (provides dlt, feedparser, and httpx).'
)


def arxiv_source(
    query: str | None = None,
    categories: list[str] | None = None,
    author: str | None = None,
    start_date: str | None = None,
    max_results: int | None = None,
    http_client: httpx.Client | None = None,
):
    """Create a dlt source that yields arXiv papers as markdown documents.

    Args:
        query: Free-text search query (searches title, abstract, and author).
        categories: Restrict to specific arXiv categories
            (e.g. ``["cs.AI", "cs.CL"]``).
        author: Restrict to papers by a specific author.
        start_date: ISO-format date string (YYYY-MM-DD) — only papers submitted
            on or after this date are fetched. Defaults to 30 days ago.
        max_results: Maximum number of results to fetch (default: unlimited,
            capped by arXiv API at ~1000 per query).
        http_client: Pre-built ``httpx.Client`` (mainly a test-injection point);
            when omitted one is built with a 30 s timeout.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if http_client is None:
        http_client = httpx.Client(timeout=30.0)

    # Build the search query string for the arXiv API.
    search_terms: list[str] = []
    if query:
        search_terms.append(f"all:{_arxiv_escape(query)}")
    if categories:
        cat_clause = " OR ".join(f"cat:{cat}" for cat in categories)
        search_terms.append(f"({cat_clause})")
    if author:
        search_terms.append(f"au:{_arxiv_escape(author)}")
    if not search_terms:
        # Default: search for recent AI/ML papers.
        search_terms.append("cat:cs.AI")

    search_query = " AND ".join(search_terms)

    # Resolve the start date.
    if start_date:
        start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    else:
        start_dt = datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)

    @dlt.resource(name=ARXIV_TABLE_NAME, primary_key="id", write_disposition="replace")
    def arxiv_papers():
        count = 0
        for paper in _iter_papers(
            http_client, search_query, start_dt, max_results
        ):
            count += 1
            yield paper
        logger.info("arXiv: synced %d paper(s) for query '%s'.", count, search_query)

    @dlt.source(name=ARXIV_SOURCE_NAME)
    def _arxiv():
        return arxiv_papers

    source = _arxiv()
    setattr(source, DOCUMENT_SOURCE_ATTR, ARXIV_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# arXiv API helpers (module-private)
# ---------------------------------------------------------------------------


def _arxiv_escape(value: str) -> str:
    """Escape a value for use in an arXiv search query."""
    # Characters that need escaping in arXiv queries: (, ), and spaces.
    return value.replace(" ", "+")


def _build_query_url(search_query: str, start: int, max_results: int) -> str:
    """Build the arXiv API query URL."""
    params = {
        "search_query": search_query,
        "start": start,
        "max_results": min(max_results, _ARXIV_MAX_RESULTS),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    return f"{_ARXIV_API_BASE}?{urlencode(params)}"


def _iter_papers(
    http_client: httpx.Client,
    search_query: str,
    start_dt: datetime,
    limit: int | None,
) -> list[dict]:
    """Yield paper row dicts from the arXiv API, respecting the rate limit.

    Papers older than ``start_dt`` stop the iteration (since results are sorted
    by submittedDate descending, we can break early).
    """
    start = 0
    batch_size = _ARXIV_MAX_RESULTS

    while True:
        if limit is not None and start >= limit:
            break

        if start > 0:
            time.sleep(_ARXIV_RATE_DELAY)

        url = _build_query_url(search_query, start, batch_size)
        logger.debug("arXiv: fetching %s", url)

        # Fetch and parse the Atom feed.
        resp = http_client.get(url)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)

        entries = feed.entries
        if not entries:
            break

        for entry in entries:
            paper = _entry_to_row(entry)
            # Break if the paper is older than the start date.
            paper_date = _parse_date(entry.get("published", ""))
            if paper_date and paper_date < start_dt:
                # We've reached papers older than the lookback window.
                # Since results are sorted descending, this is a clean break.
                return
            yield paper

        # Check if there are more results.
        total = int(feed.feed.get("opensearch_totalresults", 0))
        start += len(entries)
        if start >= total:
            break


def _entry_to_row(entry: dict) -> dict:
    """Flatten a feedparser arXiv entry into a document row.

    Only identity/provenance fields + title/abstract are kept, so re-fetching
    the same paper (unchanged) does not churn the content-hash data_id.
    """
    arxiv_id = entry.get("id", "").split("/abs/")[-1] if "abs/" in entry.get("id", "") else entry.get("id", "")
    # Strip version suffix (e.g. "2301.12345v2" → "2301.12345")
    arxiv_id = arxiv_id.rsplit("v", 1)[0] if arxiv_id and arxiv_id[-2:-1] == "v" else arxiv_id

    authors = [a.get("name", "") for a in entry.get("authors", [])]
    categories = [t.get("term", "") for t in entry.get("tags", [])]

    return {
        "id": arxiv_id,
        "url": entry.get("id", ""),
        "title": entry.get("title", "").strip().replace("\n", " "),
        "authors": ", ".join(authors),
        "categories": ", ".join(categories),
        "published": entry.get("published", ""),
        "updated": entry.get("updated", ""),
        "abstract": entry.get("summary", "").strip().replace("\n", " "),
        "content": _render_markdown(entry),
    }


def _render_markdown(entry: dict) -> str:
    """Render an arXiv entry as a markdown document."""
    title = entry.get("title", "Untitled").strip().replace("\n", " ")
    authors = [a.get("name", "") for a in entry.get("authors", [])]
    author_line = ", ".join(authors) if authors else "Unknown"
    categories = [t.get("term", "") for t in entry.get("tags", [])]
    cat_line = ", ".join(categories) if categories else "N/A"
    published = entry.get("published", "Unknown")
    abstract = entry.get("summary", "").strip().replace("\n", " ")
    arxiv_url = entry.get("id", "")

    return (
        f"# {title}\n\n"
        f"**Authors:** {author_line}\n\n"
        f"**Published:** {published}\n\n"
        f"**Categories:** {cat_line}\n\n"
        f"**URL:** {arxiv_url}\n\n"
        f"## Abstract\n\n"
        f"{abstract}\n"
    )


def _parse_date(date_str: str) -> datetime | None:
    """Parse an arXiv date string to a timezone-aware datetime."""
    if not date_str:
        return None
    try:
        # arXiv dates are typically like "2024-01-15T18:00:00Z"
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None