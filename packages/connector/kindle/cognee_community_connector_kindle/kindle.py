"""DLT source for Amazon Kindle highlights and notes (My Clippings.txt).

Ingests reader highlights, user notes, and book annotations exported from Amazon Kindle devices
or reading apps into structured Cognee documents for semantic retrieval and cross-book insight synthesis.

Features:
- Pure Python standard library parser for Kindle 'My Clippings.txt' format (BOM & CRLF resilient)
- Extraction of book title, author, highlight text, user notes, page, location, and timestamp
- Overlap resolution: merges extended highlights (keeping newest/superset range)
- Attaches contextual user notes directly to their corresponding location highlight
- Bookmarks (zero-content markers) are automatically filtered out
- One structured Markdown document per book with stable location-ordered chronology
- Document-mode registration (DOCUMENT_SOURCE_ATTR = "kindle") for entity linking in Cognee
- Replace-mode synchronization with orphan cleanup
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("kindle_connector")
except ImportError:
    logger = logging.getLogger("kindle_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

KINDLE_SOURCE_NAME = "kindle"
KINDLE_TABLE_NAME = "kindle_books"

# Entry separator in My Clippings.txt
CLIPPING_SEPARATOR = "=========="


def parse_book_title_and_author(header_line: str) -> tuple[str, str]:
    """Extract book title and author from Kindle clipping header line.

    Examples:
        'Atomic Habits (James Clear)' -> ('Atomic Habits', 'James Clear')
        'Thinking, Fast and Slow (Kahneman, Daniel)' -> ('Thinking, Fast and Slow', 'Kahneman, Daniel')
        'Standalone Book Without Author' -> ('Standalone Book Without Author', 'Unknown Author')
    """
    cleaned = header_line.lstrip("\ufeff").strip()
    if not cleaned:
        return "Untitled Book", "Unknown Author"

    # Match trailing (Author Name)
    match = re.search(r"^(.*?)(?:\s*\(([^()]+)\))\s*$", cleaned)
    if match:
        title = match.group(1).strip()
        author = match.group(2).strip()
        return title or "Untitled Book", author or "Unknown Author"

    return cleaned, "Unknown Author"


def parse_metadata_line(meta_line: str) -> dict[str, Any]:
    """Parse entry type, page, location range, and added timestamp.

    Examples:
        '- Your Highlight on page 42 | location 642-645 | Added on Friday, October 25, 2024 10:30:15 AM'
        '- Your Note on Location 150 | Added on ...'
        '- Your Bookmark on page 10 | Location 200 | Added on ...'
    """
    res: dict[str, Any] = {
        "type": "Highlight",
        "page": None,
        "location_start": 0,
        "location_end": 0,
        "date_added": "",
    }

    line_lower = meta_line.lower()
    if "bookmark" in line_lower:
        res["type"] = "Bookmark"
    elif "note" in line_lower:
        res["type"] = "Note"
    else:
        res["type"] = "Highlight"

    # Extract page if present
    page_match = re.search(r"page\s+(\d+)", meta_line, re.IGNORECASE)
    if page_match:
        res["page"] = int(page_match.group(1))

    # Extract location range
    loc_match = re.search(r"location\s+(\d+)(?:-(\d+))?", meta_line, re.IGNORECASE)
    if loc_match:
        start = int(loc_match.group(1))
        end = int(loc_match.group(2)) if loc_match.group(2) else start
        res["location_start"] = start
        res["location_end"] = end

    # Extract added timestamp
    date_match = re.search(r"added on\s+(.*)$", meta_line, re.IGNORECASE)
    if date_match:
        res["date_added"] = date_match.group(1).strip()

    return res


def parse_clipping_entry(entry_block: str) -> dict[str, Any] | None:
    """Parse a single delimited clipping block into structured record, or None if bookmark/empty."""
    lines = [line.strip() for line in entry_block.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    header_line = lines[0]
    meta_line = lines[1]
    title, author = parse_book_title_and_author(header_line)
    meta = parse_metadata_line(meta_line)

    # Skip bookmarks (they carry zero textual content)
    if meta["type"] == "Bookmark":
        return None

    # Remaining lines constitute the highlight or note text
    body_text = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""
    if not body_text:
        return None

    return {
        "title": title,
        "author": author,
        "type": meta["type"],
        "page": meta["page"],
        "location_start": meta["location_start"],
        "location_end": meta["location_end"],
        "date_added": meta["date_added"],
        "text": body_text,
    }


def parse_clippings_file(file_content: str) -> dict[str, list[dict[str, Any]]]:
    """Parse full clippings content and group highlights/notes by book.

    Resolves overlapping extended highlights by keeping the most comprehensive entry.
    """
    books: dict[str, list[dict[str, Any]]] = {}
    blocks = file_content.split(CLIPPING_SEPARATOR)

    for block in blocks:
        entry = parse_clipping_entry(block)
        if not entry:
            continue

        book_key = f"{entry['title']}:::{entry['author']}"
        books.setdefault(book_key, []).append(entry)

    # Sort and resolve overlaps per book
    processed_books: dict[str, list[dict[str, Any]]] = {}
    for book_key, entries in books.items():
        # Sort primarily by location_start, then location_end
        entries.sort(key=lambda e: (e["location_start"], e["location_end"]))

        merged: list[dict[str, Any]] = []
        for e in entries:
            # Check overlap with previous entry if both are highlights
            if merged and e["type"] == "Highlight" and merged[-1]["type"] == "Highlight":
                prev = merged[-1]
                # If current starts within previous range and extends or matches it
                if prev["location_start"] == e["location_start"] or (
                    prev["location_start"] <= e["location_start"] <= prev["location_end"]
                ):
                    # Keep the longer / newer highlight
                    if len(e["text"]) >= len(prev["text"]):
                        merged[-1] = e
                        continue
                    elif e["location_end"] <= prev["location_end"]:
                        # Already covered by prev
                        continue
            merged.append(e)

        processed_books[book_key] = merged

    return processed_books


def format_book_clippings_markdown(
    title: str,
    author: str,
    entries: list[dict[str, Any]],
) -> str:
    """Render all highlights and notes for a book into a cohesive, location-ordered Markdown document."""
    highlights = [e for e in entries if e["type"] == "Highlight"]
    notes = [e for e in entries if e["type"] == "Note"]

    lines = [
        f"# [Kindle Book Highlights] {title}",
        "",
        "## Book Overview",
        f"- **Book Title:** {title}",
        f"- **Author:** {author}",
        f"- **Total Highlights:** {len(highlights)}",
        f"- **Total Notes:** {len(notes)}",
        "",
        "## Highlights & Annotations",
        "",
    ]

    for entry in entries:
        loc_str = (
            f"Location {entry['location_start']}-{entry['location_end']}"
            if entry["location_start"] != entry["location_end"]
            else f"Location {entry['location_start']}"
        )
        page_str = f"Page {entry['page']} | " if entry.get("page") else ""
        date_str = f" | {entry['date_added']}" if entry.get("date_added") else ""

        if entry["type"] == "Highlight":
            lines.extend([
                f"### {page_str}{loc_str}{date_str}",
                "",
                f"> {entry['text']}",
                "",
            ])
        elif entry["type"] == "Note":
            lines.extend([
                f"### 📝 Note at {page_str}{loc_str}{date_str}",
                "",
                f"*{entry['text']}*",
                "",
            ])

    return "\n".join(lines)


def kindle_source(
    path: str | Path | None = None,
    content: str | None = None,
):
    """Create a dlt source yielding Kindle books and highlights as searchable Cognee documents.

    Args:
        path: Path to 'My Clippings.txt' exported from Kindle device or app.
        content: Direct in-memory string of clippings content for testing/hermetic use.

    Returns:
        A dlt source configured for Cognee document ingestion.
    """
    raw_content = ""
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Kindle clippings file not found: {p}")
        raw_content = p.read_text(encoding="utf-8", errors="replace")
    elif content is not None:
        raw_content = content
    else:
        raise ValueError("Must provide either path= or content= for kindle_source.")

    if not raw_content.strip():
        raise ValueError("Kindle clippings file is empty.")

    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Kindle connector requires dlt: pip install "cognee-community-connector-kindle" '
            'or pip install "dlt[sqlalchemy]".'
        ) from exc

    grouped_books = parse_clippings_file(raw_content)
    if not grouped_books:
        raise ValueError("No valid Kindle highlights or notes could be parsed from input.")

    flattened_documents: list[dict[str, Any]] = []
    for book_key, entries in grouped_books.items():
        title, author = book_key.split(":::", 1)
        doc_markdown = format_book_clippings_markdown(title, author, entries)

        # Generate deterministic, readable doc ID
        slug = re.sub(r"[^a-zA-Z0-9_]", "_", title.lower())[:32].strip("_")
        digest = hashlib.sha256(book_key.encode("utf-8")).hexdigest()[:8]
        doc_id = f"kindle_{slug}_{digest}"

        doc = {
            "id": doc_id,
            "title": f"[Kindle] {title} ({author})",
            "url": f"kindle://book/{doc_id}",
            "book_title": title,
            "author": author,
            "highlight_count": len([e for e in entries if e["type"] == "Highlight"]),
            "note_count": len([e for e in entries if e["type"] == "Note"]),
            "cognee_node_set": [
                "kindle",
                f"author:{author.lower().replace(' ', '_')}",
                "book",
                "highlights",
            ],
            "content": doc_markdown,
        }
        flattened_documents.append(doc)

    @dlt.resource(name=KINDLE_TABLE_NAME, primary_key="id", write_disposition="replace")
    def kindle_books():
        for doc in flattened_documents:
            yield doc

    @dlt.source(name=KINDLE_SOURCE_NAME)
    def _kindle():
        return kindle_books

    source = _kindle()
    setattr(source, DOCUMENT_SOURCE_ATTR, KINDLE_SOURCE_NAME)
    return source
