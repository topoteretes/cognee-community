"""DLT source for arXiv papers (full-snapshot sync + forget-on-delete).

Fetches arXiv paper metadata and abstracts by category or author, then yields
them as a dlt resource for cognee's ingestion pipeline.

Like the Notion connector, arXiv papers are ingested as *normal documents*:
the source declares ``cognee_document_source = "arxiv"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "arxiv"``
(not ``"dlt"``).  ``is_dlt_sourced`` therefore returns False and each paper
flows through the standard cognify entity-extraction pipeline.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging
with exactly the papers matching the current query each run.  Unchanged papers
keep a stable content-hash ``data_id``, so they are not re-ingested or
re-cognified.  A paper that drops out of the query (e.g. because the user
changed the category filter) is removed by cognee's ``orphan_cleanup`` on the
next sync.  Incremental sync is supported via the ``date_from`` / ``date_to``
parameters, which restrict results to papers submitted in a given date range.

The arXiv API is rate-limited to roughly one request per 3 seconds; the
connector builds this delay in.
"""

import time
from typing import Any
from xml.etree import ElementTree

import httpx

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("arxiv_connector")

# dlt resource / staging-table name for arXiv papers.
ARXIV_TABLE_NAME = "arxiv_papers"
ARXIV_SOURCE_NAME = "arxiv"

# arXiv API endpoint.
_ARXIV_API_URL = "https://export.arxiv.org/api/query"

# Rate limit: ~1 request per 3 seconds.
_ARXIV_RATE_LIMIT = 3.0

# Max results per page (arXiv allows up to 2000, but we use 100 for reasonable
# per-request latency and to avoid overwhelming the parser).
_ARXIV_PAGE_SIZE = 100

# Retry budget for transient API errors.
_MAX_RETRIES = 4

# Atom + arXiv XML namespaces.
_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"
_OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"

# Namespace prefix map for ElementTree.
_NS = {
    "atom": _ATOM_NS,
    "arxiv": _ARXIV_NS,
    "opensearch": _OPENSEARCH_NS,
}


