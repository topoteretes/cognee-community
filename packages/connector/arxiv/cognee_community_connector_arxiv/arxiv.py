"""arXiv research papers data-source connector for cognee.

Queries the arXiv API (export.arxiv.org/api/query) and yields paper
metadata and abstracts as structured documents for cognee.
"""

import hashlib
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("arxiv_connector")

ARXIV_TABLE_NAME = "arxiv_papers"
ARXIV_SOURCE_NAME = "arxiv"
ARXIV_API_URL = "http://export.arxiv.org/api/query"


def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.strip().split())


def parse_arxiv_feed(xml_text: str) -> list[dict[str, Any]]:
    """Parse arXiv Atom XML response into paper dictionaries."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error("arXiv: XML parse error: %s", exc)
        return []

    papers: list[dict[str, Any]] = []
    entries = root.findall("{*}entry")
    if not entries:
        entries = root.findall("entry")

    for entry in entries:
        id_elem = entry.find("{*}id")
        if id_elem is None:
            id_elem = entry.find("id")
        raw_id = id_elem.text.strip() if id_elem is not None and id_elem.text else ""
        # e.g. http://arxiv.org/abs/2301.00001v1 -> 2301.00001
        arxiv_id = raw_id.split("/abs/")[-1].strip() if "/abs/" in raw_id else raw_id

        title_elem = entry.find("{*}title")
        if title_elem is None:
            title_elem = entry.find("title")
        title = _clean_text(title_elem.text if title_elem is not None else "")

        summary_elem = entry.find("{*}summary")
        if summary_elem is None:
            summary_elem = entry.find("summary")
        summary = _clean_text(summary_elem.text if summary_elem is not None else "")

        published_elem = entry.find("{*}published")
        if published_elem is None:
            published_elem = entry.find("published")
        published = published_elem.text.strip() if published_elem is not None and published_elem.text else ""

        updated_elem = entry.find("{*}updated")
        if updated_elem is None:
            updated_elem = entry.find("updated")
        updated = updated_elem.text.strip() if updated_elem is not None and updated_elem.text else ""

        # Authors
        authors: list[str] = []
        author_elems = entry.findall("{*}author")
        if not author_elems:
            author_elems = entry.findall("author")
        for author_elem in author_elems:
            name_elem = author_elem.find("{*}name")
            if name_elem is None:
                name_elem = author_elem.find("name")
            if name_elem is not None and name_elem.text:
                authors.append(name_elem.text.strip())

        # Category
        prim_cat = ""
        cat_elem = entry.find("{*}primary_category")
        if cat_elem is None:
            cat_elem = entry.find("primary_category")
        if cat_elem is not None:
            prim_cat = cat_elem.get("term", "")

        # PDF Link
        pdf_url = ""
        for link in entry.findall("{*}link") or entry.findall("link"):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href", "")
                break
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

        # Unique stable document ID
        doc_id = f"arxiv:{arxiv_id or hashlib.sha256(title.encode('utf-8')).hexdigest()[:16]}"
        authors_str = ", ".join(authors)

        content = (
            f"# {title}\n\n"
            f"**arXiv ID:** {arxiv_id}\n"
            f"**Authors:** {authors_str}\n"
            f"**Published:** {published} (Updated: {updated})\n"
            f"**Category:** {prim_cat}\n"
            f"**PDF:** {pdf_url}\n\n"
            f"### Abstract\n{summary}"
        )

        papers.append({
            "id": doc_id,
            "arxiv_id": arxiv_id,
            "title": title,
            "authors": authors_str,
            "summary": summary,
            "published": published,
            "updated": updated,
            "primary_category": prim_cat,
            "pdf_url": pdf_url,
            "content": content,
        })

    return papers


def fetch_arxiv_papers(
    query: str | None = None,
    category: str | None = None,
    id_list: list[str] | None = None,
    max_results: int = 50,
    page_size: int = 25,
    delay_seconds: float = 3.0,
    fetch_fn: Any = None,
) -> Iterator[dict[str, Any]]:
    """Query arXiv API with rate limiting and yield paper metadata."""
    search_query = query or ""
    if category:
        cat_filter = f"cat:{category}"
        search_query = f"{search_query} AND {cat_filter}" if search_query else cat_filter

    if not search_query and not id_list:
        search_query = "cat:cs.AI"

    start = 0
    total_yielded = 0

    while total_yielded < max_results:
        batch_size = min(page_size, max_results - total_yielded)
        params: dict[str, Any] = {
            "start": start,
            "max_results": batch_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        if search_query:
            params["search_query"] = search_query
        if id_list:
            params["id_list"] = ",".join(id_list)

        if fetch_fn is not None:
            xml_data = fetch_fn(params)
        else:
            try:
                resp = requests.get(ARXIV_API_URL, params=params, timeout=20)
                resp.raise_for_status()
                xml_data = resp.text
            except Exception as exc:
                logger.error("arXiv: API request failed: %s", exc)
                break

        papers = parse_arxiv_feed(xml_data)
        if not papers:
            break

        for paper in papers:
            yield paper
            total_yielded += 1
            if total_yielded >= max_results:
                break

        start += len(papers)
        if len(papers) < batch_size:
            break

        if fetch_fn is None and delay_seconds > 0 and total_yielded < max_results:
            time.sleep(delay_seconds)


def arxiv_source(
    query: str | None = None,
    category: str | None = None,
    id_list: list[str] | None = None,
    max_results: int = 50,
    fetch_fn: Any = None,
):
    """Create a dlt source yielding arXiv papers as documents."""
    import dlt

    @dlt.resource(name=ARXIV_TABLE_NAME, primary_key="id", write_disposition="replace")
    def arxiv_papers():
        count = 0
        for paper in fetch_arxiv_papers(
            query=query,
            category=category,
            id_list=id_list,
            max_results=max_results,
            delay_seconds=0.0 if fetch_fn else 3.0,
            fetch_fn=fetch_fn,
        ):
            count += 1
            yield paper
        logger.info("arXiv: synced %d research paper(s).", count)

    @dlt.source(name=ARXIV_SOURCE_NAME)
    def _arxiv():
        return arxiv_papers

    source = _arxiv()
    setattr(source, DOCUMENT_SOURCE_ATTR, ARXIV_SOURCE_NAME)
    return source
