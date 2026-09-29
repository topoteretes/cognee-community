import asyncio
import os
from dataclasses import dataclass
from typing import Any

import cognee
from cognee.shared.logging_utils import get_logger
from firecrawl import AsyncFirecrawl
from firecrawl.v2.utils.error_handler import PaymentRequiredError, UnauthorizedError

logger = get_logger("FirecrawlTask")

# Account-level failures apply to every URL, so they are raised instead of reported per URL.
ACCOUNT_ERRORS = (UnauthorizedError, PaymentRequiredError)


@dataclass
class FirecrawlDocument:
    """Typed representation of a single scraped page or search result."""

    url: str
    title: str | None = None
    markdown: str | None = None
    description: str | None = None
    status_code: int | None = None
    error: str | None = None

    @property
    def content(self) -> str:
        return self.markdown or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "description": self.description,
            "status_code": self.status_code,
            "error": self.error,
        }


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _parse_document(raw: Any, url: str | None = None) -> FirecrawlDocument:
    """Parse a firecrawl-py Document, search result, or dict into a FirecrawlDocument.

    Scrape and search results with page content come back as ``Document`` objects
    carrying ``markdown`` and ``metadata``. Search results without page content are
    plain ``url``/``title``/``description`` objects. Dicts use the API's camelCase keys.
    """
    metadata = _get(raw, "metadata")
    return FirecrawlDocument(
        url=url
        or _get(raw, "url")
        or _get(metadata, "source_url")
        or _get(metadata, "sourceURL")
        or _get(metadata, "url")
        or "",
        title=_get(raw, "title") or _get(metadata, "title"),
        markdown=_get(raw, "markdown"),
        description=_get(raw, "description") or _get(metadata, "description"),
        status_code=_get(metadata, "status_code") or _get(metadata, "statusCode"),
        error=_get(metadata, "error"),
    )


def _build_firecrawl_client(api_key: str | None) -> AsyncFirecrawl:
    if api_key is None:
        api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        raise ValueError(
            "Firecrawl API key is required. Set the FIRECRAWL_API_KEY environment variable."
        )
    return AsyncFirecrawl(api_key=api_key)


def _is_usable(doc: dict) -> bool:
    """A page is ingested only if it has markdown and the target did not return an HTTP error."""
    return bool(doc.get("content")) and (doc.get("status_code") or 200) < 400


def _format_for_cognee(documents: list[dict]) -> str:
    return "\n\n".join(
        f"Source: {doc['url']}\nTitle: {doc.get('title') or ''}\n{doc['content']}"
        for doc in documents
    )


