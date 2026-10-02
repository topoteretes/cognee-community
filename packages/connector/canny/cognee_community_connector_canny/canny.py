"""DLT source for Canny feature requests (snapshot sync + forget-on-delete).

Reads posts (feature requests) and their comments from the Canny API and yields
**one document per post**, including its vote score, status and comments::

    import cognee
    from cognee_community_connector_canny import canny_source

    await cognee.remember(canny_source(), dataset_name="canny")

Posts are ingested as *normal documents*: the source declares
``cognee_document_source = "canny"``, so each post flows through cognify entity
extraction (not the deterministic dlt-row path).

Design
------
* **Auth** - a Canny secret API key (company settings), sent as ``apiKey`` in the
  JSON body of every POST request. No OAuth.
* **Vote counts** - the post ``score`` (votes) and ``commentCount`` are written
  into the document text, so demand is part of what the graph learns.
* **Snapshot sync** - Canny's ``posts/list`` has no ``updatedAfter`` filter and
  there is no delete feed, so the resource is a full snapshot
  (``write_disposition="replace"``, cognee's default), like the Notion and Slack
  connectors. Each run rewrites staging with exactly the posts currently visible.
  A post deleted (or merged away, or moved out of scope) simply drops out, and
  cognee's ``orphan_cleanup`` removes it from the graph and vector stores.
* **Incremental ingestion** - a post's row id is its Canny id and its content is
  stable, so unchanged posts keep the same content-hash ``data_id`` and are not
  re-ingested or re-cognified. Only new or edited posts (new comment, vote count
  change, status change, edited text) are processed again.
* **Safe failure** - any error aborts the run instead of skipping a post. Under
  ``replace`` a missing post would be forgotten as if it were deleted, so a
  failed run must leave memory untouched.

Internal (admin-only) comments are skipped unless ``include_internal=True``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("canny_connector")

CANNY_TABLE_NAME = "canny_posts"
CANNY_SOURCE_NAME = "canny"
CANNY_POSTS_URL = "https://canny.io/api/v1/posts/list"
CANNY_COMMENTS_URL = "https://canny.io/api/v2/comments/list"

_PAGE_SIZE = 100
_MAX_RETRIES = 5
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

_EXTRA_HINT = (
    "The Canny connector requires dlt and httpx: pip install cognee-community-connector-canny"
)


def canny_source(
    api_key: str | None = None,
    board_ids: Iterable[str] | None = None,
    statuses: Iterable[str] | None = None,
    include_comments: bool = True,
    include_internal: bool = False,
    client: Any = None,
):
    """Create a dlt resource that yields Canny posts as documents.

    Args:
        api_key: Canny secret API key. Falls back to ``CANNY_API_KEY``.
        board_ids: Only ingest posts from these board ids. Default: all boards.
        statuses: Only ingest posts with these statuses, e.g. ``["planned",
            "in progress"]``. Default: all.
        include_comments: Fetch and include each post's comments.
        include_internal: Also include internal (admin-only) comments.
        client: Pre-built ``httpx.Client``-like object (a test-injection point).
            It needs a ``post(url, json=...)`` method. When omitted one is built.

    Returns:
        A dlt resource for ``cognee.remember(...)`` (default ``replace``).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_key = api_key or os.environ.get("CANNY_API_KEY")
    if client is None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        if not resolved_key:
            raise ValueError("Canny API key required: pass api_key= or set CANNY_API_KEY.")
        client = httpx.Client(timeout=60.0)

    boards = list(board_ids) if board_ids else [None]
    status_param = ",".join(statuses) if statuses else None

    @dlt.resource(name=CANNY_TABLE_NAME, primary_key="id", write_disposition="replace")
    def canny_posts():
        count = 0
        for board_id in boards:
            for post in _iter_posts(client, resolved_key, board_id, status_param):
                comments = []
                if include_comments and post.get("commentCount", 1):
                    comments = list(_iter_comments(client, resolved_key, post["id"]))
                    if not include_internal:
                        comments = [c for c in comments if not c.get("internal")]
                count += 1
                yield _post_to_row(post, comments)
        logger.info("Canny: synced %d post(s).", count)

    resource = canny_posts
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(resource, DOCUMENT_SOURCE_ATTR, CANNY_SOURCE_NAME)
    return resource


