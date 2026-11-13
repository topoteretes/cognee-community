"""Readwise connector – reads highlights from the Readwise Export API."""

import logging
import dlt
from dlt.sources.helpers import requests

logger = logging.getLogger(__name__)

READWISE_SOURCE_NAME = "readwise"
DOCUMENT_SOURCE_ATTR = "is_document_source"


def readwise_highlights(api_token: str | None = dlt.secrets.value):
    """Returns a dlt source that yields Readwise highlights.

    Args:
        api_token: Readwise access token (from https://readwise.io/access_token).
    """

    @dlt.resource(name="readwise_highlights", write_disposition="replace", primary_key="id")
    def _highlights_resource():
        if not api_token:
            raise ValueError("A Readwise API token is required")

        headers = {"Authorization": f"Token {api_token}"}
        url = "https://readwise.io/api/v2/export/"
        page_cursor = None

        while True:
            params = {}
            if page_cursor:
                params["pageCursor"] = page_cursor

            resp = requests.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            for book in results:
                book_title = book.get("title", "Untitled")
                book_author = book.get("author", "Unknown")
                source_type = book.get("category", "")
                source_url = book.get("source_url", "")

                highlights = book.get("highlights", [])
                for hl in highlights:
                    yield _highlight_to_row(hl, book_title, book_author, source_type, source_url)

            page_cursor = data.get("nextPageCursor")
            if not page_cursor:
                break

    @dlt.source(name=READWISE_SOURCE_NAME)
    def _readwise():
        return _highlights_resource()

    source = _readwise()
    setattr(source, DOCUMENT_SOURCE_ATTR, READWISE_SOURCE_NAME)
    return source


def _highlight_to_row(
    hl: dict, book_title: str, book_author: str, source_type: str, source_url: str
) -> dict:
    """Convert a Readwise highlight into a flat document row."""
    text = hl.get("text", "")
    note = hl.get("note", "")
    location = hl.get("location", "")
    highlighted_at = hl.get("highlighted_at", "")
    tags = ", ".join(t.get("name", "") for t in hl.get("tags", []))

    content = (
        f"Source: {book_title}\n"
        f"Author: {book_author}\n"
        f"Type: {source_type}\n"
        f"URL: {source_url}\n"
        f"Location: {location}\n"
        f"Highlighted: {highlighted_at}\n"
        f"Tags: {tags}\n\n"
        f"Highlight:\n{text}"
    )
    if note:
        content += f"\n\nNote:\n{note}"

    return {
        "id": f"readwise_{hl.get('id')}",
        "title": f"{book_title} – highlight",
        "content": content,
    }
