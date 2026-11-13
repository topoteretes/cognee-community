from types import SimpleNamespace

import pytest

from cognee_community_connector_openalex.openalex import (
    OPENALEX_API_URL,
    _build_filters,
    _iter_works,
    abstract_from_inverted_index,
    openalex_source,
    work_to_row,
)


def test_abstract_from_inverted_index_orders_tokens():
    assert abstract_from_inverted_index({"world": [1], "Hello": [0], "!": [2]}) == "Hello world !"


def test_abstract_from_inverted_index_handles_missing_data():
    assert abstract_from_inverted_index(None) == ""
    assert abstract_from_inverted_index({}) == ""


def test_filters_normalize_doi_and_combine_scope():
    assert _build_filters(
        doi="https://doi.org/10.1000/example",
        author_id="A1",
        institution_id="I1",
        topic_id="T1",
        from_updated_date="2026-01-01",
    ) == [
        "doi:10.1000/example",
        "authorships.author.id:A1",
        "institutions.id:I1",
        "topics.id:T1",
        "from_updated_date:2026-01-01",
    ]


def test_work_to_row_builds_document_content():
    row = work_to_row(
        {
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1/test",
            "display_name": "A useful work",
            "authorships": [{"author": {"display_name": "Ada"}}],
            "topics": [{"display_name": "Databases"}],
            "abstract_inverted_index": {"Useful": [0], "abstract": [1]},
        }
    )
    assert row["id"] == "https://openalex.org/W1"
    assert row["url"] == "https://doi.org/10.1/test"
    assert "# A useful work" in row["content"]
    assert "Authors: Ada" in row["content"]
    assert "Useful abstract" in row["content"]


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _Client:
    def __init__(self):
        self.calls = []
        self.pages = [
            {"results": [{"id": "W1"}], "meta": {"next_cursor": "next"}},
            {"results": [{"id": "W2"}], "meta": {"next_cursor": None}},
        ]

    def get(self, url, *, params, timeout):
        self.calls.append((url, params, timeout))
        return _Response(self.pages.pop(0))

    def close(self):
        return None


def test_iter_works_follows_openalex_cursor_and_polite_pool_params():
    client = _Client()
    works = list(
        _iter_works(
            client,
            filters=["topics.id:T1"],
            api_key="key",
            mailto="me@example.com",
            per_page=10,
        )
    )
    assert [work["id"] for work in works] == ["W1", "W2"]
    assert client.calls[0][0] == OPENALEX_API_URL
    assert client.calls[0][1]["cursor"] == "*"
    assert client.calls[1][1]["cursor"] == "next"
    assert client.calls[0][1]["api_key"] == "key"
    assert client.calls[0][1]["mailto"] == "me@example.com"


def test_source_requires_scope():
    with pytest.raises(ValueError, match="at least one scope"):
        openalex_source(client=SimpleNamespace())
