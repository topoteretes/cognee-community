"""Unit tests for the Penpot open-source design connector."""

from pathlib import Path
import pytest

from cognee_community_connector_penpot import (
    PENPOT_SOURCE_NAME,
    PENPOT_TABLE_NAME,
    format_comment_thread_markdown,
    format_page_document_markdown,
    parse_penpot_page_objects,
    penpot_source,
)

SAMPLE_PAGE_OBJECTS = {
    "objects": {
        "board-1": {
            "id": "board-1",
            "name": "Checkout Flow Screen",
            "type": "frame",
        },
        "text-1": {
            "id": "text-1",
            "name": "Header Label",
            "type": "text",
            "text": "Complete Your Order with One-Click",
        },
        "text-2": {
            "id": "text-2",
            "name": "Subtitle Label",
            "type": "text",
            "text": "Zero transaction fees on Base L2",
        },
        "comp-1": {
            "id": "comp-1",
            "name": "Primary Pay Button",
            "type": "component",
            "componentName": "Button/PrimaryLarge",
        },
    }
}

SAMPLE_COMMENT_THREAD = {
    "id": "thread-901",
    "boardName": "Checkout Flow Screen",
    "isResolved": True,
    "comments": [
        {
            "ownerDisplayName": "Alice Designer",
            "createdAt": "2026-10-25T14:00:00Z",
            "text": "Let's change the pay button styling to outlined secondary to match the dark theme.",
        },
        {
            "ownerDisplayName": "Bob Frontend",
            "createdAt": "2026-10-25T14:30:00Z",
            "text": "Updated in the Tailwind design token library. Resolving thread.",
        },
    ],
}

SAMPLE_DESIGN_FILE = {
    "id": "file-xyz-123",
    "name": "E-Commerce Mobile App Design",
    "projectName": "Checkout Redesign Q4",
    "pages": [
        {
            "id": "page-1",
            "name": "Cart & Checkout Pages",
            "objects": SAMPLE_PAGE_OBJECTS["objects"],
        }
    ],
    "threads": [SAMPLE_COMMENT_THREAD],
}


def test_parse_penpot_page_objects():
    """Verify parsing boards, text copy, and component instances."""
    boards, text_layers, components = parse_penpot_page_objects(SAMPLE_PAGE_OBJECTS)
    assert len(boards) == 1
    assert boards[0]["name"] == "Checkout Flow Screen"
    assert "Complete Your Order with One-Click" in text_layers
    assert "Zero transaction fees on Base L2" in text_layers
    assert "Button/PrimaryLarge" in components


def test_format_page_document_markdown():
    """Verify markdown output formatting for design pages."""
    boards, text_layers, components = parse_penpot_page_objects(SAMPLE_PAGE_OBJECTS)
    markdown = format_page_document_markdown(
        file_name="E-Commerce Mobile App",
        page_name="Checkout Page",
        project_name="Checkout Redesign",
        boards=boards,
        text_layers=text_layers,
        components=components,
    )
    assert "# [Penpot Design Page] E-Commerce Mobile App / Checkout Page" in markdown
    assert "**Project:** `Checkout Redesign`" in markdown
    assert "**Screen / Board:** `Checkout Flow Screen`" in markdown
    assert "Instance of `Button/PrimaryLarge`" in markdown
    assert '"Complete Your Order with One-Click"' in markdown


def test_format_comment_thread_markdown():
    """Verify comment thread rendering with authors, timestamps, and resolution badges."""
    markdown = format_comment_thread_markdown("E-Commerce Mobile App", SAMPLE_COMMENT_THREAD)
    assert "# [Penpot Discussion] E-Commerce Mobile App — Checkout Flow Screen" in markdown
    assert "RESOLVED" in markdown
    assert "Alice Designer (2026-10-25T14:00:00Z)" in markdown
    assert "Bob Frontend (2026-10-25T14:30:00Z)" in markdown
    assert "Tailwind design token library" in markdown


def test_penpot_source_with_snapshot():
    """Verify DLT source generation and document schema from snapshot."""
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt not installed in test environment")

    source = penpot_source(snapshot_data=[SAMPLE_DESIGN_FILE])
    resources = list(source.resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == PENPOT_TABLE_NAME

    items = list(resource)
    assert len(items) == 2  # 1 page document + 1 comment thread document
    ids = {item["id"] for item in items}
    assert "penpot_page_file-xyz-123_page-1" in ids
    assert "penpot_thread_file-xyz-123_thread-901" in ids


def test_missing_snapshot_file_error():
    """Verify FileNotFoundError is raised if non-existent file path is specified."""
    with pytest.raises(FileNotFoundError):
        penpot_source(snapshot_data="/tmp/non_existent_penpot_file_9999.json")
