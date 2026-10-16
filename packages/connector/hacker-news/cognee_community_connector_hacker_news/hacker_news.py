"""dlt source for Hacker News stories, comments and threads, tracked by topic.

Discovery uses the Algolia HN Search API (far more practical than the official
Firebase API for topic filtering, per issue #4805): each configured topic is
searched with ``tags=story`` and a ``created_at_i`` lower bound covering the
tracked window. Threads are then hydrated from the official Firebase Item API
(``/v0/item/<id>.json``), which exposes each story's ``kids`` comment ids.

Sync model (mirrors the Notion connector): every run takes a **full snapshot**
of the tracked window with ``write_disposition="replace"``. Unchanged stories
keep byte-identical content, so their content-hash ``data_id`` is stable and
cognee does not re-ingest or re-cognify them (incremental in effect). Stories
that disappeared upstream (deleted, dead, or simply no longer returned) fall
out of the snapshot and cognee's existing ``orphan_cleanup`` forgets them from
the graph and vector stores.

A per-topic high-water cursor (``max_created_at_i``) is persisted in dlt state
for observability and to prove incremental progress across runs.

Error semantics: transient HTTP failures are retried, then abort the run — a
partial snapshot must never drive deletions under ``replace``. Permanently gone
items (404 / ``deleted`` / ``dead``) are omitted, which is exactly how
forget-on-delete is supposed to work. A malformed *story* hit aborts (its
identity is the row); a malformed *comment* is skipped with a warning (the
story is still yielded).
"""

import html
import re
import time
from datetime import UTC, datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("hacker_news_connector")

# dlt resource / staging-table name and source names.
HN_TABLE_NAME = "hacker_news_items"
# Public tag: what cognee records as external_metadata["source"] and what the
# document-mode marker carries.
HN_SOURCE_NAME = "hacker-news"
# dlt requires source names to be valid Python identifiers (no hyphens), so the
# dlt source itself is snake_case; the tag above is what cognee sees.
HN_DLT_SOURCE_NAME = "hacker_news"

_ALGOLIA_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
_FIREBASE_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item"
_HN_ITEM_URL = "https://news.ycombinator.com/item"

# Retry budget for rate-limited / transient HTTP responses.
_MAX_RETRIES = 5
_HTTP_TIMEOUT = 30

_EXTRA_HINT = (
    'The Hacker News connector requires the "hacker-news" extra: '
    'pip install "cognee-community-connector-hacker-news" (provides dlt and requests).'
)

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_P_RE = re.compile(r"</?p[^>]*>", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]+")


