"""DLT source for Substack newsletters (full-snapshot sync + forget-on-delete).

Fetches posts from a Substack publication's public RSS feed using feedparser,
then yields them as a dlt resource for cognee's ingestion pipeline.

Unlike the relational dlt path (SQL/CSV), Substack posts are ingested as
*normal documents*: the source declares ``cognee_document_source = "substack"``,
so ``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "substack"``
(not ``"dlt"``). ``is_dlt_sourced`` therefore returns False and each post flows
through the standard cognify entity-extraction pipeline — the right treatment
for prose — instead of the deterministic dlt-row schema-context path.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging
with exactly the posts currently visible in the feed each run. Deletions propagate
for free — an unpublished post simply disappears from the feed, falls out of the
snapshot, and cognee's existing ``orphan_cleanup`` removes it from the graph and
vector stores. Unchanged posts keep a stable content-hash ``data_id``, so they
are not re-ingested or re-cognified.

Paywalled / subscriber-only posts have their ``content:encoded`` field truncated
(or absent) in the RSS feed. The connector detects this and marks such posts with
``is_partial=True`` instead of silently ingesting clipped text.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Iterator

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

if TYPE_CHECKING:
    pass

logger = get_logger("substack_connector")

# dlt resource / staging-table name for Substack posts.
SUBSTACK_TABLE_NAME = "substack_posts"
SUBSTACK_SOURCE_NAME = "substack"

_EXTRA_HINT = (
    "The Substack connector requires feedparser: "
    'pip install "cognee-community-connector-substack" '
    "(provides dlt and feedparser)."
)

# Minimum character count to consider content non-trivial / non-truncated.
# Substack paywalled previews are typically very short (< 500 chars) while
# full free posts are usually much longer.
_PARTIAL_CONTENT_THRESHOLD = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    """Remove HTML tags from a string and normalise whitespace.

    Uses a simple regex rather than a full parser; adequate for RSS content
    where structure is relatively predictable.
    """
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _content_hash(text: str) -> str:
    """Return a stable SHA-256 fingerprint for a post's content."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _parse_pub_date(entry) -> str | None:
    """Return an ISO-8601 UTC datetime string for *entry.published*, or None."""
    raw = getattr(entry, "published", None)
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return raw  # return raw string as fallback


def _extract_content(entry) -> tuple[str, bool]:
    """Return ``(content_text, is_partial)`` for a feed entry.

    Substack exposes full HTML in ``content[0].value`` (``content:encoded``) for
    free posts and omits or severely truncates it for paywalled posts. When the
    extracted text is shorter than ``_PARTIAL_CONTENT_THRESHOLD`` characters we
    flag ``is_partial=True`` and fall back gracefully to the summary/description.
    """
    # Primary source: content:encoded (full post HTML)
    content_list = getattr(entry, "content", None)
    if content_list:
        raw_html = content_list[0].get("value", "")
        text = _strip_html(raw_html)
        if len(text) >= _PARTIAL_CONTENT_THRESHOLD:
            return text, False
        # content present but very short → paywalled truncation
        if text:
            return text, True

    # Fallback: summary / description field
    summary = _strip_html(getattr(entry, "summary", "") or "")
    if summary:
        return summary, len(summary) < _PARTIAL_CONTENT_THRESHOLD

    return "", True  # no content at all → definitely partial


def _entry_to_row(entry) -> dict:
    """Convert a feedparser entry to a flat dict suitable for dlt.

    Row shape::

        {
            "id":          str,   # stable post identifier (link URL or guid)
            "title":       str,
            "url":         str,
            "content":     str,   # cleaned plain text
            "pub_date":    str | None,  # ISO-8601 UTC or None
            "author":      str | None,
            "is_partial":  bool,  # True when content is subscriber-only preview
            "content_hash": str,  # SHA-256 of content, for change-detection
        }
    """
    url = getattr(entry, "link", "") or ""
    title = getattr(entry, "title", "") or ""
    author = getattr(entry, "author", None)
    pub_date = _parse_pub_date(entry)

    content, is_partial = _extract_content(entry)

    # Use the guid (if present and stable) as the primary key; fall back to URL.
    guid = getattr(entry, "id", None) or url

    return {
        "id": guid,
        "title": title,
        "url": url,
        "content": content,
        "pub_date": pub_date,
        "author": author,
        "is_partial": is_partial,
        "content_hash": _content_hash(content),
    }


# ---------------------------------------------------------------------------
# Feed fetching helpers
# ---------------------------------------------------------------------------


def _build_feed_url(substack_url: str) -> str:
    """Normalise *substack_url* to an RSS feed URL.

    Accepts:
    * A bare subdomain:  ``example``  → ``https://example.substack.com/feed``
    * A full domain:     ``example.substack.com`` → ``https://example.substack.com/feed``
    * A full URL:        ``https://example.substack.com`` → ``https://example.substack.com/feed``
    * Already a feed:    ``https://example.substack.com/feed`` → unchanged
    """
    url = substack_url.strip().rstrip("/")
    if url.startswith("http://") or url.startswith("https://"):
        if not url.endswith("/feed"):
            url = url + "/feed"
        return url
    # bare subdomain or domain
    if "." not in url:
        url = f"https://{url}.substack.com/feed"
    else:
        url = f"https://{url}/feed"
    return url