async def scrape_urls(
    urls: list[str],
    only_main_content: bool = True,
    timeout_ms: int = 30000,
    concurrency: int = 5,
    api_key: str | None = None,
) -> list[dict]:
    """Scrape a list of URLs into clean markdown with Firecrawl.

    Parameters
    ----------
    urls : List[str]
        URLs to scrape. JavaScript-rendered pages and PDFs are supported.
    only_main_content : bool
        Drop navigation, headers and footers, keeping the main page content.
    timeout_ms : int
        Per-page timeout in milliseconds.
    concurrency : int
        Maximum number of pages scraped at the same time.
    api_key : Optional[str]
        Firecrawl API key. Falls back to the ``FIRECRAWL_API_KEY`` environment variable.

    Returns
    -------
    List[dict]
        One dict per input URL, in input order, with keys ``url``, ``title``,
        ``content`` (markdown), ``description``, ``status_code`` and ``error``.
        A URL that fails has empty ``content`` and the SDK's error message in ``error``.
        An invalid API key or an exhausted credit balance raises instead.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1.")
    client = _build_firecrawl_client(api_key)
    semaphore = asyncio.Semaphore(concurrency)

    async def _scrape_one(url: str) -> Any:
        async with semaphore:
            return await client.scrape(
                url,
                formats=["markdown"],
                only_main_content=only_main_content,
                timeout=timeout_ms,
            )

    logger.info(f"Scraping {len(urls)} URLs with Firecrawl (concurrency={concurrency})")
    responses = await asyncio.gather(*(_scrape_one(url) for url in urls), return_exceptions=True)

    results = []
    for position, (url, response) in enumerate(zip(urls, responses, strict=True)):
        # Re-raise account errors and cancellation; report any other failure per URL.
        if isinstance(response, ACCOUNT_ERRORS) or (
            isinstance(response, BaseException) and not isinstance(response, Exception)
        ):
            raise response
        if isinstance(response, Exception):
            # Log the position, not the URL: URLs can carry tokens in their query string.
            logger.error(f"Failed to scrape URL #{position}: {type(response).__name__}")
            results.append(FirecrawlDocument(url=url, error=str(response)).to_dict())
        else:
            results.append(_parse_document(response, url=url).to_dict())
    return results


async def scrape_and_add(
    urls: list[str],
    only_main_content: bool = True,
    timeout_ms: int = 30000,
    concurrency: int = 5,
    api_key: str | None = None,
    dataset_name: str = "firecrawl",
) -> Any:
    """Scrape a list of URLs with Firecrawl and add the markdown to a cognee dataset.

    Pages with markdown and a non-error HTTP status are combined into a single text
    document, passed to ``cognee.add`` and cognified in ``dataset_name``. Parameters
    mirror :func:`scrape_urls`.
    """
    scraped = await scrape_urls(
        urls=urls,
        only_main_content=only_main_content,
        timeout_ms=timeout_ms,
        concurrency=concurrency,
        api_key=api_key,
    )

    usable = [doc for doc in scraped if _is_usable(doc)]
    if not usable:
        raise RuntimeError("No scraped pages returned any content to ingest.")

    await cognee.add(_format_for_cognee(usable), dataset_name=dataset_name)
    result = await cognee.cognify(datasets=[dataset_name])

    logger.info(f"Added {len(usable)} scraped pages to cognee dataset '{dataset_name}'")
    return result


async def search_web(
    query: str,
    limit: int = 5,
    scrape: bool = True,
    only_main_content: bool = True,
    api_key: str | None = None,
) -> list[dict]:
    """Search the web with Firecrawl and return structured results.

    Parameters
    ----------
    query : str
        Search query.
    limit : int
        Maximum number of results.
    scrape : bool
        Also return each result page as markdown in the same call.
    only_main_content : bool
        When scraping, keep only the main page content.
    api_key : Optional[str]
        Firecrawl API key. Falls back to the ``FIRECRAWL_API_KEY`` environment variable.

    Returns
    -------
    List[dict]
        Result dicts with keys ``url``, ``title``, ``content`` (markdown, empty when
        ``scrape`` is off), ``description``, ``status_code`` and ``error``.
    """
    client = _build_firecrawl_client(api_key)

    kwargs: dict[str, Any] = {"limit": limit}
    if scrape:
        kwargs["scrape_options"] = {
            "formats": ["markdown"],
            "only_main_content": only_main_content,
        }

    logger.info(f"Running Firecrawl search (limit={limit}, scrape={scrape})")
    try:
        response = await client.search(query, **kwargs)
    except Exception as e:
        logger.error(f"Firecrawl search failed: {type(e).__name__}")
        raise

    web = _get(response, "web") or []
    parsed = [_parse_document(item) for item in web]
    logger.info(f"Firecrawl returned {len(parsed)} results")
    return [doc.to_dict() for doc in parsed]


async def search_and_add(
    query: str,
    limit: int = 5,
    only_main_content: bool = True,
    api_key: str | None = None,
    dataset_name: str = "firecrawl_search",
) -> Any:
    """Search the web with Firecrawl and add each result page's markdown to cognee.

    Page content is fetched in the same search call, so there is no second request
    per result. Results without markdown or with an HTTP error status are skipped.
    Parameters mirror :func:`search_web`.
    """
    results = await search_web(
        query=query,
        limit=limit,
        scrape=True,
        only_main_content=only_main_content,
        api_key=api_key,
    )

    usable = [doc for doc in results if _is_usable(doc)]
    if not usable:
        raise RuntimeError("No Firecrawl search results returned any content to ingest.")

    await cognee.add(_format_for_cognee(usable), dataset_name=dataset_name)
    result = await cognee.cognify(datasets=[dataset_name])

    logger.info(f"Added {len(usable)} Firecrawl search results to cognee dataset '{dataset_name}'")
    return result