def hacker_news_source(
    topics: list[str],
    *,
    max_stories_per_topic: int = 25,
    max_comments_per_story: int = 10,
    comment_depth: int = 1,
    since_days: int = 30,
    http_client: Any = None,
):
    """Create a dlt source that yields Hacker News stories as markdown documents.

    Args:
        topics: Topics to track, e.g. ``["AI agents", "rust"]``. At least one
            non-empty topic is required — this is how the user selects what to
            ingest (acceptance criterion: "connects via None and selects what
            to ingest").
        max_stories_per_topic: Upper bound on stories fetched per topic per run.
        max_comments_per_story: Upper bound on comments hydrated per story
            (across all depths).
        comment_depth: How deep to follow comment threads (1 = top-level
            comments only).
        since_days: Length of the tracked window. Stories older than this are
            out of scope and fall out of the snapshot (and are forgotten).
        http_client: Object exposing ``get_json(url, params=None)`` returning
            parsed JSON (or ``None`` for 404/gone). Mainly a test-injection
            point; when omitted a ``requests``-based client is built.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    clean_topics = _validate_topics(topics)
    http = http_client if http_client is not None else _RequestsClient()

    @dlt.resource(name=HN_TABLE_NAME, primary_key="id", write_disposition="replace")
    def hn_items():
        # Full-snapshot sync over the tracked window (see module docstring).
        state = dlt.current.resource_state()
        cursors = state.setdefault("cursors", {})
        window_start_i = int(time.time()) - since_days * 86400

        seen: dict[str, dict] = {}
        for topic in clean_topics:
            for hit in _iter_topic_hits(http, topic, window_start_i, max_stories_per_topic):
                story_id = str(hit["objectID"])
                row_id = f"hn-{story_id}"
                if row_id in seen:
                    continue  # same story matched several topics
                item = _fetch_item(http, story_id)
                if item is None:
                    logger.info("Hacker News: story %s is gone upstream, skipping.", story_id)
                    continue
                comments = _fetch_comments(http, item, max_comments_per_story, comment_depth)
                seen[row_id] = _story_to_row(hit, item, comments, topic)
                created_i = int(hit.get("created_at_i") or 0)
                cursors[topic] = max(cursors.get(topic, 0), created_i)
                yield seen[row_id]

        logger.info(
            "Hacker News: synced %d item(s) across %d topic(s).", len(seen), len(clean_topics)
        )

    @dlt.source(name=HN_DLT_SOURCE_NAME)
    def _hn():
        return hn_items

    source = _hn()
    # Opt into the document ingestion path (row → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, HN_SOURCE_NAME)
    return source


def _validate_topics(topics: list[str]) -> list[str]:
    """Return stripped topics, or raise when nothing selectable was given."""
    if not isinstance(topics, (list, tuple)) or not topics:
        raise ValueError("hacker_news_source() requires at least one topic, e.g. ['rust'].")
    clean = [t.strip() for t in topics if isinstance(t, str) and t.strip()]
    if not clean:
        raise ValueError("hacker_news_source() requires at least one non-empty topic.")
    return clean


# ---------------------------------------------------------------------------
# Algolia discovery (topic-filtered, window-bounded, paginated)
# ---------------------------------------------------------------------------


def _iter_topic_hits(http: Any, topic: str, since_i: int, max_stories: int):
    """Yield raw Algolia story hits for ``topic`` newer than ``since_i``.

    Raises:
        ValueError: on a malformed hit (missing id/title) — identity is the
            row, so a bad hit aborts rather than silently corrupting the
            snapshot.
    """
    page = 0
    yielded = 0
    per_page = min(100, max_stories)
    while yielded < max_stories:
        payload = _with_retry(
            lambda page=page: http.get_json(
                _ALGOLIA_SEARCH_URL,
                params={
                    "query": topic,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{since_i}",
                    "hitsPerPage": per_page,
                    "page": page,
                },
            ),
            what=f"Algolia search for topic {topic!r}",
        )
        hits = (payload or {}).get("hits", [])
        if not hits:
            return
        for hit in hits:
            if not hit.get("objectID") or not hit.get("title"):
                raise ValueError(f"Hacker News: malformed story hit for topic {topic!r}: {hit!r}")
            yield hit
            yielded += 1
            if yielded >= max_stories:
                return
        nb_pages = (payload or {}).get("nbPages", 0)
        page += 1
        if page >= nb_pages:
            return


# ---------------------------------------------------------------------------
# Firebase hydration (threads)
# ---------------------------------------------------------------------------


def _fetch_item(http: Any, item_id: str) -> dict | None:
    """Fetch one Firebase item; ``None`` when it is gone upstream.

    Gone means: 404/empty response, or the ``deleted``/``dead`` flags. Returning
    ``None`` (rather than raising) is what makes forget-on-delete work — the
    item simply drops out of the snapshot.
    """
    data = _with_retry(
        lambda: http.get_json(f"{_FIREBASE_ITEM_URL}/{item_id}.json"),
        what=f"Hacker News item {item_id}",
    )
    if not data:
        return None
    if data.get("deleted") or data.get("dead"):
        return None
    return data


def _fetch_comments(http: Any, story: dict, max_comments: int, depth: int) -> list[dict]:
    """Hydrate a story's comment thread, bounded by count and depth.

    Malformed comments (no text) are skipped with a warning — the story itself
    is still yielded, so one bad comment can never wrongly forget a story.
    """
    comments: list[dict] = []
    stack = [(kid_id, 0) for kid_id in reversed(story.get("kids") or [])]
    while stack and len(comments) < max_comments:
        kid_id, level = stack.pop()
        node = _fetch_item(http, str(kid_id))
        if node is None:
            continue
        text = _clean_html(node.get("text") or "")
        if not text:
            logger.warning("Hacker News: skipping empty comment %s.", kid_id)
            continue
        comments.append(
            {
                "author": node.get("by") or "unknown",
                "time": node.get("time"),
                "text": text,
                "level": level,
            }
        )
        if level + 1 < depth:
            for sub_id in reversed(node.get("kids") or []):
                stack.append((str(sub_id), level + 1))
    return comments


# ---------------------------------------------------------------------------
# Row rendering (document-mode: id / url / title / content)
# ---------------------------------------------------------------------------


def _story_to_row(hit: dict, item: dict, comments: list[dict], topic: str) -> dict:
    """Flatten a story + its thread into a document row.

    Only stable fields feed ``content`` — volatile counters (points,
    num_comments) are deliberately excluded so a metadata-only change does not
    churn the content-hash ``data_id`` and trigger a pointless re-cognify.
    """
    story_id = str(hit["objectID"])
    title = hit.get("title") or ""
    url = hit.get("url") or f"{_HN_ITEM_URL}?id={story_id}"
    author = hit.get("author") or item.get("by") or "unknown"
    created = _fmt_date(hit.get("created_at_i"))

    lines = [
        f"# {title}",
        "",
        f"By {author} · {created} · [original]({url}) · topic: {topic}",
        "",
    ]
    story_text = _clean_html(item.get("text") or "")
    if story_text:
        lines += [story_text, ""]
    if comments:
        lines += ["## Discussion", ""]
        for comment in comments:
            prefix = "> " * comment["level"]
            lines.append(f"{prefix}**{comment['author']}** ({_fmt_date(comment['time'])}):")
            lines.append("")
            for text_line in comment["text"].splitlines():
                lines.append(f"{prefix}{text_line}" if text_line.strip() else prefix.rstrip())
            lines.append("")

    return {
        "id": f"hn-{story_id}",
        "url": url,
        "title": title,
        "content": "\n".join(lines).strip(),
    }


def _clean_html(raw: str) -> str:
    """Turn Hacker News HTML-escaped comment text into plain markdown-ish text."""
    if not raw:
        return ""
    text = _BR_RE.sub("\n", raw)
    text = _P_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    # Collapse runs of blank lines and trim the edges.
    collapsed: list[str] = []
    for line in lines:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    return "\n".join(collapsed).strip()


def _fmt_date(timestamp: Any) -> str:
    """Format a unix timestamp as YYYY-MM-DD (UTC); 'unknown' when absent."""
    try:
        return datetime.fromtimestamp(int(timestamp), tz=UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


# ---------------------------------------------------------------------------
# HTTP layer (retrying client + test-injection point)
# ---------------------------------------------------------------------------


class _RequestsClient:
    """Default HTTP client: ``get_json(url, params)``.

    Returns parsed JSON, or ``None`` for 404/410 (gone upstream). Other HTTP
    errors raise; retrying them is the caller's job (``_with_retry``), so the
    retry policy also applies to injected test clients.
    """

    def __init__(self) -> None:
        import requests

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "cognee-community-hacker-news/0.1.0"})

    def get_json(self, url: str, params: dict | None = None) -> Any:
        resp = self._session.get(url, params=params, timeout=_HTTP_TIMEOUT)
        if resp.status_code in (404, 410):
            return None  # gone upstream — the caller treats this as deleted
        resp.raise_for_status()
        return resp.json()


def _with_retry(operation, *, what: str):
    """Run ``operation``; retry transient failures, then let them abort the run."""
    for attempt in range(_MAX_RETRIES):
        try:
            return operation()
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            delay = _retry_delay(exc, attempt)
            logger.warning(
                "Hacker News: %s — retrying %s in %.1fs (%d/%d).",
                exc,
                what,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / timeout / network errors worth retrying."""
    import requests

    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return status in (429, 500, 502, 503, 504)
    return False


def _retry_delay(exc: Exception, attempt: int) -> float:
    """Seconds to wait: the Retry-After header when present, else backoff."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    header = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)