def _fetch_feed(feed_url: str, http_etag: str | None = None, http_modified: str | None = None):
    """Fetch *feed_url* with feedparser and return the parsed feed object.

    Args:
        feed_url:       The RSS feed URL to fetch.
        http_etag:      Optional ETag from a previous fetch for conditional GET.
        http_modified:  Optional Last-Modified from a previous fetch.

    Returns:
        A feedparser ``FeedParserDict``.

    Raises:
        ImportError: If feedparser is not installed.
        RuntimeError: If the feed cannot be fetched (HTTP error, empty, etc.).
    """
    try:
        import feedparser
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    kwargs: dict = {}
    if http_etag:
        kwargs["etag"] = http_etag
    if http_modified:
        kwargs["modified"] = http_modified

    feed = feedparser.parse(feed_url, **kwargs)

    status = getattr(feed, "status", None)
    if status == 304:
        # Not modified since last fetch — return empty entries list.
        logger.info("Feed not modified (304): %s", feed_url)
        feed.entries = []
        return feed

    if status is not None and status >= 400:
        raise RuntimeError(
            f"Failed to fetch Substack feed {feed_url!r}: HTTP {status}. "
            "Check that the publication URL is correct and publicly accessible."
        )

    if feed.bozo and not feed.entries:
        exc = getattr(feed, "bozo_exception", None)
        raise RuntimeError(
            f"Failed to parse Substack feed {feed_url!r}: {exc}"
        )

    return feed


# ---------------------------------------------------------------------------
# Public dlt source
# ---------------------------------------------------------------------------


def substack_source(
    substack_url: str,
    max_posts: int | None = None,
    http_etag: str | None = None,
    http_modified: str | None = None,
):
    """Create a dlt source that yields Substack posts as plain-text documents.

    The feed URL is derived from *substack_url* — pass any of:

    * A bare subdomain name:   ``"example"``
    * A full domain:           ``"example.substack.com"``
    * A complete URL:          ``"https://example.substack.com"``
    * The RSS feed URL itself: ``"https://example.substack.com/feed"``

    No authentication is required; Substack's RSS feed is public.

    Args:
        substack_url:    Substack publication identifier (see above).
        max_posts:       Optional cap on number of posts to ingest. Useful for
                         testing or first-run bootstrapping.
        http_etag:       Optional ETag for conditional HTTP GET (bandwidth saving).
        http_modified:   Optional Last-Modified header value for conditional GET.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.

    Example::

        import cognee
        from cognee_community_connector_substack import substack_source

        await cognee.remember(
            substack_source("example.substack.com"),
            dataset_name="substack",
        )

        results = await cognee.search(
            query_text="What did the newsletter say about AI?",
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=["substack"],
        )
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    feed_url = _build_feed_url(substack_url)
    logger.info("Substack connector: feed URL resolved to %s", feed_url)

    @dlt.resource(
        name=SUBSTACK_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def substack_posts() -> Iterator[dict]:
        """Full-snapshot sync of Substack posts.

        Each run replaces staging with exactly the posts currently in the RSS
        feed. Posts removed from the feed (unpublished / deleted) fall out of
        the snapshot and cognee's orphan_cleanup forgets them from the graph
        and vector stores. Unchanged posts have a stable content_hash ``id``,
        so they are not re-ingested or re-cognified.

        Paywalled posts are marked ``is_partial=True`` rather than silently
        ingesting a truncated preview as if it were the full text.
        """
        feed = _fetch_feed(feed_url, http_etag=http_etag, http_modified=http_modified)

        entries = feed.entries
        if max_posts is not None:
            entries = entries[:max_posts]

        published_count = 0
        partial_count = 0

        for entry in entries:
            row = _entry_to_row(entry)
            if row["is_partial"]:
                partial_count += 1
                logger.debug(
                    "Post %r flagged as partial (subscriber-only preview): url=%s",
                    row["title"],
                    row["url"],
                )
            yield row
            published_count += 1

        logger.info(
            "Substack connector: yielded %d posts (%d partial) from %s",
            published_count,
            partial_count,
            feed_url,
        )

    # Mark this source as document-mode so resolve_dlt_sources routes posts
    # through normal cognify (LLM entity extraction) instead of the relational
    # dlt-row schema-context path.
    setattr(substack_posts, DOCUMENT_SOURCE_ATTR, SUBSTACK_SOURCE_NAME)

    @dlt.source(name=SUBSTACK_SOURCE_NAME)
    def _source():
        return substack_posts

    src = _source()
    setattr(src, DOCUMENT_SOURCE_ATTR, SUBSTACK_SOURCE_NAME)
    return src
