"""An incremental PubMed source for cognee.

Each run performs a lightweight PMID sweep for the configured query. It fetches
only articles changed in PubMed's ``edat`` window (plus newly discovered PMIDs)
and emits hard-delete markers for PMIDs that disappeared from the sweep.
"""

import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
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
    client: "PubMedClient | None" = None,
):
    """Create a dlt source for PubMed articles matching ``query``.

    Args:
        query: PubMed query, for example ``"traditional Chinese medicine"``.
        email: Contact address required by NCBI. Falls back to ``PUBMED_EMAIL``.
        api_key: Optional NCBI API key. Falls back to ``NCBI_API_KEY``.
        client: Injectable client used by tests; normally constructed internally.
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

    @dlt.resource(
        name=PUBMED_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def pubmed_articles():
        yield from sync_articles(
            resolved_client,
            query,
            dlt.current.resource_state(),
        )

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
        yield from self.iter_articles_by_ids(self.search_ids(term))

    def search_ids(self, term: str) -> list[str]:
        """Return all PMIDs matching an E-utilities search term."""
        root = ET.fromstring(
            self._get(
                "esearch.fcgi",
                {"db": "pubmed", "term": term, "retmax": "100000"},
            )
        )
        return [node.text for node in root.findall(".//IdList/Id") if node.text]

    def iter_articles_by_ids(self, ids: list[str]) -> Iterator[dict[str, Any]]:
        """Fetch and normalize the supplied PMIDs in bounded batches."""
        for start in range(0, len(ids), _DEFAULT_BATCH_SIZE):
            yield from _parse_articles(self._fetch(ids[start : start + _DEFAULT_BATCH_SIZE]))

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
    # S310 is safe here because callers cannot override the fixed NCBI base URL.
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return response.read()


def _with_edat_range(query: str, date_from: str | None, date_to: str | None) -> str:
    """Add an E-utilities edit-date range without changing an unrestricted query."""
    if not date_from:
        return query
    return f"({query}) AND {date_from}:{date_to or '3000/12/31'}[edat]"


def _deleted_row(pmid: str) -> dict[str, Any]:
    """Build a dlt hard-delete marker for one vanished PubMed record."""
    return {"id": pmid, "_deleted": True}


def sync_articles(
    client: PubMedClient,
    query: str,
    state: dict[str, Any],
    *,
    today: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed articles and deletions, then advance the persisted cursor.

    PubMed exposes edit dates at day precision. The inclusive cursor therefore
    deliberately overlaps the boundary day so edits made later on that day are
    not missed; stable document hashes keep those occasional repeats harmless.
    """
    sync_date = today or datetime.now(UTC).strftime("%Y/%m/%d")
    known_ids = set(state.get("known_ids", []))
    current_order = client.search_ids(query)
    current_ids = set(current_order)

    if known_ids and state.get("last_edat"):
        changed_ids = set(
            client.search_ids(_with_edat_range(query, state["last_edat"], sync_date))
        )
        # A newly matching article can carry an older edit date (for example
        # after a query/scope change), so never rely on the cursor alone.
        changed_ids.update(current_ids - known_ids)
    else:
        changed_ids = current_ids

    changed_order = [pmid for pmid in current_order if pmid in changed_ids]
    changed = 0
    for article in client.iter_articles_by_ids(changed_order):
        article["_deleted"] = False
        changed += 1
        yield article

    deleted_ids = known_ids - current_ids
    for pmid in sorted(deleted_ids):
        yield _deleted_row(pmid)

    # State advances only after the generator completes successfully. A search,
    # fetch, or parse failure leaves the old cursor available for a safe retry.
    state["known_ids"] = sorted(current_ids)
    state["last_edat"] = sync_date
    logger.info("PubMed: %d changed article(s), %d deletion(s).", changed, len(deleted_ids))


def _text(element: ET.Element | None) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


def _parse_articles(payload: bytes) -> Iterator[dict[str, Any]]:
    """Turn PubMed XML into stable document rows with provenance."""
    root = ET.fromstring(payload)
    for article in root.findall(".//PubmedArticle"):
        pmid = _text(article.find("./MedlineCitation/PMID"))
        if not pmid:
            continue
        title = _text(article.find("./MedlineCitation/Article/ArticleTitle"))
        abstract = "\n".join(
            filter(
                None,
                (
                    _text(item)
                    for item in article.findall(
                        "./MedlineCitation/Article/Abstract/AbstractText"
                    )
                ),
            )
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
    parts = (_text(date.find("Year")), _text(date.find("Month")), _text(date.find("Day")))
    return "-".join(filter(None, parts))
