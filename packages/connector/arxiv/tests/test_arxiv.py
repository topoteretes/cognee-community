"""Unit tests for the arXiv dlt connector.

Runnable in CI without hitting the arXiv API: HTTP requests are mocked, and
XML parsing / query building / date filtering are tested against fixture data.
"""

import xml.etree.ElementTree as ET

import pytest
from types import SimpleNamespace

from cognee_community_connector_arxiv.arxiv import (
    _build_content,
    _build_query,
    _clean_text,
    _entry_to_paper,
    _parse_xml,
    arxiv_source,
)

# ---------------------------------------------------------------------------
# Fixture: a minimal arXiv API Atom response with two entries.
# ---------------------------------------------------------------------------

_FIXTURE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>2</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2501.12345v1</id>
    <title>Test Paper One: On the Question of Things</title>
    <summary>A paper about   the   question   of things,
with a line break and  spacing noise.</summary>
    <published>2026-01-15T10:00:00Z</published>
    <updated>2026-01-16T10:00:00Z</updated>
    <author><name>Alice Example</name></author>
    <author><name>Bob Sample</name></author>
    <link rel="alternate" type="text/html" href="http://arxiv.org/abs/2501.12345v1"/>
    <link rel="related" type="application/pdf" href="http://arxiv.org/pdf/2501.12345v1"/>
    <arxiv:primary_category term="cs.AI"/>
    <category term="cs.AI"/>
    <category term="cs.LG"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2501.67890v2</id>
    <title>Test Paper Two: More Things</title>
    <summary>Another abstract.</summary>
    <published>2026-02-01T10:00:00Z</published>
    <updated>2026-02-02T10:00:00Z</updated>
    <author><name>Carol Tester</name></author>
    <link rel="alternate" type="text/html" href="http://arxiv.org/abs/2501.67890v2"/>
    <link rel="related" type="application/pdf" href="http://arxiv.org/pdf/2501.67890v2"/>
    <arxiv:primary_category term="cs.CL"/>
    <category term="cs.CL"/>
  </entry>
</feed>
"""


def _fixture_entries():
    root = _parse_xml(_FIXTURE_XML)
    return root.findall("{http://www.w3.org/2005/Atom}entry")


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------


def test_build_query_single_category():
    assert _build_query(["cs.AI"], None, None, None) == "cat:cs.AI"


def test_build_query_multiple_categories():
    assert _build_query(["cs.AI", "cs.LG"], None, None, None) == "(cat:cs.AI OR cat:cs.LG)"


def test_build_query_author_only():
    assert _build_query([], "LeCun", None, None) == "au:LeCun"


def test_build_query_categories_and_author():
    assert _build_query(["cs.AI"], "LeCun", None, None) == "cat:cs.AI AND au:LeCun"


def test_build_query_empty_returns_wildcard():
    assert _build_query([], None, None, None) == "*"


# ---------------------------------------------------------------------------
# Entry parsing (DB-free)
# ---------------------------------------------------------------------------


def test_entry_to_paper_extracts_fields():
    entries = _fixture_entries()
    paper = _entry_to_paper(entries[0], None, None)

    assert paper is not None
    assert paper["id"] == "2501.12345v1"
    assert paper["title"] == "Test Paper One: On the Question of Things"
    assert "question of things" in paper["content"]
    assert paper["authors"] == ["Alice Example", "Bob Sample"]
    assert paper["categories"] == ["cs.AI", "cs.LG"]
    assert paper["primary_category"] == "cs.AI"
    assert paper["published_date"] == "2026-01-15T10:00:00Z"
    assert "http://arxiv.org/abs/2501.12345v1" in paper["url"]
    assert "http://arxiv.org/pdf/2501.12345v1" in paper["pdf_url"]


def test_entry_to_paper_date_range_filters():
    entries = _fixture_entries()

    # First paper (2026-01-15) is inside the range, second (2026-02-01) is not.
    paper = _entry_to_paper(entries[0], "2026-01-01", "2026-01-31")
    assert paper is not None

    # Second paper is outside the range.
    paper2 = _entry_to_paper(entries[1], "2026-01-01", "2026-01-31")
    assert paper2 is None


def test_clean_text_normalizes():
    assert _clean_text("  hello   world \n\n") == "hello world"
    assert _clean_text("line one\nline two") == "line one\nline two"


def test_build_content_contains_metadata():
    content = _build_content(
        title="Title",
        summary="Abstract text",
        authors=["A", "B"],
        categories=["cs.AI"],
        primary_category="cs.AI",
        abs_url="http://arxiv.org/abs/2501.00001",
        pdf_url="http://arxiv.org/pdf/2501.00001",
    )
    assert "# Title" in content
    assert "**Primary Category:** cs.AI" in content
    assert "**Authors:** A, B" in content
    assert "Abstract text" in content
    assert "http://arxiv.org/abs/2501.00001" in content


# ---------------------------------------------------------------------------
# Source-level behavior (mocked client)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, text, status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


class FakeClient:
    """httpx.Client stand-in that returns fixture XML and records calls."""

    def __init__(self, responses=None):
        self.responses = responses or [FakeResponse(_FIXTURE_XML)]
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0)


def test_source_declares_document_marker():
    source = arxiv_source(categories=["cs.AI"], client=FakeClient())
    assert hasattr(source, "cognee_document_source")
    assert source.cognee_document_source == "arxiv"


def test_pagination_rate_limits(tmp_path, monkeypatch):
    """Multiple pages sleep between requests to respect the 3s rate limit."""
    import cognee_community_connector_arxiv.arxiv as arxiv_mod

    sleeps = []
    monkeypatch.setattr(arxiv_mod.time, "sleep", sleeps.append)

    response = FakeResponse(_FIXTURE_XML)
    client = FakeClient(responses=[response, response])

    # Force tiny page size by monkeypatching so we get 2+ requests.
    monkeypatch.setattr(arxiv_mod, "_ARXIV_PAGE_SIZE", 1)
    monkeypatch.setattr(arxiv_mod, "_ARXIV_RATE_LIMIT", 3.0)

    papers = list(arxiv_mod._fetch_papers(client, ["cs.AI"], None, 2, None, None))
    assert len(papers) == 2
    assert len(client.calls) >= 2
    assert sleeps == [3.0]  # one inter-page sleep


def test_retry_on_transient_error(tmp_path, monkeypatch):
    import cognee_community_connector_arxiv.arxiv as arxiv_mod

    # First call 503s, second succeeds.
    client = FakeClient(
        responses=[
            FakeResponse("error", status_code=503, headers={"retry-after": "1"}),
            FakeResponse(_FIXTURE_XML),
        ]
    )

    papers = list(arxiv_mod._fetch_papers(client, ["cs.AI"], None, 5, None, None))
    assert len(papers) == 2
    assert len(client.calls) == 2