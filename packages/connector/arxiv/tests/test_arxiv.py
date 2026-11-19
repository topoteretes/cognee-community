"""Offline contract tests for arXiv query snapshots and DLT replacement safety."""

from __future__ import annotations

from urllib.error import HTTPError

import dlt
import pytest
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from dlt.pipeline.exceptions import PipelineStepFailed

import cognee_community_connector_arxiv.arxiv as arxiv
from cognee_community_connector_arxiv import arxiv_source

ATOM = "http://www.w3.org/2005/Atom"
OPEN_SEARCH = "http://a9.com/-/spec/opensearch/1.1/"
ARXIV = "http://arxiv.org/schemas/atom"


def _entry(
    paper_id="2401.00001v2",
    title="A Representative Paper",
    abstract="An abstract\nwith normalized whitespace.",
    updated="2024-01-04T10:00:00Z",
    categories=("cs.AI", "cs.LG"),
    authors=("Ada Lovelace", "Alan Turing"),
):
    authors_xml = "".join(f"<author><name>{name}</name></author>" for name in authors)
    categories_xml = "".join(f'<category term="{cat}" />' for cat in categories)
    return (
        f"<entry><id>https://arxiv.org/abs/{paper_id}</id>"
        "<published>2024-01-02T09:00:00Z</published>"
        f"<updated>{updated}</updated><title> {title}\n </title>"
        f"<summary> {abstract} </summary>{authors_xml}{categories_xml}"
        f'<arxiv:primary_category term="{categories[0]}" />'
        "</entry>"
    )


def _feed(entries, *, total=None, start=0, items=None, updated="2026-10-05T00:00:00Z"):
    if total is None:
        total = len(entries)
    if items is None:
        items = len(entries)
    return (
        f'<feed xmlns="{ATOM}" xmlns:opensearch="{OPEN_SEARCH}" '
        f'xmlns:arxiv="{ARXIV}">'
        f"<updated>{updated}</updated><opensearch:totalResults>{total}</opensearch:totalResults>"
        f"<opensearch:startIndex>{start}</opensearch:startIndex>"
        f"<opensearch:itemsPerPage>{items}</opensearch:itemsPerPage>"
        f"{''.join(entries)}</feed>"
    ).encode()


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def query_page(self, query, start, page_size):
        self.calls.append((query, start, page_size))
        value = self.pages[start]
        if isinstance(value, Exception):
            raise value
        return value


def _paper(paper_id, title=None, **kwargs):
    return _entry(paper_id=paper_id, title=title or f"Paper {paper_id}", **kwargs)


def _resource_rows(source):
    resource = next(iter(source.resources.values()))
    return list(resource)


def _pipeline_sync(pipeline, source):
    pipeline.run(source)
    with pipeline.sql_client() as client:
        return client.execute_sql("SELECT id, title, content FROM arxiv_papers ORDER BY id")


@pytest.fixture
def dlt_pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="arxiv_snapshot_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'arxiv.db'}"),
        dataset_name="arxiv_snapshot_test",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def test_parse_extracts_stable_identity_metadata_and_abstract():
    row = arxiv._parse_page(_feed([_paper("2401.00001v2", title="A Representative Paper")]), 0, 10)[
        "papers"
    ][0]

    assert row["id"] == "2401.00001"
    assert row["url"] == "https://arxiv.org/abs/2401.00001"
    assert row["title"] == "A Representative Paper"
    assert row["content"] == (
        "Authors: Ada Lovelace, Alan Turing\nCategories: cs.AI, cs.LG\n\n"
        "Submitted: 2024-01-02T09:00:00Z\n"
        "Updated: 2024-01-04T10:00:00Z\n\n## Abstract\n"
        "An abstract with normalized whitespace."
    )


def test_old_style_arxiv_identifier_keeps_slash_and_strips_version():
    row = arxiv._parse_page(_feed([_paper("hep-ex/0307015v4")]), 0, 10)["papers"][0]
    assert row["id"] == "hep-ex/0307015"
    assert row["url"] == "https://arxiv.org/abs/hep-ex/0307015"


def test_rendering_and_document_data_item_are_deterministic():
    xml = _feed([_paper("2401.00001v2")])
    first = arxiv._parse_page(xml, 0, 10)["papers"][0]
    second = arxiv._parse_page(xml, 0, 10)["papers"][0]
    assert first == second

    row = type("Row", (), {"row_data": first, "content_hash": "ignored"})()
    item = _build_document_data_item(row, "stable-data-id", "arxiv")
    assert item.external_metadata["source"] == "arxiv"
    assert item.external_metadata["external_id"] == "2401.00001"
    assert item.external_metadata["url"] == "https://arxiv.org/abs/2401.00001"
    assert item.data.count(f"# {first['title']}") == 1
    assert "An abstract with normalized whitespace." in item.data


