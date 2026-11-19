"""OpenAlex Works connector for Cognee document ingestion.

The source fetches a scoped, paginated snapshot of OpenAlex Works and presents
each work as a normal Cognee document.  A replace snapshot is intentional: it
lets Cognee's existing orphan cleanup remove works that disappear from a scope.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any, Protocol

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("openalex_connector")

OPENALEX_API_URL = "https://api.openalex.org/works"
OPENALEX_SOURCE_NAME = "openalex"
OPENALEX_TABLE_NAME = "openalex_works"
DEFAULT_PAGE_SIZE = 100
MAX_RETRIES = 4


class _Response(Protocol):
    status_code: int
    headers: dict[str, str]

    def json(self) -> dict[str, Any]: ...

    def raise_for_status(self) -> None: ...


class _HttpClient(Protocol):
    def get(self, url: str, *, params: dict[str, str], timeout: float) -> _Response: ...

    def close(self) -> None: ...


def openalex_source(
    *,
    doi: str | None = None,
    author_id: str | None = None,
    institution_id: str | None = None,
    topic_id: str | None = None,
    from_updated_date: str | None = None,
    api_key: str | None = None,
    mailto: str | None = None,
    per_page: int = DEFAULT_PAGE_SIZE,
    client: _HttpClient | None = None,
):
    """Create a dlt source for a scoped snapshot of OpenAlex Works.

    At least one scope is required to avoid accidentally ingesting the entire
    OpenAlex catalogue. ``doi``, author, institution and topic scopes can be
    combined. ``from_updated_date`` accepts OpenAlex's ``YYYY-MM-DD`` format.

    ``api_key`` and ``mailto`` default to ``OPENALEX_API_KEY`` and
    ``OPENALEX_MAILTO``.  The latter opts requests into OpenAlex's polite pool.
    """
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - installation guard
        raise ImportError(
            'The OpenAlex connector requires dlt. Install "cognee-community-connector-openalex".'
        ) from exc

    if not 1 <= per_page <= 200:
        raise ValueError("per_page must be between 1 and 200.")

    if not any((doi, author_id, institution_id, topic_id)):
        raise ValueError("Provide at least one scope: doi, author_id, institution_id, or topic_id.")

    filters = _build_filters(
        doi=doi,
        author_id=author_id,
        institution_id=institution_id,
        topic_id=topic_id,
        from_updated_date=from_updated_date,
    )
    resolved_api_key = api_key or os.getenv("OPENALEX_API_KEY")
    resolved_mailto = mailto or os.getenv("OPENALEX_MAILTO")

    @dlt.resource(name=OPENALEX_TABLE_NAME, primary_key="id", write_disposition="replace")
    def openalex_works() -> Iterator[dict[str, Any]]:
        owned_client = client is None
        request_client = client or _new_http_client()
        try:
            count = 0
            for work in _iter_works(
                request_client,
                filters=filters,
                api_key=resolved_api_key,
                mailto=resolved_mailto,
                per_page=per_page,
            ):
                count += 1
                yield work_to_row(work)
            logger.info("OpenAlex: synced %d work(s).", count)
        finally:
            if owned_client:
                request_client.close()

    @dlt.source(name=OPENALEX_SOURCE_NAME)
    def _openalex():
        return openalex_works

    source = _openalex()
    setattr(source, DOCUMENT_SOURCE_ATTR, OPENALEX_SOURCE_NAME)
    return source


def _new_http_client() -> _HttpClient:
    import httpx

    return httpx.Client(headers={"User-Agent": "cognee-community-openalex/0.1"})


def _build_filters(
    *,
    doi: str | None,
    author_id: str | None,
    institution_id: str | None,
    topic_id: str | None,
    from_updated_date: str | None,
) -> list[str]:
    """Build documented OpenAlex filter clauses without issuing a request."""
    filters: list[str] = []
    if doi:
        filters.append(f"doi:{doi.removeprefix('https://doi.org/')}")
    if author_id:
        filters.append(f"authorships.author.id:{author_id}")
    if institution_id:
        filters.append(f"institutions.id:{institution_id}")
    if topic_id:
        filters.append(f"topics.id:{topic_id}")
    if from_updated_date:
        filters.append(f"from_updated_date:{from_updated_date}")
    return filters


def _iter_works(
    client: _HttpClient,
    *,
    filters: list[str],
    api_key: str | None,
    mailto: str | None,
    per_page: int,
) -> Iterator[dict[str, Any]]:
    """Yield every page in an OpenAlex cursor stream, retrying transient failures."""
    cursor = "*"
    while cursor:
        params = {"filter": ",".join(filters), "per-page": str(per_page), "cursor": cursor}
        if api_key:
            params["api_key"] = api_key
        if mailto:
            params["mailto"] = mailto
        response = _get_with_retry(client, params)
        payload = response.json()
        yield from payload.get("results", [])
        cursor = (payload.get("meta") or {}).get("next_cursor")


def _get_with_retry(client: _HttpClient, params: dict[str, str]) -> _Response:
    """Request one page, honoring Retry-After on rate-limit responses."""
    for attempt in range(MAX_RETRIES):
        response = client.get(OPENALEX_API_URL, params=params, timeout=30.0)
        if response.status_code not in {429, 500, 502, 503, 504}:
            response.raise_for_status()
            return response
        if attempt == MAX_RETRIES - 1:
            response.raise_for_status()
        delay = _retry_delay(response.headers, attempt)
        logger.warning("OpenAlex returned %s; retrying in %.1fs.", response.status_code, delay)
        time.sleep(delay)
    raise AssertionError("unreachable")


def _retry_delay(headers: dict[str, str], attempt: int) -> float:
    try:
        return float(headers.get("retry-after", headers.get("Retry-After", "")))
    except ValueError:
        return float(2**attempt)


def abstract_from_inverted_index(index: dict[str, list[int]] | None) -> str:
    """Decode OpenAlex's ``abstract_inverted_index`` into readable prose."""
    if not index:
        return ""
    positions = [position for values in index.values() for position in values]
    if not positions:
        return ""
    words = [""] * (max(positions) + 1)
    for token, token_positions in index.items():
        for position in token_positions:
            if 0 <= position < len(words):
                words[position] = token
    return " ".join(word for word in words if word)


def work_to_row(work: dict[str, Any]) -> dict[str, Any]:
    """Map an OpenAlex Work to a stable Cognee document row."""
    authors = [
        authorship.get("author", {}).get("display_name")
        for authorship in work.get("authorships") or []
        if authorship.get("author", {}).get("display_name")
    ]
    topics = [topic.get("display_name") for topic in work.get("topics") or [] if topic.get("display_name")]
    abstract = abstract_from_inverted_index(work.get("abstract_inverted_index"))
    title = work.get("display_name") or "Untitled work"
    parts = [f"# {title}"]
    if authors:
        parts.append(f"Authors: {', '.join(authors)}")
    if topics:
        parts.append(f"Topics: {', '.join(topics)}")
    if abstract:
        parts.append(abstract)
    return {
        "id": work.get("id"),
        "title": title,
        "content": "\n\n".join(parts),
        "url": work.get("doi") or work.get("id"),
        "doi": work.get("doi"),
        "publication_date": work.get("publication_date"),
        "updated_date": work.get("updated_date"),
    }
