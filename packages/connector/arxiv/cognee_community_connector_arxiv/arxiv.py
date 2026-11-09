"""DLT source for arXiv papers (incremental sync + forget-on-delete).

Fetches paper metadata and abstracts from arXiv's Atom API by category or
author query, then yields them as a dlt resource for cognee's ingestion
pipeline.

arXiv requires no authentication. The API is rate-limited to roughly one
request every 3 seconds — the connector enforces this delay internally.

The source uses ``write_disposition="merge"`` with ``primary_key="arxiv_id"``
so incremental syncs pick up only new or changed papers. Papers that vanish
from a query result (e.g. reclassified to a different category) are removed
on the next full snapshot via ``orphan_cleanup``.
"""

import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("arxiv_connector")

ARXIV_TABLE_NAME = "arxiv_papers"
ARXIV_SOURCE_NAME = "arxiv"
ARXIV_API_URL = "http://export.arxiv.org/api/query"

# arXiv rate limit: one request every 3 seconds.
_REQUEST_INTERVAL = 3.0

# Atom feed namespaces used by the arXiv API.
_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

_EXTRA_HINT = (
    'The arXiv connector requires the "arxiv" extra: pip install "cognee[arxiv]" '
    "(provides dlt and httpx)."
)


def arxiv_source(
    query: str | None = None,
    categories: list[str] | None = None,
    author: str | None = None,
    max_results: int = 100,
    submitted_after: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields arXiv papers as documents.

    Args:
        query: Free-text search query. If omitted, ``categories`` and/or
            ``author`` are used to construct the query.
        categories: arXiv category list, e.g. ["cs.AI", "cs.CL"]. Combined
            with ``author`` if both given.
        author: Author name to search for.
        max_results: Maximum number of papers to fetch (default 100).
        submitted_after: Only fetch papers submitted after this date
            (format: YYYY-MM-DD). Used for incremental sync.
        client: Pre-built HTTP client (mainly a test-injection point);
            when omitted ``httpx.Client`` is used.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    search_query = _build_query(query, categories, author, submitted_after)

    if client is None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc
        client = httpx.Client(timeout=30.0)

    @dlt.resource(name=ARXIV_TABLE_NAME, primary_key="arxiv_id", write_disposition="merge")
    def arxiv_papers():
        count = 0
        for paper in _iter_papers(client, search_query, max_results):
            count += 1
            yield _paper_to_row(paper)
        logger.info("arXiv: synced %d paper(s).", count)

    @dlt.source(name=ARXIV_SOURCE_NAME)
    def _arxiv():
        return arxiv_papers

    source = _arxiv()
    setattr(source, DOCUMENT_SOURCE_ATTR, ARXIV_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


def _build_query(
    query: str | None,
    categories: list[str] | None,
    author: str | None,
    submitted_after: str | None,
) -> str:
    """Build an arXiv search_query string from the given parameters."""
    parts: list[str] = []

    if query:
        parts.append(f"all:{urllib.parse.quote(query)}")

    if categories:
        cat_parts = [f"cat:{cat}" for cat in categories]
        if len(cat_parts) > 1:
            parts.append("(" + "+OR+".join(cat_parts) + ")")
        else:
            parts.append(cat_parts[0])

    if author:
        parts.append(f"au:{urllib.parse.quote(author)}")

    if submitted_after:
        parts.append(f"submittedDate:[{submitted_after}000000+TO+99999999999999]")

    if not parts:
        parts.append("all:computer science")

    return "+AND+".join(parts)


# ---------------------------------------------------------------------------
# arXiv API helpers (module-private)
# ---------------------------------------------------------------------------


def _iter_papers(client, search_query: str, max_results: int):
    """Yield raw arXiv paper dicts, paginating through the API."""
    start = 0
    page_size = min(max_results, 100)

    while start < max_results:
        params = {
            "search_query": search_query,
            "start": str(start),
            "max_results": str(min(page_size, max_results - start)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }

        response = _request(client, params)

        total = _parse_total_results(response)
        entries = _parse_entries(response)

        if not entries:
            return

        for entry in entries:
            yield entry

        start += len(entries)

        if start >= total or start >= max_results:
            return

        # Respect arXiv rate limit before the next page.
        time.sleep(_REQUEST_INTERVAL)


def _request(client, params: dict) -> str:
    """Make a rate-limited request to the arXiv API."""
    response = client.get(ARXIV_API_URL, params=params)
    response.raise_for_status()
    return response.text


def _parse_total_results(xml_text: str) -> int:
    """Extract totalResults from the arXiv Atom feed."""
    root = ET.fromstring(xml_text)
    total_elem = root.find("opensearch:totalResults", _NAMESPACES)
    if total_elem is not None and total_elem.text:
        return int(total_elem.text)
    return 0


def _parse_entries(xml_text: str) -> list[dict]:
    """Parse arXiv Atom entries into paper dicts."""
    root = ET.fromstring(xml_text)
    entries = []

    for entry in root.findall("atom:entry", _NAMESPACES):
        entries.append(_parse_entry(entry))

    return entries


def _parse_entry(entry: ET.Element) -> dict:
    """Parse a single Atom entry into a paper dict."""
    def _text(tag: str, ns: str = "atom") -> str:
        elem = entry.find(f"{ns}:{tag}", _NAMESPACES)
        return elem.text.strip() if elem is not None and elem.text else ""

    def _attr(tag: str, attr: str, ns: str = "atom") -> str:
        elem = entry.find(f"{ns}:{tag}", _NAMESPACES)
        return elem.get(attr, "") if elem is not None else ""

    arxiv_id = _text("id")
    # Strip the URL prefix to get the bare arXiv ID.
    if arxiv_id.startswith("http://arxiv.org/abs/"):
        arxiv_id = arxiv_id[len("http://arxiv.org/abs/"):]

    # Remove version suffix for the primary key (v1, v2, etc.)
    base_id = arxiv_id.split("v")[0] if "v" in arxiv_id else arxiv_id

    authors = []
    for author_elem in entry.findall("atom:author", _NAMESPACES):
        name_elem = author_elem.find("atom:name", _NAMESPACES)
        if name_elem is not None and name_elem.text:
            authors.append(name_elem.text.strip())

    primary_category = _attr("primary_category", "term", "arxiv")

    categories = []
    for cat_elem in entry.findall("atom:category", _NAMESPACES):
        term = cat_elem.get("term", "")
        if term and term not in categories:
            categories.append(term)

    return {
        "arxiv_id": base_id,
        "arxiv_url": _text("id"),
        "title": _text("title").replace("\n", " ").strip(),
        "abstract": _text("summary").replace("\n", " ").strip(),
        "authors": ", ".join(authors),
        "primary_category": primary_category,
        "categories": ", ".join(categories),
        "published": _text("published"),
        "updated": _text("updated"),
        "doi": _text("doi", "arxiv"),
        "comment": _text("comment", "arxiv"),
        "journal_ref": _text("journal_ref", "arxiv"),
    }


def _paper_to_row(paper: dict) -> dict:
    """Flatten an arXiv paper into a document row for cognee."""
    content_parts = [
        f"# {paper['title']}",
        "",
        f"**Authors:** {paper['authors']}",
        f"**Categories:** {paper['categories']}",
        f"**Published:** {paper['published']}",
        "",
        "## Abstract",
        "",
        paper["abstract"],
    ]

    if paper.get("doi"):
        content_parts.extend(["", f"DOI: {paper['doi']}"])
    if paper.get("comment"):
        content_parts.extend(["", f"Comment: {paper['comment']}"])

    return {
        "id": paper["arxiv_id"],
        "url": paper["arxiv_url"],
        "title": paper["title"],
        "content": "\n".join(content_parts),
    }
