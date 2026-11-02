"""Runnable example for the Amazon Kindle highlights connector.

Demonstrates parsing 'My Clippings.txt', extracting title/author metadata,
merging extended highlights, and generating structured Cognee documents for agent recall.
"""

from cognee_community_connector_kindle import (
    format_book_clippings_markdown,
    kindle_source,
    parse_book_title_and_author,
    parse_clippings_file,
)

SAMPLE_CLIPPINGS = """﻿Atomic Habits (James Clear)
- Your Highlight on page 42 | location 642-645 | Added on Friday, October 25, 2024 10:30:15 AM

You do not rise to the level of your goals. You fall to the level of your systems.
==========
Atomic Habits (James Clear)
- Your Note on page 42 | location 645 | Added on Friday, October 25, 2024 10:32:00 AM

Focus on identity-based habits rather than outcome-based habits.
==========
Thinking, Fast and Slow (Daniel Kahneman)
- Your Highlight on page 10 | location 120-125 | Added on Saturday, October 26, 2024 09:15:00 AM

System 1 operates automatically and quickly, with little or no effort and no sense of voluntary control.
==========
Thinking, Fast and Slow (Daniel Kahneman)
- Your Highlight on page 24 | location 310-315 | Added on Saturday, October 26, 2024 10:45:00 AM

A reliable way to make people believe in falsehoods is frequent repetition, because familiarity is not easily distinguished from truth.
==========
"""


def main():
    print("=" * 70)
    print(" Amazon Kindle Highlights Connector for Cognee - Demo")
    print("=" * 70)

    # 1. Test title and author extraction
    print("\n1. Testing Title and Author Extraction:")
    headers = [
        "Atomic Habits (James Clear)",
        "Thinking, Fast and Slow (Daniel Kahneman)",
        "Zero to One: Notes on Startups (Peter Thiel; Blake Masters)",
        "Standalone Uncredited Article",
    ]
    for h in headers:
        title, author = parse_book_title_and_author(h)
        print(f"   - Raw:    '{h}'")
        print(f"     Parsed: Book: '{title}' | Author: '{author}'")

    # 2. Parse clippings content
    print("\n2. Parsing and Grouping Clippings by Book:")
    books = parse_clippings_file(SAMPLE_CLIPPINGS)
    print(f"   Total Distinct Books Parsed: {len(books)}")
    for book_key, entries in books.items():
        title, author = book_key.split(":::", 1)
        highlights = [e for e in entries if e["type"] == "Highlight"]
        notes = [e for e in entries if e["type"] == "Note"]
        print(f"   - '{title}' by {author}: {len(highlights)} highlight(s), {len(notes)} note(s)")

    # 3. Format markdown preview
    print("\n3. Rendered Cognee Document Preview for 'Atomic Habits':")
    print("-" * 50)
    first_key = list(books.keys())[0]
    title, author = first_key.split(":::", 1)
    doc_markdown = format_book_clippings_markdown(title, author, books[first_key])
    print(doc_markdown)
    print("-" * 50)

    # 4. Initialize DLT source
    try:
        print("\n4. Initializing DLT source...")
        source = kindle_source(content=SAMPLE_CLIPPINGS)
        print("   ✓ DLT source initialized successfully!")
        print(f"   ✓ Document mode marker: {getattr(source, 'cognee_document_source', None)}")
    except ImportError as e:
        print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ Kindle highlights connector demo completed successfully!")


if __name__ == "__main__":
    main()
