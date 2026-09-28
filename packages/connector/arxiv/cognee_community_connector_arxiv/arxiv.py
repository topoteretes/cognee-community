"""DLT source for arXiv papers (full-snapshot sync + forget-on-delete).

Fetches paper metadata and abstracts from the arXiv Atom API and yields them as
a dlt resource for cognee's ingestion pipeline.

Like the Notion and Slack connectors, papers are ingested as *normal documents*:
the source declares ``cognee_document_source = "arxiv"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "arxiv"``
(not ``"dlt"``). ``is_dlt_sourced`` therefore returns False and each paper flows
through the standard cognify entity-extraction pipeline — the right treatment
for prose abstracts — instead of the deterministic dlt-row schema-context path.

Sync model
----------
The source is a full snapshot of a *query*, not of all of arXiv:
``write_disposition="replace"`` rewrites staging with exactly the papers the
configured query matches on each run. This is what makes the two acceptance
criteria compatible rather than contradictory:

* **Forget-on-delete** falls out for free. A withdrawn or reclassified paper
  stops matching the query, so it is absent from the snapshot and cognee's
  existing ``orphan_cleanup`` removes it from the graph and vector stores.
  arXiv has no delete feed, so — as with Notion — absence is the only available
  deletion signal.
* **Incremental** is handled by content hashing, not by a shrinking cursor.
  Unchanged papers produce a byte-identical row, keep a stable content-hash
  ``data_id``, and are therefore not re-ingested or re-cognified. A cursor that
  narrowed the query each run would be actively wrong here: under ``replace``,
  fetching only new papers would drop every previously-synced paper out of the
  snapshot and cognee would forget the entire back catalogue.

``submitted_from`` / ``submitted_to`` therefore *bound the corpus* ("cs.AI
papers from 2026"), and are deliberately not advanced automatically between
runs.

Row shape is restricted to ``id``/``url``/``title``/``content`` and the URL is
stored version-stripped, so a new version that does not change the title or
abstract does not churn the content-hash ``data_id``. A v2 whose abstract *did*
change produces different content and is correctly re-ingested.
"""

import re
import time
from collections.abc import Callable, Iterator
from typing import Any
from xml.etree import ElementTree

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("arxiv_connector")

# dlt resource / staging-table name for arXiv papers.
ARXIV_TABLE_NAME = "arxiv_papers"
ARXIV_SOURCE_NAME = "arxiv"

# HTTPS endpoint (arXiv's docs still advertise http://; https works and is used
# here so the feed cannot be tampered with in transit).
ARXIV_API_URL = "https://export.arxiv.org/api/query"

# arXiv's terms ask for no more than one request every 3 seconds. This is built
# in rather than discovered: exceeding it earns a 403 from their rate limiter.
MIN_REQUEST_INTERVAL = 3.0

# Results per request. arXiv permits up to 2000 but recommends smaller pages;
# 100 keeps each request well inside their timeout.
_PAGE_SIZE = 100

# Retry budget for rate-limited / transient API responses.
_MAX_RETRIES = 5

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"

# Trailing version marker on an arXiv identifier: "2101.00001v2" -> "2101.00001",
# and the pre-2007 form "math/0309136v1" -> "math/0309136".
_VERSION_SUFFIX = re.compile(r"v\d+$")

_EXTRA_HINT = (
    'The arXiv connector requires the "arxiv" extra: pip install "cognee[arxiv]" '
    "(provides dlt and httpx)."
)


