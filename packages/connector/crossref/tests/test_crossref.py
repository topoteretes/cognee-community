"""Tests for Crossref DOI metadata connector for cognee."""

import pytest
from pathlib import Path
from cognee_community_connector_crossref.crossref import (
    DOCUMENT_SOURCE_ATTR,
    normalize_doi,
    format_crossref_document,
    parse_crossref_works,
    crossref_source,
)


@pytest.fixture
def mock_crossref_payload():
    return {
        "items": [
            {
                "DOI": "10.1145/3442188.3445922",
                "title": ["On the Dangers of Stochastic Parrots: Can Language Models Be Too Big? 🦜"],
                "container-title": ["Proceedings of the 2021 ACM FAccT Conference"],
                "author": [
                    {"given": "Emily M.", "family": "Bender", "ORCID": "https://orcid.org/0000-0001-6585-8025"},
                    {"given": "Timnit", "family": "Gebru"},
                    {"given": "Angelina", "family": "McMillan-Major"},
                    {"given": "Shmargaret", "family": "Shmitchell"},
                ],
                "issued": {"date-parts": [[2021, 3, 1]]},
                "is-referenced-by-count": 2450,
                "license": [
                    {
                        "URL": "https://www.acm.org/publications/policies/author-rights",
                        "start": {"date-parts": [[2021, 3, 1]]},
                    }
                ],
            }
        ]
    }


def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1145/3442188.3445922") == "10.1145/3442188.3445922"
    assert normalize_doi("http://dx.doi.org/10.1038/nature12373") == "10.1038/nature12373"
    assert normalize_doi("DOI: 10.1000/182") == "10.1000/182"
    assert normalize_doi("10.1000/182") == "10.1000/182"


def test_format_crossref_document(mock_crossref_payload):
    work = mock_crossref_payload["items"][0]
    md = format_crossref_document(work)
    assert "[Crossref Publication] On the Dangers of Stochastic Parrots" in md
    assert "Canonical DOI:** `10.1145/3442188.3445922`" in md
    assert "[Emily M. Bender](https://orcid.org/0000-0001-6585-8025)" in md
    assert "Timnit Gebru" in md
    assert "Proceedings of the 2021 ACM FAccT Conference" in md
    assert "2021-03-01" in md
    assert "2,450" in md
    assert "https://www.acm.org/publications/policies/author-rights" in md


def test_parse_crossref_works(mock_crossref_payload):
    docs = parse_crossref_works(mock_crossref_payload)
    assert len(docs) == 1
    doc = docs[0]
    assert "10_1145_3442188_3445922" in doc["id"]
    assert "crossref:doi:10_1145_3442188_3445922" in doc["node_sets"]
    assert "crossref:author:bender" in doc["node_sets"]
    assert "crossref:author:gebru" in doc["node_sets"]
    assert doc["metadata"]["source"] == DOCUMENT_SOURCE_ATTR


def test_missing_snapshot_file_error():
    with pytest.raises(FileNotFoundError):
        crossref_source(snapshot_data="/non/existent/crossref_snapshot.json")


def test_crossref_source_with_snapshot(mock_crossref_payload):
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt is not installed in the test environment")

    source = crossref_source(snapshot_data=mock_crossref_payload)
    assert source is not None