def test_snapshot_follows_all_pages_and_builds_selected_query():
    pages = FakeClient(
        {
            0: _feed([_paper("2401.00001v1"), _paper("2401.00002v1")], total=3, start=0),
            2: _feed([_paper("2401.00003v1")], total=3, start=2, updated="2026-10-05T00:00:00Z"),
        }
    )
    rows = list(arxiv._iter_snapshot(pages, '(cat:cs.AI) AND (au:"Ada Lovelace")', 2))

    assert [row["id"] for row in rows] == ["2401.00001", "2401.00002", "2401.00003"]
    assert [call[1:] for call in pages.calls] == [(0, 2), (2, 2)]


def test_arxiv_final_page_echoes_requested_max_results_not_actual_entry_count():
    # Live arXiv behavior for the acceptance query: total=530, max_results=300;
    # offset 0 has 300 entries and offset 300 has 230, while both pages report
    # itemsPerPage=300. The metadata is the requested maximum, not final-page count.
    papers = [_paper(f"2601.{index:05d}v1") for index in range(530)]
    client = FakeClient(
        {
            0: _feed(papers[:300], total=530, start=0, items=300),
            300: _feed(papers[300:], total=530, start=300, items=300),
        }
    )

    query = '(cat:cs.LG) AND (au:"Yoshua Bengio") AND submittedDate:[201001010000 TO 202610042359]'
    rows = list(arxiv._iter_snapshot(client, query, 300))

    assert len(rows) == 530
    assert [call[0] for call in client.calls] == [query, query]
    assert [call[1:] for call in client.calls] == [(0, 300), (300, 300)]
    assert rows[0]["id"] == "2601.00000"
    assert rows[-1]["id"] == "2601.00529"


def test_source_requests_category_author_and_submitted_date_selection():
    client = FakeClient({0: _feed([], total=0)})
    source = arxiv_source(
        categories=["cs.AI", "cs.LG", "cs.AI"],
        authors=["Ada Lovelace"],
        submitted_date_range=("202401010000", "202412312359"),
        client=client,
    )

    assert _resource_rows(source) == []
    query = client.calls[0][0]
    assert query == (
        '(cat:cs.AI OR cat:cs.LG) AND (au:"Ada Lovelace") AND '
        "submittedDate:[202401010000 TO 202412312359]"
    )


def test_empty_valid_query_yields_empty_snapshot():
    client = FakeClient({0: _feed([], total=0)})
    assert list(arxiv._iter_snapshot(client, "cat:cs.AI", 100)) == []
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "Select at least one"),
        ({"categories": ["cs.AI OR all:foo"]}, "category identifiers"),
        ({"authors": [""]}, "non-empty strings"),
        ({"categories": ["cs.AI"], "page_size": 0}, "page_size"),
        (
            {"categories": ["cs.AI"], "submitted_date_range": ("202501010000", "202401010000")},
            "no later",
        ),
        (
            {"categories": ["cs.AI"], "submitted_date_range": ("20240101", "20241231")},
            "YYYYMMDDHHMM",
        ),
    ],
)
def test_configuration_validation(kwargs, message):
    with pytest.raises(ValueError, match=message):
        arxiv_source(client=FakeClient({}), **kwargs)


def test_page_size_may_not_exceed_provider_slice_limit():
    with pytest.raises(ValueError, match="between 1 and 2000"):
        arxiv_source(categories=["cs.AI"], page_size=2001, client=FakeClient({}))


def test_malformed_xml_fails_closed():
    with pytest.raises(arxiv.ArxivAPIError, match="malformed XML"):
        list(arxiv._iter_snapshot(FakeClient({0: b"<feed>"}), "cat:cs.AI", 5))


def test_unexpected_document_fails_closed():
    with pytest.raises(arxiv.ArxivAPIError, match="instead of an Atom feed"):
        list(arxiv._iter_snapshot(FakeClient({0: b"<html/>"}), "cat:cs.AI", 5))


def test_malformed_or_missing_paging_metadata_fails_closed():
    with pytest.raises(arxiv.ArxivAPIError, match="missing totalResults"):
        payload = b'<feed xmlns="%s" />' % ATOM.encode()
        list(arxiv._iter_snapshot(FakeClient({0: payload}), "cat:cs.AI", 5))


def test_inconsistent_start_index_fails_closed():
    page = _feed([_paper("2401.00001v1")], total=1, start=1, items=1)
    with pytest.raises(arxiv.ArxivAPIError, match="invalid paging metadata at offset 0"):
        list(arxiv._iter_snapshot(FakeClient({0: page}), "cat:cs.AI", 1))


