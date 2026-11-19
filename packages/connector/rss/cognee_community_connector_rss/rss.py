"""RSS / Atom feed connector for cognee — a ``dlt`` source that turns feeds into memory.

Subscribe to blogs, changelogs, newsletters, and release feeds — "ask my feeds".
Like the sibling Confluence connector this builds entirely on the existing DLT
ingestion subsystem; the source produced here is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_rss import rss_source

    await cognee.remember(
        rss_source(feed_urls=["https://example.com/feed.xml"]),
        dataset_name="my_feeds",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by entry id
        max_rows_per_table=0,        # 0 = no row cap (see note below)
    )

Design
------
* **Auth** — none. Any RSS 2.0 / RSS 1.0 / Atom URL works.
* **Primary key** — the entry id (RSS ``guid`` / Atom ``id``), namespaced with a
  short hash of the feed URL so two feeds carrying the same guid cannot
  collide. Entries without an id fall back to the link, then to a stable hash
  of the entry content.
* **Incremental signal** — the entry's ``updated`` / ``published`` timestamp
  (per the issue spec). An entry is re-emitted only when its timestamp is newer
  than what was stored on the previous run; entries whose feed carries no
  usable timestamps fall back to a content-hash comparison. The per-entry
  state lives in dlt's per-resource state, so re-running ``remember`` resumes
  where it left off and re-embeds only the delta.
* **Forget-on-delete** — feeds have no deletion feed: an entry that is deleted
  upstream simply disappears from the feed document. Each run re-fetches every
  configured feed and compares the live entry ids against the ids seen on the
  previous run; vanished entries are emitted with the ``_deleted`` hard-delete
  marker, dlt removes those rows on ``merge``, and cognee's existing
  ``orphan_cleanup`` purges them from the graph + vector + relational stores.
  A feed that fails to fetch or parses to zero entries is skipped for the run
  (its entries are never tombstoned on unseen evidence), and removing a feed
  URL from ``feed_urls`` tombstones that feed's entries on the next sync.
* **Content** — HTML in ``content:encoded`` / ``atom:content`` / ``description``
  / ``summary`` is stripped to plain text (entries are prose — raw markup would
  pollute entity extraction). ``content`` is preferred over ``summary``, and
  over ``description`` on RSS. A title-only entry falls back to its title as
  content.

.. note::
   cognee's ``ingest_dlt_source`` reads at most ``max_rows_per_table`` rows
   from the dlt destination (default 50). For real feeds pass
   ``max_rows_per_table=0`` (unlimited) so orphan-cleanup compares against the
   *whole* synced corpus rather than a truncated window.

.. note::
   Feeds that window their archive (e.g. "latest 20 items only") treat entries
   scrolling off the feed as upstream deletions — that is the only deletion
   signal RSS has, and it matches the snapshot semantics of the Notion
   connector.

.. note::
   Syncing several *different* feed sets into one dataset? Give each
   ``rss_source(...)`` call its own ``resource_name`` so each set keeps its own
   dlt state and staging table (otherwise one set's sync would tombstone the
   other's entries).
"""

from __future__ import annotations

import calendar
import hashlib
import html
import re
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("rss_connector")

# dlt resource / staging-table name, and the system_metadata["source"] tag
# stamped on every document this connector produces.
RSS_SOURCE_NAME = "rss"
RSS_TABLE_NAME = "rss_entries"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_FETCH_TIMEOUT_SECONDS = 30
_USER_AGENT = "cognee-community-connector-rss/0.1"

FetchFn = Callable[[str], bytes]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _clean_html(raw: str | None) -> str:
    """Strip feed markup down to plain text.

    Entries are prose; feeding raw HTML into entity extraction is noisy, so we
    drop tags, unescape entities, and collapse whitespace. Dependency-free on
    purpose (feedparser already sanitized the markup, this just flattens it).
    """
    if not raw:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", raw))).strip()


def _feed_tag(feed_url: str) -> str:
    """Stable short hash of the feed URL, used to namespace entry ids per feed."""
    return hashlib.sha256(feed_url.encode("utf-8")).hexdigest()[:12]


