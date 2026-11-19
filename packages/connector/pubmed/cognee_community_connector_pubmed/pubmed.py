"""PubMed data-source connector for cognee.

The connector uses NCBI ESearch to discover PMIDs and EFetch to retrieve article
metadata and abstracts.  A persisted ``edat`` cursor makes later runs
incremental, while NCBI's official deleted-PMID feed drives hard deletes.
"""

from __future__ import annotations

import gzip
import os
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("pubmed_connector")

_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_DELETED_PMIDS_URL = "https://ftp.ncbi.nlm.nih.gov/pubmed/deleted.pmids.gz"
_SOURCE_NAME = "pubmed"
_TABLE_NAME = "pubmed_articles"
_DEFAULT_BATCH_SIZE = 100
_MAX_ESEARCH_RESULTS = 10_000


def _date_string(value: date | str) -> str:
    """Normalize an ESearch date to ``YYYY/MM/DD``."""
    if isinstance(value, date):
        return value.strftime("%Y/%m/%d")
    for pattern in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).strftime("%Y/%m/%d")
        except ValueError:
            pass
    raise ValueError(f"invalid date {value!r}; expected YYYY/MM/DD or YYYY-MM-DD")


def _text(element: ET.Element | None) -> str:
    """Return normalized mixed XML text, including inline formatting nodes."""
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def _publication_date(article: ET.Element) -> str:
    pub_date = article.find("Article/Journal/JournalIssue/PubDate")
    if pub_date is None:
        return ""
    medline_date = _text(pub_date.find("MedlineDate"))
    if medline_date:
        return medline_date
    parts = [_text(pub_date.find(name)) for name in ("Year", "Month", "Day")]
    return "-".join(part for part in parts if part)


def _entrez_date(record: ET.Element) -> str:
    for item in record.findall("PubmedData/History/PubMedPubDate"):
        if item.get("PubStatus") != "entrez":
            continue
        parts = [_text(item.find(name)) for name in ("Year", "Month", "Day")]
        if all(parts):
            return f"{parts[0]}/{parts[1].zfill(2)}/{parts[2].zfill(2)}"
    return ""


def _article_ids(record: ET.Element) -> dict[str, str]:
    ids: dict[str, str] = {}
    for item in record.findall("PubmedData/ArticleIdList/ArticleId"):
        kind = item.get("IdType")
        value = _text(item)
        if kind and value:
            ids[kind] = value
    return ids


def _parse_article(record: ET.Element) -> dict[str, Any] | None:
    """Flatten one ``PubmedArticle`` element into a document-mode row."""
    citation = record.find("MedlineCitation")
    if citation is None:
        return None
    pmid = _text(citation.find("PMID"))
    if not pmid:
        return None

    article = citation.find("Article")
    if article is None:
        return None

    title = _text(article.find("ArticleTitle"))
    abstract_parts: list[str] = []
    for part in article.findall("Abstract/AbstractText"):
        body = _text(part)
        if not body:
            continue
        label = (part.get("Label") or "").strip()
        abstract_parts.append(f"{label}: {body}" if label else body)
    abstract = "\n\n".join(abstract_parts)

    authors: list[str] = []
    for author in article.findall("AuthorList/Author"):
        collective = _text(author.find("CollectiveName"))
        if collective:
            authors.append(collective)
            continue
        name = " ".join(
            part
            for part in (
                _text(author.find("ForeName")),
                _text(author.find("LastName")),
                _text(author.find("Suffix")),
            )
            if part
        )
        if name:
            authors.append(name)

    ids = _article_ids(record)
    content = f"{title}\n\n{abstract}".strip()
    return {
        "id": pmid,
        "title": title,
        "content": content,
        "abstract": abstract,
        "authors": authors,
        "journal": _text(article.find("Journal/Title")),
        "publication_date": _publication_date(citation),
        "entrez_date": _entrez_date(record),
        "doi": ids.get("doi", ""),
        "pmc_id": ids.get("pmc", ""),
        "publication_types": [
            value
            for item in article.findall("PublicationTypeList/PublicationType")
            if (value := _text(item))
        ],
        "mesh_terms": [
            value
            for item in citation.findall("MeshHeadingList/MeshHeading/DescriptorName")
            if (value := _text(item))
        ],
        "keywords": [
            value for item in citation.findall("KeywordList/Keyword") if (value := _text(item))
        ],
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "_deleted": False,
    }


