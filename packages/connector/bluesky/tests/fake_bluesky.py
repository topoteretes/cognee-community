"""In-memory mock client simulating Bluesky public XRPC API."""

from typing import Any

from cognee_community_connector_bluesky.bluesky import (
    BlueskyAPIError,
    BlueskyClient,
    BlueskyNotFoundError,
    BlueskyRateLimitedError,
)


class FakeBlueskyClient(BlueskyClient):
    """In-memory Bluesky client simulating real XRPC endpoints with zero network calls."""

    def __init__(self, base_url: str = "https://public.api.bsky.app"):
        super().__init__(base_url=base_url, sleep=lambda _: None)
        self.authors: dict[str, list[dict[str, Any]]] = {}
        self.searches: dict[str, list[dict[str, Any]]] = {}
        self.rate_limited = False
        self.server_error = False
        self.requests_count = 0
        self.setup_default_fixtures()

    def setup_default_fixtures(self) -> None:
        """Seed realistic Bluesky posts."""
        self.authors["bsky.app"] = [
            {
                "post": {
                    "uri": "at://did:plc:z72i7/app.bsky.feed.post/3k12345",
                    "author": {
                        "handle": "bsky.app",
                        "displayName": "Bluesky Official",
                    },
                    "record": {
                        "text": "Welcome to the AT Protocol network! Open federated social media.",
                        "createdAt": "2026-10-15T14:30:00Z",
                    },
                }
            },
            {
                "post": {
                    "uri": "at://did:plc:z72i7/app.bsky.feed.post/3k12346",
                    "author": {
                        "handle": "bsky.app",
                        "displayName": "Bluesky Official",
                    },
                    "record": {
                        "text": "Check out our latest decentralization roadmap and developer docs.",
                        "createdAt": "2026-10-18T10:00:00Z",
                    },
                }
            },
        ]

        self.searches["GraphRAG"] = [
            {
                "uri": "at://did:plc:a89bc/app.bsky.feed.post/4m98765",
                "author": {
                    "handle": "researcher.bsky.social",
                    "displayName": "AI Researcher",
                },
                "record": {
                    "text": (
                        "GraphRAG with persistent memory makes AI agents "
                        "dramatically more consistent."
                    ),
                    "createdAt": "2026-10-20T08:15:00Z",
                },
            }
        ]

    def get_author_feed(
        self, actor: str, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        self.requests_count += 1
        if self.rate_limited:
            raise BlueskyRateLimitedError("Bluesky rate limit exceeded (HTTP 429)")
        if self.server_error:
            raise BlueskyAPIError(
                "Bluesky request failed with HTTP 500: /xrpc/app.bsky.feed.getAuthorFeed"
            )

        if actor not in self.authors:
            raise BlueskyNotFoundError(f"Bluesky actor not found: {actor}")

        items = self.authors[actor][:limit]
        return {"feed": items}

    def search_posts(
        self, query: str, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        self.requests_count += 1
        if self.rate_limited:
            raise BlueskyRateLimitedError("Bluesky rate limit exceeded (HTTP 429)")
        if self.server_error:
            raise BlueskyAPIError(
                "Bluesky request failed with HTTP 500: /xrpc/app.bsky.feed.searchPosts"
            )

        matches = self.searches.get(query, [])[:limit]
        return {"posts": matches}
