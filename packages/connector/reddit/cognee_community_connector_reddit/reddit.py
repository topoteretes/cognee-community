"""DLT source for Reddit posts and comment trees (snapshot sync + forget-on-delete).

Fetches Reddit submissions and comment threads from specified subreddits or post IDs,
renders them into markdown documents, and yields them as a dlt resource for Cognee's
ingestion and cognify pipelines.
"""

from collections.abc import Iterator
import os
import time
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("reddit_connector")
except ImportError:
    import logging
    logger = logging.getLogger("reddit_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

REDDIT_TABLE_NAME = "reddit_posts"
REDDIT_SOURCE_NAME = "reddit"
REDDIT_OAUTH_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_OAUTH_API_BASE = "https://oauth.reddit.com"
REDDIT_PUBLIC_API_BASE = "https://www.reddit.com"

_DEFAULT_USER_AGENT = "cognee-community-connector-reddit/0.1.0"
_MAX_RETRIES = 5
_DEFAULT_MAX_DEPTH = 5


class RedditClient:
    """Client for interacting with the Reddit API via OAuth2 or public endpoints."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        username: str | None = None,
        password: str | None = None,
        user_agent: str | None = None,
        token: str | None = None,
        http_client: Any = None,
    ):
        import httpx

        self.user_agent = user_agent or os.environ.get("REDDIT_USER_AGENT", _DEFAULT_USER_AGENT)
        self.client_id = client_id or os.environ.get("REDDIT_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("REDDIT_CLIENT_SECRET")
        self.username = username or os.environ.get("REDDIT_USERNAME")
        self.password = password or os.environ.get("REDDIT_PASSWORD")
        self.access_token = token or os.environ.get("REDDIT_ACCESS_TOKEN")
        self.token_expiry = 0.0

        self.client = http_client or httpx.Client(timeout=20.0)

    def _ensure_token(self) -> str | None:
        """Authenticate using Script App OAuth flow if credentials are provided."""
        if self.access_token and time.time() < self.token_expiry:
            return self.access_token

        if self.client_id and self.client_secret and self.username and self.password:
            try:
                auth = (self.client_id, self.client_secret)
                data = {
                    "grant_type": "password",
                    "username": self.username,
                    "password": self.password,
                }
                headers = {"User-Agent": self.user_agent}
                resp = self.client.post(REDDIT_OAUTH_URL, auth=auth, data=data, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
                self.access_token = payload.get("access_token")
                expires_in = payload.get("expires_in", 3600)
                self.token_expiry = time.time() + expires_in - 60
                return self.access_token
            except Exception as exc:
                logger.warning("Reddit OAuth failed, falling back to public endpoints: %s", exc)

        return self.access_token

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a GET request with rate-limit and transient error retry logic."""
        import httpx

        token = self._ensure_token()
        headers = {"User-Agent": self.user_agent}

        if token:
            base_url = REDDIT_OAUTH_API_BASE
            headers["Authorization"] = f"bearer {token}"
        else:
            base_url = REDDIT_PUBLIC_API_BASE

        clean_path = path.lstrip("/")
        if not token and not clean_path.endswith(".json"):
            clean_path = f"{clean_path}.json"

        url = f"{base_url}/{clean_path}"

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self.client.get(url, params=params, headers=headers)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 2 ** attempt))
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                delay = 2 ** attempt
                time.sleep(delay)

        return {}


def reddit_source(
    subreddits: list[str] | None = None,
    post_ids: list[str] | None = None,
    listing: str = "hot",
    limit: int = 25,
    max_comment_depth: int = _DEFAULT_MAX_DEPTH,
    client: RedditClient | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    username: str | None = None,
    password: str | None = None,
    user_agent: str | None = None,
):
    """Create a dlt source that yields Reddit posts and comments as markdown documents.

    Args:
        subreddits: List of subreddit names (e.g. ['python', 'artificial']).
        post_ids: Optional list of explicit submission IDs to ingest.
        listing: Subreddit listing category ('hot', 'new', 'top', 'rising'). Default 'hot'.
        limit: Maximum number of submissions to fetch per subreddit. Default 25.
        max_comment_depth: Max depth for parsing nested comment trees. Default 5.
        client: Pre-instantiated RedditClient (useful for testing and dependency injection).
        client_id: Reddit application client ID.
        client_secret: Reddit application client secret.
        username: Reddit account username.
        password: Reddit account password.
        user_agent: Custom User-Agent header string.

    Returns:
        A dlt source yielding documents ready for Cognee ingestion.
    """
    import dlt

    if client is None:
        client = RedditClient(
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
            user_agent=user_agent,
        )

    target_subreddits = subreddits or []
    target_post_ids = post_ids or []

    @dlt.resource(name=REDDIT_TABLE_NAME, primary_key="id", write_disposition="replace")
    def reddit_posts() -> Iterator[dict[str, Any]]:
        count = 0

        # Fetch explicitly requested post IDs first
        for post_id in target_post_ids:
            try:
                row = _fetch_post_by_id(client, post_id, max_comment_depth)
                if row:
                    count += 1
                    yield row
            except Exception as exc:
                logger.warning("Failed to fetch post ID %s: %s", post_id, exc)

        # Ingest posts across specified subreddits
        for sub in target_subreddits:
            sub_name = sub.strip().lstrip("r/").strip("/")
            try:
                for row in _iter_subreddit_posts(client, sub_name, listing, limit, max_comment_depth):
                    count += 1
                    yield row
            except Exception as exc:
                logger.warning("Failed to fetch posts from r/%s: %s", sub_name, exc)

        logger.info("Reddit connector: synced %d submission(s).", count)

    @dlt.source(name=REDDIT_SOURCE_NAME)
    def _reddit():
        return reddit_posts

    source = _reddit()
    setattr(source, DOCUMENT_SOURCE_ATTR, REDDIT_SOURCE_NAME)
    return source


