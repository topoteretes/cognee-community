"""PubMed connector for cognee (incremental edat sync + forget-on-deletion).

Searches PubMed through NCBI's E-utilities and ingests each matching article's metadata and
abstract as a Markdown document for cognee's memory layer ("what does the literature say
about CRISPR delivery vectors?").

Document Ingestion Path (Path B):
Like the Notion connector, articles are ingested as documents rather than raw relational
tables: the source declares ``cognee_document_source = "pubmed"`` (via
``DOCUMENT_SOURCE_ATTR``), so ``resolve_dlt_sources`` routes every row through standard
``cognify`` entity extraction.

Search-then-fetch:
E-utilities is a two-step API. ``esearch`` turns a PubMed query into PMIDs and ``efetch``
returns the article XML for batches of them. ESearch never returns more than 10,000 PMIDs
for one query, so larger result sets are split into smaller Entrez-date windows until each
fits, instead of being silently truncated.

Incremental sync:
The Entrez date (``edat``) is the day a record was added to PubMed. The last synced day is
kept in ``dlt.current.resource_state()`` and the next run searches from that day onwards,
skipping PMIDs it already ingested. Changing the query restarts the window from the
configured ``mindate``; PMIDs that are still matched are not fetched again.

Forget-on-delete:
Each run re-lists the PMIDs the query currently matches. Known articles that are gone
(deleted from PubMed, or no longer matched) and known articles that were since retracted are
emitted as ``{"id": ..., "_deleted": True}``, which dlt hard-deletes on merge and cognee's
``orphan_cleanup`` purges from the graph and vector stores. An empty listing while articles
are known is treated as an outage, not a mass deletion, and any API error aborts the run
before state is written.
"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("pubmed_connector")

PUBMED_TABLE_NAME = "pubmed_articles"
PUBMED_SOURCE_NAME = "pubmed"
DEFAULT_API_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL_NAME = "cognee-community-connector-pubmed"

# ESearch will not page past the first 10,000 PMIDs of a query.
MAX_SEARCH_RESULTS = 10_000
MAX_FETCH_BATCH = 200
# PubMed's oldest records are from the late 18th century.
EARLIEST_DATE = date(1700, 1, 1)

_MAX_RETRIES = 5
_RETRY_STATUSES = (500, 502, 503, 504)
# NCBI allows 3 requests/second without an API key and 10 with one; stay just under.
_INTERVAL_WITHOUT_KEY = 0.34
_INTERVAL_WITH_KEY = 0.11

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}


class NCBIError(RuntimeError):
    """E-utilities reported an error in an otherwise successful response."""


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def _parse_date(value: str | date | None) -> date | None:
    """Accept ``date`` objects and ``YYYY/MM/DD`` / ``YYYY-MM-DD`` strings."""
    if value is None or isinstance(value, date):
        return value
    text = value.strip().replace("-", "/")
    try:
        return datetime.strptime(text, "%Y/%m/%d").date()
    except ValueError as exc:
        raise ValueError(f"Expected a date as YYYY/MM/DD, got {value!r}.") from exc


def _fmt(day: date) -> str:
    return day.strftime("%Y/%m/%d")


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def _make_session() -> Any:
    try:
        import requests
    except ImportError as exc:
        raise ImportError(
            'The PubMed connector requires "requests". Install with:\n'
            '    pip install "cognee-community-connector-pubmed"'
        ) from exc

    session = requests.Session()
    session.headers.update({"User-Agent": f"{TOOL_NAME}/0.1.0"})
    return session


def _is_transient_network_error(exc: Exception) -> bool:
    try:
        import requests
    except ImportError:
        return False
    return isinstance(exc, requests.exceptions.Timeout | requests.exceptions.ConnectionError)


def _retry_delay(response: Any, attempt: int) -> float:
    headers = getattr(response, "headers", {}) or {}
    header = headers.get("Retry-After") or headers.get("retry-after")
    if header:
        try:
            return max(0.5, float(header))
        except (TypeError, ValueError):
            pass
    return float(2**attempt)


class NCBIClient:
    """Throttled E-utilities client for the PubMed database.

    Requests are sent as POST so long PMID lists and queries are never truncated in a URL,
    and so the API key stays out of request URLs and access logs.
    """

    def __init__(
        self,
        session: Any = None,
        *,
        api_key: str | None = None,
        email: str | None = None,
        base_url: str = DEFAULT_API_BASE,
    ) -> None:
        self.session = session or _make_session()
        self.api_key = api_key or None
        self.email = email or None
        self.base_url = base_url.rstrip("/")
        self._interval = _INTERVAL_WITH_KEY if self.api_key else _INTERVAL_WITHOUT_KEY
        self._last_request = 0.0

    # -- transport ------------------------------------------------------------

    def _throttle(self) -> None:
        wait = self._last_request + self._interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _post(self, endpoint: str, params: dict[str, Any]) -> Any:
        data = {"db": "pubmed", "tool": TOOL_NAME, **params}
        if self.api_key:
            data["api_key"] = self.api_key
        if self.email:
            data["email"] = self.email
        url = f"{self.base_url}/{endpoint}"

        for attempt in range(_MAX_RETRIES):
            final_attempt = attempt == _MAX_RETRIES - 1
            self._throttle()
            try:
                response = self.session.request("POST", url, data=data, timeout=60.0)
            except Exception as exc:
                if final_attempt or not _is_transient_network_error(exc):
                    raise
                delay = float(2**attempt)
                logger.warning(
                    "PubMed: network error %s — retrying in %.1fs (%d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
                continue

            status = response.status_code
            if status == 429 or status in _RETRY_STATUSES:
                if final_attempt:
                    response.raise_for_status()
                delay = _retry_delay(response, attempt) if status == 429 else float(2**attempt)
                logger.warning(
                    "PubMed: HTTP %d from %s — retrying in %.1fs (%d/%d).",
                    status,
                    endpoint,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
                continue

            if status == 400 and "api key" in (response.text or "").lower():
                raise PermissionError("NCBI rejected the API key. Check NCBI_API_KEY.")
            response.raise_for_status()
            return response

        raise RuntimeError("unreachable")  # pragma: no cover

    # -- E-utilities ----------------------------------------------------------

    def esearch(self, term: str, start: date, end: date, retmax: int) -> tuple[int, list[str]]:
        """Return (total matches, first ``retmax`` PMIDs) for ``term`` within an edat window."""
        body = self._post(
            "esearch.fcgi",
            {
                "term": term,
                "retmode": "json",
                "retmax": retmax,
                "datetype": "edat",
                "mindate": _fmt(start),
                "maxdate": _fmt(end),
            },
        ).json()
        if "error" in body:
            raise NCBIError(f"ESearch failed: {body['error']}")
        result = body.get("esearchresult") or {}
        if "ERROR" in result:
            raise NCBIError(f"ESearch failed: {result['ERROR']}")
        return int(result.get("count") or 0), [str(pmid) for pmid in result.get("idlist") or []]

    def search_ids(self, term: str, start: date, end: date) -> list[str]:
        """All PMIDs matching ``term`` with an Entrez date in ``[start, end]``, oldest-safe.

        Windows with more than 10,000 hits are bisected by date until every slice can be
        listed completely.
        """
        count, ids = self.esearch(term, start, end, MAX_SEARCH_RESULTS)
        if count <= MAX_SEARCH_RESULTS:
            return ids
        if start >= end:
            logger.warning(
                "PubMed: %d records were added on %s for this query; only the first %d can be "
                "listed. Narrow the query to ingest them all.",
                count,
                _fmt(start),
                MAX_SEARCH_RESULTS,
            )
            return ids
        middle = start + (end - start) // 2
        return self.search_ids(term, start, middle) + self.search_ids(
            term, middle + timedelta(days=1), end
        )

    def efetch(self, pmids: list[str]) -> ET.Element:
        """Fetch the PubMed XML for up to ``MAX_FETCH_BATCH`` PMIDs."""
        response = self._post("efetch.fcgi", {"id": ",".join(pmids), "retmode": "xml"})
        root = ET.fromstring(response.content)
        if root.tag == "eFetchResult":
            raise NCBIError(f"EFetch failed: {_text(root.find('ERROR')) or 'unknown error'}")
        return root


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------


@dataclass
class Article:
    """The parts of a PubMed record that are worth remembering."""

    pmid: str
    title: str = ""
    abstract: list[tuple[str, str]] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    affiliations: list[str] = field(default_factory=list)
    journal: str = ""
    pub_date: str = ""
    doi: str = ""
    pmcid: str = ""
    mesh_terms: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    publication_types: list[str] = field(default_factory=list)
    entrez_date: str = ""
    retracted: bool = False

    @property
    def url(self) -> str:
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"


def _text(element: ET.Element | None) -> str:
    """All text inside ``element`` (titles and abstracts contain inline markup like <i>)."""
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _section_label(label: str) -> str:
    """``BACKGROUND`` -> ``Background``; mixed-case labels are kept as written."""
    return label.capitalize() if label.isupper() else label


def _format_date(element: ET.Element | None) -> str:
    """Render a PubDate/ArticleDate as ``2024 Mar 15`` (or MedlineDate verbatim)."""
    if element is None:
        return ""
    if medline := _text(element.find("MedlineDate")):
        return medline
    parts = [_text(element.find(tag)) for tag in ("Year", "Month", "Day")]
    return " ".join(p for p in parts if p)


def _history_date(history: ET.Element | None, status: str) -> str:
    """ISO date of the ``PubMedPubDate`` with the given ``PubStatus`` (e.g. ``entrez``)."""
    if history is None:
        return ""
    for node in history.findall("PubMedPubDate"):
        if node.get("PubStatus") != status:
            continue
        try:
            year = int(_text(node.find("Year")))
            raw_month = _text(node.find("Month"))
            month = int(raw_month) if raw_month.isdigit() else _MONTHS[raw_month[:3].lower()]
            day = int(_text(node.find("Day")) or 1)
            return date(year, month, day).isoformat()
        except (KeyError, ValueError):
            return ""
    return ""


def _author_name(author: ET.Element) -> str:
    if collective := _text(author.find("CollectiveName")):
        return collective
    last = _text(author.find("LastName"))
    first = _text(author.find("ForeName")) or _text(author.find("Initials"))
    return f"{first} {last}".strip()


def _article_id(container: ET.Element | None, id_type: str) -> str:
    if container is None:
        return ""
    for node in container.findall("ArticleIdList/ArticleId"):
        if node.get("IdType") == id_type:
            return _text(node)
    return ""


def _parse_article(record: ET.Element) -> Article | None:
    """Parse a ``PubmedArticle`` or ``PubmedBookArticle`` element."""
    is_book = record.tag == "PubmedBookArticle"
    citation = record.find("BookDocument" if is_book else "MedlineCitation")
    if citation is None:
        return None
    pmid = _text(citation.find("PMID"))
    if not pmid:
        return None

    body = citation if is_book else citation.find("Article")
    if body is None:
        body = citation
    pubmed_data = record.find("PubmedBookData" if is_book else "PubmedData")

    abstract = [
        (_section_label(node.get("Label") or ""), _text(node))
        for node in body.findall("Abstract/AbstractText")
        if _text(node)
    ]

    authors: list[str] = []
    affiliations: list[str] = []
    for author in body.findall("AuthorList/Author"):
        if author.get("ValidYN") == "N":
            continue
        if name := _author_name(author):
            authors.append(name)
        affiliations.extend(_text(a) for a in author.findall("AffiliationInfo/Affiliation"))

    if is_book:
        title = _text(body.find("ArticleTitle")) or _text(body.find("Book/BookTitle"))
        journal = _text(body.find("Book/BookTitle"))
        pub_date = _format_date(body.find("Book/PubDate"))
    else:
        title = _text(body.find("ArticleTitle")) or _text(body.find("VernacularTitle"))
        journal = _text(body.find("Journal/Title")) or _text(body.find("Journal/ISOAbbreviation"))
        pub_date = _format_date(body.find("Journal/JournalIssue/PubDate")) or _format_date(
            body.find("ArticleDate")
        )

    doi = _article_id(pubmed_data, "doi")
    if not doi:
        for node in body.findall("ELocationID"):
            if node.get("EIdType") == "doi":
                doi = _text(node)
                break

    mesh_terms = []
    for heading in citation.findall("MeshHeadingList/MeshHeading"):
        descriptor = heading.find("DescriptorName")
        if term := _text(descriptor):
            major = descriptor is not None and descriptor.get("MajorTopicYN") == "Y"
            mesh_terms.append(f"{term} (major topic)" if major else term)

    publication_types = _unique(
        _text(p) for p in body.findall("PublicationTypeList/PublicationType")
    )
    retracted = "Retracted Publication" in publication_types or any(
        node.get("RefType") == "RetractionIn"
        for node in citation.findall("CommentsCorrectionsList/CommentsCorrections")
    )

    return Article(
        pmid=pmid,
        title=title,
        abstract=abstract,
        authors=authors,
        affiliations=_unique(affiliations),
        journal=journal,
        pub_date=pub_date,
        doi=doi,
        pmcid=_article_id(pubmed_data, "pmc"),
        mesh_terms=mesh_terms,
        keywords=_unique(_text(k) for k in citation.findall("KeywordList/Keyword")),
        publication_types=publication_types,
        entrez_date=_history_date(
            pubmed_data.find("History") if pubmed_data is not None else None, "entrez"
        ),
        retracted=retracted,
    )


def parse_pubmed_xml(root: ET.Element | str | bytes) -> tuple[list[Article], list[str]]:
    """Parse an EFetch ``PubmedArticleSet`` into (articles, PMIDs of deleted citations)."""
    if not isinstance(root, ET.Element):
        root = ET.fromstring(root)
    articles = [
        article
        for record in root
        if record.tag in ("PubmedArticle", "PubmedBookArticle")
        and (article := _parse_article(record)) is not None
    ]
    deleted = [_text(p) for p in root.findall("DeleteCitation/PMID") if _text(p)]
    return articles, deleted


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_article_markdown(article: Article, *, include_heading: bool = True) -> str:
    """Render an article into deterministic Markdown: citation, abstract, then indexing terms."""
    blocks: list[str] = []
    if include_heading:
        blocks.append(f"# {article.title or f'PubMed article {article.pmid}'}")

    citation = [f"**PMID:** {article.pmid}"]
    if article.doi:
        citation.append(f"**DOI:** {article.doi}")
    if article.pmcid:
        citation.append(f"**PMCID:** {article.pmcid}")
    if article.journal:
        journal = f"**Journal:** {article.journal}"
        citation.append(f"{journal} ({article.pub_date})" if article.pub_date else journal)
    header = [" | ".join(citation)]
    if article.authors:
        header.append(f"**Authors:** {', '.join(article.authors)}")
    if article.publication_types:
        header.append(f"**Publication Types:** {', '.join(article.publication_types)}")
    blocks.append("\n".join(header))

    if article.abstract:
        sections = [f"### {label}\n{text}" if label else text for label, text in article.abstract]
        blocks.append("## Abstract\n" + "\n\n".join(sections))

    for heading, items in (
        ("Medical Subject Headings (MeSH)", article.mesh_terms),
        ("Keywords", article.keywords),
        ("Affiliations", article.affiliations),
    ):
        if items:
            blocks.append(f"## {heading}\n" + "\n".join(f"- {item}" for item in items))

    return "\n\n".join(blocks)


def _article_row(article: Article) -> dict[str, Any]:
    """Flatten an article into a document row.

    cognee prepends ``# {title}`` to ``content``, so the body leaves the heading out.
    """
    return {
        "id": f"pubmed:{article.pmid}",
        "title": article.title or f"PubMed article {article.pmid}",
        "url": article.url,
        "content": render_article_markdown(article, include_heading=False),
        "journal": article.journal or None,
        "publication_date": article.pub_date or None,
        "doi": article.doi or None,
        "pmcid": article.pmcid or None,
        "entrez_date": article.entrez_date or None,
        "_deleted": False,
    }


def _tombstone(pmid: str) -> dict[str, Any]:
    return {"id": f"pubmed:{pmid}", "_deleted": True}


# ---------------------------------------------------------------------------
# Sync engine (pure given client + state dict)
# ---------------------------------------------------------------------------


def _scope_key(term: str, mindate: date | None, maxdate: date | None) -> str:
    """Identify what is being synced; the cursor is only valid for the same scope."""
    bounds = f"{_fmt(mindate) if mindate else ''}..{_fmt(maxdate) if maxdate else ''}"
    return f"{' '.join(term.split())}|{bounds}"


def _batches(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def sync_pubmed(
    client: NCBIClient,
    state: dict[str, Any],
    *,
    term: str,
    mindate: str | date | None = None,
    maxdate: str | date | None = None,
    batch_size: int = 100,
    detect_deletions: bool = True,
    drop_retracted: bool = True,
    today: date | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield new PubMed articles and deletion tombstones since the last run.

    ``state`` holds ``last_edat`` (the last synced Entrez day, ``YYYY/MM/DD``), ``known_ids``
    (PMIDs already ingested) and ``scope`` (the query the cursor belongs to). It is written
    only after the whole run succeeded, so an interrupted run is simply repeated.

    Args:
        client: E-utilities client.
        state: Mutable sync state (``dlt.current.resource_state()`` in production).
        term: PubMed query, in the same syntax as the PubMed search box.
        mindate: Oldest Entrez date to ingest (``YYYY/MM/DD``); default: no lower bound.
        maxdate: Newest Entrez date to ingest; default: today.
        batch_size: PMIDs per EFetch request (capped at 200).
        detect_deletions: Re-list the query's PMIDs each run to forget removed articles.
        drop_retracted: Skip retracted articles and forget known ones once retracted.
        today: Override "today" (UTC), for tests.
    """
    if not term or not term.strip():
        raise ValueError("A PubMed search term is required.")
    floor = _parse_date(mindate) or EARLIEST_DATE
    ceiling = _parse_date(maxdate)
    end = min(today or datetime.now(timezone.utc).date(), ceiling or date.max)
    batch_size = max(1, min(batch_size, MAX_FETCH_BATCH))

    scope = _scope_key(term, _parse_date(mindate), ceiling)
    known_ids: set[str] = set(state.get("known_ids", []))
    cursor = _parse_date(state.get("last_edat")) if state.get("scope") == scope else None
    if state.get("scope") not in (None, scope):
        logger.info("PubMed: query or date bounds changed; re-scanning the full window.")

    # NCBI stamps Entrez dates in US Eastern time, so a record added late on the previous
    # Eastern day can still be missing from a run made just after UTC midnight. Re-reading one
    # day of overlap costs a single search; already-known PMIDs are not fetched again.
    start = max(floor, cursor - timedelta(days=1)) if cursor else floor

    window_ids = client.search_ids(term, start, end) if start <= end else []
    # Retraction leaves the Entrez date unchanged, so known articles need a separate check.
    # New ones carry the flag in their own record and are dropped while parsing.
    retracted_ids: set[str] = set()
    if drop_retracted and known_ids:
        retracted_ids = set(
            client.search_ids(f"({term}) AND retracted publication[pt]", floor, end)
        )

    new_ids = sorted(set(window_ids) - known_ids - retracted_ids, key=int)
    emitted: set[str] = set()
    removed: set[str] = set()

    for batch in _batches(new_ids, batch_size):
        articles, deleted = parse_pubmed_xml(client.efetch(batch))
        for article in articles:
            if drop_retracted and article.retracted:
                continue
            emitted.add(article.pmid)
            yield _article_row(article)
        # A DeleteCitation here is a PMID ESearch still listed but PubMed already removed:
        # nothing to ingest. Known PMIDs are never re-fetched; the sweep below forgets them.
        if missing := set(batch) - {a.pmid for a in articles} - set(deleted):
            logger.warning("PubMed: EFetch returned nothing for PMIDs %s.", sorted(missing))

    removed |= known_ids & retracted_ids

    if detect_deletions and known_ids:
        # On a full scan the window already is the complete listing.
        live_ids = set(window_ids) if start == floor else set(client.search_ids(term, floor, end))
        if live_ids:
            removed |= known_ids - live_ids
        else:
            logger.warning(
                "PubMed: the query matched nothing while %d articles are known; skipping the "
                "deletion sweep in case this is an outage.",
                len(known_ids),
            )

    for pmid in sorted(removed, key=int):
        yield _tombstone(pmid)

    state["scope"] = scope
    state["last_edat"] = _fmt(max(end, cursor) if cursor else end)
    state["known_ids"] = sorted((known_ids | emitted) - removed, key=int)
    logger.info(
        "PubMed: synced %d article(s), %d deletion(s); %d known, cursor at %s.",
        len(emitted),
        len(removed),
        len(state["known_ids"]),
        state["last_edat"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def pubmed_source(
    *,
    term: str,
    mindate: str | date | None = None,
    maxdate: str | date | None = None,
    api_key: str | None = None,
    email: str | None = None,
    batch_size: int = 100,
    detect_deletions: bool = True,
    drop_retracted: bool = True,
    session: Any = None,
    base_url: str | None = None,
) -> Any:
    """Create a ``dlt`` source that yields PubMed articles as markdown documents.

    Args:
        term: PubMed query selecting what to ingest, e.g. ``"CRISPR gene therapy"`` or
            ``'"Nature"[Journal] AND 2024[dp]'``.
        mindate: Oldest Entrez date (``YYYY/MM/DD``) to ingest.
        maxdate: Newest Entrez date to ingest (default: today, moving forward each run).
        api_key: Optional NCBI API key (raises the limit from 3 to 10 requests/second).
            Falls back to the ``NCBI_API_KEY`` environment variable.
        email: Contact address NCBI asks tools to send. Falls back to ``NCBI_EMAIL``.
        batch_size: PMIDs per EFetch request (max 200).
        detect_deletions: Re-list the query's PMIDs each run to forget removed articles.
        drop_retracted: Skip retracted articles and forget known ones once retracted.
        session: Pre-built HTTP session (for test injection).
        base_url: E-utilities base URL (falls back to ``NCBI_EUTILS_BASE_URL``).

    Returns:
        A ``dlt`` source configured for ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The PubMed connector requires dlt. Install with:\n"
            '    pip install "cognee-community-connector-pubmed"'
        ) from exc

    if not term or not term.strip():
        raise ValueError('A PubMed search term is required, e.g. term="CRISPR gene therapy".')
    # Validate dates up front instead of failing inside the pipeline.
    _parse_date(mindate)
    _parse_date(maxdate)

    client = NCBIClient(
        session,
        api_key=(api_key or os.environ.get("NCBI_API_KEY", "")).strip() or None,
        email=(email or os.environ.get("NCBI_EMAIL", "")).strip() or None,
        base_url=base_url or os.environ.get("NCBI_EUTILS_BASE_URL") or DEFAULT_API_BASE,
    )

    @dlt.resource(
        name=PUBMED_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def pubmed_articles() -> Iterator[dict[str, Any]]:
        yield from sync_pubmed(
            client,
            dlt.current.resource_state(),
            term=term,
            mindate=mindate,
            maxdate=maxdate,
            batch_size=batch_size,
            detect_deletions=detect_deletions,
            drop_retracted=drop_retracted,
        )

    @dlt.source(name=PUBMED_SOURCE_NAME)
    def _pubmed():
        return pubmed_articles

    source = _pubmed()
    # Opt into document mode so rows route through cognify entity extraction
    setattr(source, DOCUMENT_SOURCE_ATTR, PUBMED_SOURCE_NAME)
    return source