def _parse_articles(payload: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    rows: list[dict[str, Any]] = []
    for record in root.findall("PubmedArticle"):
        if (row := _parse_article(record)) is not None:
            rows.append(row)
    return rows


@dataclass
class PubMedClient:
    """Small rate-limited client for the ESearch/EFetch calls used by the source."""

    session: Any
    api_key: str | None = None
    email: str | None = None
    tool: str = "cognee-pubmed-connector"
    request_interval: float | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _last_request_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.request_interval is None:
            self.request_interval = 0.1 if self.api_key else 1 / 3
        if self.request_interval < 0:
            raise ValueError("request_interval must be non-negative")

    def _params(self, values: dict[str, Any]) -> dict[str, Any]:
        params = dict(values)
        params["tool"] = self.tool
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["email"] = self.email
        return params

    def _get(self, endpoint: str, params: dict[str, Any]) -> Any:
        now = self.clock()
        interval = self.request_interval
        assert interval is not None
        if self._last_request_at is not None:
            wait = interval - (now - self._last_request_at)
            if wait > 0:
                self.sleep(wait)
        response = self.session.get(
            f"{_EUTILS_BASE}/{endpoint}", params=self._params(params), timeout=60
        )
        self._last_request_at = self.clock()
        response.raise_for_status()
        return response

    def search(
        self,
        query: str,
        *,
        mindate: str,
        maxdate: str,
    ) -> list[str]:
        """Return all selected PMIDs for one inclusive ``edat`` window."""
        response = self._get(
            "esearch.fcgi",
            {
                "db": "pubmed",
                "term": query,
                "datetype": "edat",
                "mindate": mindate,
                "maxdate": maxdate,
                "retmode": "json",
                "retstart": 0,
                "retmax": _MAX_ESEARCH_RESULTS,
                "sort": "pub_date",
            },
        )
        result = response.json()["esearchresult"]
        count = int(result["count"])
        if count > _MAX_ESEARCH_RESULTS:
            raise ValueError(
                f"PubMed query matched {count} records in one edat window; ESearch exposes "
                f"only {_MAX_ESEARCH_RESULTS}. Narrow the query or date range."
            )
        ids = [str(value) for value in result.get("idlist", [])]
        if len(ids) != count:
            raise ValueError(f"PubMed ESearch returned {len(ids)} of {count} expected PMIDs")
        return ids

    def fetch(self, pmids: Sequence[str]) -> list[dict[str, Any]]:
        """Fetch and parse one PMID batch."""
        if not pmids:
            return []
        response = self._get(
            "efetch.fcgi",
            {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"},
        )
        return _parse_articles(response.content)

    def deleted_pmids(self) -> set[str]:
        """Download NCBI's authoritative list of deleted PubMed identifiers."""
        response = self.session.get(_DELETED_PMIDS_URL, timeout=60)
        response.raise_for_status()
        return set(gzip.decompress(response.content).decode("ascii").split())


def _make_session() -> Any:
    try:
        import requests
    except ImportError as exc:
        raise ImportError(
            'The PubMed connector requires "requests". Install the package with '
            'pip install "cognee-community-connector-pubmed".'
        ) from exc
    session = requests.Session()
    session.headers.update({"Accept": "application/json, application/xml;q=0.9"})
    return session


def _deleted_row(pmid: str) -> dict[str, Any]:
    return {"id": pmid, "_deleted": True}


def sync_articles(
    client: PubMedClient,
    query: str,
    state: dict[str, Any],
    *,
    start_date: date | str,
    end_date: date | str | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> Iterator[dict[str, Any]]:
    """Yield new PubMed articles and deletion markers, then advance sync state.

    ESearch date boundaries are inclusive and ``edat`` has day resolution.  The
    connector therefore searches the previous cursor day again and filters
    already-known PMIDs.  This avoids losing records inserted later on the same
    day while keeping the second run a metadata-fetch no-op.
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= batch_size <= 200:
        raise ValueError("batch_size must be between 1 and 200")
    stored_query = state.get("query")
    if stored_query is not None and stored_query != query:
        raise ValueError("query changed for existing PubMed sync state; use a new dataset/resource")

    window_start = state.get("last_edat") or _date_string(start_date)
    window_end = _date_string(end_date or date.today())
    if window_start > window_end:
        raise ValueError("start_date/cursor must not be after end_date")

    known_ids = {str(value) for value in state.get("known_ids", [])}
    selected_ids = client.search(
        query,
        mindate=window_start,
        maxdate=window_end,
    )
    deleted_upstream = client.deleted_pmids()
    deleted = known_ids & deleted_upstream
    new_ids = list(
        dict.fromkeys(
            pmid for pmid in selected_ids if pmid not in known_ids and pmid not in deleted_upstream
        )
    )

    fetched_ids: set[str] = set()
    for offset in range(0, len(new_ids), batch_size):
        batch = new_ids[offset : offset + batch_size]
        rows = client.fetch(batch)
        returned_ids = {row["id"] for row in rows}
        if missing := set(batch) - returned_ids:
            raise ValueError(f"PubMed EFetch omitted requested PMIDs: {sorted(missing)}")
        for row in rows:
            fetched_ids.add(row["id"])
            yield row

    for pmid in sorted(deleted):
        yield _deleted_row(pmid)

    state["query"] = query
    state["last_edat"] = window_end
    state["known_ids"] = sorted((known_ids | fetched_ids) - deleted)
    logger.info(
        "PubMed: %d new article(s), %d deletion(s), edat %s..%s.",
        len(fetched_ids),
        len(deleted),
        window_start,
        window_end,
    )


def pubmed_source(
    query: str,
    *,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    api_key: str | None = None,
    email: str | None = None,
    tool: str = "cognee-pubmed-connector",
    batch_size: int = _DEFAULT_BATCH_SIZE,
    session: Any = None,
    request_interval: float | None = None,
):
    """Create a dlt resource for PubMed metadata and abstracts.

    Args:
        query: PubMed/Entrez search expression selecting articles to ingest.
        start_date: Inclusive first ``edat`` date. Defaults to 30 days ago on
            the first run; later runs resume from persisted resource state.
        end_date: Inclusive final ``edat`` date. Defaults to today.
        api_key: Optional NCBI API key. Falls back to ``NCBI_API_KEY``.
        email: Optional contact email sent to NCBI. Falls back to ``NCBI_EMAIL``.
        tool: E-utilities client identifier.
        batch_size: EFetch batch size, from 1 through 200.
        session: Injected requests-compatible session, primarily for tests.
        request_interval: Seconds between E-utilities calls. Defaults to NCBI's
            3 requests/second limit, or 10 requests/second with an API key.

    Returns:
        A document-mode dlt resource configured for merge upserts and hard deletes.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The PubMed connector requires dlt: pip install "cognee-community-connector-pubmed".'
        ) from exc

    first_date = start_date or (date.today() - timedelta(days=30))
    client = PubMedClient(
        session=session if session is not None else _make_session(),
        api_key=api_key or os.getenv("NCBI_API_KEY"),
        email=email or os.getenv("NCBI_EMAIL"),
        tool=tool,
        request_interval=request_interval,
    )

    @dlt.resource(
        name=_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def pubmed_articles():
        yield from sync_articles(
            client,
            query,
            dlt.current.resource_state(),
            start_date=first_date,
            end_date=end_date,
            batch_size=batch_size,
        )

    resource = pubmed_articles()
    setattr(resource, DOCUMENT_SOURCE_ATTR, _SOURCE_NAME)
    return resource