def arxiv_source(
    categories: list[str] | None = None,
    authors: list[str] | None = None,
    search_query: str | None = None,
    submitted_from: str | None = None,
    submitted_to: str | None = None,
    max_results: int | None = None,
    fetch: Callable[[dict[str, Any]], str] | None = None,
):
    """Create a dlt source that yields arXiv papers as markdown documents.

    Args:
        categories: arXiv categories to ingest, e.g. ``["cs.AI", "cs.CL"]``.
            Combined with OR.
        authors: Author names to ingest. Combined with OR, and ANDed with
            ``categories`` when both are given.
        search_query: A raw arXiv query string. When given it replaces anything
            built from ``categories``/``authors`` (the submitted-date window is
            still applied), as an escape hatch for arXiv's full query syntax.
        submitted_from: Inclusive lower bound on submission date, ``YYYYMMDD``.
        submitted_to: Inclusive upper bound on submission date, ``YYYYMMDD``.
        max_results: Stop after this many papers. Omit to fetch every match.
        fetch: Callable taking the query params and returning the Atom XML body
            (mainly a test-injection point); when omitted, httpx is used.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.

    Raises:
        ValueError: if no query was specified at all — an unbounded sync of all
            of arXiv is never what the caller meant.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    query = _build_query(categories, authors, search_query, submitted_from, submitted_to)
    fetch_fn = fetch or _default_fetch
    # Per-source throttle state: module-level state would leak the last-request
    # timestamp between unrelated sources (and between tests).
    throttle = _Throttle(MIN_REQUEST_INTERVAL)

    @dlt.resource(name=ARXIV_TABLE_NAME, primary_key="id", write_disposition="replace")
    def arxiv_papers():
        # Full-snapshot sync: each run replaces staging with exactly the papers
        # matching `query` now. A paper that stops matching (withdrawn,
        # reclassified) is absent from the snapshot and orphan_cleanup forgets
        # it. Unchanged papers keep a stable content-hash data_id, so they are
        # not re-ingested/re-cognified.
        #
        # A fetch or parse error is NOT swallowed: because staging is
        # authoritative (replace), a partial snapshot would forget every paper
        # that failed to arrive. Letting the error abort the run leaves staging
        # — and memory — untouched, which is the safe failure. Transient blips
        # are already retried in _request; only a persistent failure reaches
        # here.
        count = 0
        for entry in _iter_entries(fetch_fn, query, throttle, max_results):
            count += 1
            yield _entry_to_row(entry)
        logger.info("arXiv: synced %d paper(s).", count)

    @dlt.source(name=ARXIV_SOURCE_NAME)
    def _arxiv():
        return arxiv_papers

    source = _arxiv()
    # Opt into the document ingestion path (paper -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, ARXIV_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


def _build_query(
    categories: list[str] | None,
    authors: list[str] | None,
    search_query: str | None,
    submitted_from: str | None,
    submitted_to: str | None,
) -> str:
    """Assemble an arXiv ``search_query`` from the configured scope."""
    clauses: list[str] = []

    if search_query:
        clauses.append(f"({search_query})")
    else:
        if categories:
            clauses.append(_or_clause("cat", categories))
        if authors:
            clauses.append(_or_clause("au", authors))

    window = _date_window(submitted_from, submitted_to)
    if window:
        clauses.append(window)

    if not clauses:
        raise ValueError(
            "An arXiv query is required: pass categories=, authors=, search_query=, "
            "or a submitted_from/submitted_to window. Syncing all of arXiv is not "
            "supported."
        )
    return " AND ".join(clauses)


def _or_clause(field: str, values: list[str]) -> str:
    """Build ``(field:a OR field:b)``, quoting values that contain spaces."""
    terms = [f'{field}:"{v}"' if " " in v else f"{field}:{v}" for v in values]
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def _date_window(submitted_from: str | None, submitted_to: str | None) -> str:
    """Build a ``submittedDate:[from TO to]`` clause, or "" when unbounded.

    arXiv wants ``YYYYMMDDHHMM``; callers pass plain ``YYYYMMDD`` and the bounds
    are widened to cover the whole day at each end.
    """
    if not submitted_from and not submitted_to:
        return ""
    start = _as_timestamp(submitted_from, "0000") if submitted_from else "190001010000"
    end = _as_timestamp(submitted_to, "2359") if submitted_to else "299912312359"
    return f"submittedDate:[{start} TO {end}]"


def _as_timestamp(value: str, time_suffix: str) -> str:
    """Normalize ``YYYYMMDD`` (or an already-full ``YYYYMMDDHHMM``) for arXiv."""
    digits = value.replace("-", "").strip()
    if len(digits) == 12:
        return digits
    if len(digits) == 8:
        return digits + time_suffix
    raise ValueError(f"Expected a YYYYMMDD or YYYYMMDDHHMM date, got {value!r}.")


# ---------------------------------------------------------------------------
# arXiv API helpers (module-private)
# ---------------------------------------------------------------------------


class _Throttle:
    """Spaces requests at least ``interval`` seconds apart.

    arXiv asks for one request per 3 seconds. The first call never sleeps; each
    later call sleeps only the remainder actually owed, so parsing time counts
    toward the interval instead of being added to it.
    """

    def __init__(self, interval: float, sleep: Callable[[float], None] = time.sleep):
        self._interval = interval
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self._last is not None:
            owed = self._interval - (now - self._last)
            if owed > 0:
                self._sleep(owed)
        self._last = time.monotonic()


def _default_fetch(params: dict[str, Any]) -> str:
    """Fetch one page of Atom XML from the arXiv API."""
    try:
        import httpx
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    response = httpx.get(ARXIV_API_URL, params=params, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    return response.text


def _request(fetch: Callable[[dict[str, Any]], str], params: dict[str, Any]) -> str:
    """Fetch a page, retrying rate-limit / transient errors.

    arXiv answers over-rate clients with 403 and transient upstream problems
    with 5xx; neither should abort a sync while a retry budget remains.
    Permanent errors and an exhausted budget propagate so the caller can decide.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            return fetch(params)
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            delay = _retry_after(exc, attempt)
            logger.warning(
                "arXiv: %s — retrying in %.1fs (%d/%d).", exc, delay, attempt + 1, _MAX_RETRIES
            )
            time.sleep(delay)
    raise AssertionError("unreachable: the retry loop always returns or raises")


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / timeout / network errors worth retrying."""
    try:
        import httpx
    except ImportError:
        return False

    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (403, 429, 500, 502, 503, 504)
    return False


def _retry_after(exc: Exception, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    header = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        # Never back off below arXiv's own minimum spacing.
        return max(float(2**attempt), MIN_REQUEST_INTERVAL)


def _iter_entries(
    fetch: Callable[[dict[str, Any]], str],
    query: str,
    throttle: _Throttle,
    max_results: int | None,
) -> Iterator[ElementTree.Element]:
    """Yield Atom ``<entry>`` elements across arXiv's offset pagination."""
    start = 0
    seen = 0
    while True:
        page_size = _PAGE_SIZE
        if max_results is not None:
            page_size = min(page_size, max_results - seen)
            if page_size <= 0:
                return

        throttle.wait()
        body = _request(
            fetch,
            {
                "search_query": query,
                "start": start,
                "max_results": page_size,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
        )
        entries, total = _parse_feed(body)
        if not entries:
            return

        yield from entries
        seen += len(entries)
        start += len(entries)

        # Stop on a short page (the last one), on reaching the reported total,
        # or on the caller's cap. The short-page check also protects against a
        # feed that reports a total it never delivers.
        if len(entries) < page_size:
            return
        if total is not None and start >= total:
            return
        if max_results is not None and seen >= max_results:
            return


def _parse_feed(body: str) -> tuple[list[ElementTree.Element], int | None]:
    """Parse an Atom feed into its entries plus the reported total, if any.

    Raises:
        RuntimeError: if the feed is arXiv's error document. arXiv reports a
            malformed query as HTTP 200 with a single entry titled "Error", so
            without this check the error text would be ingested as if it were a
            paper.
    """
    # ElementTree does not resolve external entities, and the feed is fetched
    # from arXiv over HTTPS, so stdlib parsing is appropriate here.
    root = ElementTree.fromstring(body)
    entries = root.findall(f"{_ATOM}entry")

    if len(entries) == 1 and _text(entries[0], f"{_ATOM}title") == "Error":
        raise RuntimeError(f"arXiv rejected the query: {_text(entries[0], f'{_ATOM}summary')}")

    total_text = root.findtext(f"{_OPENSEARCH}totalResults")
    try:
        total = int(total_text) if total_text is not None else None
    except ValueError:
        total = None
    return entries, total


# ---------------------------------------------------------------------------
# Entry -> row
# ---------------------------------------------------------------------------


def _entry_to_row(entry: ElementTree.Element) -> dict[str, Any]:
    """Flatten an Atom entry into a document row.

    Only ``id``/``url``/``title``/``content`` are kept, and both identifiers are
    version-stripped, so a new version that leaves the title and abstract
    unchanged does not churn the content-hash data_id.
    """
    paper_id = _paper_id(entry)
    return {
        "id": paper_id,
        "url": f"https://arxiv.org/abs/{paper_id}" if paper_id else "",
        "title": _clean(_text(entry, f"{_ATOM}title")),
        "content": _render_entry(entry),
    }


def _paper_id(entry: ElementTree.Element) -> str:
    """Extract the version-stripped arXiv id from an entry's id URL."""
    raw = _text(entry, f"{_ATOM}id")
    if not raw:
        return ""
    # "https://arxiv.org/abs/2101.00001v2" -> "2101.00001"; the pre-2007 form
    # "https://arxiv.org/abs/math/0309136v1" keeps its archive prefix.
    ident = raw.split("/abs/", 1)[1] if "/abs/" in raw else raw.rsplit("/", 1)[-1]
    return _VERSION_SUFFIX.sub("", ident)


def _render_entry(entry: ElementTree.Element) -> str:
    """Render a paper's provenance block and abstract to markdown.

    Deliberately *without* the title: ``_build_document_data_item`` prepends
    ``# {title}`` when it turns the row into a document, so emitting the title
    here too would give every paper a duplicated heading.
    """
    authors = _authors(entry)
    categories = _categories(entry)
    published = _text(entry, f"{_ATOM}published")
    abstract = _clean(_text(entry, f"{_ATOM}summary"))

    lines: list[str] = []
    if authors:
        lines.append(f"**Authors:** {', '.join(authors)}")
    if categories:
        lines.append(f"**Categories:** {', '.join(categories)}")
    if published:
        lines.append(f"**Published:** {published}")
    if abstract:
        # Blank line so the abstract reads as a paragraph rather than another
        # metadata field — but only when there is a metadata block above it.
        if lines:
            lines.append("")
        lines.append(abstract)
    return "\n".join(lines)


def _authors(entry: ElementTree.Element) -> list[str]:
    """Author display names, in feed order, skipping empty entries."""
    names = [_clean(_text(a, f"{_ATOM}name")) for a in entry.findall(f"{_ATOM}author")]
    return [n for n in names if n]


def _categories(entry: ElementTree.Element) -> list[str]:
    """Category terms, primary first, without duplicates."""
    terms: list[str] = []
    primary = entry.find(f"{_ARXIV}primary_category")
    if primary is not None and primary.get("term"):
        terms.append(primary.get("term", ""))
    for category in entry.findall(f"{_ATOM}category"):
        term = category.get("term")
        if term and term not in terms:
            terms.append(term)
    return terms


def _text(element: ElementTree.Element | None, path: str) -> str:
    """Text of a child element, or "" when absent."""
    if element is None:
        return ""
    return (element.findtext(path) or "").strip()


def _clean(value: str) -> str:
    """Collapse the newlines and padding arXiv wraps its text fields with."""
    return " ".join(value.split())
