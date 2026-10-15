"""DLT source for RSS / Atom feeds (full-snapshot sync + forget-on-delete).

Fetches one or more feeds, parses them with feedparser (RSS and Atom, and
malformed feeds gracefully), and yields each entry as a dlt resource row for
cognee's ingestion pipeline.

Like the Notion connector, feed entries are ingested as *normal documents*: the
source declares ``cognee_document_source = "rss"``, so ``resolve_dlt_sources``
tags each row ``external_metadata["source"] = "rss"`` (not ``"dlt"``).
``is_dlt_sourced`` therefore returns False and each entry flows through the
standard cognify entity-extraction pipeline — the right treatment for prose —
instead of the deterministic dlt-row schema-context path.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging
with exactly the entries currently present in the configured feeds each run.
Deletions propagate for free — an entry dropped upstream is absent from the
snapshot and cognee's existing ``orphan_cleanup`` removes it from the graph and
vector stores. Unchanged entries keep a stable content-hash ``data_id``, so they
are not re-ingested or re-cognified; only new or edited entries do work
(incremental sync without a cursor).

Because most feeds are a rolling window, an entry that simply ages out of the
feed is reconciled out of memory too. Point the connector at a full-archive feed,
or ingest into a dedicated dataset, when that is not the behaviour you want.
"""

import html
import os
import re
import time
from collections.abc import Callable
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("rss_connector")

# dlt resource / staging-table name for feed entries.
RSS_TABLE_NAME = "rss_entries"
RSS_SOURCE_NAME = "rss"

# Retry budget for rate-limited / transient feed fetches.
_MAX_RETRIES = 5
_DEFAULT_TIMEOUT = 30.0

_EXTRA_HINT = (
    'The RSS connector requires dlt and feedparser: pip install "cognee-community-connector-rss".'
)

# Turn block-level tags into newlines before stripping the rest, so paragraphs
# and list items in HTML feed bodies do not run together into one line.
_BREAK_RE = re.compile(r"(?i)<\s*(?:br|/p|/div|/li|/h[1-6])\s*/?>")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def rss_source(
    feed_urls: str | list[str] | None = None,
    fetcher: Callable[[str], bytes] | None = None,
):
    """Create a dlt source that yields RSS / Atom entries as documents.

    Args:
        feed_urls: One or more feed URLs (RSS or Atom). A single string is
            accepted for convenience. Falls back to the ``RSS_FEED_URLS`` env
            var (comma- or whitespace-separated).
        fetcher: Pre-built ``url -> bytes`` callable (mainly a test-injection
            point); when omitted feeds are fetched over HTTPS with httpx.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc
    try:
        import feedparser  # noqa: F401
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    urls = _resolve_feed_urls(feed_urls)
    if not urls:
        raise ValueError("RSS feed URL(s) required: pass feed_urls= or set RSS_FEED_URLS.")
    fetch = fetcher or _default_fetcher

    @dlt.resource(name=RSS_TABLE_NAME, primary_key="id", write_disposition="replace")
    def rss_entries():
        # Full-snapshot sync: each run replaces staging with exactly the entries
        # currently in the feeds. An entry dropped upstream falls out of staging
        # and cognee's orphan_cleanup forgets it; unchanged entries keep a stable
        # content-hash data_id and are not re-cognified.
        #
        # A fetch/parse failure is NOT swallowed (see _iter_entries): because
        # staging is authoritative under replace, a feed missing from a partial
        # snapshot would forget all of its entries as if deleted. Letting the
        # error abort the run leaves staging — and memory — untouched, which is
        # the safe failure.
        count = 0
        for url in urls:
            for row in _iter_entries(url, fetch):
                count += 1
                yield row
        logger.info("RSS: synced %d entr(ies) from %d feed(s).", count, len(urls))

    @dlt.source(name=RSS_SOURCE_NAME)
    def _rss():
        return rss_entries

    source = _rss()
    # Opt into the document ingestion path (entry → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, RSS_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _resolve_feed_urls(feed_urls: str | list[str] | None) -> list[str]:
    """Normalise the feed_urls argument (or env fallback) to a list of URLs."""
    if feed_urls is None:
        raw = os.environ.get("RSS_FEED_URLS", "")
        return [url for url in re.split(r"[,\s]+", raw) if url]
    if isinstance(feed_urls, str):
        return [feed_urls] if feed_urls else []
    return [url for url in feed_urls if url]


# ---------------------------------------------------------------------------
# Fetching (with retry) — module-private
# ---------------------------------------------------------------------------


def _default_fetcher(url: str) -> bytes:
    """Fetch a feed's raw bytes over HTTPS, retrying transient errors."""
    import httpx

    def _get() -> bytes:
        response = httpx.get(
            url,
            timeout=_DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "cognee-community-connector-rss"},
        )
        response.raise_for_status()
        return response.content

    return _request(_get)


