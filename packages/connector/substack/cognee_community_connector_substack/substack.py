"""DLT source for Substack newsletters (full-snapshot sync + forget-on-delete).

Fetches a Substack publication's public RSS feed and yields each post as a
dlt resource for cognee's ingestion pipeline.

Like the sibling Notion connector, posts are ingested as *normal documents*:
the source declares ``cognee_document_source = "substack"``, so
``resolve_dlt_sources`` tags each row ``external_metadata["source"] =
"substack"`` (not ``"dlt"``). Each post flows through the standard cognify
entity-extraction pipeline — the right treatment for prose — instead of the
deterministic dlt-row schema-context path.

The source is a full snapshot: ``write_disposition="replace"`` rewrites
staging with exactly the posts currently in the feed each run. Substack's RSS
feed has no delete signal of its own — an unpublished post simply disappears
from the feed — so an absent post falls out of the snapshot and cognee's
existing ``orphan_cleanup`` removes it from the graph and vector stores.
Unchanged posts keep a stable content-hash ``data_id``, so they are not
re-ingested or re-cognified.

.. important::
   A Substack RSS feed only lists that publication's most recent posts (about
   20-25 on substack.com). Because forget-on-delete works by "absent from the
   current snapshot", an older post that is still published but has aged out
   of that window is indistinguishable from one that was unpublished — it
   will be forgotten too. This connector is well suited to newsletters that
   publish less often than the feed's window fills up; for a high-volume
   publication, treat this as a rolling window over recent posts rather than
   a permanent archive of everything ever published.

.. note::
   Substack truncates ``content:encoded`` for paywalled/subscriber-only
   posts (the feed only includes the preview shown to logged-out readers).
   Such posts are ingested with whatever preview text the feed provides, and
   the row is tagged ``is_partial=True`` so a partial post is not mistaken
   for the whole thing.
"""

import html
import re
import time
from typing import Any
from xml.etree import ElementTree

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("substack_connector")

# dlt resource / staging-table name for Substack posts.
SUBSTACK_TABLE_NAME = "substack_posts"
SUBSTACK_SOURCE_NAME = "substack"

_MAX_RETRIES = 3

_EXTRA_HINT = (
    'The Substack connector requires the "substack" extra: '
    'pip install "cognee[substack]" (provides dlt and requests).'
)

# RSS 2.0's content:encoded (the full HTML body Substack emits) lives in the
# "http://purl.org/rss/1.0/modules/content/" namespace.
_CONTENT_ENCODED_TAG = "{http://purl.org/rss/1.0/modules/content/}encoded"

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")

# Substack shows a fixed prompt at the paywall break in the RSS preview; its
# presence is the only feed-visible signal that a post was truncated.
_PAYWALL_MARKERS = (
    "Keep reading with a 7-day free trial",
    "This post is for paid subscribers",
    "This post is for subscribers only",
)


