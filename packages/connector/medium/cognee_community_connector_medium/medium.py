"""DLT source for Medium articles and feeds (snapshot sync + forget-on-delete).

Fetches public Medium articles via article URLs or author/publication feeds, extracts
readable markdown text and metadata, and yields them as a dlt resource for Cognee's
knowledge graph and vector ingestion pipelines.
"""

from collections.abc import Iterator
import hashlib
import re
import time
from typing import Any
import xml.etree.ElementTree as ET

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("medium_connector")
except ImportError:
    import logging
    logger = logging.getLogger("medium_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

MEDIUM_TABLE_NAME = "medium_articles"
MEDIUM_SOURCE_NAME = "medium"

_DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; CogneeMediumConnector/0.1.0; +https://github.com/topoteretes/cognee)"
_MAX_RETRIES = 5


class MediumClient:
    """Client for fetching Medium articles and RSS feeds."""

    def __init__(self, http_client: Any = None, user_agent: str | None = None):
        import httpx

        self.user_agent = user_agent or _DEFAULT_USER_AGENT
        self.client = http_client or httpx.Client(
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
            timeout=25.0,
        )

    def fetch_feed(self, feed_target: str) -> str:
        """Fetch XML content for an author or publication feed."""
        clean_target = feed_target.strip().lstrip("@")
        if feed_target.startswith("http://") or feed_target.startswith("https://"):
            url = feed_target
        elif "/" in clean_target:
            url = f"https://medium.com/feed/{clean_target}"
        else:
            url = f"https://medium.com/feed/@{clean_target}"

        return self._request_text(url)

    def fetch_article(self, article_url: str) -> str:
        """Fetch raw HTML of a single Medium article."""
        return self._request_text(article_url)

    def _request_text(self, url: str) -> str:
        import httpx

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self.client.get(url)
                if resp.status_code == 429:
                    delay = float(resp.headers.get("Retry-After", 2 ** attempt))
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.text
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == _MAX_RETRIES - 1:
                    logger.warning("Failed to fetch %s: %s", url, exc)
                    raise
                time.sleep(2 ** attempt)

        return ""


def medium_source(
    article_urls: list[str] | None = None,
    authors: list[str] | None = None,
    publications: list[str] | None = None,
    client: MediumClient | None = None,
    user_agent: str | None = None,
):
    """Create a dlt source yielding Medium articles as markdown documents.

    Args:
        article_urls: Direct URLs to specific Medium articles.
        authors: List of Medium usernames/handles (e.g. ['@username'] or ['username']).
        publications: List of publication slugs (e.g. ['towards-data-science']).
        client: Pre-built MediumClient instance (for tests or custom configurations).
        user_agent: Custom HTTP User-Agent string.

    Returns:
        A dlt source yielding document rows ready for Cognee.
    """
    import dlt

    if client is None:
        client = MediumClient(user_agent=user_agent)

    urls = article_urls or []
    target_authors = authors or []
    target_publications = publications or []

    @dlt.resource(name=MEDIUM_TABLE_NAME, primary_key="id", write_disposition="replace")
    def medium_articles() -> Iterator[dict[str, Any]]:
        seen_urls = set()
        count = 0

        # 1. Process explicit article URLs
        for url in urls:
            clean_url = _canonicalize_url(url)
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)
            try:
                html = client.fetch_article(clean_url)
                row = _parse_article_html(html, clean_url)
                if row:
                    count += 1
                    yield row
            except Exception as exc:
                logger.warning("Failed to ingest Medium article %s: %s", clean_url, exc)

        # 2. Process author feeds
        for author in target_authors:
            try:
                feed_xml = client.fetch_feed(author)
                for item in _parse_feed_items(feed_xml):
                    clean_url = _canonicalize_url(item.get("url", ""))
                    if not clean_url or clean_url in seen_urls:
                        continue
                    seen_urls.add(clean_url)
                    count += 1
                    yield item
            except Exception as exc:
                logger.warning("Failed to ingest author feed %s: %s", author, exc)

        # 3. Process publication feeds
        for pub in target_publications:
            try:
                feed_xml = client.fetch_feed(pub)
                for item in _parse_feed_items(feed_xml):
                    clean_url = _canonicalize_url(item.get("url", ""))
                    if not clean_url or clean_url in seen_urls:
                        continue
                    seen_urls.add(clean_url)
                    count += 1
                    yield item
            except Exception as exc:
                logger.warning("Failed to ingest publication feed %s: %s", pub, exc)

        logger.info("Medium connector: synced %d article(s).", count)

    @dlt.source(name=MEDIUM_SOURCE_NAME)
    def _medium():
        return medium_articles

    source = _medium()
    setattr(source, DOCUMENT_SOURCE_ATTR, MEDIUM_SOURCE_NAME)
    return source