def test_unexpected_items_per_page_value_fails_closed():
    page = _feed([_paper("2401.00001v1")], total=1, start=0, items=4)
    with pytest.raises(arxiv.ArxivAPIError, match="invalid itemsPerPage"):
        list(arxiv._iter_snapshot(FakeClient({0: page}), "cat:cs.AI", 3))


def test_short_page_fails_closed():
    page = _feed([_paper("2401.00001v1")], total=2, start=0, items=2)
    with pytest.raises(arxiv.ArxivAPIError, match="expected 2"):
        list(arxiv._iter_snapshot(FakeClient({0: page}), "cat:cs.AI", 2))


def test_zero_sized_page_fails_when_results_remain():
    page = _feed([], total=1, start=0, items=0)
    with pytest.raises(arxiv.ArxivAPIError, match="no pagination progress"):
        list(arxiv._iter_snapshot(FakeClient({0: page}), "cat:cs.AI", 2))


def test_provider_page_size_shorter_than_requested_is_followed_to_completion():
    client = FakeClient(
        {
            start: _feed([_paper(f"2401.{start:05d}v1")], total=3, start=start, items=1)
            for start in range(3)
        }
    )

    rows = list(arxiv._iter_snapshot(client, "cat:cs.AI", 2))

    assert len(rows) == 3
    assert [call[1:] for call in client.calls] == [(0, 2), (1, 2), (2, 2)]


def test_changing_provider_page_size_fails_closed():
    client = FakeClient(
        {
            0: _feed([_paper("2401.00001v1")], total=3, start=0, items=1),
            1: _feed(
                [_paper("2401.00002v1"), _paper("2401.00003v1")],
                total=3,
                start=1,
                items=2,
            ),
        }
    )

    with pytest.raises(arxiv.ArxivAPIError, match="changed its effective page size"):
        list(arxiv._iter_snapshot(client, "cat:cs.AI", 2))


def test_final_short_page_is_valid_when_count_matches_remaining_results():
    client = FakeClient(
        {
            0: _feed([_paper("2401.00001v1"), _paper("2401.00002v1")], total=3, start=0, items=2),
            2: _feed([_paper("2401.00003v1")], total=3, start=2, items=2),
        }
    )

    assert len(list(arxiv._iter_snapshot(client, "cat:cs.AI", 2))) == 3


def test_provider_query_result_limit_fails_before_snapshot_is_accepted():
    client = FakeClient({0: _feed([_paper("2401.00001v1")], total=30_001, start=0, items=1)})
    with pytest.raises(arxiv.ArxivAPIError, match="above the API limit of 30000"):
        list(arxiv._iter_snapshot(client, "cat:cs.AI", 1))
    assert len(client.calls) == 1


def test_feed_updated_timestamp_can_change_between_pages():
    client = FakeClient(
        {
            0: _feed([_paper("2401.00001v1")], total=2, start=0, items=1),
            1: _feed(
                [_paper("2401.00002v1")],
                total=2,
                start=1,
                items=1,
                updated="2026-10-05T00:00:03Z",
            ),
        }
    )

    assert len(list(arxiv._iter_snapshot(client, "cat:cs.AI", 1))) == 2


def test_unstable_result_total_fails_closed():
    client = FakeClient(
        {
            0: _feed([_paper("2401.00001v1")], total=2, start=0, items=1),
            1: _feed([_paper("2401.00002v1")], total=3, start=1, items=1),
        }
    )
    with pytest.raises(arxiv.ArxivAPIError, match="changed while paging"):
        list(arxiv._iter_snapshot(client, "cat:cs.AI", 1))


def test_duplicate_ids_across_pages_fail_closed():
    client = FakeClient(
        {
            0: _feed([_paper("2401.00001v1")], total=2, start=0, items=1),
            1: _feed([_paper("2401.00001v2")], total=2, start=1, items=1),
        }
    )
    with pytest.raises(arxiv.ArxivAPIError, match="duplicate paper id"):
        list(arxiv._iter_snapshot(client, "cat:cs.AI", 1))


def test_duplicate_ids_within_page_fail_closed():
    client = FakeClient(
        {0: _feed([_paper("2401.00001v1"), _paper("2401.00001v2")], total=2, start=0, items=2)}
    )
    with pytest.raises(arxiv.ArxivAPIError, match="duplicate paper id"):
        list(arxiv._iter_snapshot(client, "cat:cs.AI", 2))


def test_withdrawn_title_remains_a_present_record():
    row = arxiv._parse_page(_feed([_paper("2401.00001v3", title="A Paper (withdrawn)")]), 0, 1)[
        "papers"
    ][0]
    assert row["id"] == "2401.00001"
    assert "withdrawn" in row["title"]