def _iter_subreddit_posts(
    client: RedditClient,
    subreddit: str,
    listing: str,
    limit: int,
    max_comment_depth: int,
) -> Iterator[dict[str, Any]]:
    """Iterate through submissions in a subreddit using after pagination."""
    params = {"limit": min(limit, 100)}
    path = f"r/{subreddit}/{listing}"
    data = client.get(path, params=params)

    listing_data = data.get("data", {}) if isinstance(data, dict) else {}
    children = listing_data.get("children", [])

    for child in children:
        post_data = child.get("data", {})
        post_id = post_data.get("id")
        if not post_id:
            continue

        yield _build_post_row(client, post_data, max_comment_depth)


def _fetch_post_by_id(
    client: RedditClient,
    post_id: str,
    max_comment_depth: int,
) -> dict[str, Any] | None:
    """Fetch a single post and its comment thread by ID."""
    clean_id = post_id.replace("t3_", "")
    path = f"comments/{clean_id}"
    response = client.get(path)

    if isinstance(response, list) and len(response) > 0:
        post_listing = response[0].get("data", {}).get("children", [])
        if post_listing:
            post_data = post_listing[0].get("data", {})
            comments_listing = response[1].get("data", {}).get("children", []) if len(response) > 1 else []
            return _build_post_row_with_comments(post_data, comments_listing, max_comment_depth)

    return None


def _build_post_row(
    client: RedditClient,
    post_data: dict[str, Any],
    max_comment_depth: int,
) -> dict[str, Any]:
    """Fetch comments and format the submission into a document row."""
    post_id = post_data.get("id", "")
    permalink = post_data.get("permalink", "")
    comments_listing = []

    if permalink:
        try:
            path = permalink.lstrip("/")
            thread_data = client.get(path, params={"depth": max_comment_depth, "limit": 50})
            if isinstance(thread_data, list) and len(thread_data) > 1:
                comments_listing = thread_data[1].get("data", {}).get("children", [])
        except Exception as exc:
            logger.debug("Comments fetch skipped for %s: %s", post_id, exc)

    return _build_post_row_with_comments(post_data, comments_listing, max_comment_depth)


def _build_post_row_with_comments(
    post_data: dict[str, Any],
    comments_children: list[dict[str, Any]],
    max_comment_depth: int,
) -> dict[str, Any]:
    """Format post metadata, body, and comments into markdown document structure."""
    post_id = post_data.get("id", "")
    title = post_data.get("title", "Untitled Submission")
    author = post_data.get("author", "[deleted]")
    subreddit = post_data.get("subreddit", "unknown")
    selftext = post_data.get("selftext", "")
    url = post_data.get("url") or f"https://www.reddit.com{post_data.get('permalink', '')}"
    score = post_data.get("score", 0)

    md_lines = [
        f"# {title}",
        f"**Author:** u/{author} | **Subreddit:** r/{subreddit} | **Upvotes:** {score}",
        f"**Source URL:** {url}",
        "",
    ]

    if selftext:
        md_lines.extend(["## Post Content", selftext, ""])

    comments_md = _render_comment_tree(comments_children, depth=0, max_depth=max_comment_depth)
    if comments_md.strip():
        md_lines.extend(["## Discussion & Comments", comments_md])

    return {
        "id": f"reddit_{post_id}",
        "url": url,
        "title": f"r/{subreddit}: {title}",
        "content": "\n".join(md_lines),
    }


def _render_comment_tree(children: list[dict[str, Any]], depth: int, max_depth: int) -> str:
    """Recursively render comments into nested markdown with depth bounds."""
    if depth > max_depth or not children:
        return ""

    lines: list[str] = []
    indent = "  " * depth

    for item in children:
        kind = item.get("kind")
        if kind != "t1":
            continue

        c_data = item.get("data", {})
        author = c_data.get("author", "[deleted]")
        body = c_data.get("body", "").strip()
        score = c_data.get("score", 0)

        if not body or body == "[removed]":
            continue

        clean_body = body.replace("\n", f"\n{indent}  ")
        lines.append(f"{indent}- **u/{author}** ({score} points):\n{indent}  {clean_body}")

        replies = c_data.get("replies")
        if isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            nested = _render_comment_tree(reply_children, depth + 1, max_depth)
            if nested:
                lines.append(nested)

    return "\n".join(lines)