def _entry_timestamp(entry: dict) -> int | None:
    """Entry change signal as a UTC epoch second: ``updated``, else ``published``.

    feedparser normalizes RSS ``pubDate`` and Atom ``updated``/``published``
    into ``*_parsed`` UTC struct_times. ``None`` when the entry carries no
    usable timestamp (the caller then falls back to the content hash).
    """
    parsed = entry.get("updated_parsed") or entry.get("published_parsed")
    return calendar.timegm(parsed) if parsed else None


def _entry_content(entry: dict) -> str:
    """Full entry text: Atom/RSS ``content`` when present, else ``summary``.

    feedparser maps RSS ``content:encoded`` and Atom ``content`` onto the
    ``content`` list and RSS ``description`` / Atom ``summary`` onto
    ``summary``. Full content is preferred over the summary excerpt.
    """
    content = entry.get("content")
    if isinstance(content, list) and content and content[0].get("value"):
        raw = content[0]["value"]
    else:
        raw = entry.get("summary") or ""
    return _clean_html(raw)


def _entry_to_row(entry: dict, feed_url: str) -> tuple[dict[str, Any], int | None, str]:
    """Flatten a feedparser entry into a document row.

    Returns ``(row, timestamp, content_hash)``. The row id is namespaced with
    the feed's tag so identical guids across feeds cannot collide under the
    ``id`` primary key. Only identity/provenance + text are kept, so the
    content-hash ``data_id`` cognee derives downstream does not churn on
    feed-level metadata changes.
    """
    title = _clean_html(entry.get("title") or "")
    content = _entry_content(entry) or title
    url = entry.get("link") or ""
    timestamp = _entry_timestamp(entry)

    raw_id = entry.get("id") or url
    if not raw_id:
        raw_id = hashlib.sha256(f"{title}\n{content}\n{timestamp}".encode()).hexdigest()

    row = {
        "id": f"{_feed_tag(feed_url)}:{raw_id}",
        "title": title,
        "content": content,
        "url": url,
        "_deleted": False,
    }
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return row, timestamp, content_hash


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _http_fetch(feed_url: str) -> bytes:
    """Fetch the raw feed document. Stdlib-only so no extra dependency is needed."""
    request = urllib.request.Request(feed_url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        return response.read()


def _parse_feed(raw: bytes) -> Any:
    """Parse RSS 2.0 / RSS 1.0 / Atom bytes with feedparser.

    feedparser is deliberately tolerant: it recovers entries from real-world
    malformed documents (``bozo=True`` but entries present) and reports what it
    had to fix via ``bozo_exception``. Only a document with no recoverable
    entries is treated as unusable by the caller.
    """
    import feedparser

    return feedparser.parse(raw)


# ---------------------------------------------------------------------------
# Sync (pure given a fetch fn + state dict — unit-testable)
# ---------------------------------------------------------------------------
def sync_feeds(
    fetch: FetchFn,
    feed_urls: list[str],
    state: dict,
    *,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield new/changed entries since the last run, plus hard-delete markers.

    One fetch per feed enumerates the *current* entries; per-entry timestamps
    (content hash as fallback) drive change detection, and the id set drives
    deletion detection. All state (``entries``) is advanced in ``state`` so the
    next run is a no-op when nothing changed. A feed whose fetch fails or that
    parses to zero entries is skipped for the run — its entries are neither
    emitted nor tombstoned on that evidence, matching the connector's
    transient-failure posture.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(fetched_feeds=0, skipped_feeds=0, emitted=0, deleted=0, skipped_empty=0)

    # Fetching the same URL twice would double-emit rows; dedupe, keep order.
    seen_urls: list[str] = []
    for feed_url in feed_urls:
        if feed_url not in seen_urls:
            seen_urls.append(feed_url)

    known: dict[str, dict] = dict(state.get("entries", {}))
    current_tags = {_feed_tag(feed_url) for feed_url in seen_urls}
    fetched_tags: set[str] = set()
    present_by_tag: dict[str, set[str]] = {}
    seen_entries: dict[str, dict] = {}

    for feed_url in seen_urls:
        tag = _feed_tag(feed_url)
        try:
            raw = fetch(feed_url)
        except Exception as exc:
            stats["skipped_feeds"] += 1
            logger.warning("RSS: skipping feed %s (fetch failed): %s", feed_url, exc)
            continue

        feed = _parse_feed(raw)
        entries = list(feed.entries or [])
        if not entries:
            # Malformed (bozo) documents and transient empty responses both land
            # here. Tombstoning on zero parsed entries would mass-delete the
            # feed's memory on one bad response, so the feed is skipped and its
            # prior state preserved instead.
            stats["skipped_feeds"] += 1
            reason = "unparseable" if feed.bozo else "no entries"
            logger.warning("RSS: skipping feed %s (%s).", feed_url, reason)
            continue

        fetched_tags.add(tag)
        present = set()
        present_by_tag[tag] = present
        for entry in entries:
            row, timestamp, content_hash = _entry_to_row(entry, feed_url)
            key = row["id"]
            if not row["content"]:
                # Neither content nor title — nothing to remember.
                stats["skipped_empty"] += 1
                continue
            present.add(key)

            previous = known.get(key)
            changed = previous is None
            if not changed:
                if timestamp is not None and previous.get("ts") is not None:
                    changed = timestamp > previous["ts"]
                else:
                    changed = content_hash != previous.get("hash")
            seen_entries[key] = {"ts": timestamp, "hash": content_hash}
            if changed:
                stats["emitted"] += 1
                yield row

    # Deletion detection: a known entry that vanished from its (successfully
    # fetched) feed, or whose whole feed was removed from the configuration,
    # is gone upstream — emit the hard-delete marker so dlt's merge drops it
    # and cognee's orphan_cleanup forgets it from memory.
    deleted: list[str] = []
    for key in known:
        tag = key.split(":", 1)[0]
        if tag not in current_tags:
            # The feed URL was removed from the configuration: its entries are
            # no longer wanted, regardless of what this run's fetches did.
            deleted.append(key)
        elif tag in fetched_tags and key not in present_by_tag[tag]:
            # The feed was fetched successfully but no longer carries the
            # entry: it is gone upstream.
            deleted.append(key)

    for key in sorted(deleted):
        seen_entries.pop(key, None)
        stats["deleted"] += 1
        yield {"id": key, "_deleted": True}

    # Failed/skipped feeds keep their prior state so a transient outage never
    # turns into deletions on a later run.
    for key, meta in known.items():
        if key.split(":", 1)[0] not in fetched_tags and key.split(":", 1)[0] in current_tags:
            seen_entries.setdefault(key, meta)

    state["entries"] = seen_entries
    logger.info(
        "RSS: %d feed(s) synced, %d entr(ies) emitted, %d deletion(s), %d feed(s) skipped.",
        len(fetched_tags),
        stats["emitted"],
        stats["deleted"],
        stats["skipped_feeds"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def rss_source(
    feed_urls: list[str],
    *,
    resource_name: str = RSS_TABLE_NAME,
    fetch: FetchFn | None = None,
):
    """Return a ``dlt`` resource that yields RSS/Atom entries for ``remember``.

    Args:
        feed_urls: Feed URLs to sync (RSS 2.0, RSS 1.0/RDF, or Atom).
        resource_name: Stable dlt resource name. dlt state is keyed per
            resource name, so hosts syncing several different feed sets into
            one dataset should give each set its own name (and thereby its own
            incremental state and staging table).
        fetch: Replacement for the HTTP fetcher (URL -> raw bytes). Mainly an
            injection point for tests; when omitted the stdlib fetcher is used.

    Returns:
        A ``dlt`` resource (``rss_entries``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The RSS connector requires dlt. Install it with the connector:\n"
            '    pip install "cognee-community-connector-rss"'
        ) from exc

    if not feed_urls or not all(isinstance(url, str) and url.strip() for url in feed_urls):
        raise ValueError("feed_urls must be a non-empty list of feed URL strings.")

    fetch_fn = fetch or _http_fetch
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker (matching gmail/confluence):
        # rows where it is True are removed from the dlt destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def rss_entries():
        resource_state = dlt.current.resource_state()
        yield from sync_feeds(fetch_fn, feed_urls, resource_state, stats=stats)

    resource = rss_entries()
    # Opt into the document ingestion path: each entry row (id/title/content/url)
    # becomes a text document that flows through normal cognify (LLM graph
    # extraction). resolve_dlt_sources reads this marker; it never imports this
    # connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, RSS_SOURCE_NAME)
    # Host-readable diagnostics contain counts only, never feed or entry content.
    resource.cognee_sync_stats = stats
    return resource
