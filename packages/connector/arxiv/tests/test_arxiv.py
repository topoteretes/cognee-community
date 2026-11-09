"""Unit tests for the arXiv dlt connector.

DB-free tests for query construction, entry parsing, and paper-to-row
flattening. All runnable in CI without a live arXiv connection.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cognee_community_connector_arxiv.arxiv import (
    ARXIV_SOURCE_NAME,
    _build_query,
    _parse_entries,
    _parse_entry,
    _parse_total_results,
    _paper_to_row,
    arxiv_source,
)


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


def test_build_query_with_categories():
    q = _build_query(None, ["cs.AI", "cs.CL"], None, None)
    assert "cat:cs.AI" in q
    assert "cat:cs.CL" in q
    assert "OR" in q


def test_build_query_single_category():
    q = _build_query(None, ["cs.AI"], None, None)
    assert q == "cat:cs.AI"


def test_build_query_with_author():
    q = _build_query(None, None, "Bengio", None)
    assert "au:Bengio" in q


def test_build_query_with_free_text():
    q = _build_query("transformer attention", None, None, None)
    assert "all:transformer" in q or "all:transformer%20attention" in q


def test_build_query_with_submitted_after():
    q = _build_query(None, ["cs.AI"], None, "2026-01-01")
    assert "submittedDate:[2026-01-01" in q
    assert "99999999999999" in q


def test_build_query_combines_with_and():
    q = _build_query(None, ["cs.AI"], "Bengio", None)
    assert "AND" in q
    assert "cat:cs.AI" in q
    assert "au:Bengio" in q


def test_build_query_defaults_when_no_params():
    q = _build_query(None, None, None, None)
    assert "all:" in q


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

_SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">2</opensearch:totalResults>
  <opensearch:startIndex xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">0</opensearch:startIndex>
  <opensearch:itemsPerPage xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">2</opensearch:itemsPerPage>
  <entry>
    <id>http://arxiv.org/abs/2601.12345v2</id>
    <updated>2026-01-15T00:00:00Z</updated>
    <published>2026-01-10T00:00:00Z</published>
    <title>Attention Is All You Need: A Revisit</title>
    <summary>We revisit the transformer architecture and propose improvements.</summary>
    <author>
      <name>Alice Smith</name>
    </author>
    <author>
      <name>Bob Jones</name>
    </author>
    <arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="cs.CL" />
    <arxiv:doi xmlns:arxiv="http://arxiv.org/schemas/atom">10.1000/test</arxiv:doi>
    <arxiv:comment xmlns:arxiv="http://arxiv.org/schemas/atom">15 pages, 5 figures</arxiv:comment>
    <category term="cs.CL" />
    <category term="cs.AI" />
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2601.67890v1</id>
    <updated>2026-01-20T00:00:00Z</updated>
    <published>2026-01-18T00:00:00Z</published>
    <title>Graph Neural Networks for Reasoning</title>
    <summary>We propose a GNN-based approach for logical reasoning.</summary>
    <author>
      <name>Carol Wang</name>
    </author>
    <arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="cs.AI" />
    <category term="cs.AI" />
  </entry>
</feed>"""


def test_parse_total_results():
    assert _parse_total_results(_SAMPLE_XML) == 2


def test_parse_total_results_empty():
    empty_xml = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
