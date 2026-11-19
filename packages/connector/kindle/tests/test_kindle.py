"""Unit tests for the Amazon Kindle highlights connector."""

from pathlib import Path
import pytest

from cognee_community_connector_kindle import (
    KINDLE_SOURCE_NAME,
    KINDLE_TABLE_NAME,
    format_book_clippings_markdown,
    kindle_source,
    parse_book_title_and_author,
    parse_clipping_entry,
    parse_clippings_file,
)

SAMPLE_CLIPPINGS_CONTENT = """﻿Atomic Habits (James Clear)
- Your Highlight on page 42 | location 642-645 | Added on Friday, October 25, 2024 10:30:15 AM

You do not rise to the level of your goals. You fall to the level of your systems.
==========
Atomic Habits (James Clear)
- Your Note on page 42 | location 645 | Added on Friday, October 25, 2024 10:32:00 AM

Focus on identity-based habits rather than outcome-based habits.
==========
Atomic Habits (James Clear)
- Your Bookmark on page 50 | location 750 | Added on Friday, October 25, 2024 11:00:00 AM


==========
Thinking, Fast and Slow (Daniel Kahneman)
- Your Highlight on page 10 | location 120-125 | Added on Saturday, October 26, 2024 09:15:00 AM

System 1 operates automatically and quickly, with little or no effort and no sense of voluntary control.
==========
"""

# Sample for testing highlight extensions
SAMPLE_OVERLAPPING_CLIPPINGS = """﻿Deep Work (Cal Newport)
- Your Highlight on location 100-102 | Added on Monday, October 28, 2024 14:00:00 PM

Deep work is the ability to focus
==========
Deep Work (Cal Newport)
- Your Highlight on location 100-105 | Added on Monday, October 28, 2024 14:01:00 PM

Deep work is the ability to focus without distraction on a cognitively demanding task.
==========
"""


def test_parse_book_title_and_author():
    """Verify title and author extraction across various formats and BOM characters."""
    title, author = parse_book_title_and_author("Atomic Habits (James Clear)")
    assert title == "Atomic Habits"
    assert author == "James Clear"

    # With UTF-8 BOM
    title_bom, author_bom = parse_book_title_and_author("\ufeffThinking, Fast and Slow (Daniel Kahneman)")
    assert title_bom == "Thinking, Fast and Slow"
    assert author_bom == "Daniel Kahneman"

    # No author in parentheses
    title_raw, author_raw = parse_book_title_and_author("Standalone Manual")
    assert title_raw == "Standalone Manual"
    assert author_raw == "Unknown Author"


def test_parse_clipping_entry():
    """Verify parsing highlight, note, and skipping bookmark."""
    highlight_block = """Atomic Habits (James Clear)
- Your Highlight on page 42 | location 642-645 | Added on Friday, October 25, 2024 10:30:15 AM

You do not rise to the level of your goals.
"""
    parsed = parse_clipping_entry(highlight_block)
    assert parsed is not None
    assert parsed["title"] == "Atomic Habits"
    assert parsed["author"] == "James Clear"
    assert parsed["type"] == "Highlight"
    assert parsed["page"] == 42
    assert parsed["location_start"] == 642
    assert parsed["location_end"] == 645
    assert "You do not rise to the level of your goals." in parsed["text"]

    # Bookmark should be skipped (None)
    bookmark_block = """Atomic Habits (James Clear)
- Your Bookmark on page 50 | location 750 | Added on Friday, October 25, 2024 11:00:00 AM
"""
    assert parse_clipping_entry(bookmark_block) is None


def test_parse_clippings_file_overlap_resolution():
    """Verify overlapping extended highlights are resolved to the longer entry."""
    books = parse_clippings_file(SAMPLE_OVERLAPPING_CLIPPINGS)
    assert len(books) == 1
    key = "Deep Work:::Cal Newport"
    assert key in books
    entries = books[key]
    assert len(entries) == 1
    assert "on a cognitively demanding task" in entries[0]["text"]
    assert entries[0]["location_end"] == 105


def test_format_book_clippings_markdown():
    """Verify markdown output structure for a book document."""
    books = parse_clippings_file(SAMPLE_CLIPPINGS_CONTENT)
    key = "Atomic Habits:::James Clear"
    markdown = format_book_clippings_markdown("Atomic Habits", "James Clear", books[key])

    assert "# [Kindle Book Highlights] Atomic Habits" in markdown
    assert "- **Author:** James Clear" in markdown
    assert "- **Total Highlights:** 1" in markdown
    assert "- **Total Notes:** 1" in markdown
    assert "> You do not rise to the level of your goals." in markdown
    assert "📝 Note at Page 42 | Location 645" in markdown
    assert "*Focus on identity-based habits rather than outcome-based habits.*" in markdown


def test_kindle_source_with_content():
    """Verify DLT source generation and document schema."""
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt not installed in test environment")

    source = kindle_source(content=SAMPLE_CLIPPINGS_CONTENT)
    resources = list(source.resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == KINDLE_TABLE_NAME

    items = list(resource)
    assert len(items) == 2
    titles = {item["book_title"] for item in items}
    assert "Atomic Habits" in titles
    assert "Thinking, Fast and Slow" in titles


def test_missing_file_error():
    """Verify FileNotFoundError is raised for non-existent clippings file."""
    with pytest.raises(FileNotFoundError):
        kindle_source(path="/tmp/non_existent_clippings_9999.txt")