def _request(call: Callable[[], Any]):
    """Run ``call``, retrying rate-limit / server / timeout / network errors.

    Permanent errors (e.g. 404) and exhausted retries propagate so the caller
    can decide — under replace, aborting is safer than committing a partial
    snapshot that reconciles live entries as deletions.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            return call()
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            headers = getattr(getattr(exc, "response", None), "headers", None)
            delay = _retry_after(headers, attempt)
            logger.warning(
                "RSS: %s — retrying in %.1fs (%d/%d).", exc, delay, attempt + 1, _MAX_RETRIES
            )
            time.sleep(delay)


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / timeout / network errors worth retrying."""
    import httpx

    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


def _retry_after(headers: Any, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


# ---------------------------------------------------------------------------
# Parsing — module-private
# ---------------------------------------------------------------------------


def _iter_entries(url: str, fetch: Callable[[str], bytes]):
    """Yield document rows for one feed's entries.

    Require a recognized RSS/Atom format before accepting an empty result.
    feedparser can parse an HTML/XML error page without setting ``bozo``; that
    response is not an authoritative empty feed. Malformed recognized feeds
    that still yield entries are used with a warning.
    """
    import feedparser

    parsed = feedparser.parse(fetch(url))
    if not parsed.get("version", "").startswith(("rss", "atom")):
        raise ValueError(f"RSS: response from {url} is not a recognized RSS/Atom feed")
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        raise ValueError(f"RSS: feed {url} could not be parsed: {parsed.get('bozo_exception')}")
    if getattr(parsed, "bozo", 0):
        logger.warning(
            "RSS: feed %s is malformed but yielded %d entr(ies): %s",
            url,
            len(parsed.entries),
            parsed.get("bozo_exception"),
        )

    for entry in parsed.entries:
        row = _entry_to_row(entry)
        if row is not None:
            yield row


def _entry_to_row(entry: Any) -> dict | None:
    """Flatten a feed entry into a document row.

    Only ``id``/``url``/``title``/``content`` are kept, so a metadata-only change
    (e.g. a re-stamped ``published`` with identical text) does not churn the
    content-hash data_id. An entry with no usable identity is skipped.
    """
    entry_id = _entry_id(entry)
    if not entry_id:
        return None
    return {
        "id": entry_id,
        "url": entry.get("link", ""),
        "title": entry.get("title", ""),
        "content": _entry_content(entry),
    }


def _entry_id(entry: Any) -> str:
    """Stable identity: RSS <guid> / Atom <id>, falling back to the link."""
    return entry.get("id") or entry.get("guid") or entry.get("link") or ""


def _entry_content(entry: Any) -> str:
    """Plain-text body: Atom <content> first, then RSS <description>/summary."""
    raw = ""
    contents = entry.get("content")
    if contents:
        raw = contents[0].get("value", "")
    if not raw:
        raw = entry.get("summary", "") or entry.get("description", "")
    return _html_to_text(raw)


def _html_to_text(text: str) -> str:
    """Strip HTML tags and unescape entities from a feed body."""
    if not text:
        return ""
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()
