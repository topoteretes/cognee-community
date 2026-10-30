import asyncio
import os
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp
import cognee
from cognee.shared.logging_utils import get_logger

logger = get_logger("SerplyTask")

SERPLY_API_URL = "https://api.serply.io/v1"

SearchType = Literal["web", "news"]
TimeRange = Literal["day", "week", "month", "year"]

# Google's tbs values for "past day/week/month/year"; Serply passes them through.
TIME_RANGES = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}

# Serply returns at most 10 organic results per request.
MAX_RESULTS = 10

# Account-level failures apply to every request, so they are raised instead of reported per URL.
ACCOUNT_ERROR_STATUSES = (401, 402)


@dataclass
class SerplySearchResult:
    """Typed representation of a single Serply search result."""

    url: str
    title: str | None = None
    description: str | None = None
    position: int | None = None
    published: str | None = None
    markdown: str | None = None
    error: str | None = None

    @property
    def content(self) -> str:
        return self.markdown or self.description or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "description": self.description,
            "position": self.position,
            "published": self.published,
            "error": self.error,
        }


def _parse_result(raw: dict) -> SerplySearchResult:
    metadata = raw.get("metadata") or {}
    return SerplySearchResult(
        url=raw.get("link") or "",
        title=raw.get("title"),
        description=raw.get("description"),
        position=raw.get("position"),
        published=metadata.get("published_time"),
    )


def _build_headers(api_key: str | None) -> dict[str, str]:
    if api_key is None:
        api_key = os.getenv("SERPLY_API_KEY")
    if not api_key:
        raise ValueError("Serply API key is required. Set the SERPLY_API_KEY environment variable.")
    # Serply sits behind Cloudflare, which rejects aiohttp's default User-Agent.
    return {"X-Api-Key": api_key, "User-Agent": "cognee-community"}


async def _call(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    async with session.request(
        method, f"{SERPLY_API_URL}{path}", params=params, json=payload
    ) as response:
        if response.status >= 400:
            body = await response.text()
            raise aiohttp.ClientResponseError(
                response.request_info,
                response.history,
                status=response.status,
                message=body[:200],
            )
        if path == "/request":
            return await response.text()
        return await response.json()


async def search_web(
    query: str,
    num_results: int = 10,
    search_type: SearchType = "web",
    country: str | None = None,
    time_range: TimeRange | None = None,
    include_content: bool = False,
    concurrency: int = 5,
    timeout: float = 60,
    api_key: str | None = None,
) -> list[dict]:
    """Search Google with Serply and return structured results.

    Parameters
    ----------
    query : str
        Search query. Google operators such as ``site:`` and ``filetype:`` work.
    num_results : int
        Number of results to return (1-10, default 10).
    search_type : str
        ``web`` for organic results or ``news`` for news articles.
    country : Optional[str]
        Two-letter country code to localise results (Google's ``gl``), e.g. ``us``.
    time_range : Optional[str]
        Only return results from the past ``day``, ``week``, ``month`` or ``year``.
    include_content : bool
        Also fetch each result page as markdown. Each page is one extra request.
    concurrency : int
        Maximum number of pages fetched at the same time when ``include_content`` is on.
    timeout : float
        Total timeout in seconds for each request.
    api_key : Optional[str]
        Serply API key. Falls back to the ``SERPLY_API_KEY`` environment variable.

    Returns
    -------
    List[dict]
        Result dicts with keys ``url``, ``title``, ``content`` (page markdown when
        fetched, otherwise the search snippet), ``description``, ``position``,
        ``published`` and ``error``.
    """
    if not 1 <= num_results <= MAX_RESULTS:
        raise ValueError(f"num_results must be between 1 and {MAX_RESULTS}.")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1.")
    headers = _build_headers(api_key)

    params: dict[str, Any] = {"q": query, "num": num_results}
    if search_type == "news":
        params["tbm"] = "nws"
    if country:
        params["gl"] = country.lower()
    if time_range:
        params["tbs"] = TIME_RANGES[time_range]

    logger.info(f"Running Serply search (type={search_type}, n={num_results})")
    async with aiohttp.ClientSession(
        headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as session:
        try:
            response = await _call(session, "GET", "/search", params=params)
        except Exception as e:
            logger.error(f"Serply search failed: {type(e).__name__}")
            raise

        # Keep only real URLs, and trim: a full SERP page can hold more than ``num`` results.
        parsed = [
            _parse_result(raw)
            for raw in response.get("results") or []
            if str(raw.get("link") or "").startswith(("http://", "https://"))
        ][:num_results]

        if include_content and parsed:
            semaphore = asyncio.Semaphore(concurrency)

            async def _fetch_one(url: str) -> str:
                async with semaphore:
                    return await _call(
                        session,
                        "POST",
                        "/request",
                        payload={"url": url, "response_type": "markdown"},
                    )

            pages = await asyncio.gather(
                *(_fetch_one(result.url) for result in parsed), return_exceptions=True
            )
            for position, (result, page) in enumerate(zip(parsed, pages, strict=True)):
                if isinstance(page, aiohttp.ClientResponseError) and (
                    page.status in ACCOUNT_ERROR_STATUSES
                ):
                    raise page
                if isinstance(page, BaseException) and not isinstance(page, Exception):
                    raise page
                if isinstance(page, Exception):
                    # Log the position, not the URL: URLs can carry tokens in their query string.
                    logger.error(f"Failed to fetch result #{position}: {type(page).__name__}")
                    result.error = str(page)
                else:
                    result.markdown = page

    logger.info(f"Serply returned {len(parsed)} results")
    return [result.to_dict() for result in parsed]


async def search_and_add(
    query: str,
    num_results: int = 10,
    search_type: SearchType = "web",
    country: str | None = None,
    time_range: TimeRange | None = None,
    concurrency: int = 5,
    timeout: float = 60,
    api_key: str | None = None,
    dataset_name: str = "serply",
) -> Any:
    """Run a Serply search and add each result page's markdown to a cognee dataset.

    Result pages are fetched as markdown. A page that cannot be fetched falls back to
    its search snippet. Every result with content is combined into a single text
    document, passed to ``cognee.add`` and cognified in ``dataset_name``.
    Parameters mirror :func:`search_web`.
    """
    results = await search_web(
        query=query,
        num_results=num_results,
        search_type=search_type,
        country=country,
        time_range=time_range,
        include_content=True,
        concurrency=concurrency,
        timeout=timeout,
        api_key=api_key,
    )

    usable = [r for r in results if r.get("content")]
    if not usable:
        raise RuntimeError("No Serply results returned any content to ingest.")

    combined_text = "\n\n".join(
        f"Source: {r['url']}\nTitle: {r.get('title') or ''}\n{r['content']}" for r in usable
    )

    await cognee.add(combined_text, dataset_name=dataset_name)
    result = await cognee.cognify(datasets=[dataset_name])

    logger.info(f"Added {len(usable)} Serply results to cognee dataset '{dataset_name}'")
    return result
