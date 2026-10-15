"""Unit tests for the arXiv dlt connector.

Two layers, all runnable in CI without network access:

* DB-free tests for query construction, Atom parsing, entry→row flattening,
  rate-limit spacing, retry classification, and the generic document DataItem
  tagging (``source="arxiv"``) that routes papers through normal cognify.
* dlt-pipeline tests (injected fetcher, temp sqlite destination) covering the
  acceptance criteria: re-sync reflects edits, and papers that stop matching
  the query drop out of the full-snapshot load (forget-on-delete).

Unlike the Notion connector there is no client library to import-skip on: arXiv
needs no auth and the fetcher is injected, so every test here runs anywhere.
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest

# The row → document-DataItem mapping is generic and owned by the ingestion
# layer (any document source uses it), not the connector.
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_arxiv import arxiv as arxiv_module
from cognee_community_connector_arxiv.arxiv import (
    ARXIV_SOURCE_NAME,
    MIN_REQUEST_INTERVAL,
    _as_timestamp,
    _authors,
    _build_query,
    _categories,
    _entry_to_row,
    _is_transient,
    _iter_entries,
    _paper_id,
    _parse_feed,
    _render_entry,
    _request,
    _retry_after,
    _Throttle,
    arxiv_source,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _entry_xml(
    paper_id="2101.00001v1",
    title="A Paper",
    summary="An abstract.",
    authors=("Ada Lovelace",),
    categories=("cs.AI",),
    published="2026-01-01T00:00:00Z",
):
    author_xml = "".join(f"<author><name>{name}</name></author>" for name in authors)
    primary = f'<arxiv:primary_category term="{categories[0]}"/>' if categories else ""
    category_xml = "".join(f'<category term="{term}"/>' for term in categories)
    return (
        "<entry>"
        f"<id>https://arxiv.org/abs/{paper_id}</id>"
        f"<title>{title}</title>"
        f"<summary>{summary}</summary>"
        f"<published>{published}</published>"
        f"{author_xml}{primary}{category_xml}"
        "</entry>"
    )


def _feed(entries_xml, total=None):
    total_xml = f"<opensearch:totalResults>{total}</opensearch:totalResults>" if total else ""
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:arxiv="http://arxiv.org/schemas/atom" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"{total_xml}{''.join(entries_xml)}"
        "</feed>"
    )


def _one_entry(**kwargs):
    """Parse a single entry element, for the element-level helpers."""
    entries, _ = _parse_feed(_feed([_entry_xml(**kwargs)]))
    return entries[0]


class FakeArxiv:
    """Stand-in for the arXiv API backed by an in-memory list of papers.

    Honours ``start``/``max_results`` so pagination is exercised for real, and
    records every call so request spacing and query strings can be asserted.
    """

    def __init__(self, papers, report_total=True):
        self.papers = list(papers)
        self.calls = []
        self.report_total = report_total

    def __call__(self, params):
        self.calls.append(params)
        start = int(params["start"])
        size = int(params["max_results"])
        page = self.papers[start : start + size]
        total = len(self.papers) if self.report_total else None
        return _feed([_entry_xml(**paper) for paper in page], total=total)


def _recording_throttle():
    """A throttle that records what it would have slept instead of sleeping."""
    slept = []
    return _Throttle(MIN_REQUEST_INTERVAL, sleep=slept.append), slept


# ---------------------------------------------------------------------------
# Query construction (DB-free)
# ---------------------------------------------------------------------------


def test_build_query_single_category_is_unwrapped():
    assert _build_query(["cs.AI"], None, None, None, None) == "cat:cs.AI"


def test_build_query_ors_categories_and_ands_authors():
    query = _build_query(["cs.AI", "cs.CL"], ["Ada Lovelace"], None, None, None)
    assert query == '(cat:cs.AI OR cat:cs.CL) AND au:"Ada Lovelace"'


def test_build_query_quotes_only_values_with_spaces():
    assert _build_query(None, ["Hinton"], None, None, None) == "au:Hinton"


def test_build_query_raw_search_query_replaces_built_clauses():
    query = _build_query(["cs.AI"], ["Ada"], 'ti:"attention"', None, None)
    assert query == '(ti:"attention")'


def test_build_query_raw_search_query_still_takes_date_window():
    query = _build_query(None, None, "all:graph", "20260101", "20260201")
    assert query == "(all:graph) AND submittedDate:[202601010000 TO 202602012359]"


def test_build_query_date_window_pads_day_bounds():
    query = _build_query(["cs.AI"], None, None, "2026-01-01", "2026-02-01")
    assert "submittedDate:[202601010000 TO 202602012359]" in query


def test_build_query_open_ended_window_is_bounded_on_both_sides():
    # An unbounded side still needs a concrete bound: arXiv rejects a
    # half-open range.
    query = _build_query(None, None, None, "20260101", None)
    assert query == "submittedDate:[202601010000 TO 299912312359]"


def test_build_query_without_any_scope_is_rejected():
    with pytest.raises(ValueError, match="all of arXiv"):
        _build_query(None, None, None, None, None)


def test_as_timestamp_accepts_full_precision():
    assert _as_timestamp("202601011530", "0000") == "202601011530"


def test_as_timestamp_rejects_malformed_date():
    with pytest.raises(ValueError, match="YYYYMMDD"):
        _as_timestamp("2026", "0000")


# ---------------------------------------------------------------------------
# Feed parsing (DB-free)
# ---------------------------------------------------------------------------


def test_parse_feed_returns_entries_and_total():
    entries, total = _parse_feed(_feed([_entry_xml(), _entry_xml(paper_id="2101.2v1")], total=7))
    assert len(entries) == 2
    assert total == 7


def test_parse_feed_without_total_returns_none():
    _, total = _parse_feed(_feed([_entry_xml()]))
    assert total is None


def test_parse_feed_empty_is_not_an_error():
    entries, _ = _parse_feed(_feed([]))
    assert entries == []


def test_parse_feed_raises_on_arxiv_error_document():
    # arXiv reports a malformed query as HTTP 200 with a single "Error" entry;
    # ingesting that as a paper would silently poison memory.
    error_entry = (
        "<entry><id>https://arxiv.org/api/errors</id><title>Error</title>"
        "<summary>sortBy must be one of...</summary></entry>"
    )
    with pytest.raises(RuntimeError, match="sortBy must be one of"):
        _parse_feed(_feed([error_entry]))


def test_parse_feed_does_not_mistake_a_real_paper_titled_error():
    # Guard the guard: a legitimate single result must survive.
    entries, _ = _parse_feed(_feed([_entry_xml(title="Error Analysis of Solvers")]))
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# Entry -> row (DB-free)
# ---------------------------------------------------------------------------


def test_paper_id_strips_version_suffix():
    assert _paper_id(_one_entry(paper_id="2101.00001v3")) == "2101.00001"


def test_paper_id_keeps_pre_2007_archive_prefix():
    assert _paper_id(_one_entry(paper_id="math/0309136v1")) == "math/0309136"


def test_paper_id_handles_unversioned_id():
    assert _paper_id(_one_entry(paper_id="2101.00001")) == "2101.00001"


def test_entry_to_row_flattens_and_strips_version_from_url():
    row = _entry_to_row(_one_entry(paper_id="2101.00001v2", title="On Graphs"))
    assert row["id"] == "2101.00001"
    # Version-stripped so a v3 that changes nothing textual does not churn the
    # content hash.
    assert row["url"] == "https://arxiv.org/abs/2101.00001"
    assert row["title"] == "On Graphs"
    assert "published" not in row


def test_entry_to_row_collapses_wrapped_whitespace():
    # arXiv hard-wraps titles and abstracts across lines with leading padding.
    row = _entry_to_row(_one_entry(title="A\n  wrapped\n  title"))
    assert row["title"] == "A wrapped title"


def test_render_entry_omits_title_to_avoid_duplicate_heading():
    # _build_document_data_item prepends "# {title}"; emitting it here too
    # would give every paper two headings.
    content = _render_entry(_one_entry(title="On Graphs"))
    assert "# On Graphs" not in content


def test_render_entry_includes_provenance_then_abstract():
    content = _render_entry(
        _one_entry(
            authors=("Ada Lovelace", "Alan Turing"),
            categories=("cs.AI", "cs.CL"),
            summary="We show that things.",
        )
    )
    assert "**Authors:** Ada Lovelace, Alan Turing" in content
    assert "**Categories:** cs.AI, cs.CL" in content
    assert "**Published:** 2026-01-01T00:00:00Z" in content
    assert content.endswith("We show that things.")
    # Abstract separated from the metadata block by a blank line.
    assert "\n\nWe show that things." in content


def test_render_entry_with_only_an_abstract_has_no_leading_blank_line():
    content = _render_entry(
        _one_entry(authors=(), categories=(), published="", summary="Just prose.")
    )
    assert content == "Just prose."


def test_authors_skips_empty_names():
    entry = _one_entry(authors=("Ada", "", "Alan"))
    assert _authors(entry) == ["Ada", "Alan"]


def test_categories_puts_primary_first_without_duplicates():
    entry = _one_entry(categories=("cs.CL", "cs.AI", "cs.CL"))
    assert _categories(entry) == ["cs.CL", "cs.AI"]


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "id": "2101.00001",
            "url": "https://arxiv.org/abs/2101.00001",
            "title": "On Graphs",
            "content": "**Authors:** Ada\n\nWe show that things.",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "2101.00001")

    item = _build_document_data_item(row, data_id, "arxiv")

    # source="arxiv" (not "dlt") is what routes the paper through normal cognify.
    assert item.external_metadata["source"] == "arxiv"
    assert item.external_metadata["url"] == "https://arxiv.org/abs/2101.00001"
    assert item.external_metadata["external_id"] == "2101.00001"
    assert item.data_id == data_id
    assert item.data.startswith("# On Graphs")
    assert "We show that things." in item.data


def test_arxiv_source_declares_document_marker():
    # resolve_dlt_sources routes on the document-source marker (not on this name),
    # but the tag it carries is the source name; keep it stable.
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = arxiv_source(categories=["cs.AI"], fetch=FakeArxiv([]))
    assert ARXIV_SOURCE_NAME == "arxiv"
    assert document_source_tag(source) == "arxiv"


# ---------------------------------------------------------------------------
# Pagination + rate limiting (DB-free)
# ---------------------------------------------------------------------------


def test_iter_entries_paginates_until_short_page():
    fake = FakeArxiv([{"paper_id": f"2101.{n}v1"} for n in range(250)])
    throttle, _ = _recording_throttle()

    entries = list(_iter_entries(fake, "cat:cs.AI", throttle, None))

    assert len(entries) == 250
    # 100 + 100 + 50: the short final page ends the loop.
    assert [call["start"] for call in fake.calls] == [0, 100, 200]


def test_iter_entries_stops_on_reported_total_without_extra_request():
    # An exact multiple of the page size would otherwise cost one empty request.
    fake = FakeArxiv([{"paper_id": f"2101.{n}v1"} for n in range(200)])
    throttle, _ = _recording_throttle()

    entries = list(_iter_entries(fake, "cat:cs.AI", throttle, None))

    assert len(entries) == 200
    assert len(fake.calls) == 2


def test_iter_entries_terminates_when_total_overstates_the_feed():
    # A feed claiming more results than it serves must not loop forever.
    fake = FakeArxiv([{"paper_id": "2101.1v1"}], report_total=False)
    throttle, _ = _recording_throttle()

    assert len(list(_iter_entries(fake, "cat:cs.AI", throttle, None))) == 1


def test_iter_entries_respects_max_results():
    fake = FakeArxiv([{"paper_id": f"2101.{n}v1"} for n in range(250)])
    throttle, _ = _recording_throttle()

    entries = list(_iter_entries(fake, "cat:cs.AI", throttle, 120))

    assert len(entries) == 120
    # Second request asks for only the 20 still needed, not a full page.
    assert fake.calls[1]["max_results"] == 20


def test_iter_entries_sends_the_query_and_a_deterministic_sort():
    fake = FakeArxiv([{"paper_id": "2101.1v1"}])
    throttle, _ = _recording_throttle()

    list(_iter_entries(fake, "cat:cs.AI", throttle, None))

    assert fake.calls[0]["search_query"] == "cat:cs.AI"
    assert fake.calls[0]["sortBy"] == "submittedDate"


def test_throttle_does_not_sleep_before_the_first_request():
    throttle, slept = _recording_throttle()
    throttle.wait()
    assert slept == []


def test_throttle_spaces_consecutive_requests():
    throttle, slept = _recording_throttle()
    throttle.wait()
    throttle.wait()
    assert len(slept) == 1
    assert 0 < slept[0] <= MIN_REQUEST_INTERVAL


def test_throttle_sleeps_only_the_time_still_owed(monkeypatch):
    # Work done between requests counts toward the interval rather than being
    # added to it.
    clock = iter([100.0, 100.0, 102.0, 102.0])
    monkeypatch.setattr(arxiv_module.time, "monotonic", lambda: next(clock))
    throttle, slept = _recording_throttle()

    throttle.wait()
    throttle.wait()

    assert slept == [pytest.approx(1.0)]


def test_pagination_is_throttled_between_requests():
    fake = FakeArxiv([{"paper_id": f"2101.{n}v1"} for n in range(150)])
    throttle, slept = _recording_throttle()

    list(_iter_entries(fake, "cat:cs.AI", throttle, None))

    # Two requests, one gap: the rate limit is built in, not discovered.
    assert len(fake.calls) == 2
    assert len(slept) == 1


# ---------------------------------------------------------------------------
# Retry classification (DB-free)
# ---------------------------------------------------------------------------


def _status_error(status, headers=None):
    request = httpx.Request("GET", arxiv_module.ARXIV_API_URL)
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_is_transient_covers_rate_limit_and_server_errors():
    # 403 is arXiv's over-rate response, not an auth failure — it must retry.
    assert _is_transient(_status_error(403))
    assert _is_transient(_status_error(429))
    assert _is_transient(_status_error(503))


def test_is_transient_excludes_permanent_errors():
    assert not _is_transient(_status_error(404))
    assert not _is_transient(ValueError("nope"))


def test_is_transient_covers_network_errors():
    assert _is_transient(httpx.ConnectError("no route"))


def test_retry_after_prefers_the_header():
    assert _retry_after(_status_error(429, {"retry-after": "7"}), 0) == 7.0


def test_retry_after_never_backs_off_below_the_arxiv_minimum():
    # 2**0 == 1s would be faster than arXiv permits.
    assert _retry_after(_status_error(503), 0) == MIN_REQUEST_INTERVAL


def test_retry_after_grows_with_attempts():
    assert _retry_after(_status_error(503), 3) == 8.0


def test_request_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr(arxiv_module.time, "sleep", lambda _: None)
    attempts = []

    def flaky(params):
        attempts.append(params)
        if len(attempts) < 3:
            raise _status_error(503)
        return "ok"

    assert _request(flaky, {}) == "ok"
    assert len(attempts) == 3


def test_request_reraises_permanent_error_without_retrying(monkeypatch):
    monkeypatch.setattr(arxiv_module.time, "sleep", lambda _: None)
    attempts = []

    def broken(params):
        attempts.append(params)
        raise _status_error(404)

    with pytest.raises(httpx.HTTPStatusError):
        _request(broken, {})
    assert len(attempts) == 1


def test_request_gives_up_after_the_retry_budget(monkeypatch):
    monkeypatch.setattr(arxiv_module.time, "sleep", lambda _: None)
    attempts = []

    def always_down(params):
        attempts.append(params)
        raise _status_error(503)

    with pytest.raises(httpx.HTTPStatusError):
        _request(always_down, {})
    assert len(attempts) == 5


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete (needs dlt)
# ---------------------------------------------------------------------------


def _run_pipeline(dlt, fake, tmp_path, run=0):
    """Run arxiv_source through a dlt pipeline into a temp sqlite destination."""
    db_path = (tmp_path / "arxiv.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="arxiv_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="arxiv_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(arxiv_source(categories=["cs.AI"], fetch=fake))
    return pipeline


def _read_papers(pipeline):
    """Return {id: row-dict} for the arxiv_papers table.

    Reads positionally (the SELECT fixes the column order) since dlt's
    sqlalchemy cursor exposes a SQLAlchemy Result without DB-API ``description``.
    """
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM arxiv_papers") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_papers_with_rendered_content(dlt_mod, tmp_path):
    fake = FakeArxiv([{"paper_id": "2101.1v1", "title": "Alpha", "summary": "alpha body"}])

    pipeline = _run_pipeline(dlt_mod, fake, tmp_path)

    papers = _read_papers(pipeline)
    assert set(papers) == {"2101.1"}
    assert papers["2101.1"]["title"] == "Alpha"
    assert "alpha body" in papers["2101.1"]["content"]


def test_revised_abstract_is_reflected_on_resync(dlt_mod, tmp_path):
    fake = FakeArxiv([{"paper_id": "2101.1v1", "title": "Alpha", "summary": "first version"}])
    _run_pipeline(dlt_mod, fake, tmp_path)

    # v2 with a rewritten abstract: same id, new content.
    fake.papers[0] = {"paper_id": "2101.1v2", "title": "Alpha", "summary": "revised version"}
    pipeline = _run_pipeline(dlt_mod, fake, tmp_path, run=1)

    papers = _read_papers(pipeline)
    assert set(papers) == {"2101.1"}
    assert "revised version" in papers["2101.1"]["content"]
    assert "first version" not in papers["2101.1"]["content"]


def test_paper_leaving_the_query_is_removed_on_resync(dlt_mod, tmp_path):
    # Withdrawn or reclassified: the paper stops matching, so it is absent from
    # the next full snapshot and orphan_cleanup forgets it.
    fake = FakeArxiv(
        [
            {"paper_id": "2101.1v1", "title": "Alpha"},
            {"paper_id": "2101.2v1", "title": "Beta"},
        ]
    )
    pipeline = _run_pipeline(dlt_mod, fake, tmp_path)
    assert set(_read_papers(pipeline)) == {"2101.1", "2101.2"}

    fake.papers = [{"paper_id": "2101.1v1", "title": "Alpha"}]
    pipeline = _run_pipeline(dlt_mod, fake, tmp_path, run=1)

    assert set(_read_papers(pipeline)) == {"2101.1"}


def test_fetch_error_aborts_sync_leaving_memory_untouched(dlt_mod, tmp_path):
    fake = FakeArxiv([{"paper_id": "2101.1v1", "title": "Alpha", "summary": "alpha body"}])
    pipeline = _run_pipeline(dlt_mod, fake, tmp_path)

    def broken(params):
        raise RuntimeError("arXiv is down")

    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        _run_pipeline(dlt_mod, broken, tmp_path, run=1)

    # Staging is authoritative under `replace`; a partial snapshot must never
    # be allowed to forget live papers.
    papers = _read_papers(pipeline)
    assert set(papers) == {"2101.1"}
    assert "alpha body" in papers["2101.1"]["content"]