def arxiv_source(
    categories: list[str] | None = None,
    author: str | None = None,
    max_results: int = 500,
    date_from: str | None = None,
    date_to: str | None = None,
    client: httpx.Client | None = None,
):
    """Create a dlt source that yields arXiv papers as documents.

    Args:
        categories: arXiv category ids to search, e.g. ``["cs.AI", "cs.LG"]``.
            When omitted, all categories are searched.
        author: Restrict to papers by this author (last name or full name).
        max_results: Maximum number of papers to fetch (default 500, max 2000).
        date_from: Earliest submission date (inclusive), ISO format e.g.
            ``"2024-01-01"``.  When omitted, no lower bound.
        date_to: Latest submission date (inclusive), ISO format e.g.
            ``"2024-12-31"``.  When omitted, no upper bound.
        client: Pre-built ``httpx.Client`` (mainly a test-injection point); when
            omitted one is built with default settings.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The arXiv connector requires the "arxiv" extra: pip install "cognee[arxiv]" '
            "(provides dlt and httpx)."
        ) from exc

    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(30.0))

    resolved_categories = categories or []
    resolved_max = min(max_results, 2000)

    @dlt.resource(name=ARXIV_TABLE_NAME, primary_key="id", write_disposition="replace")
    def arxiv_papers():
        count = 0
        for paper in _fetch_papers(
            client=client,
            categories=resolved_categories,
            author=author,
            max_results=resolved_max,
            date_from=date_from,
            date_to=date_to,
        ):
            count += 1
            yield paper
        logger.info("arXiv: synced %d paper(s).", count)

    @dlt.source(name=ARXIV_SOURCE_NAME)
    def _arxiv():
        return arxiv_papers

    source = _arxiv()
    setattr(source, DOCUMENT_SOURCE_ATTR, ARXIV_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# arXiv API helpers (module-private)
# ---------------------------------------------------------------------------


def _build_query(
    categories: list[str],
    author: str | None,
    date_from: str | None,
    date_to: str | None,
) -> str:
    """Build the arXiv API search_query parameter.

    Combines category filters, author filter, and date range into a single
    query string using arXiv's search syntax.
    """
    parts: list[str] = []

    if categories:
        cat_parts = [f"cat:{cat}" for cat in categories]
        if len(cat_parts) == 1:
            parts.append(cat_parts[0])
        else:
            parts.append(f"({' OR '.join(cat_parts)})")

    if author:
        parts.append(f'au:{author}')

    if date_from or date_to:
        # arXiv API supports date-range filtering via the search_query, but
        # the syntax is limited.  We'll filter in the query and also do
        # client-side filtering on the results for accuracy.
        pass

    return " AND ".join(parts) if parts else "*"


def _fetch_papers(
    client: httpx.Client,
    categories: list[str],
    author: str | None,
    max_results: int,
    date_from: str | None,
    date_to: str | None,
) -> list[dict]:
    """Fetch papers from the arXiv API with pagination and rate limiting.

    Yields paper dicts, one per paper.
    """
    search_query = _build_query(categories, author, date_from, date_to)
    start = 0

    while start < max_results:
        page_size = min(_ARXIV_PAGE_SIZE, max_results - start)
        url = _build_url(search_query, start, page_size)

        response = _request_with_retry(client, url)
        if response is None:
            break

        root = _parse_xml(response.text)
        total_results = _get_total_results(root)
        if total_results == 0:
            break

        entries = root.findall(f"atom:entry", _NS)
        for entry in entries:
            paper = _entry_to_paper(entry, date_from, date_to)
            if paper is not None:
                yield paper

        # Check if we got fewer results than requested (last page).
        returned = len(entries)
        if returned < page_size:
            break

        start += page_size

        # Respect rate limit.
        if start < max_results:
            time.sleep(_ARXIV_RATE_LIMIT)


def _build_url(search_query: str, start: int, max_results: int) -> str:
    """Build the arXiv API query URL."""
    from urllib.parse import urlencode, quote

    params = {
        "search_query": search_query,
        "start": str(start),
        "max_results": str(max_results),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    return f"{_ARXIV_API_URL}?{urlencode(params, safe=':')}"


def _request_with_retry(client: httpx.Client, url: str) -> httpx.Response | None:
    """Make a GET request with retry on transient errors."""
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.get(url)
            if response.status_code == 200:
                return response
            if response.status_code in (429, 500, 502, 503, 504):
                delay = _retry_after(response.headers, attempt)
                logger.warning(
                    "arXiv: HTTP %d — retrying in %.1fs (%d/%d).",
                    response.status_code,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            # Permanent error.
            logger.error("arXiv: HTTP %d — %s", response.status_code, response.text[:200])
            return None
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt == _MAX_RETRIES - 1:
                logger.error("arXiv: request failed after %d retries: %s", _MAX_RETRIES, exc)
                return None
            delay = _retry_after(None, attempt)
            logger.warning("arXiv: %s — retrying in %.1fs (%d/%d).", exc, delay, attempt + 1, _MAX_RETRIES)
            time.sleep(delay)
    return None


def _retry_after(headers, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


def _parse_xml(xml_text: str) -> ElementTree.Element:
    """Parse the arXiv API Atom XML response."""
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    return root


def _get_total_results(root: ElementTree.Element) -> int:
    """Extract the totalResults count from the OpenSearch response."""
    elem = root.find("opensearch:totalResults", _NS)
    if elem is not None and elem.text:
        try:
            return int(elem.text)
        except ValueError:
            pass
    return 0


def _entry_to_paper(
    entry: ElementTree.Element,
    date_from: str | None,
    date_to: str | None,
) -> dict | None:
    """Convert an Atom entry to a paper dict.

    Applies date-range filtering (arXiv API date queries are approximate).
    Returns None if the paper is outside the requested date range.
    """
    # Required fields.
    id_elem = entry.find("atom:id", _NS)
    title_elem = entry.find("atom:title", _NS)
    summary_elem = entry.find("atom:summary", _NS)
    published_elem = entry.find("atom:published", _NS)

    if id_elem is None or title_elem is None:
        return None

    arxiv_id = id_elem.text or ""
    # Extract the arXiv ID from the full URL.
    # URL format: http://arxiv.org/abs/XXXX.XXXXX or http://arxiv.org/abs/YY-XXXX
    paper_id = arxiv_id.strip().rsplit("/", 1)[-1] if arxiv_id else ""

    title = _clean_text(title_elem.text or "")
    summary = _clean_text(summary_elem.text or "") if summary_elem is not None else ""
    published = (published_elem.text or "") if published_elem is not None else ""

    # Client-side date filtering.
    if date_from and published:
        if published[:10] < date_from:
            return None
    if date_to and published:
        if published[:10] > date_to:
            return None

    # Authors.
    authors: list[str] = []
    for author_elem in entry.findall("atom:author", _NS):
        name_elem = author_elem.find("atom:name", _NS)
        if name_elem is not None and name_elem.text:
            authors.append(name_elem.text.strip())

    # Categories.
    categories: list[str] = []
    for cat_elem in entry.findall("atom:category", _NS):
        term = cat_elem.get("term", "")
        if term:
            categories.append(term)

    # Primary category.
    primary_cat = ""
    primary_elem = entry.find("arxiv:primary_category", _NS)
    if primary_elem is not None:
        primary_cat = primary_elem.get("term", "")

    # Links.
    abs_url = ""
    pdf_url = ""
    for link_elem in entry.findall("atom:link", _NS):
        rel = link_elem.get("rel", "")
        href = link_elem.get("href", "")
        if rel == "alternate":
            abs_url = href
        elif rel == "related" and href.endswith(".pdf"):
            pdf_url = href

    # Build the content for cognee: title + abstract as markdown.
    content = _build_content(title, summary, authors, categories, primary_cat, abs_url, pdf_url)

    return {
        "id": paper_id,
        "url": abs_url,
        "title": title,
        "content": content,
        "authors": authors,
        "categories": categories,
        "primary_category": primary_cat,
        "published_date": published,
        "pdf_url": pdf_url,
    }


def _clean_text(text: str) -> str:
    """Clean arXiv text: strip whitespace and normalize newlines."""
    import re
    text = text.strip()
    # arXiv sometimes double-wraps text with newlines.
    text = re.sub(r"\n\s+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text


def _build_content(
    title: str,
    summary: str,
    authors: list[str],
    categories: list[str],
    primary_category: str,
    abs_url: str,
    pdf_url: str,
) -> str:
    """Build a markdown document from paper metadata."""
    lines = [
        f"# {title}",
        "",
        f"**Primary Category:** {primary_category}",
        f"**Categories:** {', '.join(categories)}",
        f"**Authors:** {', '.join(authors)}",
        f"**URL:** {abs_url}",
        f"**PDF:** {pdf_url}",
        "",
        "## Abstract",
        "",
        summary,
        "",
    ]
    return "\n".join(lines)