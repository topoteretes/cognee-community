"""DLT source for JSON Feed (jsonfeed.org).

Ingests modern blog, changelog, and newsletter feeds published in JSON Feed format (v1 / v1.1)
into searchable Cognee documents with entity extraction and link graph support.

Features:
- Full support for JSON Feed 1.0 and 1.1 specs (including multi-page next_url traversal)
- Content prioritization (content_text preferred, clean HTML-to-text fallback with tag stripping)
- Resilient ID generation (SHA-256 fallback if feed item lacks explicit id)
- Rich metadata capture (authors, tags, dates, canonical URLs, and media attachments)
- Document-mode registration (DOCUMENT_SOURCE_ATTR = "jsonfeed") for entity linking in Cognee
- Replace-mode synchronization with orphan cleanup
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("jsonfeed_connector")
except ImportError:
    logger = logging.getLogger("jsonfeed_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

JSONFEED_SOURCE_NAME = "jsonfeed"
JSONFEED_TABLE_NAME = "jsonfeed_items"


def clean_html_to_text(html_str: str | None) -> str:
    """Convert HTML content into clean, formatted plain text with preserved line breaks."""
    if not html_str or not isinstance(html_str, str):
        return ""

    text = html_str
    # Replace block-level tags with double newlines
    text = re.sub(r"(?i)</?(?:p|div|section|article|blockquote)[^>]*>", "\n\n", text)
    # Replace line breaks and list items
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    # Replace headings with markdown style
    text = re.sub(r"(?i)<h[1-6][^>]*>", "\n\n### ", text)
    text = re.sub(r"(?i)</h[1-6]>", "\n", text)
    # Strip remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape HTML entities
    text = html.unescape(text)
    # Normalize multiple whitespace lines
    lines = [line.strip() for line in text.splitlines()]
    clean_lines: list[str] = []
    prev_empty = False
    for line in lines:
        if line:
            clean_lines.append(line)
            prev_empty = False
        elif not prev_empty:
            clean_lines.append("")
            prev_empty = True
    return "\n".join(clean_lines).strip()


def generate_item_id(item: dict[str, Any], feed_url: str = "") -> str:
    """Return the item's unique id, or generate a deterministic SHA-256 fallback."""
    explicit_id = item.get("id")
    if explicit_id is not None and str(explicit_id).strip():
        return str(explicit_id).strip()

    # Fallback to deterministic hash of url + title + date
    url = item.get("url") or item.get("external_url") or ""
    title = item.get("title") or ""
    date = item.get("date_published") or item.get("date_modified") or ""
    seed = f"{feed_url}::{url}::{title}::{date}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"jsonfeed_item_{digest}"


def format_feed_item_markdown(feed_meta: dict[str, Any], item: dict[str, Any]) -> str:
    """Render a JSON Feed item into structured Markdown with complete metadata and body text."""
    feed_title = feed_meta.get("title", "JSON Feed")
    item_title = item.get("title") or "Untitled Post"
    item_url = item.get("url") or item.get("external_url") or ""
    date_pub = item.get("date_published") or "Unknown Date"
    date_mod = item.get("date_modified")

    # Author resolution
    authors_raw = item.get("authors") or feed_meta.get("authors") or []
    author_names = []
    if isinstance(authors_raw, list):
        for a in authors_raw:
            if isinstance(a, dict):
                name = a.get("name")
                if name:
                    author_names.append(name)
            elif isinstance(a, str):
                author_names.append(a)

    authors_str = ", ".join(author_names) if author_names else "Unspecified Author"
    tags = item.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]

    lines = [
        f"# [{feed_title}] {item_title}",
        "",
        "## Post Metadata",
        f"- **Feed Title:** {feed_title}",
        f"- **Published Date:** {date_pub}",
    ]

    if date_mod and date_mod != date_pub:
        lines.append(f"- **Last Modified:** {date_mod}")

    lines.append(f"- **Author(s):** {authors_str}")

    if item_url:
        lines.append(f"- **Article Link:** [{item_title}]({item_url})")

    if tags:
        lines.append(f"- **Categories / Tags:** {', '.join(f'`{t}`' for t in tags)}")

    # Extract body content: prefer content_text, fallback to content_html, then summary
    body_text = ""
    if item.get("content_text"):
        body_text = item["content_text"].strip()
    elif item.get("content_html"):
        body_text = clean_html_to_text(item["content_html"])
    elif item.get("summary"):
        body_text = clean_html_to_text(item["summary"])

    lines.extend([
        "",
        "## Article Content",
        "",
        body_text or "_No text content provided in feed._",
    ])

    # Check for podcast or media attachments
    attachments = item.get("attachments") or []
    if attachments and isinstance(attachments, list):
        lines.extend([
            "",
            "## Media Attachments",
        ])
        for att in attachments:
            if isinstance(att, dict):
                att_url = att.get("url", "")
                att_title = att.get("title") or "Media File"
                mime = att.get("mime_type", "unknown")
                duration = att.get("duration_in_seconds")
                dur_str = f" ({duration}s)" if duration else ""
                lines.append(f"- [{att_title}]({att_url}) — `{mime}`{dur_str}")

    return "\n".join(lines)


