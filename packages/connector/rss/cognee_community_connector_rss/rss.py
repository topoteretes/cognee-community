"""DLT source for RSS and Atom feeds (full-snapshot sync + forget-on-delete).

Fetches one or more feeds and yields each entry as a markdown document for
cognee's ingestion pipeline.

Like the Notion connector, entries are ingested as *normal documents*: the
source declares ``cognee_document_source = "rss"``, so ``resolve_dlt_sources``
tags each row ``external_metadata["source"] = "rss"``. Each entry then flows
through the standard cognify entity-extraction pipeline.

The source is a full snapshot: ``write_disposition="replace"`` rewrites staging
with exactly the entries currently present in the feeds. An item removed
upstream simply disappears from the next fetch, so cognee's ``orphan_cleanup``
forgets it. Unchanged entries keep a stable ``id`` (guid / link), so they are
not re-ingested. ``published`` / ``updated`` are used only to skip entries
older than an optional cursor, not as a merge key — combining a ``since``
cursor with replace would look like a deletion of older live items.

Malformed feeds are skipped with a warning rather than aborting the whole run
when other feeds in the same source still parsed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("rss_connector")

RSS_TABLE_NAME = "rss_entries"
RSS_SOURCE_NAME = "rss"

_EXTRA_HINT = (
    'The RSS connector requires the "rss" extra: pip install '
    '"cognee-community-connector-rss" (provides dlt and feedparser).'
)


def rss_source(
    feed_urls: Iterable[str],
    since: datetime | str | None = None,
    fetch: Callable[[str], Any] | None = None,
):
    """Create a dlt source that yields RSS/Atom entries as markdown documents.

    Args:
        feed_urls: Feed URLs to ingest. Both RSS 2.0 and Atom are accepted.
        since: Optional cursor. Entries with ``published``/``updated`` strictly
            before this timestamp are skipped. Prefer omitting this and relying
            on the full snapshot so forget-on-delete stays correct.
        fetch: Test injection. When omitted, ``feedparser.parse(url)`` is used.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    urls = [url.strip() for url in feed_urls if url and url.strip()]
    if not urls:
        raise ValueError("rss_source requires at least one feed URL.")

    since_dt = _parse_since(since)
    parse_feed = fetch or _default_fetch

    @dlt.resource(name=RSS_TABLE_NAME, primary_key="id", write_disposition="replace")
    def rss_entries():
        count = 0
        seen_ids: set[str] = set()
        for url in urls:
            parsed = parse_feed(url)
            if _is_unusable(parsed):
                logger.warning("RSS: skipping unusable feed %s", url)
                continue
            for entry in parsed.get("entries") or []:
                row = _entry_to_row(entry, feed_url=url)
                if row is None or row["id"] in seen_ids:
                    continue
                if since_dt and not _is_on_or_after(row.get("published"), since_dt):
                    continue
                seen_ids.add(row["id"])
                count += 1
                yield row
        logger.info("RSS: synced %d entr(y/ies) from %d feed(s).", count, len(urls))

    @dlt.source(name=RSS_SOURCE_NAME)
    def _rss():
        return rss_entries

    source = _rss()
    setattr(source, DOCUMENT_SOURCE_ATTR, RSS_SOURCE_NAME)
    return source


def _default_fetch(url: str) -> dict[str, Any]:
    try:
        import feedparser
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc
    return feedparser.parse(url)


def _is_unusable(parsed: Any) -> bool:
    """True when feedparser could not produce a usable feed.

    ``bozo`` alone is not fatal — many valid feeds have minor XML quirks.
    We skip only when there are no entries *and* feedparser flagged a parse
    problem, or when the result is not a mapping at all.
    """
    if not isinstance(parsed, dict) and not hasattr(parsed, "get"):
        return True
    entries = parsed.get("entries") or []
    if entries:
        return False
    return bool(parsed.get("bozo"))


def _parse_since(since: datetime | str | None) -> datetime | None:
    if since is None:
        return None
    if isinstance(since, datetime):
        return _as_utc(since)
    parsed = _parse_datetime(since)
    if parsed is None:
        raise ValueError(f"Could not parse since= {since!r}")
    return parsed


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        try:
            return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            pass
        try:
            return _as_utc(parsedate_to_datetime(text))
        except (TypeError, ValueError, OverflowError):
            return None
    # feedparser time_struct
    if hasattr(value, "tm_year"):
        return datetime(
            value.tm_year,
            value.tm_mon,
            value.tm_mday,
            value.tm_hour,
            value.tm_min,
            value.tm_sec,
            tzinfo=timezone.utc,
        )
    return None


def _is_on_or_after(published: str | None, since_dt: datetime) -> bool:
    parsed = _parse_datetime(published)
    if parsed is None:
        # Keep undated entries; dropping them would look like a deletion.
        return True
    return parsed >= since_dt


def _entry_to_row(entry: Any, feed_url: str) -> dict[str, str] | None:
    """Flatten an RSS/Atom entry into a document row.

    Identity is ``guid`` (or ``id`` / ``link``). Title + content are the only
    text fields kept so a metadata-only bump does not churn the content hash.
    """
    entry_id = _text(entry.get("id") or entry.get("guid") or entry.get("link"))
    if not entry_id:
        return None
    title = _text(entry.get("title"))
    content = _entry_content(entry)
    link = _text(entry.get("link")) or feed_url
    published = _format_published(entry)
    return {
        "id": entry_id,
        "url": link,
        "title": title,
        "content": content,
        "published": published,
        "feed_url": feed_url,
    }


def _entry_content(entry: Any) -> str:
    contents = entry.get("content")
    if isinstance(contents, list) and contents:
        first = contents[0]
        if isinstance(first, dict):
            value = _text(first.get("value"))
            if value:
                return value
        value = _text(first)
        if value:
            return value
    for key in ("summary", "description"):
        value = _text(entry.get(key))
        if value:
            return value
    return ""


def _format_published(entry: Any) -> str:
    for key in ("published", "updated", "created"):
        parsed = _parse_datetime(entry.get(f"{key}_parsed") or entry.get(key))
        if parsed is not None:
            return parsed.isoformat()
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _text(value.get("value") or value.get("#text") or value.get("href"))
    return str(value).strip()
