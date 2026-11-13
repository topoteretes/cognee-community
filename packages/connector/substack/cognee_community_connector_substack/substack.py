"""Substack newsletter data-source connector for cognee.

Fetches published posts and essays from Substack publications
into cognee memory documents.
"""

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("substack_connector")

SUBSTACK_TABLE_NAME = "substack_posts"
SUBSTACK_SOURCE_NAME = "substack"

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(raw_html: str | None) -> str:
    if not raw_html:
        return ""
    unescaped = html.unescape(raw_html)
    # Replace paragraph and header tags with newlines
    unescaped = re.sub(r"<\s*p[^>]*>", "\n\n", unescaped, flags=re.IGNORECASE)
    unescaped = re.sub(r"<\s*h[1-6][^>]*>", "\n\n### ", unescaped, flags=re.IGNORECASE)
    unescaped = re.sub(r"<\s*li[^>]*>", "\n- ", unescaped, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", unescaped)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _normalize_feed_url(target: str) -> tuple[str, str]:
    """Return publication name and normalized feed URL."""
    cleaned = target.strip()
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        parsed = urlparse(cleaned)
        netloc = parsed.netloc
        pub_name = netloc.replace(".substack.com", "").replace("www.", "")
        if not cleaned.endswith("/feed"):
            cleaned = cleaned.rstrip("/") + "/feed"
        return pub_name, cleaned
    else:
        # Given publication subdomain e.g. "astralcodexten"
        pub_name = cleaned.lower()
        return pub_name, f"https://{pub_name}.substack.com/feed"


def parse_substack_feed(xml_text: str, default_pub_name: str = "Substack") -> list[dict[str, Any]]:
    """Parse Substack RSS XML into structured newsletter posts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error("Substack: XML parse error: %s", exc)
        return []

    posts: list[dict[str, Any]] = []
    channel = root.find("channel")
    if channel is None:
        channel = root

    pub_title = ""
    title_elem = channel.find("title")
    if title_elem is not None and title_elem.text:
        pub_title = title_elem.text.strip()
    if not pub_title:
        pub_title = default_pub_name

    for item in root.findall(".//item"):
        t_elem = item.find("title")
        title = t_elem.text.strip() if t_elem is not None and t_elem.text else "Untitled"

        link_elem = item.find("link")
        link = link_elem.text.strip() if link_elem is not None and link_elem.text else ""

        guid_elem = item.find("guid")
        guid = guid_elem.text.strip() if guid_elem is not None and guid_elem.text else link

        pub_date_elem = item.find("pubDate")
        pub_date = pub_date_elem.text.strip() if pub_date_elem is not None and pub_date_elem.text else ""

        author_elem = item.find("{*}creator")
        if author_elem is None:
            author_elem = item.find("author")
        author = author_elem.text.strip() if author_elem is not None and author_elem.text else ""

        # Content / Description
        desc_elem = item.find("{*}encoded")
        if desc_elem is None:
            desc_elem = item.find("description")
        body_text = _clean_html(desc_elem.text if desc_elem is not None else "")

        # Extract post slug or unique id
        slug = urlparse(link).path.strip("/").split("/")[-1] if link else ""
        doc_id = f"substack:{pub_title}:{slug or hashlib.sha256(guid.encode('utf-8')).hexdigest()[:16]}"

        content = (
            f"# {title}\n\n"
            f"**Publication:** {pub_title}\n"
            f"**Author:** {author}\n"
            f"**Published:** {pub_date}\n"
            f"**Link:** {link}\n\n"
            f"{body_text}"
        )

        posts.append({
            "id": doc_id,
            "publication": pub_title,
            "title": title,
            "author": author,
            "published": pub_date,
            "link": link,
            "content": content,
        })

    return posts


def iter_substack_posts(
    publications: list[str] | str,
    fetch_fn: Any = None,
) -> Iterator[dict[str, Any]]:
    """Yield parsed newsletter posts from one or more Substack publications."""
    pubs = [publications] if isinstance(publications, str) else publications

    for pub in pubs:
        pub_name, feed_url = _normalize_feed_url(pub)
        if fetch_fn is not None:
            xml_text = fetch_fn(pub)
        else:
            try:
                resp = requests.get(feed_url, timeout=15, headers={"User-Agent": "cognee-substack-connector/1.0"})
                resp.raise_for_status()
                xml_text = resp.text
            except Exception as exc:
                logger.error("Substack: failed to fetch %s (%s): %s", pub, feed_url, exc)
                continue

        posts = parse_substack_feed(xml_text, default_pub_name=pub_name)
        yield from posts


def substack_source(
    publications: list[str] | str,
    fetch_fn: Any = None,
):
    """Create a dlt source yielding Substack newsletter posts as documents."""
    import dlt

    @dlt.resource(name=SUBSTACK_TABLE_NAME, primary_key="id", write_disposition="replace")
    def substack_posts():
        count = 0
        for post in iter_substack_posts(publications, fetch_fn=fetch_fn):
            count += 1
            yield post
        logger.info("Substack: synced %d post(s).", count)

    @dlt.source(name=SUBSTACK_SOURCE_NAME)
    def _substack():
        return substack_posts

    source = _substack()
    setattr(source, DOCUMENT_SOURCE_ATTR, SUBSTACK_SOURCE_NAME)
    return source
