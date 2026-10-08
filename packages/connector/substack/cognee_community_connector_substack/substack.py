"""DLT source for Substack newsletters (RSS, full-snapshot sync + forget-on-delete).

Reads a publication's public RSS feed (no auth) and yields each post as a markdown
document for cognee's ingestion pipeline.

Like the Notion connector, posts are ingested as *normal documents*: the source
declares ``cognee_document_source = "substack"``, so ``resolve_dlt_sources`` tags
each row ``external_metadata["source"] = "substack"`` and every post flows through
the standard cognify entity-extraction pipeline.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging with
exactly the posts currently in the feed each run. An unpublished/deleted post simply
disappears from the feed (RSS has no delete signal), so it is absent from the
snapshot and cognee's existing ``orphan_cleanup`` removes it from the graph and
vector stores. Unchanged posts keep a stable content-hash ``data_id`` so they are
not re-ingested or re-cognified; that is the "incremental" behaviour.

Known limitation: Substack's feed only exposes the most recent posts (typically
~20). A post that rolls out of that window is indistinguishable from a deleted one
and will be forgotten on the next sync.

Safety: a fetch/parse failure, or a feed with zero posts, aborts the run instead of
yielding a partial/empty snapshot, because under ``replace`` that would be read as
"everything was deleted".
"""

import re
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("substack_connector")

# dlt resource / staging-table name for Substack posts.
SUBSTACK_TABLE_NAME = "substack_posts"
SUBSTACK_SOURCE_NAME = "substack"

_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
_DC_NS = "http://purl.org/dc/elements/1.1/"