</feed>"""
    assert _parse_total_results(empty_xml) == 0


def test_parse_entries_count():
    entries = _parse_entries(_SAMPLE_XML)
    assert len(entries) == 2


def test_parse_entry_strips_url_prefix():
    entries = _parse_entries(_SAMPLE_XML)
    assert entries[0]["arxiv_id"] == "2601.12345"  # version stripped
    assert entries[1]["arxiv_id"] == "2601.67890"


def test_parse_entry_title():
    entries = _parse_entries(_SAMPLE_XML)
    assert "Attention Is All You Need" in entries[0]["title"]


def test_parse_entry_abstract():
    entries = _parse_entries(_SAMPLE_XML)
    assert "transformer architecture" in entries[0]["abstract"]


def test_parse_entry_authors():
    entries = _parse_entries(_SAMPLE_XML)
    assert "Alice Smith" in entries[0]["authors"]
    assert "Bob Jones" in entries[0]["authors"]
    assert entries[1]["authors"] == "Carol Wang"


def test_parse_entry_categories():
    entries = _parse_entries(_SAMPLE_XML)
    assert entries[0]["primary_category"] == "cs.CL"
    assert "cs.CL" in entries[0]["categories"]
    assert "cs.AI" in entries[0]["categories"]
    assert entries[1]["primary_category"] == "cs.AI"


def test_parse_entry_doi_and_comment():
    entries = _parse_entries(_SAMPLE_XML)
    assert entries[0]["doi"] == "10.1000/test"
    assert "15 pages" in entries[0]["comment"]
    assert entries[1]["doi"] == ""
    assert entries[1]["comment"] == ""


def test_parse_entry_published_date():
    entries = _parse_entries(_SAMPLE_XML)
    assert entries[0]["published"] == "2026-01-10T00:00:00Z"
    assert entries[1]["published"] == "2026-01-18T00:00:00Z"


# ---------------------------------------------------------------------------
# Paper -> row flattening
# ---------------------------------------------------------------------------


def test_paper_to_row_contains_markdown():
    paper = {
        "arxiv_id": "2601.12345",
        "arxiv_url": "http://arxiv.org/abs/2601.12345v2",
        "title": "Test Paper",
        "abstract": "This is the abstract.",
        "authors": "Alice, Bob",
        "primary_category": "cs.AI",
        "categories": "cs.AI, cs.CL",
        "published": "2026-01-10T00:00:00Z",
        "updated": "2026-01-15T00:00:00Z",
        "doi": "",
        "comment": "",
        "journal_ref": "",
    }

    row = _paper_to_row(paper)

    assert row["id"] == "2601.12345"
    assert row["url"] == "http://arxiv.org/abs/2601.12345v2"
    assert row["title"] == "Test Paper"
    assert "# Test Paper" in row["content"]
    assert "Alice, Bob" in row["content"]
    assert "This is the abstract." in row["content"]
    assert "## Abstract" in row["content"]


def test_paper_to_row_includes_doi_when_present():
    paper = {
        "arxiv_id": "2601.12345",
        "arxiv_url": "http://arxiv.org/abs/2601.12345",
        "title": "Test",
        "abstract": "Abstract.",
        "authors": "Author",
        "primary_category": "cs.AI",
        "categories": "cs.AI",
        "published": "2026-01-01T00:00:00Z",
        "updated": "2026-01-01T00:00:00Z",
        "doi": "10.1000/test",
        "comment": "5 pages",
        "journal_ref": "",
    }

    row = _paper_to_row(paper)
    assert "DOI: 10.1000/test" in row["content"]
    assert "Comment: 5 pages" in row["content"]


def test_paper_to_row_omits_empty_fields():
    paper = {
        "arxiv_id": "2601.12345",
        "arxiv_url": "http://arxiv.org/abs/2601.12345",
        "title": "Test",
        "abstract": "Abstract.",
        "authors": "Author",
        "primary_category": "cs.AI",
        "categories": "cs.AI",
        "published": "2026-01-01T00:00:00Z",
        "updated": "2026-01-01T00:00:00Z",
        "doi": "",
        "comment": "",
        "journal_ref": "",
    }

    row = _paper_to_row(paper)
    assert "DOI:" not in row["content"]
    assert "Comment:" not in row["content"]


# ---------------------------------------------------------------------------
# Source construction
# ---------------------------------------------------------------------------


def test_arxiv_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = arxiv_source(categories=["cs.AI"], max_results=1)
    assert ARXIV_SOURCE_NAME == "arxiv"
    assert document_source_tag(source) == "arxiv"