# ---------------------------------------------------------------------------
# Canny API helpers (module-private)
# ---------------------------------------------------------------------------


def _request(client: Any, url: str, api_key: str | None, body: dict) -> dict:
    """POST to Canny, retrying rate-limit / transient errors.

    Permanent errors and exhausted retries raise, so a partial read can never be
    mistaken for a complete snapshot.
    """
    import httpx

    payload = {"apiKey": api_key, **body} if api_key else dict(body)
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.post(url, json=payload)
        except httpx.TransportError as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            delay = float(2**attempt)
            logger.warning("Canny: %s - retrying in %.1fs.", exc, delay)
            time.sleep(delay)
            continue

        status = response.status_code
        if status in _TRANSIENT_STATUS and attempt < _MAX_RETRIES - 1:
            delay = _retry_after(response.headers, attempt)
            logger.warning("Canny: HTTP %s - retrying in %.1fs.", status, delay)
            time.sleep(delay)
            continue
        if status in (401, 403):
            raise PermissionError(
                f"Canny rejected the API key (HTTP {status}). Check CANNY_API_KEY."
            )
        response.raise_for_status()
        return response.json()

    raise RuntimeError("Canny: retry loop exited unexpectedly.")  # pragma: no cover


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait: the Retry-After header, else exponential backoff."""
    header = None
    if headers:
        header = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return max(float(header), 0.0)
    except (TypeError, ValueError):
        return float(2**attempt)


def _iter_posts(
    client: Any, api_key: str | None, board_id: str | None, statuses: str | None
) -> Iterator[dict]:
    """Yield posts across ``skip`` pagination, oldest first (stable order)."""
    skip = 0
    while True:
        body: dict[str, Any] = {"limit": _PAGE_SIZE, "skip": skip, "sort": "oldest"}
        if board_id:
            body["boardID"] = board_id
        if statuses:
            body["status"] = statuses
        payload = _request(client, CANNY_POSTS_URL, api_key, body)
        posts = payload.get("posts") or []
        yield from posts
        # Stop on the last page, or on an empty page so we can never loop forever.
        if not payload.get("hasMore") or not posts:
            return
        skip += len(posts)


def _iter_comments(client: Any, api_key: str | None, post_id: str) -> Iterator[dict]:
    """Yield all comments of a post across cursor pagination."""
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        body: dict[str, Any] = {"postID": post_id, "limit": _PAGE_SIZE}
        if cursor:
            body["cursor"] = cursor
        payload = _request(client, CANNY_COMMENTS_URL, api_key, body)
        yield from payload.get("items") or []
        cursor = payload.get("cursor")
        if not payload.get("hasNextPage") or not cursor or cursor in seen:
            return
        seen.add(cursor)


def _post_to_row(post: dict, comments: list[dict]) -> dict:
    """Flatten a Canny post + comments into a document row.

    Only ``id``/``title``/``content``/``url`` are kept, and comments are sorted
    oldest first, so the content (and its hash) only changes when the post does.
    """
    lines = []
    for label, value in (
        ("Board", (post.get("board") or {}).get("name")),
        ("Category", (post.get("category") or {}).get("name")),
        ("Status", post.get("status")),
        ("Votes", post.get("score")),
        ("Comments", post.get("commentCount")),
        ("Author", (post.get("author") or {}).get("name")),
        ("Tags", ", ".join(t["name"] for t in post.get("tags") or [] if t.get("name"))),
        ("Created", post.get("created")),
    ):
        if value not in (None, ""):
            lines.append(f"{label}: {value}")

    parts = ["\n".join(lines), (post.get("details") or "").strip()]
    ordered = sorted(comments, key=lambda c: (c.get("created") or "", c.get("id") or ""))
    if ordered:
        rendered = []
        for comment in ordered:
            who = (comment.get("author") or {}).get("name") or "Unknown"
            text = (comment.get("value") or "").strip()
            if text:
                rendered.append(f"- {who}: {text}")
        if rendered:
            parts.append("Comments:\n" + "\n".join(rendered))

    return {
        "id": post.get("id"),
        "title": (post.get("title") or "").strip(),
        "content": "\n\n".join(p for p in parts if p),
        "url": post.get("url") or None,
    }
