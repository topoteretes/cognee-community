"""Complete-snapshot DLT source for arXiv metadata and abstracts."""

from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("arxiv_connector")

ARXIV_SOURCE_NAME = "arxiv"
ARXIV_TABLE_NAME = "arxiv_papers"
_API_URL = "https://export.arxiv.org/api/query"
_ATOM = "http://www.w3.org/2005/Atom"
_OPEN_SEARCH = "http://a9.com/-/spec/opensearch/1.1/"
_ARXIV = "http://arxiv.org/schemas/atom"
_PAGE_LIMIT = 2_000
_RESULT_LIMIT = 30_000
_REQUEST_INTERVAL = 3.0
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT: float | None = None
_CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_VERSION_RE = re.compile(r"v[0-9]+$")


class ArxivAPIError(RuntimeError):
    """Raised when arXiv cannot provide a complete, valid query snapshot."""


def arxiv_source(
    categories: list[str] | None = None,
    authors: list[str] | None = None,
    submitted_date_range: tuple[str, str] | None = None,
    page_size: int = 200,
    client: Any = None,
):
    """Create a DLT source containing a complete snapshot of an arXiv query.

    Select at least one category or author. ``submitted_date_range`` accepts
    two UTC bounds formatted as ``YYYYMMDDHHMM``. ``client`` is an optional
    query-page client for offline tests; normal use requires no credentials.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "Install cognee-community-connector-arxiv to use the arXiv connector."
        ) from exc

    selected_categories = _validate_categories(categories)
    selected_authors = _validate_authors(authors)
    date_range = _validate_date_range(submitted_date_range)
    if not selected_categories and not selected_authors:
        raise ValueError("Select at least one arXiv category or author.")
    if not 1 <= page_size <= _PAGE_LIMIT:
        raise ValueError(f"page_size must be between 1 and {_PAGE_LIMIT}.")

    query = _build_query(selected_categories, selected_authors, date_range)
    api = client if client is not None else _ArxivClient()

    @dlt.resource(name=ARXIV_TABLE_NAME, primary_key="id", write_disposition="replace")
    def arxiv_papers():
        count = 0
        for paper in _iter_snapshot(api, query, page_size):
            count += 1
            yield paper
        logger.info("arXiv: completed snapshot of %d paper(s).", count)

    @dlt.source(name=ARXIV_SOURCE_NAME)
    def _arxiv():
        return arxiv_papers

    source = _arxiv()
    setattr(source, DOCUMENT_SOURCE_ATTR, ARXIV_SOURCE_NAME)
    return source


def _validate_categories(categories: list[str] | None) -> list[str]:
    if categories is not None and not isinstance(categories, list):
        raise ValueError("categories must be a list of arXiv category identifiers.")
    values = categories or []
    if any(not isinstance(value, str) or not _CATEGORY_RE.fullmatch(value) for value in values):
        raise ValueError("Categories must be arXiv category identifiers such as 'cs.AI'.")
    return list(dict.fromkeys(values))


def _validate_authors(authors: list[str] | None) -> list[str]:
    if authors is not None and not isinstance(authors, list):
        raise ValueError("authors must be a list of author names.")
    values = authors or []
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("Author filters must be non-empty strings.")
    return list(dict.fromkeys(value.strip() for value in values))


def _validate_date_range(date_range: tuple[str, str] | None) -> tuple[str, str] | None:
    if date_range is None:
        return None
    if not isinstance(date_range, tuple) or len(date_range) != 2:
        raise ValueError("submitted_date_range must be a pair of YYYYMMDDHHMM strings.")
    start, end = date_range
    try:
        start_time = time.strptime(start, "%Y%m%d%H%M")
        end_time = time.strptime(end, "%Y%m%d%H%M")
    except (TypeError, ValueError) as exc:
        raise ValueError("submitted_date_range bounds must use YYYYMMDDHHMM in UTC.") from exc
    if start_time > end_time:
        raise ValueError("submitted_date_range start must be no later than its end.")
    return start, end


def _build_query(
    categories: list[str], authors: list[str], date_range: tuple[str, str] | None
) -> str:
    terms: list[str] = []
    if categories:
        terms.append("(" + " OR ".join(f"cat:{category}" for category in categories) + ")")
    if authors:
        author_terms = []
        for author in authors:
            escaped = author.replace("\\", "\\\\").replace('"', '\\"')
            author_terms.append(f'au:"{escaped}"')
        terms.append("(" + " OR ".join(author_terms) + ")")
    if date_range:
        terms.append(f"submittedDate:[{date_range[0]} TO {date_range[1]}]")
    return " AND ".join(terms)


class _ArxivClient:
    """Small synchronous client with a process-wide serialized request gate."""

    def query_page(self, query: str, start: int, page_size: int) -> bytes:
        global _LAST_REQUEST_AT

        params = urllib.parse.urlencode(
            {
                "search_query": query,
                "start": start,
                "max_results": page_size,
                "sortBy": "submittedDate",
                "sortOrder": "ascending",
            }
        )
        request = urllib.request.Request(
            f"{_API_URL}?{params}", headers={"User-Agent": "cognee-community-connector-arxiv/0.1"}
        )
        with _REQUEST_LOCK:
            now = time.monotonic()
            if _LAST_REQUEST_AT is not None:
                time.sleep(max(0.0, _REQUEST_INTERVAL - (now - _LAST_REQUEST_AT)))
            _LAST_REQUEST_AT = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                raise ArxivAPIError(
                    f"arXiv API returned HTTP {exc.code} for result page starting at {start}."
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ArxivAPIError(
                    f"Could not retrieve arXiv result page starting at {start}: {exc}"
                ) from exc


def _iter_snapshot(client: Any, query: str, page_size: int) -> Iterator[dict[str, Any]]:
    """Yield all query results, rejecting inconsistent or incomplete pages."""
    start = 0
    expected_total: int | None = None
    provider_page_size: int | None = None
    seen_ids: set[str] = set()

    while expected_total is None or start < expected_total:
        payload = client.query_page(query, start, page_size)
        page = _parse_page(payload, requested_start=start, page_size=page_size)
        if expected_total is None:
            expected_total = page["total"]
            if expected_total > _RESULT_LIMIT:
                raise ArxivAPIError(
                    f"arXiv query matched {expected_total} results, above the API limit of "
                    f"{_RESULT_LIMIT}. Narrow the selection with fewer categories/authors or "
                    "a submitted_date_range; no snapshot was loaded."
                )
        elif page["total"] != expected_total:
            raise ArxivAPIError(
                "arXiv query results changed while paging; retry the complete snapshot."
            )

        remaining = expected_total - start
        reported_page_size = page["items_per_page"]
        if reported_page_size == 0 and remaining > 0:
            raise ArxivAPIError(
                f"arXiv made no pagination progress at offset {start}; "
                "refusing an incomplete snapshot."
            )
        if reported_page_size < page_size:
            if provider_page_size is None:
                provider_page_size = reported_page_size
            elif reported_page_size != provider_page_size:
                raise ArxivAPIError(
                    "arXiv changed its effective page size while paging; "
                    "retry the complete snapshot."
                )
        elif provider_page_size is not None and remaining > provider_page_size:
            raise ArxivAPIError(
                "arXiv changed its effective page size while paging; retry the complete snapshot."
            )

        expected_page_size = provider_page_size or page_size
        expected_count = min(expected_page_size, remaining)
        if len(page["papers"]) != expected_count:
            raise ArxivAPIError(
                f"arXiv returned {len(page['papers'])} entries at offset {start}; "
                f"expected {expected_count} from the query total and effective page size. "
                "Refusing an incomplete snapshot."
            )

        for paper in page["papers"]:
            if paper["id"] in seen_ids:
                raise ArxivAPIError(
                    f"arXiv returned duplicate paper id {paper['id']!r}; "
                    "refusing an incomplete snapshot."
                )
            seen_ids.add(paper["id"])
            yield paper
        start += len(page["papers"])


def _parse_page(payload: bytes, requested_start: int, page_size: int) -> dict[str, Any]:
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, TypeError) as exc:
        raise ArxivAPIError(
            f"arXiv returned malformed XML for result page at {requested_start}."
        ) from exc
    if root.tag != f"{{{_ATOM}}}feed":
        raise ArxivAPIError("arXiv returned an unexpected XML document instead of an Atom feed.")

    total = _integer_child(root, f"{{{_OPEN_SEARCH}}}totalResults", "totalResults")
    start_index = _integer_child(root, f"{{{_OPEN_SEARCH}}}startIndex", "startIndex")
    items_per_page = _integer_child(root, f"{{{_OPEN_SEARCH}}}itemsPerPage", "itemsPerPage")
    if total < 0 or start_index != requested_start:
        raise ArxivAPIError(f"arXiv returned invalid paging metadata at offset {requested_start}.")
    entries = root.findall(f"{{{_ATOM}}}entry")
    # arXiv echoes max_results in itemsPerPage, including on a short final page
    # (for example, max_results=300, itemsPerPage=300, but 230 final entries).
    # Other OpenSearch providers may report a smaller effective page size. The
    # iterator reconciles either convention against actual entries and total.
    if items_per_page < 0 or items_per_page > page_size:
        raise ArxivAPIError(
            f"arXiv returned invalid itemsPerPage metadata at offset {requested_start}."
        )
    _text_child(root, f"{{{_ATOM}}}updated", "feed updated")
    papers = [_entry_to_row(entry) for entry in entries]
    return {
        "total": total,
        "items_per_page": items_per_page,
        "papers": papers,
    }


def _integer_child(element: ET.Element, tag: str, label: str) -> int:
    value = _text_child(element, tag, label)
    try:
        return int(value)
    except ValueError as exc:
        raise ArxivAPIError(f"arXiv returned invalid {label} value {value!r}.") from exc


def _text_child(element: ET.Element, tag: str, label: str) -> str:
    child = element.find(tag)
    value = " ".join("".join(child.itertext()).split()) if child is not None else ""
    if not value:
        raise ArxivAPIError(f"arXiv response is missing {label}.")
    return value


def _entry_to_row(entry: ET.Element) -> dict[str, Any]:
    source_url = _text_child(entry, f"{{{_ATOM}}}id", "paper id")
    parsed_url = urllib.parse.urlparse(source_url)
    if parsed_url.hostname not in {"arxiv.org", "export.arxiv.org"}:
        raise ArxivAPIError(f"arXiv returned an unexpected paper identifier {source_url!r}.")
    if not parsed_url.path.startswith("/abs/"):
        raise ArxivAPIError(f"arXiv returned an invalid paper identifier {source_url!r}.")
    raw_id = parsed_url.path.removeprefix("/abs/").strip("/")
    if not raw_id:
        raise ArxivAPIError(f"arXiv returned an invalid paper identifier {source_url!r}.")
    paper_id = _VERSION_RE.sub("", raw_id)

    title = _text_child(entry, f"{{{_ATOM}}}title", "paper title")
    abstract = _text_child(entry, f"{{{_ATOM}}}summary", "paper abstract")
    published = _text_child(entry, f"{{{_ATOM}}}published", "paper published date")
    updated = _text_child(entry, f"{{{_ATOM}}}updated", "paper updated date")
    authors = [
        " ".join("".join(name.itertext()).split())
        for name in entry.findall(f"{{{_ATOM}}}author/{{{_ATOM}}}name")
    ]
    categories = [
        category.attrib["term"]
        for category in entry.findall(f"{{{_ATOM}}}category")
        if category.attrib.get("term")
    ]
    primary_category = entry.find(f"{{{_ARXIV}}}primary_category")
    if primary_category is not None and primary_category.attrib.get("term"):
        primary = primary_category.attrib["term"]
        if primary not in categories:
            categories.insert(0, primary)

    content = _render_paper(authors, categories, published, updated, abstract)
    return {
        "id": paper_id,
        "url": f"https://arxiv.org/abs/{paper_id}",
        "title": title,
        "content": content,
    }


def _render_paper(
    authors: list[str],
    categories: list[str],
    published: str,
    updated: str,
    abstract: str,
) -> str:
    lines: list[str] = []
    if authors:
        lines.append(f"Authors: {', '.join(authors)}")
    if categories:
        lines.append(f"Categories: {', '.join(categories)}")
    if lines:
        lines.append("")
    lines.extend([f"Submitted: {published}", f"Updated: {updated}", "", "## Abstract", abstract])
    return "\n".join(lines).strip()
