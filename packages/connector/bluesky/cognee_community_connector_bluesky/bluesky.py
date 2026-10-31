"""Bluesky (AT Protocol) data-source connector for Cognee."""

import hashlib
import logging
import time
from collections.abc import Callable, Generator
from typing import Any
from urllib.parse import urlparse

import dlt
import requests
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = logging.getLogger("bluesky_connector")

DEFAULT_BASE_URL = "https://public.api.bsky.app"
DEFAULT_TIMEOUT = (10, 30)
DEFAULT_RETRIES = 3


class BlueskySourceError(RuntimeError):
    """Base error raised by the Bluesky connector."""


class BlueskyAPIError(BlueskySourceError):
    """Bluesky API returned an unexpected error or could not be reached."""


class BlueskyNotFoundError(BlueskyAPIError):
    """The requested actor or post was not found (HTTP 404)."""


class BlueskyRateLimitedError(BlueskySourceError):
    """Bluesky answered with HTTP 429 rate limit exceeded."""


def _clean_path(path: str) -> str:
    """Clean endpoint path for safe error messages."""
    return path.split("?")[0]


def _normalize_handle(raw: str) -> str:
    """Normalize user handle, stripping leading '@'."""
    cleaned = raw.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    if not cleaned or " " in cleaned:
        raise ValueError(f"Invalid Bluesky handle: {raw!r}")
    return cleaned