def parse_json_feed_payload(
    feed_data: dict[str, Any] | str,
    source_identifier: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse raw JSON Feed payload string or dict, returning (feed_metadata, items)."""
    if isinstance(feed_data, str):
        payload = json.loads(feed_data)
    elif isinstance(feed_data, dict):
        payload = feed_data
    else:
        raise ValueError(f"Expected dict or JSON string for feed_data, got {type(feed_data)}")

    # Validate version if present
    version = payload.get("version", "")
    if version and not re.search(r"jsonfeed\.org/version/1(?:\.1)?", version):
        logger.warning("Unrecognized JSON Feed version: %s (attempting parse anyway)", version)

    feed_meta = {
        "title": payload.get("title") or source_identifier or "JSON Feed",
        "home_page_url": payload.get("home_page_url", ""),
        "feed_url": payload.get("feed_url") or source_identifier,
        "description": payload.get("description", ""),
        "authors": payload.get("authors", []),
        "language": payload.get("language", "en"),
        "next_url": payload.get("next_url"),
    }

    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list):
        raw_items = []

    return feed_meta, raw_items


def fetch_json_feed(
    feed_url: str,
    max_pages: int = 5,
    timeout: float = 15.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch JSON Feed from a remote URL, following next_url pagination up to max_pages."""
    import requests

    headers = {
        "Accept": "application/feed+json, application/json;q=0.9, */*;q=0.8",
        "User-Agent": "Cognee-JSONFeed-Connector/0.1",
    }

    current_url = feed_url
    page_count = 0
    all_items: list[dict[str, Any]] = []
    feed_meta: dict[str, Any] = {}

    while current_url and page_count < max_pages:
        logger.info("JSON Feed: Fetching page %d from %s", page_count + 1, current_url)
        resp = requests.get(current_url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()

        page_meta, page_items = parse_json_feed_payload(payload, source_identifier=feed_url)
        if page_count == 0:
            feed_meta = page_meta

        all_items.extend(page_items)
        page_count += 1
        current_url = page_meta.get("next_url")

    return feed_meta, all_items


def jsonfeed_source(
    feed_urls: str | list[str] | None = None,
    snapshot_data: dict[str, Any] | list[dict[str, Any]] | str | Path | None = None,
    max_pages: int = 5,
    max_items: int | None = None,
    filter_tags: list[str] | None = None,
):
    """Create a dlt source yielding JSON Feed posts as searchable Cognee documents.

    Args:
        feed_urls: A URL string or list of URLs pointing to JSON Feeds.
        snapshot_data: Optional pre-fetched feed dict, list of feed dicts, or path to local JSON.
        max_pages: Max pages to traverse per feed URL via next_url pagination (default: 5).
        max_items: Optional maximum total items to ingest across feeds.
        filter_tags: Optional list of tag names to filter items (case-insensitive).

    Returns:
        A dlt source configured for Cognee document ingestion.
    """
    # Pre-validate file path if string or Path provided
    if isinstance(snapshot_data, (str, Path)):
        p = Path(snapshot_data)
        if not p.exists():
            raise FileNotFoundError(f"JSON Feed snapshot file not found: {p}")

    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The JSON Feed connector requires dlt: pip install "cognee-community-connector-jsonfeed" '
            'or pip install "dlt[sqlalchemy]".'
        ) from exc

    resolved_feeds: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    # 1. Process snapshot data (offline / test injection)
    if snapshot_data is not None:
        if isinstance(snapshot_data, dict):
            # Single feed dict
            meta, items = parse_json_feed_payload(snapshot_data, source_identifier="snapshot")
            resolved_feeds.append((meta, items))
        elif isinstance(snapshot_data, list):
            for entry in snapshot_data:
                if isinstance(entry, dict):
                    meta, items = parse_json_feed_payload(entry, source_identifier="snapshot")
                    resolved_feeds.append((meta, items))
        elif isinstance(snapshot_data, (str, Path)):
            p = Path(snapshot_data)
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                meta, items = parse_json_feed_payload(raw, source_identifier=str(p))
                resolved_feeds.append((meta, items))
            elif isinstance(raw, list):
                for entry in raw:
                    meta, items = parse_json_feed_payload(entry, source_identifier=str(p))
                    resolved_feeds.append((meta, items))

    # 2. Process remote feed URLs
    if feed_urls:
        url_list = [feed_urls] if isinstance(feed_urls, str) else feed_urls
        for url in url_list:
            if not url or not isinstance(url, str):
                continue
            try:
                meta, items = fetch_json_feed(url.strip(), max_pages=max_pages)
                resolved_feeds.append((meta, items))
            except Exception as exc:
                logger.warning("JSON Feed: Error fetching feed from %s: %s", url, exc)
                # Re-raise so transient errors don't trigger orphan cleanup
                raise

    if not resolved_feeds:
        raise ValueError("No valid JSON Feed data provided via feed_urls= or snapshot_data=.")

    tag_filter_set = {t.strip().lower() for t in filter_tags} if filter_tags else None

    # Flatten and yield items
    flattened_documents: list[dict[str, Any]] = []
    total_yielded = 0

    for feed_meta, items in resolved_feeds:
        feed_title = feed_meta.get("title", "JSON Feed")
        feed_url = feed_meta.get("feed_url", "")

        for item in items:
            if max_items is not None and total_yielded >= max_items:
                break

            # Filter by tags if specified
            item_tags = item.get("tags") or []
            if isinstance(item_tags, str):
                item_tags = [item_tags]
            item_tags_lower = {t.lower() for t in item_tags}

            if tag_filter_set and not (item_tags_lower & tag_filter_set):
                continue

            item_id = generate_item_id(item, feed_url=feed_url)
            markdown_content = format_feed_item_markdown(feed_meta, item)
            item_title = item.get("title") or "Untitled Post"

            doc = {
                "id": item_id,
                "title": f"[{feed_title}] {item_title}",
                "url": item.get("url") or item.get("external_url") or f"{feed_url}#{item_id}",
                "feed_title": feed_title,
                "feed_url": feed_url,
                "date_published": item.get("date_published", ""),
                "date_modified": item.get("date_modified", ""),
                "tags": item_tags,
                "cognee_node_set": [
                    f"feed:{feed_title}",
                    "jsonfeed",
                    "blog",
                ],
                "content": markdown_content,
            }
            flattened_documents.append(doc)
            total_yielded += 1

    @dlt.resource(name=JSONFEED_TABLE_NAME, primary_key="id", write_disposition="replace")
    def jsonfeed_items():
        for doc in flattened_documents:
            yield doc

    @dlt.source(name=JSONFEED_SOURCE_NAME)
    def _jsonfeed():
        return jsonfeed_items

    source = _jsonfeed()
    setattr(source, DOCUMENT_SOURCE_ATTR, JSONFEED_SOURCE_NAME)
    return source
