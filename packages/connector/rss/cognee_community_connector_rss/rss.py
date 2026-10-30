"""RSS / Atom feed data-source connector for cognee.

Fetches and parses RSS 2.0, RSS 1.0, and Atom syndication feeds
into cognee memory documents.
"""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("rss_connector")

RSS_TABLE_NAME = "rss_feed_entries"
RSS_SOURCE_NAME = "rss"

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = _TAG_RE.sub(" ", raw_html)
    return " ".join(text.split())


def _get_text(elem: ET.Element | None, tag: str) -> str:
    if elem is None:
        return ""
    child = elem.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def _find_elem(elem: ET.Element, tags: list[str]) -> ET.Element | None:
    for tag in tags:
        child = elem.find(tag)
        if child is not None:
            return child
    return None


def parse_feed_content(content: str, default_feed_title: str = "RSS Feed") -> list[dict[str, Any]]:
    """Parse RSS or Atom XML string into entry dictionaries."""
    entries: list[dict[str, Any]] = []

    # Check for JSON Feed
    trimmed = content.strip()
    if trimmed.startswith("{"):
        try:
            data = json.loads(trimmed)
            feed_title = data.get("title", default_feed_title)
            for item in data.get("items", []):
                item_id = item.get("id") or item.get("url")
                title = item.get("title", "Untitled")
                link = item.get("url", "")
                text = item.get("content_text") or _clean_html(item.get("content_html", ""))
                date_str = item.get("date_published") or item.get("date_modified", "")
                author = item.get("author", {}).get("name", "") if isinstance(item.get("author"), dict) else ""

                entry_id = f"feed:{hashlib.sha256(str(item_id or link).encode('utf-8')).hexdigest()[:16]}"
                entries.append({
                    "id": entry_id,
                    "feed_title": feed_title,
                    "title": title,
                    "link": link,
                    "published": date_str,
                    "author": author,
                    "summary": text[:300],
                    "content": f"# {title}\n\n**Published:** {date_str}\n**Link:** {link}\n\n{text}",
                })
            return entries
        except Exception:
            pass

    # XML (RSS or Atom)
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.error("RSS: failed to parse XML: %s", exc)
        return []

    # Strip XML namespace prefixes if present
    # Check if Atom (<feed>)
    tag = root.tag.lower()
    is_atom = "feed" in tag

    if is_atom:
        feed_title = _get_text(root, "{*}title") or root.findtext("title") or default_feed_title
        entries_list = root.findall("{*}entry")
        if not entries_list:
            entries_list = root.findall("entry")
        for entry in entries_list:
            title = _get_text(entry, "{*}title") or entry.findtext("title") or "Untitled"
            link_elem = entry.find("{*}link")
            if link_elem is None:
                link_elem = entry.find("link")
            link = ""
            if link_elem is not None:
                link = link_elem.get("href") or link_elem.text or ""

            published = (
                _get_text(entry, "{*}published")
                or _get_text(entry, "{*}updated")
                or entry.findtext("published")
                or entry.findtext("updated")
                or ""
            )
            author_elem = entry.find("{*}author")
            if author_elem is None:
                author_elem = entry.find("author")
            author = ""
            if author_elem is not None:
                author = _get_text(author_elem, "{*}name") or author_elem.findtext("name") or ""

            summary = (
                _clean_html(_get_text(entry, "{*}summary") or entry.findtext("summary"))
                or _clean_html(_get_text(entry, "{*}content") or entry.findtext("content"))
            )
            item_id = _get_text(entry, "{*}id") or entry.findtext("id") or link

            entry_id = f"feed:{hashlib.sha256(str(item_id or link).encode('utf-8')).hexdigest()[:16]}"
            entries.append({
                "id": entry_id,
                "feed_title": feed_title,
                "title": title,
                "link": link,
                "published": published,
                "author": author,
                "summary": summary[:300],
                "content": f"# {title}\n\n**Published:** {published}\n**Link:** {link}\n\n{summary}",
            })
    else:
        # RSS 2.0 or RSS 1.0 (RDF)
        channel = root.find("channel")
        if channel is None:
            channel = root
        feed_title = _get_text(channel, "title") or default_feed_title

        items = root.findall(".//item")
        for item in items:
            title = _get_text(item, "title") or "Untitled"
            link = _get_text(item, "link") or ""
            guid = _get_text(item, "guid") or link
            published = _get_text(item, "pubDate") or _get_text(item, "{*}date")
            author = _get_text(item, "author") or _get_text(item, "{*}creator")
            description = _clean_html(_get_text(item, "description") or _get_text(item, "{*}encoded"))

            entry_id = f"feed:{hashlib.sha256(str(guid or link).encode('utf-8')).hexdigest()[:16]}"
            entries.append({
                "id": entry_id,
                "feed_title": feed_title,
                "title": title,
                "link": link,
                "published": published,
                "author": author,
                "summary": description[:300],
                "content": f"# {title}\n\n**Published:** {published}\n**Link:** {link}\n\n{description}",
            })

    return entries


def iter_feed_entries(urls_or_content: list[str] | str) -> Iterator[dict[str, Any]]:
    """Yield parsed entries from one or more feed URLs or XML content strings."""
    items = [urls_or_content] if isinstance(urls_or_content, str) else urls_or_content

    for item in items:
        trimmed = item.strip()
        if trimmed.startswith("http://") or trimmed.startswith("https://"):
            try:
                resp = requests.get(trimmed, timeout=15, headers={"User-Agent": "cognee-rss-connector/1.0"})
                resp.raise_for_status()
                content = resp.text
                domain = urlparse(trimmed).netloc
            except Exception as exc:
                logger.error("RSS: failed to fetch %s: %s", trimmed, exc)
                continue
        else:
            content = item
            domain = "Feed"

        entries = parse_feed_content(content, default_feed_title=domain)
        yield from entries


def rss_source(feed_urls: list[str] | str):
    """Create a dlt source yielding RSS / Atom feed entries as documents."""
    import dlt

    @dlt.resource(name=RSS_TABLE_NAME, primary_key="id", write_disposition="replace")
    def rss_entries():
        count = 0
        for entry in iter_feed_entries(feed_urls):
            count += 1
            yield entry
        logger.info("RSS: synced %d feed item(s).", count)

    @dlt.source(name=RSS_SOURCE_NAME)
    def _rss():
        return rss_entries

    source = _rss()
    setattr(source, DOCUMENT_SOURCE_ATTR, RSS_SOURCE_NAME)
    return source