class BlueskyClient:
    """Synchronous REST client for Bluesky public XRPC APIs."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("The Bluesky base_url cannot be empty")
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Invalid Bluesky base_url: {base_url!r}")

        self.base_url = base
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"BlueskyClient(base_url={self.base_url!r})"

    def request(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "Cognee-Connector-Bluesky/0.1.0",
        }

        for attempt in range(DEFAULT_RETRIES):
            last = attempt == DEFAULT_RETRIES - 1
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=DEFAULT_TIMEOUT,
                )
            except requests.RequestException as exc:
                if last:
                    raise BlueskyAPIError(
                        f"Bluesky request failed on {_clean_path(endpoint)}: {type(exc).__name__}"
                    ) from None
                self._sleep(2**attempt)
                continue

            if response.status_code >= 500 and not last:
                self._sleep(2**attempt)
                continue

            return self._parse(response, endpoint)

    def _parse(self, response: requests.Response, endpoint: str) -> Any:
        status = response.status_code
        if status == 404:
            raise BlueskyNotFoundError(
                f"Bluesky resource not found (HTTP 404): {_clean_path(endpoint)}"
            )
        if status == 429:
            raise BlueskyRateLimitedError("Bluesky rate limit exceeded (HTTP 429)")

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if status != 200:
            raise BlueskyAPIError(
                f"Bluesky request failed with HTTP {status}: {_clean_path(endpoint)}"
            )

        if not isinstance(payload, dict):
            raise BlueskyAPIError(
                f"Bluesky returned an unexpected response format for {_clean_path(endpoint)}"
            )

        return payload

    def get_author_feed(
        self, actor: str, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        """Fetch posts authored by a specific handle or DID."""
        params: dict[str, Any] = {"actor": actor, "limit": min(limit, 100)}
        if cursor:
            params["cursor"] = cursor
        return self.request("/xrpc/app.bsky.feed.getAuthorFeed", params=params)

    def search_posts(
        self, query: str, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        """Search public Bluesky posts by keyword query."""
        params: dict[str, Any] = {"q": query, "limit": min(limit, 100)}
        if cursor:
            params["cursor"] = cursor
        return self.request("/xrpc/app.bsky.feed.searchPosts", params=params)


def _format_post_content(
    uri: str,
    handle: str,
    display_name: str,
    created_at: str,
    text: str,
    url: str,
) -> str:
    """Format a Bluesky post into clean Markdown for Cognee."""
    author_str = f"{display_name} (@{handle})" if display_name else f"@{handle}"
    return (
        f"# Bluesky Post by {author_str}\n"
        f"**Posted:** {created_at}\n"
        f"**URL:** {url}\n"
        f"**AT URI:** `{uri}`\n\n"
        f"{text.strip()}\n"
    )


def _iter_bluesky_rows(
    client: BlueskyClient,
    actors: list[str],
    queries: list[str],
    limit_per_feed: int,
    previous_state: dict[str, str],
) -> Generator[dict[str, Any], None, None]:
    """Iterate Bluesky posts, calculating SHA-256 state and tombstoning deletions."""
    current_ids: set[str] = set()

    # Process author feeds
    for actor in actors:
        try:
            feed_data = client.get_author_feed(actor, limit=limit_per_feed)
            feed_items = feed_data.get("feed", [])
        except BlueskyNotFoundError:
            logger.warning("Bluesky actor %s not found", actor)
            continue

        for item in feed_items:
            post = item.get("post", {})
            uri = post.get("uri")
            if not uri:
                continue

            author = post.get("author", {})
            record = post.get("record", {})

            handle = author.get("handle", actor)
            display_name = author.get("displayName", "")
            created_at = record.get("createdAt", "")
            text = record.get("text", "")

            # Extract rkey from uri: at://did:plc:.../app.bsky.feed.post/{rkey}
            rkey = uri.split("/")[-1]
            post_url = f"https://bsky.app/profile/{handle}/post/{rkey}"

            post_id = f"bsky:{uri}"
            current_ids.add(post_id)

            content = _format_post_content(
                uri=uri,
                handle=handle,
                display_name=display_name,
                created_at=created_at,
                text=text,
                url=post_url,
            )

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            # Skip unchanged
            if previous_state.get(post_id) == content_hash:
                continue

            previous_state[post_id] = content_hash
            snippet = text[:60].replace("\n", " ")
            title = (
                f"Bluesky post by @{handle}: {snippet}..."
                if len(text) > 60
                else f"Bluesky post by @{handle}: {snippet}"
            )

            yield {
                "id": post_id,
                "actor_handle": handle,
                "author_name": display_name,
                "created_at": created_at,
                "title": title,
                "content": content,
                "url": post_url,
                "_deleted": False,
            }

    # Process keyword search feeds
    for q in queries:
        try:
            search_data = client.search_posts(q, limit=limit_per_feed)
            posts = search_data.get("posts", [])
        except BlueskyNotFoundError:
            continue

        for post in posts:
            uri = post.get("uri")
            if not uri:
                continue

            author = post.get("author", {})
            record = post.get("record", {})

            handle = author.get("handle", "")
            display_name = author.get("displayName", "")
            created_at = record.get("createdAt", "")
            text = record.get("text", "")

            rkey = uri.split("/")[-1]
            post_url = f"https://bsky.app/profile/{handle}/post/{rkey}"

            post_id = f"bsky:{uri}"
            current_ids.add(post_id)

            content = _format_post_content(
                uri=uri,
                handle=handle,
                display_name=display_name,
                created_at=created_at,
                text=text,
                url=post_url,
            )

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            if previous_state.get(post_id) == content_hash:
                continue

            previous_state[post_id] = content_hash
            snippet = text[:60].replace("\n", " ")
            title = f"Bluesky search '{q}' by @{handle}: {snippet}"

            yield {
                "id": post_id,
                "actor_handle": handle,
                "author_name": display_name,
                "created_at": created_at,
                "title": title,
                "content": content,
                "url": post_url,
                "_deleted": False,
            }

    # Emit tombstones for dropped posts
    dropped_ids = set(previous_state.keys()) - current_ids
    for dropped_id in sorted(dropped_ids):
        del previous_state[dropped_id]
        yield {
            "id": dropped_id,
            "actor_handle": "",
            "author_name": "",
            "created_at": "",
            "title": f"Deleted: {dropped_id}",
            "content": "",
            "url": "",
            "_deleted": True,
        }


def bluesky_source(
    actors: list[str] | tuple[str, ...] | None = None,
    queries: list[str] | tuple[str, ...] | None = None,
    limit_per_feed: int = 50,
    base_url: str = DEFAULT_BASE_URL,
    client: BlueskyClient | None = None,
    service: BlueskyClient | None = None,
) -> Any:
    """Create a DLT data-source for ingesting Bluesky posts into Cognee memory."""
    active_client = client or service
    if active_client is None:
        active_client = BlueskyClient(base_url=base_url)

    normalized_actors: list[str] = []
    if actors:
        normalized_actors = [_normalize_handle(a) for a in actors]

    valid_queries: list[str] = []
    if queries:
        valid_queries = [q.strip() for q in queries if q.strip()]

    if not normalized_actors and not valid_queries:
        raise ValueError("At least one actor handle (e.g. 'bsky.app') or query must be specified")

    def _source() -> Any:
        @dlt.resource(
            name="bluesky_posts",
            write_disposition="merge",
            primary_key="id",
        )
        def bluesky_resource() -> Generator[dict[str, Any], None, None]:
            state = dlt.current.resource_state().setdefault("posts", {})
            yield from _iter_bluesky_rows(
                active_client,
                actors=normalized_actors,
                queries=valid_queries,
                limit_per_feed=limit_per_feed,
                previous_state=state,
            )

        setattr(bluesky_resource, DOCUMENT_SOURCE_ATTR, "bluesky")
        return bluesky_resource

    return dlt.source(_source, name="bluesky")()
