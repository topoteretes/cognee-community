import os
from dataclasses import dataclass
from typing import Any

import cognee
import httpx
from cognee.shared.logging_utils import get_logger

logger = get_logger("KeenableTask")

PUBLIC_SEARCH_URL = "https://api.keenable.ai/v1/search/public"
KEYED_SEARCH_URL = "https://api.keenable.ai/v1/search"
APP_TITLE = "cognee-community"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_RESULTS = 50


@dataclass
class KeenableSearchResult:
    """Typed representation of a single Keenable search result."""

    url: str
    title: str | None = None
    snippet: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "content": self.snippet or "",
            "snippet": self.snippet,
        }


def _parse_result(raw: Any) -> KeenableSearchResult | None:
    if not isinstance(raw, dict):
        return None
    url = raw.get("url") or raw.get("link") or ""
    if not url:
        return None
    snippet = raw.get("snippet") or raw.get("content") or raw.get("description") or ""
    return KeenableSearchResult(
        url=url,
        title=raw.get("title") or None,
        snippet=snippet or None,
    )


def _request_target(api_key: str | None) -> tuple[str, dict[str, str]]:
    headers = {
        "Content-Type": "application/json",
        "X-Keenable-Title": APP_TITLE,
    }
    key = (api_key or os.getenv("KEENABLE_API_KEY") or "").strip()
    if key:
        headers["X-API-Key"] = key
        return KEYED_SEARCH_URL, headers
    return PUBLIC_SEARCH_URL, headers


def _clamp_results(num_results: int) -> int:
    if num_results < 1:
        return 1
    return min(MAX_RESULTS, num_results)


async def search_web(
    query: str,
    num_results: int = 10,
    api_key: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict]:
    """Search the web with Keenable and return structured results.

    An API key is optional. When ``api_key`` and ``KEENABLE_API_KEY`` are
    empty, the call uses the public search endpoint and sends the
    ``X-Keenable-Title`` header required there. When a key is present, the
    keyed ``/v1/search`` endpoint is used instead.

    Parameters
    ----------
    query : str
        Natural language search query.
    num_results : int
        Number of results to return (1-50, default 10).
    api_key : Optional[str]
        Keenable API key. Falls back to the ``KEENABLE_API_KEY`` environment
        variable. Leave both empty for the keyless public endpoint.
    timeout_seconds : float
        HTTP timeout for the search request.

    Returns
    -------
    List[dict]
        Result dicts with keys ``url``, ``title``, ``content``, ``snippet``.
    """
    url, headers = _request_target(api_key)
    body = {"query": query, "max_results": _clamp_results(num_results)}
    logger.info(f"Running Keenable search: {query!r} (n={body['max_results']}, url={url})")

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception as e:
        logger.error(f"Keenable search failed: {e!s}")
        raise

    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, list):
        raw_results = []
    parsed = [item for item in (_parse_result(r) for r in raw_results) if item is not None]
    logger.info(f"Keenable returned {len(parsed)} results")
    return [r.to_dict() for r in parsed]


async def search_and_add(
    query: str,
    num_results: int = 10,
    api_key: str | None = None,
    dataset_name: str = "keenable",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """Run a Keenable search and add the returned content to a cognee dataset.

    Each result is combined into a single text document and passed to
    ``cognee.add`` followed by ``cognee.cognify``.

    Parameters mirror :func:`search_web`.
    """
    results = await search_web(
        query=query,
        num_results=num_results,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )

    usable = [r for r in results if r.get("content")]
    if not usable:
        raise RuntimeError("No Keenable results returned any content to ingest.")

    combined_text = "\n\n".join(
        f"Source: {r['url']}\nTitle: {r.get('title') or ''}\n{r['content']}" for r in usable
    )

    await cognee.add(combined_text, dataset_name=dataset_name)
    result = await cognee.cognify()

    logger.info(
        f"Added {len(usable)} Keenable results for query {query!r} "
        f"to cognee dataset '{dataset_name}'"
    )
    return result
