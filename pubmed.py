"""A full-snapshot PubMed source for cognee.

The connector queries NCBI E-utilities, fetches article metadata and abstracts,
and yields normal cognee documents.  A run replaces its staging snapshot: an
article that no longer appears in the selected PubMed query is therefore
eligible for cognee's downstream orphan cleanup.
"""

import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("pubmed_connector")

PUBMED_SOURCE_NAME = "pubmed"
PUBMED_TABLE_NAME = "pubmed_articles"
_EUTILS_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_DEFAULT_BATCH_SIZE = 100

_EXTRA_HINT = (
    'The PubMed connector requires the "pubmed" extra: '
    'pip install "cognee-community-connector-pubmed".'
)


def pubmed_source(
    query: str,
    email: str | None = None,
    api_key: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    client: "PubMedClient | None" = None,
):
    """Create a dlt source for PubMed articles matching ``query``.

    Args:
        query: PubMed query, for example ``"traditional Chinese medicine"``.
        email: Contact address required by NCBI. Falls back to ``PUBMED_EMAIL``.
        api_key: Optional NCBI API key. Falls back to ``NCBI_API_KEY``.
        date_from: Optional inclusive ``YYYY/MM/DD`` E-utilities ``edat`` start.
        date_to: Optional inclusive end date; defaults to today at NCBI.
        client: Injectable client used by tests; normally constructed internally.

    The date range is useful for an incremental *discovery* run. Do not use a
    date-limited query to reconcile deletion: a full selection snapshot is what
    makes forget-on-delete safe.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_email = email or os.environ.get("PUBMED_EMAIL")
    if not resolved_email:
        raise ValueError("PubMed contact email required: pass email= or set PUBMED_EMAIL.")
    if not query.strip():
        raise ValueError("PubMed query must not be empty.")

    resolved_client = client or PubMedClient(
        email=resolved_email, api_key=api_key or os.environ.get("NCBI_API_KEY")
    )
    search_term = _with_edat_range(query, date_from, date_to)

    @dlt.resource(name=PUBMED_TABLE_NAME, primary_key="id", write_disposition="replace")
    def pubmed_articles():
        count = 0
        for article in resolved_client.iter_articles(search_term):
            count += 1
            yield article
        logger.info("PubMed: synced %d article(s).", count)

    @dlt.source(name=PUBMED_SOURCE_NAME)
    def _pubmed():
        return pubmed_articles

    source = _pubmed()
    setattr(source, DOCUMENT_SOURCE_ATTR, PUBMED_SOURCE_NAME)
    return source


class PubMedClient:
    """Small NCBI E-utilities client with deterministic, polite throttling."""

    def __init__(
        self,
        email: str,
        api_key: str | None = None,
        request: Callable[[str, dict[str, str]], bytes] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.email = email
        self.api_key = api_key
        self._request = request or _http_get
        self._sleep = sleep
        self._last_request_at: float | None = None

    def iter_articles(self, term: str) -> Iterator[dict[str, str]]:
        """Search all matching PMIDs, then fetch them in batches."""
        ids = self._search_ids(term)
        for start in range(0, len(ids), _DEFAULT_BATCH_SIZE):
            yield from _parse_articles(self._fetch(ids[start : start + _DEFAULT_BATCH_SIZE]))

    def _search_ids(self, term: str) -> list[str]:
        root = ET.fromstring(self._get("esearch.fcgi", {"db": "pubmed", "term": term, "retmax": "100000"}))
        return [node.text for node in root.findall(".//IdList/Id") if node.text]

    def _fetch(self, ids: list[str]) -> bytes:
        return self._get(
            "efetch.fcgi",
            {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
        )

    def _get(self, endpoint: str, params: dict[str, str]) -> bytes:
        interval = 0.1 if self.api_key else 0.34
        if self._last_request_at is not None:
            remaining = interval - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        request_params = {**params, "email": self.email, "tool": "cognee-community"}
        if self.api_key:
            request_params["api_key"] = self.api_key
        response = self._request(f"{_EUTILS_BASE_URL}/{endpoint}", request_params)
        self._last_request_at = time.monotonic()
        return response


def _http_get(url: str, params: dict[str, str]) -> bytes:
    """Issue one GET request without adding a third-party HTTP dependency."""
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed NCBI base URL
        return response.read()


def _with_edat_range(query: str, date_from: str | None, date_to: str | None) -> str:
    """Add an E-utilities edit-date range without changing an unrestricted query."""
    if not date_from:
        return query
    return f"({query}) AND {date_from}:{date_to or '3000/12/31'}[edat]"


def _text(element: ET.Element | None) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


def _parse_articles(payload: bytes) -> Iterator[dict[str, str]]:
    """Turn PubMed XML into stable document rows with provenance."""
    root = ET.fromstring(payload)
    for article in root.findall(".//PubmedArticle"):
        pmid = _text(article.find("./MedlineCitation/PMID"))
        if not pmid:
            continue
        title = _text(article.find("./MedlineCitation/Article/ArticleTitle"))
        abstract = "\n".join(
            filter(None, (_text(item) for item in article.findall("./MedlineCitation/Article/Abstract/AbstractText")))
        )
        journal = _text(article.find("./MedlineCitation/Article/Journal/Title"))
        publication_date = _publication_date(article)
        content_parts = [part for part in (title, abstract) if part]
        if not content_parts:
            continue
        yield {
            "id": pmid,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "title": title or f"PubMed {pmid}",
            "content": "\n\n".join(content_parts),
            "journal": journal,
            "publication_date": publication_date,
        }


def _publication_date(article: ET.Element) -> str:
    date = article.find("./MedlineCitation/Article/Journal/JournalIssue/PubDate")
    if date is None:
        return ""
    return "-".join(filter(None, (_text(date.find("Year")), _text(date.find("Month")), _text(date.find("Day")))))
