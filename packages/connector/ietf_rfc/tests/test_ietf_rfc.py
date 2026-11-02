"""Unit tests for the IETF RFC and Internet Standards connector."""

from pathlib import Path
import pytest

from cognee_community_connector_ietf_rfc import (
    IETF_RFC_SOURCE_NAME,
    IETF_RFC_TABLE_NAME,
    format_rfc_markdown,
    ietf_rfc_source,
    normalize_rfc_number,
    parse_rfc_relationships,
)

SAMPLE_RFC_9111_HTTP_CACHING = {
    "number": 9111,
    "title": "HTTP Caching",
    "status": "Internet Standard",
    "stream": "IETF",
    "working_group": "httpbis",
    "publication_date": "2022-06",
    "authors": ["Roy T. Fielding", "Mark Nottingham", "Julian Reschke"],
    "also_known_as": ["STD 98"],
    "obsoletes": [7234, 2616],
    "obsoleted_by": [],
    "updates": [],
    "updated_by": [],
    "abstract": "The Hypertext Transfer Protocol (HTTP) is a stateless application-level protocol. This document defines HTTP caching mechanisms and constraints on cache servers.",
    "errata": [
        {
            "errata_id": 7012,
            "status": "Verified",
            "type": "Editorial",
            "section": "3.2",
            "notes": "Typographical clarification in heuristic freshness calculation formula.",
        }
    ],
}

SAMPLE_RFC_7234_LEGACY_CACHING = {
    "number": 7234,
    "title": "Hypertext Transfer Protocol (HTTP/1.1): Caching",
    "status": "Proposed Standard",
    "stream": "IETF",
    "working_group": "httpbis",
    "publication_date": "2014-06",
    "authors": ["Roy T. Fielding", "Mark Nottingham", "Julian Reschke"],
    "obsoletes": [2616],
    "obsoleted_by": [9111],
    "abstract": "HTTP/1.1 caching specification (superseded by RFC 9111).",
}


def test_normalize_rfc_number():
    """Verify normalization of various RFC representation formats."""
    assert normalize_rfc_number(9111) == 9111
    assert normalize_rfc_number("9111") == 9111
    assert normalize_rfc_number("RFC 9111") == 9111
    assert normalize_rfc_number("rfc7234") == 7234
    assert normalize_rfc_number("  RFC  2616  ") == 2616

    with pytest.raises(ValueError):
        normalize_rfc_number("not_an_rfc_number")


def test_parse_rfc_relationships():
    """Verify extraction and deduplication of RFC relationship lists."""
    assert parse_rfc_relationships([7234, "RFC 2616", 7234]) == [2616, 7234]
    assert parse_rfc_relationships("RFC 7234, RFC 2616, RFC 2068") == [2068, 2616, 7234]
    assert parse_rfc_relationships("") == []
    assert parse_rfc_relationships(None) == []


def test_format_rfc_markdown():
    """Verify Markdown rendering, supersession state badges, and errata formatting."""
    # Active standard
    md_active = format_rfc_markdown(SAMPLE_RFC_9111_HTTP_CACHING)
    assert "# RFC 9111: HTTP Caching" in md_active
    assert "CURRENT & ACTIVE" in md_active
    assert "- **Maturity Level:** `Internet Standard`" in md_active
    assert "- **Working Group:** `httpbis`" in md_active
    assert "- **Also Known As:** `STD 98`" in md_active
    assert "**Obsoletes:** RFC 2616, RFC 7234" in md_active
    assert "## Verified Errata & Amendments" in md_active
    assert "Erratum #7012" in md_active

    # Obsoleted standard
    md_obsoleted = format_rfc_markdown(SAMPLE_RFC_7234_LEGACY_CACHING)
    assert "# RFC 7234:" in md_obsoleted
    assert "**OBSOLETED** by RFC 9111" in md_obsoleted
    assert "**Obsoleted By:** RFC 9111" in md_obsoleted


def test_ietf_rfc_source_with_snapshot():
    """Verify DLT source generation, supersession flags, and metadata."""
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt not installed in test environment")

    source = ietf_rfc_source(
        snapshot_data=[
            SAMPLE_RFC_9111_HTTP_CACHING,
            SAMPLE_RFC_7234_LEGACY_CACHING,
        ]
    )

    resources = list(source.resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == IETF_RFC_TABLE_NAME

    items = list(resource)
    assert len(items) == 2

    # Map by number
    by_num = {item["number"]: item for item in items}
    assert by_num[9111]["is_obsoleted"] is False
    assert by_num[7234]["is_obsoleted"] is True
    assert by_num[9111]["id"] == "rfc9111"
    assert by_num[7234]["id"] == "rfc7234"
    assert "ietf" in by_num[9111]["cognee_node_set"]


def test_missing_snapshot_file_error():
    """Verify FileNotFoundError is raised if non-existent file is provided."""
    with pytest.raises(FileNotFoundError):
        ietf_rfc_source(snapshot_data="/tmp/non_existent_rfc_index_9999.json")
