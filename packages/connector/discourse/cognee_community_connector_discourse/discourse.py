"""dlt source for Discourse forum topics (incremental sync + forget-on-delete).

Fetches Discourse forum topics via the JSON API, exporting each topic as
raw Markdown, then yields them as a dlt resource for cognee's ingestion
pipeline.

Discourse topics are ingested as *normal documents*: the source declares
``cognee_document_source = "discourse"``, so ``resolve_dlt_sources`` tags each
row ``external_metadata["source"] = "discourse"`` (not ``"dlt"``). Forum Q&A
threads therefore flow through the standard cognify entity-extraction
pipeline — the right treatment for conversational prose.

Incremental sync uses the ``bumped_at`` field (last activity timestamp) from
the topic listing, with a small overlap window to catch late-updating threads.

Deletion: ``write_disposition="replace"`` so each sync produces the complete
current set of topics; anything dropped from the listing gets cleaned up via
orphan cleanup.

Public forums require no authentication. Private forums use the
``Api-Key`` + ``Api-Username`` header pattern.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("discourse_connector")

DISCOURSE_TABLE_NAME = "discourse_topics"
DISCOURSE_SOURCE_NAME = "discourse"

_MAX_RETRIES = 5
_OVERLAP_WINDOW_MINUTES = 5
_PAGE_SIZE = 30

_EXTRA_HINT = (
    'The Discourse connector requires the "discourse" extra: '
    'pip install "cognee[discourse]" (provides dlt and requests).'
)


def discourse_source(
    base_url: str | None = None,
    api_key: str | None = None,
    api_username: str | None = None,
    category_ids: list[int] | None = None,
    tags: list[str] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Discourse forum topics.

    Args:
        base_url: Discourse instance base URL (e.g. ``https://forum.example.com``).
            Falls back to ``DISCOURSE_BASE_URL``.
        api_key: Optional Discourse API key (for private forums). Falls back to
            ``DISCOURSE_API_KEY``.
        api_username: Optional API username (required when using api_key). Falls
            back to ``DISCOURSE_API_USERNAME``.
        category_ids: Optional list of category IDs to restrict ingestion scope.
        tags: Optional list of tags to filter topics.
        client: Pre-built HTTP client callable (test-injection point).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        try:
            import requests
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc

        resolved_url = (base_url or os.environ.get("DISCOURSE_BASE_URL", "")).rstrip("/")
        if not resolved_url:
            raise ValueError(
                "Discourse base URL required: pass base_url or set DISCOURSE_BASE_URL."
            )

        resolved_key = api_key or os.environ.get("DISCOURSE_API_KEY")
        resolved_user = api_username or os.environ.get("DISCOURSE_API_USERNAME")

        session = requests.Session()
        if resolved_key:
            session.headers.update({"Api-Key": resolved_key})
            if resolved_user:
                session.headers.update({"Api-Username": resolved_user})

        def _api_request(method: str, path: str, **kwargs: Any) -> Any:
            url = f"{resolved_url}{path}"
            for attempt in range(_MAX_RETRIES):
                response = session.request(method, url, **kwargs)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("retry-after", 2**attempt))
                    time.sleep(retry_after)
                    continue
                if response.status_code >= 500 and attempt < _MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                # /raw/{id} returns plain text, not JSON
                if "raw" in path:
                    return {"markdown": response.text}
                return response.json()
            raise RuntimeError(
                f"Discourse API: {method} {path} failed after {_MAX_RETRIES} retries."
            )

        client = _api_request

    @dlt.resource(
        name=DISCOURSE_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def discourse_topics() -> Iterator[dict[str, Any]]:
        import dlt as _runtime_dlt

        state = _runtime_dlt.current.resource_state()
        last_bumped = state.get("last_bumped_at")
        if last_bumped:
            cursor_dt = datetime.fromisoformat(last_bumped).replace(tzinfo=UTC) - timedelta(
                minutes=_OVERLAP_WINDOW_MINUTES
            )
            cursor_iso = cursor_dt.isoformat()
        else:
            cursor_iso = None

        count = 0
        for topic in _iter_topics(client, category_ids, tags):
            bumped_at = topic.get("bumped_at") or topic.get("last_posted_at")
            if cursor_iso and bumped_at and bumped_at < cursor_iso:
                continue

            count += 1
            yield _topic_to_row(client, topic)

            if bumped_at:
                current = state.get("last_bumped_at")
                if current is None or bumped_at > current:
                    state["last_bumped_at"] = bumped_at

        logger.info("Discourse: synced %d topic(s).", count)

    @dlt.source(name=DISCOURSE_SOURCE_NAME)
    def _discourse() -> Any:
        return discourse_topics

    source = _discourse()
    setattr(source, DOCUMENT_SOURCE_ATTR, DISCOURSE_SOURCE_NAME)
    return source


def _iter_topics(
    client: Any,
    category_ids: list[int] | None,
    tags: list[str] | None,
) -> Iterable[dict[str, Any]]:
    """Yield Discourse topics using pagination."""
    page = 0
    seen_ids: set[int] = set()

    while True:
        params: dict[str, Any] = {"page": page}
        if tags:
            params["tags"] = tags[0] if len(tags) == 1 else tags

        if category_ids:
            # If scoped to categories, fetch from each category's latest feed
            for cat_id in category_ids:
                yield from _fetch_topic_page(client, f"/c/{cat_id}/l/latest.json", params, seen_ids)
        else:
            yield from _fetch_topic_page(client, "/latest.json", params, seen_ids)

        page += 1
        # Stop when we've gone through several pages without new topics
        # (Discourse latest.json typically has a practical limit)
        if page > 100:
            break


def _fetch_topic_page(
    client: Any,
    endpoint: str,
    params: dict[str, Any],
    seen_ids: set[int],
) -> Iterator[dict[str, Any]]:
    """Fetch one page of topics from a Discourse listing endpoint."""
    try:
        data = client("GET", endpoint, params=params)
    except Exception:
        return

    topic_list = data.get("topic_list", {})
    topics = topic_list.get("topics", [])

    if not topics:
        return

    for topic in topics:
        topic_id = topic.get("id")
        if topic_id is None or topic_id in seen_ids:
            continue
        seen_ids.add(topic_id)
        yield topic


def _topic_to_row(client: Any, topic: dict[str, Any]) -> dict[str, Any]:
    """Transform a raw Discourse topic listing into a cognee document row."""
    topic_id = topic.get("id", 0)
    title = topic.get("title", "Untitled topic")
    slug = topic.get("slug", "")

    # Build body parts
    body_parts: list[str] = []
    body_parts.append(f"Discourse topic: {title}")
    body_parts.append(f"URL: /t/{slug}/{topic_id}")

    category_id = topic.get("category_id")
    if category_id:
        body_parts.append(f"Category ID: {category_id}")

    topic_tags = topic.get("tags", [])
    if topic_tags:
        body_parts.append(f"Tags: {', '.join(topic_tags)}")

    body_parts.append(f"Created: {topic.get('created_at', 'unknown')}")
    body_parts.append(f"Last activity: {topic.get('bumped_at', 'unknown')}")
    body_parts.append(f"Posts: {topic.get('posts_count', 0)}")
    body_parts.append(f"Views: {topic.get('views', 0)}")

    poster = topic.get("posters", [])
    if poster and isinstance(poster, list):
        first_poster = poster[0]
        if isinstance(first_poster, dict):
            user = first_poster.get("user", {})
            if isinstance(user, dict) and user.get("username"):
                body_parts.append(f"Posted by: {user.get('username')}")

    # Fetch raw markdown
    try:
        raw_data = client("GET", f"/raw/{topic_id}")
        if isinstance(raw_data, dict) and raw_data.get("markdown"):
            body_parts.append(f"Content:\n{raw_data['markdown']}")
    except Exception:
        logger.warning("Failed to fetch raw markdown for topic %s", topic_id)

    return {
        "id": topic_id,
        "category_id": category_id,
        "slug": slug,
        "title": title,
        "tags": topic_tags,
        "created_at": topic.get("created_at"),
        "bumped_at": topic.get("bumped_at") or topic.get("last_posted_at"),
        "posts_count": topic.get("posts_count"),
        "views": topic.get("views"),
        "like_count": topic.get("like_count"),
        "text": "\n\n".join(body_parts),
        "raw": topic,
        "source": DISCOURSE_SOURCE_NAME,
    }