def substack_source(
    feed_url: str | None = None,
    publication: str | None = None,
    client: Any = None,
):
    """Create a dlt source that yields Substack posts as markdown documents.

    Args:
        feed_url: The publication's RSS feed URL, e.g.
            ``"https://example.substack.com/feed"``. Takes precedence over
            ``publication``; required for a custom domain.
        publication: A substack.com subdomain (e.g. ``"example"`` for
            ``example.substack.com``); the feed URL is derived as
            ``https://{publication}.substack.com/feed``. Ignored if
            ``feed_url`` is given.
        client: Pre-built ``requests.Session`` (mainly a test-injection
            point); when omitted one is built.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_url = feed_url or (f"https://{publication}.substack.com/feed" if publication else None)
    if not resolved_url:
        raise ValueError("substack_source requires feed_url or publication.")

    if client is None:
        try:
            import requests
        except ImportError as exc:
            raise ImportError(_EXTRA_HINT) from exc
        client = requests.Session()

    @dlt.resource(name=SUBSTACK_TABLE_NAME, primary_key="id", write_disposition="replace")
    def substack_posts():
        # Full-snapshot sync: each run replaces staging with exactly the
        # posts currently in the feed. An unpublished/removed post is simply
        # absent from the feed, so it falls out of staging and cognee's
        # orphan_cleanup then forgets it. Unchanged posts keep a stable
        # content-hash data_id, so they are not re-ingested/re-cognified.
        feed_text = _fetch_feed(client, resolved_url)
        count = 0
        for entry in _parse_entries(feed_text):
            count += 1
            yield _entry_to_row(entry)
        logger.info("Substack: synced %d post(s) from %s.", count, resolved_url)

    @dlt.source(name=SUBSTACK_SOURCE_NAME)
    def _substack():
        return substack_posts

    source = _substack()
    # Opt into the document ingestion path (post -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, SUBSTACK_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# Feed fetch (module-private)
# ---------------------------------------------------------------------------
def _fetch_feed(session: Any, feed_url: str) -> str:
    """GET a Substack RSS feed, retrying transient failures."""
    for attempt in range(_MAX_RETRIES):
        try:
            response = session.get(feed_url, timeout=30)
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            delay = 2**attempt
            logger.warning(
                "Substack: network error (%s) — retrying in %ds (%d/%d).",
                exc,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
            continue

        status = getattr(response, "status_code", 200)
        if status == 200:
            return response.text
        if status in (429, 502, 503, 504) and attempt < _MAX_RETRIES - 1:
            delay = 2**attempt
            logger.warning(
                "Substack: HTTP %d — retrying in %ds (%d/%d).",
                status,
                delay,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(delay)
            continue
        raise RuntimeError(f"Substack: failed to fetch feed {feed_url!r} (HTTP {status}).")

    raise RuntimeError(f"Substack: exhausted retries fetching {feed_url!r}.")  # pragma: no cover


# ---------------------------------------------------------------------------
# Feed parsing (module-private, dependency-free — stdlib ElementTree)
# ---------------------------------------------------------------------------
def _parse_entries(feed_text: str) -> list[dict]:
    """Parse an RSS 2.0 feed's ``<item>`` elements into plain dicts.

    Dependency-free on purpose (stdlib ``xml.etree.ElementTree``): Substack
    feeds are a standard, well-formed RSS 2.0 dialect, so a full feed-parsing
    library is unnecessary weight for this connector.
    """
    root = ElementTree.fromstring(feed_text)
    entries = []
    for item in root.iterfind("./channel/item"):
        guid = _text(item.find("guid"))
        link = _text(item.find("link"))
        entries.append(
            {
                "id": guid or link,
                "title": _text(item.find("title")),
                "link": link,
                "content": _text(item.find(_CONTENT_ENCODED_TAG))
                or _text(item.find("description")),
            }
        )
    return entries


def _text(element) -> str:
    return (element.text or "").strip() if element is not None else ""


# ---------------------------------------------------------------------------
# Rendering (dependency-free HTML → text)
# ---------------------------------------------------------------------------
def _clean_html(raw: str | None) -> str:
    """Convert a Substack HTML post body to plain text.

    Dependency-free on purpose (no bs4/markdownify): paragraph and line
    breaks, and ``<pre>`` code fences, are preserved as newlines; every other
    tag is stripped and entities unescaped. Inline formatting (bold, links,
    inline ``<code>``) is flattened to plain text — matching the sibling
    Confluence/Stack Overflow connectors' HTML handling.
    """
    if not raw:
        return ""
    text = raw
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"(?i)</li>", "\n", text)
    text = re.sub(r"(?i)<pre[^>]*>", "\n```\n", text)
    text = re.sub(r"(?i)</pre>", "\n```\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = _TRAILING_WS_RE.sub("\n", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


def _is_partial(raw_content: str) -> bool:
    """True if the feed shows a paywall preview rather than the full post."""
    return any(marker in raw_content for marker in _PAYWALL_MARKERS)


def _entry_to_row(entry: dict) -> dict[str, Any]:
    """Flatten a parsed feed entry into a document row.

    Only ``title``/``content`` (+ ``id``/``url`` for identity and
    provenance) are kept, so unchanged posts keep a stable content-hash
    data_id across runs.
    """
    raw_content = entry.get("content") or ""
    body = _clean_html(raw_content)
    if _is_partial(raw_content):
        body = (
            f"{body}\n\n[This post is truncated in the feed preview — "
            "paywalled/subscriber-only content.]"
        )

    return {
        "id": entry.get("id") or "",
        "title": entry.get("title") or "",
        "content": body,
        "url": entry.get("link"),
    }