def _canonicalize_url(url: str) -> str:
    """Strip tracking query parameters (e.g. ?source=rss) to yield a canonical URL."""
    if not url:
        return ""
    return re.sub(r"\?.*$", "", url.strip())


def _generate_doc_id(canonical_url: str) -> str:
    """Generate a stable deterministic document ID from a canonical URL."""
    return f"medium_{hashlib.sha256(canonical_url.encode('utf-8')).hexdigest()[:16]}"


def _parse_feed_items(xml_content: str) -> list[dict[str, Any]]:
    """Parse articles from a Medium RSS feed."""
    if not xml_content.strip():
        return []

    items = []
    try:
        root = ET.fromstring(xml_content)
        # Handle standard RSS channel items
        channel = root.find("channel")
        item_nodes = channel.findall("item") if channel is not None else root.findall(".//item")

        for node in item_nodes:
            title = node.findtext("title", "Untitled Article").strip()
            link = node.findtext("link", "").strip()
            author = (
                node.findtext("{http://purl.org/dc/elements/1.1/}creator")
                or node.findtext("author", "Medium Contributor")
            ).strip()
            pub_date = node.findtext("pubDate", "").strip()

            # Categories / tags
            categories = [
                elem.text.strip()
                for elem in node.findall("category")
                if elem.text and elem.text.strip()
            ]

            # Content encoded or description
            content_html = (
                node.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                or node.findtext("description", "")
            )

            clean_markdown = _html_to_markdown(content_html)
            canonical = _canonicalize_url(link)
            doc_id = _generate_doc_id(canonical)

            header = [
                f"# {title}",
                f"**Author:** {author} | **Published:** {pub_date}",
                f"**Source:** {canonical}",
            ]
            if categories:
                header.append(f"**Tags:** {', '.join(categories)}")
            header.append("\n---\n")

            full_text = "\n".join(header) + "\n" + clean_markdown

            items.append({
                "id": doc_id,
                "url": canonical,
                "title": title,
                "content": full_text,
            })
    except Exception as exc:
        logger.warning("Error parsing feed XML: %s", exc)

    return items


def _parse_article_html(html_content: str, url: str) -> dict[str, Any] | None:
    """Extract article title, author, and readable body from Medium article HTML."""
    from bs4 import BeautifulSoup

    if not html_content:
        return None

    soup = BeautifulSoup(html_content, "html.parser")

    # Extract title
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text().strip()
    elif soup.title:
        title = soup.title.get_text().replace(" - Medium", "").strip()

    if not title:
        title = "Untitled Medium Article"

    # Extract author
    author = "Medium Author"
    author_meta = soup.find("meta", {"name": "author"}) or soup.find("meta", {"property": "author"})
    if author_meta and author_meta.get("content"):
        author = author_meta["content"].strip()

    # Extract article container
    article_tag = soup.find("article") or soup.find("main") or soup

    # Remove script, style, navigation elements
    for el in article_tag.find_all(["script", "style", "nav", "footer", "button"]):
        el.decompose()

    markdown_body = _html_to_markdown(str(article_tag))
    canonical = _canonicalize_url(url)
    doc_id = _generate_doc_id(canonical)

    full_content = f"# {title}\n**Author:** {author}\n**Source:** {canonical}\n\n---\n\n{markdown_body}"

    return {
        "id": doc_id,
        "url": canonical,
        "title": title,
        "content": full_content,
    }


def _html_to_markdown(html_text: str) -> str:
    """Convert HTML content into structured markdown text."""
    from bs4 import BeautifulSoup

    if not html_text:
        return ""

    soup = BeautifulSoup(html_text, "html.parser")

    # Headings
    for h in soup.find_all("h1"):
        h.replace_with(f"\n\n# {h.get_text().strip()}\n\n")
    for h in soup.find_all("h2"):
        h.replace_with(f"\n\n## {h.get_text().strip()}\n\n")
    for h in soup.find_all("h3"):
        h.replace_with(f"\n\n### {h.get_text().strip()}\n\n")

    # Paragraphs & line breaks
    for p in soup.find_all("p"):
        p.replace_with(f"\n\n{p.get_text().strip()}\n\n")
    for br in soup.find_all("br"):
        br.replace_with("\n")

    # Code blocks
    for pre in soup.find_all("pre"):
        code_text = pre.get_text().strip()
        pre.replace_with(f"\n\n```\n{code_text}\n```\n\n")

    # Lists
    for li in soup.find_all("li"):
        li.replace_with(f"\n- {li.get_text().strip()}")

    # Extract plain text with clean whitespace
    raw_text = soup.get_text()
    cleaned = re.sub(r"\n{3,}", "\n\n", raw_text)
    return cleaned.strip()