def test_dlt_resource_uses_replace_document_mode_and_stable_primary_key():
    source = arxiv_source(categories=["cs.AI"], client=FakeClient({0: _feed([], total=0)}))
    resource = next(iter(source.resources.values()))
    schema = resource.compute_table_schema()
    write_disposition = schema["write_disposition"]
    if isinstance(write_disposition, dict):
        write_disposition = write_disposition.get("disposition")
    assert schema["columns"]["id"]["primary_key"] is True
    assert write_disposition == "replace"
    assert getattr(source, arxiv.DOCUMENT_SOURCE_ATTR) == "arxiv"


def test_complete_snapshot_dlt_e2e_is_repeatable_and_reconciles_absence(dlt_pipeline):
    first = FakeClient({0: _feed([_paper("2401.00001v1"), _paper("2401.00002v1")], total=2)})
    rows1 = _pipeline_sync(dlt_pipeline, arxiv_source(categories=["cs.AI"], client=first))
    assert [row[0] for row in rows1] == ["2401.00001", "2401.00002"]

    second = FakeClient({0: _feed([_paper("2401.00001v1"), _paper("2401.00002v1")], total=2)})
    rows2 = _pipeline_sync(dlt_pipeline, arxiv_source(categories=["cs.AI"], client=second))
    assert rows2 == rows1

    changed = FakeClient({0: _feed([_paper("2401.00001v2", title="Revised Paper")], total=1)})
    rows3 = _pipeline_sync(dlt_pipeline, arxiv_source(categories=["cs.AI"], client=changed))
    assert [row[0] for row in rows3] == ["2401.00001"]
    assert rows3[0][1] == "Revised Paper"


@pytest.mark.parametrize(
    "failed_pages",
    [
        {0: arxiv.ArxivAPIError("first page unavailable")},
        {
            0: _feed([_paper("2401.00001v1")], total=2, start=0, items=1),
            1: arxiv.ArxivAPIError("middle page unavailable"),
        },
        {
            0: _feed([_paper("2401.00001v1")], total=2, start=0, items=1),
            1: b"<broken",
        },
        {
            0: _feed([_paper("2401.00001v1")], total=2, start=0, items=1),
            1: _feed([], total=2, start=1, items=0),
        },
        {0: _feed([_paper("2401.00001v1")], total=30_001, start=0, items=1)},
    ],
    ids=["first-page-error", "middle-page-error", "malformed-page", "short-page", "query-limit"],
)
def test_failed_or_incomplete_replacement_preserves_previous_snapshot(dlt_pipeline, failed_pages):
    initial = FakeClient({0: _feed([_paper("2401.00001v1"), _paper("2401.00002v1")], total=2)})
    previous = _pipeline_sync(dlt_pipeline, arxiv_source(categories=["cs.AI"], client=initial))

    with pytest.raises(PipelineStepFailed):
        _pipeline_sync(
            dlt_pipeline,
            arxiv_source(categories=["cs.AI"], client=FakeClient(failed_pages)),
        )

    with dlt_pipeline.sql_client() as client:
        after_failure = client.execute_sql(
            "SELECT id, title, content FROM arxiv_papers ORDER BY id"
        )
    assert after_failure == previous


def test_valid_empty_snapshot_replaces_dlt_rows(dlt_pipeline):
    initial = FakeClient({0: _feed([_paper("2401.00001v1")], total=1)})
    _pipeline_sync(dlt_pipeline, arxiv_source(categories=["cs.AI"], client=initial))
    empty = FakeClient({0: _feed([], total=0)})

    rows = _pipeline_sync(dlt_pipeline, arxiv_source(categories=["cs.AI"], client=empty))

    assert rows == []


def test_http_rate_limit_becomes_explicit_provider_error(monkeypatch):
    def http_429(request, timeout):
        raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(arxiv, "_LAST_REQUEST_AT", None)
    monkeypatch.setattr(arxiv.urllib.request, "urlopen", http_429)
    with pytest.raises(arxiv.ArxivAPIError, match="HTTP 429"):
        arxiv._ArxivClient().query_page("cat:cs.AI", 0, 1)


def test_request_gate_enforces_three_second_spacing(monkeypatch):
    clock = {"now": 0.0, "sleeps": []}

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"ok"

    monkeypatch.setattr(arxiv, "_LAST_REQUEST_AT", None)
    monkeypatch.setattr(arxiv.time, "monotonic", monotonic)
    monkeypatch.setattr(arxiv.time, "sleep", sleep)
    monkeypatch.setattr(arxiv.urllib.request, "urlopen", lambda request, timeout: Response())
    client = arxiv._ArxivClient()

    assert client.query_page("cat:cs.AI", 0, 1) == b"ok"
    assert client.query_page("cat:cs.AI", 1, 1) == b"ok"
    assert clock["sleeps"] == [3.0]