# Retry budget for rate-limited / transient responses.
_MAX_RETRIES = 5
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
_HEADERS = {
    # Substack's CDN rejects some default client user agents.
    "User-Agent": "cognee-community-connector-substack/0.1 (+https://github.com/topoteretes/cognee)",
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

_EXTRA_HINT = (
    "The Substack connector requires dlt: pip install cognee-community-connector-substack "
    "(provides dlt and httpx)."
)

# Phrases Substack puts in the feed body of paywalled posts. Heuristic: the RSS
# format has no official "this is truncated" flag.
_PAYWALL_MARKERS = (
    "paid subscribers",
    "paying subscribers",
    "subscribe to continue reading",
    "upgrade to paid",
    "this post is for subscribers",
    'class="paywall"',
)
_PARTIAL_NOTE = "_[Preview only: the full post is behind a paywall.]_"


def substack_source(publication: str, fetch: Callable[[str], Any] | None = None):
    """Create a dlt source that yields Substack posts as markdown documents.

    Args:
        publication: Publication name (``"platformer"``), host
            (``"platformer.news"``) or full feed URL
            (``"https://platformer.substack.com/feed"``).
        fetch: Callable ``url -> bytes | str`` returning the feed XML. Mainly a
            test-injection point; defaults to an HTTP fetch with retry/backoff.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    feed_url = resolve_feed_url(publication)
    fetcher = fetch or _fetch_feed

    @dlt.resource(name=SUBSTACK_TABLE_NAME, primary_key="id", write_disposition="replace")
    def substack_posts():
        # Full-snapshot sync: each run replaces staging with exactly the posts in
        # the feed right now. Errors are NOT swallowed (see module docstring).
        rows = _parse_feed(fetcher(feed_url))
        if not rows:
            raise ValueError(
                f"Substack: feed {feed_url} contained no posts; refusing to replace the "
                "snapshot with an empty set (this would forget everything)."
            )
        yield from rows
        logger.info("Substack: synced %d post(s) from %s.", len(rows), feed_url)

    @dlt.source(name=SUBSTACK_SOURCE_NAME)
    def _substack():
        return substack_posts

    source = _substack()
    # Opt into the document ingestion path (post -> text document -> cognify).
    setattr(source, DOCUMENT_SOURCE_ATTR, SUBSTACK_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Feed URL + HTTP helpers (module-private)
# ---------------------------------------------------------------------------


def resolve_feed_url(publication: str) -> str:
    """Turn a publication name, host or URL into its RSS feed URL."""
    value = (publication or "").strip()
    if not value:
        raise ValueError("Substack publication required, e.g. 'platformer' or a feed URL.")
    if "://" not in value:
        host = value if "." in value else f"{value}.substack.com"
        value = f"https://{host}"
    parsed = urlparse(value)
    if not parsed.netloc:
        raise ValueError(f"Could not parse Substack publication: {publication!r}")
    path = parsed.path.rstrip("/")
    if not path.endswith("/feed"):
        path = "/feed"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _fetch_feed(url: str, client: httpx.Client | None = None) -> bytes:
    """GET the feed, retrying rate-limit / transient errors with backoff.

    Permanent errors (404, 403, ...) and exhausted retries propagate.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        for attempt in range(_MAX_RETRIES):
            try:
                response = client.get(url, headers=_HEADERS)
                response.raise_for_status()
                return response.content
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                    raise
                headers = exc.response.headers if isinstance(exc, httpx.HTTPStatusError) else None
                delay = _retry_after(headers, attempt)
                logger.warning(
                    "Substack: %s - retrying in %.1fs (%d/%d).",
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover
    finally:
        if owns_client:
            client.close()


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / timeout / network errors worth retrying."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUSES
    return False


def _retry_after(headers, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


# ---------------------------------------------------------------------------
# Feed parsing (module-private)
# ---------------------------------------------------------------------------


def _parse_feed(data: bytes | str) -> list[dict]:
    """Parse RSS 2.0 XML into document rows (deduplicated by post id)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"Substack: feed is not valid XML: {exc}") from exc

    channel = root.find("channel")
    if channel is None:
        raise ValueError("Substack: response is not an RSS feed (no <channel>).")

    rows: list[dict] = []
    seen: set[str] = set()
    for item in channel.findall("item"):
        row = _item_to_row(item)
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append(row)
    return rows


def _item_to_row(item: ET.Element) -> dict:
    """Flatten one RSS <item> into a document row.

    Only stable fields are kept (no volatile counters), so unrelated feed churn
    does not change the content-hash data_id.
    """
    guid = _text(item.find("guid"))
    link = _text(item.find("link"))
    post_id = guid or link
    if not post_id:
        raise ValueError("Substack: feed item has neither <guid> nor <link>.")

    encoded_html = _text(item.find(f"{{{_CONTENT_NS}}}encoded"))
    source_html = encoded_html or _text(item.find("description"))
    body = _html_to_markdown(source_html)

    is_partial = (not encoded_html) or _looks_paywalled(source_html)
    if is_partial:
        body = f"{body}\n\n{_PARTIAL_NOTE}".strip()

    return {
        "id": post_id,
        "url": link,
        "title": _text(item.find("title")),
        "author": _text(item.find(f"{{{_DC_NS}}}creator")),
        "published": _parse_date(_text(item.find("pubDate"))),
        "is_partial": is_partial,
        "content": body,
    }


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def _parse_date(value: str) -> str:
    """RFC 822 pubDate -> ISO 8601; falls back to the raw string."""
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


def _looks_paywalled(html: str) -> bool:
    lowered = (html or "").lower()
    return any(marker in lowered for marker in _PAYWALL_MARKERS)


# ---------------------------------------------------------------------------
# HTML -> markdown (stdlib only)
# ---------------------------------------------------------------------------

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_TAGS = {"p", "div", "figure", "figcaption", "table", "tr"}


class _MarkdownParser(HTMLParser):
    """Minimal HTML -> markdown converter for newsletter bodies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0
        self._pre = 0
        self._lists: list[list] = []  # [tag, counter]

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif self._skip:
            return
        elif tag in _HEADINGS:
            self.out.append("\n\n" + "#" * _HEADINGS[tag] + " ")
        elif tag in ("ul", "ol"):
            self._lists.append([tag, 0])
            self.out.append("\n")
        elif tag == "li":
            depth = max(len(self._lists) - 1, 0)
            if self._lists and self._lists[-1][0] == "ol":
                self._lists[-1][1] += 1
                marker = f"{self._lists[-1][1]}."
            else:
                marker = "-"
            self.out.append("\n" + "  " * depth + marker + " ")
        elif tag == "pre":
            self._pre += 1
            self.out.append("\n\n```\n")
        elif tag == "blockquote":
            self.out.append("\n\n> ")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self.out.append("\n\n---\n\n")
        elif tag in _BLOCK_TAGS:
            self.out.append("\n\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(self._skip - 1, 0)
        elif self._skip:
            return
        elif tag in _HEADINGS or tag in _BLOCK_TAGS or tag == "blockquote":
            self.out.append("\n\n")
        elif tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            self.out.append("\n")
        elif tag == "pre":
            self._pre = max(self._pre - 1, 0)
            self.out.append("\n```\n\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._pre:
            self.out.append(data)
            return
        text = re.sub(r"\s+", " ", data)
        if not self.out or self.out[-1][-1:] in ("\n", " "):
            text = text.lstrip()
        if text:
            self.out.append(text)


def _html_to_markdown(html: str) -> str:
    """Convert a feed HTML body to readable markdown text."""
    if not html:
        return ""
    parser = _MarkdownParser()
    parser.feed(html)
    parser.close()
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
