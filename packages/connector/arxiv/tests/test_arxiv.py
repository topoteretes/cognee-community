"""Unit tests for the arXiv connector.

The arXiv Atom API is fully mocked via ``FakeArxivClient`` — no network traffic
and no credentials are required, so these run in CI. The fake reimplements the
parts of the API the connector leans on (server-side ``submittedDate`` filtering,
ascending submission order, ``start``/``max_results`` paging, the ``totalResults``
count), so a bug in how the connector drives the API shows up here rather than
only against the live service.

Coverage:

  - arXiv ids are split into a stable id + version, so revisions upsert
  - search queries OR within a facet and AND across facets
  - an unscoped source is rejected rather than syncing all of arXiv
  - Atom entries flatten to rows with title/authors folded into the text
  - full backfill yields every paper and records the cursor + id set
  - incremental re-sync yields ONLY papers submitted since the cursor
  - a paper new to the corpus but older than the cursor is still ingested
  - papers that vanish from the sweep become hard-delete markers
  - an empty sweep does not mass-delete a known corpus
  - revisions are re-emitted only with track_revisions=True
  - paging walks past the first page and stops at max_papers
  - the rate limiter keeps requests min_interval apart
  - the dlt resource is wired with merge + id PK + the hard_delete column
  - a real dlt merge removes the marked row (end-to-end forget-on-delete)

The end-to-end "deletion removes from memory" guarantee is provided by cognee's
existing ``orphan_cleanup`` path; here we prove the connector emits the markers
that drive it, and that dlt acts on them.
"""

import re

import pytest

from cognee_community_connector_arxiv.arxiv import (
    _ArxivClient,
    _clean,
    _entry_to_row,
    _to_cursor,
    arxiv_source,
    build_search_query,
    iter_entries,
    parse_feed,
    split_arxiv_id,
)

# ---------------------------------------------------------------------------
# Fake arXiv Atom API
# ---------------------------------------------------------------------------
_FEED_TEMPLATE = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>{total}</opensearch:totalResults>
  <opensearch:startIndex>{start}</opensearch:startIndex>
{entries}
</feed>"""

_ENTRY_TEMPLATE = """  <entry>
    <id>http://arxiv.org/abs/{paper_id}v{version}</id>
    <title>{title}</title>
    <summary>{abstract}</summary>
    <published>{published}</published>
    <updated>{updated}</updated>
{authors}
{categories}
    <arxiv:primary_category term="{primary}"/>
  </entry>"""

_RANGE_RE = re.compile(r"submittedDate:\[(\d{12}) TO (\d{12})\]")


def _paper(
    paper_id,
    *,
    published,
    updated=None,
    version=1,
    title="A Paper",
    abstract="An abstract.",
    authors=("Ada Lovelace",),
    categories=("cs.AI",),
):
    """Build a paper as the fake API stores it."""
    return {
        "paper_id": paper_id,
        "published": published,
        "updated": updated or published,
        "version": version,
        "title": title,
        "abstract": abstract,
        "authors": list(authors),
        "categories": list(categories),
    }


def _render_entry(paper):
    authors = "\n".join(
        f"    <author>\n      <name>{name}</name>\n    </author>" for name in paper["authors"]
    )
    categories = "\n".join(
        f'    <category term="{term}" scheme="http://arxiv.org/schemas/atom"/>'
        for term in paper["categories"]
    )
    return _ENTRY_TEMPLATE.format(
        paper_id=paper["paper_id"],
        version=paper["version"],
        title=paper["title"],
        abstract=paper["abstract"],
        published=paper["published"],
        updated=paper["updated"],
        authors=authors,
        categories=categories,
        primary=paper["categories"][0],
    )


class FakeArxivClient:
    """In-memory stand-in for :class:`_ArxivClient` backed by a paper list."""

    def __init__(self, papers):
        self.papers = list(papers)
        self.requests = []

    def get(self, params):
        self.requests.append(params)
        query = params["search_query"]

        # arXiv filters the submission window server-side; so does the fake, or
        # the incremental pass would look correct while sending a broken query.
        matched = self.papers
        window = _RANGE_RE.search(query)
        if window:
            low, high = window.group(1), window.group(2)
            matched = [p for p in matched if low <= _to_cursor(p["published"]) <= high]

        matched = sorted(matched, key=lambda p: p["published"])
        total = len(matched)

        start = int(params["start"])
        page = matched[start : start + int(params["max_results"])]
        entries = "\n".join(_render_entry(p) for p in page)
        return _FEED_TEMPLATE.format(total=total, start=start, entries=entries)


def _rows(client, state, **kwargs):
    """Run one sync and return the emitted rows."""
    from cognee_community_connector_arxiv.arxiv import sync_papers

    kwargs.setdefault("categories", ["cs.AI"])
    return list(sync_papers(client, state, **kwargs))


# ---------------------------------------------------------------------------
# Id / query / parsing helpers
# ---------------------------------------------------------------------------
def test_split_arxiv_id_strips_the_version_suffix():
    assert split_arxiv_id("http://arxiv.org/abs/2608.09617v2") == ("2608.09617", 2)
    assert split_arxiv_id("http://arxiv.org/abs/2608.27454v1") == ("2608.27454", 1)


def test_split_arxiv_id_handles_old_style_and_unversioned_ids():
    # Pre-2007 ids carry a category prefix and a slash.
    assert split_arxiv_id("http://arxiv.org/abs/math.GT/0309136v1") == ("math.GT/0309136", 1)
    assert split_arxiv_id("http://arxiv.org/abs/2608.09617") == ("2608.09617", None)


def test_build_search_query_ors_within_a_facet_and_ands_across_facets():
    query = build_search_query(["cs.AI", "cs.LG"], ["Ada Lovelace"], None, None)
    assert query == '(cat:cs.AI OR cat:cs.LG) AND au:"Ada Lovelace"'


def test_build_search_query_appends_the_submitted_date_window():
    query = build_search_query(["cs.AI"], None, None, "202608250000")
    assert query == "cat:cs.AI AND submittedDate:[202608250000 TO 999912312359]"


def test_build_search_query_rejects_an_unscoped_query():
    with pytest.raises(ValueError, match="needs a scope"):
        build_search_query(None, None, None, None)


def test_to_cursor_truncates_an_atom_timestamp_to_minutes():
    assert _to_cursor("2026-08-25T00:20:29Z") == "202608250020"


def test_clean_collapses_the_newlines_arxiv_wraps_text_at():
    assert _clean("Deep\n  Learning\nfor   Graphs") == "Deep Learning for Graphs"


def test_entry_to_row_folds_title_and_authors_into_the_cognified_text():
    xml = _FEED_TEMPLATE.format(
        total=1,
        start=0,
        entries=_render_entry(
            _paper(
                "2608.00001",
                published="2026-08-01T00:00:00Z",
                version=3,
                title="Graph\nMemory",
                abstract="We study\nmemory.",
                authors=("Ada Lovelace", "Alan Turing"),
                categories=("cs.AI", "cs.LG"),
            )
        ),
    )
    entries, total = parse_feed(xml)
    assert total == 1
    row = _entry_to_row(entries[0])

    assert row["id"] == "2608.00001"
    assert row["version"] == 3
    assert row["title"] == "Graph Memory"
    assert row["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert row["categories"] == ["cs.AI", "cs.LG"]
    assert row["primary_category"] == "cs.AI"
    assert row["url"] == "https://arxiv.org/abs/2608.00001"
    # The cognify pipeline reads `content`, so the byline must survive into it.
    assert "Graph Memory" in row["content"]
    assert "Ada Lovelace, Alan Turing" in row["content"]
    assert "We study memory." in row["content"]


# ---------------------------------------------------------------------------
# Sync behaviour
# ---------------------------------------------------------------------------
def test_backfill_yields_all_papers_and_records_cursor_and_ids():
    client = FakeArxivClient(
        [
            _paper("2608.00001", published="2026-08-01T00:00:00Z"),
            _paper("2608.00002", published="2026-08-02T00:00:00Z"),
        ]
    )
    state = {}
    rows = _rows(client, state)

    assert [r["id"] for r in rows] == ["2608.00001", "2608.00002"]
    assert state["last_submitted"] == "2026-08-02T00:00:00Z"
    assert state["known_ids"] == ["2608.00001", "2608.00002"]


def test_incremental_yields_only_papers_submitted_since_the_cursor():
    papers = [
        _paper("2608.00001", published="2026-08-01T00:00:00Z"),
        _paper("2608.00002", published="2026-08-02T00:00:00Z"),
    ]
    client = FakeArxivClient(papers)
    state = {}
    _rows(client, state)

    papers.append(_paper("2608.00003", published="2026-08-03T00:00:00Z"))
    rows = _rows(FakeArxivClient(papers), state)

    assert [r["id"] for r in rows] == ["2608.00003"]
    assert state["last_submitted"] == "2026-08-03T00:00:00Z"


def test_incremental_with_no_changes_is_a_noop():
    papers = [_paper("2608.00001", published="2026-08-01T00:00:00Z")]
    state = {}
    _rows(FakeArxivClient(papers), state)

    rows = _rows(FakeArxivClient(papers), state)
    assert rows == []


def test_new_paper_below_the_cursor_is_still_ingested():
    """A paper can enter the scope with an old submission date (cross-list)."""
    papers = [_paper("2608.00002", published="2026-08-02T00:00:00Z")]
    state = {}
    _rows(FakeArxivClient(papers), state)

    # Cross-listed into cs.AI later, but submitted before the cursor.
    papers.insert(0, _paper("2608.00001", published="2026-08-01T00:00:00Z"))
    rows = _rows(FakeArxivClient(papers), state)

    assert [r["id"] for r in rows] == ["2608.00001"]


def test_deleted_paper_emits_a_hard_delete_marker():
    papers = [
        _paper("2608.00001", published="2026-08-01T00:00:00Z"),
        _paper("2608.00002", published="2026-08-02T00:00:00Z"),
    ]
    state = {}
    _rows(FakeArxivClient(papers), state)

    rows = _rows(FakeArxivClient(papers[:1]), state)
    assert rows == [{"id": "2608.00002", "_deleted": True}]
    assert state["known_ids"] == ["2608.00001"]


def test_empty_sweep_does_not_mass_delete_and_preserves_state():
    papers = [
        _paper("2608.00001", published="2026-08-01T00:00:00Z"),
        _paper("2608.00002", published="2026-08-02T00:00:00Z"),
    ]
    state = {}
    _rows(FakeArxivClient(papers), state)

    # arXiv 503s / returns nothing: this must not be read as "everything deleted".
    rows = _rows(FakeArxivClient([]), state)
    assert rows == []
    assert state["known_ids"] == ["2608.00001", "2608.00002"]


def test_revision_is_ignored_by_default():
    papers = [_paper("2608.00001", published="2026-08-01T00:00:00Z")]
    state = {}
    _rows(FakeArxivClient(papers), state)

    revised = [
        _paper(
            "2608.00001",
            published="2026-08-01T00:00:00Z",
            updated="2026-08-09T00:00:00Z",
            version=2,
        )
    ]
    # submittedDate is unchanged, so the incremental pass cannot see the revision.
    assert _rows(FakeArxivClient(revised), state) == []


def test_revision_is_reemitted_when_track_revisions_is_set():
    papers = [_paper("2608.00001", published="2026-08-01T00:00:00Z")]
    state = {}
    _rows(FakeArxivClient(papers), state, track_revisions=True)

    revised = [
        _paper(
            "2608.00001",
            published="2026-08-01T00:00:00Z",
            updated="2026-08-09T00:00:00Z",
            version=2,
        )
    ]
    rows = _rows(FakeArxivClient(revised), state, track_revisions=True)

    assert [r["id"] for r in rows] == ["2608.00001"]
    assert rows[0]["version"] == 2


def test_capped_sweep_does_not_falsely_delete_papers_past_its_window():
    """max_papers truncates the sweep; ids beyond it were never examined.

    Regression: with the cap smaller than the corpus, `known_ids` outgrows the
    swept window every run, so an unbounded `known_ids - current_ids` reported
    live papers as deleted and purged them from memory.
    """
    papers = [
        _paper(f"26{i:02d}.0000{i}", published=f"2026-{i:02d}-01T00:00:00Z") for i in range(1, 8)
    ]
    state = {}
    seen_deletions = []
    for _ in range(3):
        rows = _rows(FakeArxivClient(papers), state, page_size=3, max_papers=3)
        seen_deletions += [r["id"] for r in rows if r.get("_deleted")]

    assert seen_deletions == []  # nothing was removed upstream


def test_capped_sweep_still_deletes_inside_its_window():
    """The cap must not disable forget-on-delete for papers it does cover."""
    papers = [
        _paper(f"26{i:02d}.0000{i}", published=f"2026-{i:02d}-01T00:00:00Z") for i in range(1, 8)
    ]
    state = {}
    _rows(FakeArxivClient(papers), state, page_size=3, max_papers=3)

    # Drop the second paper, which sits inside the swept (oldest-3) window.
    remaining = [p for p in papers if p["paper_id"] != "2602.00002"]
    rows = _rows(FakeArxivClient(remaining), state, page_size=3, max_papers=3)

    assert [r["id"] for r in rows if r.get("_deleted")] == ["2602.00002"]


def test_detect_deletions_off_skips_the_sweep_and_keeps_rows():
    papers = [
        _paper("2608.00001", published="2026-08-01T00:00:00Z"),
        _paper("2608.00002", published="2026-08-02T00:00:00Z"),
    ]
    state = {}
    _rows(FakeArxivClient(papers), state, detect_deletions=False)

    client = FakeArxivClient(papers[:1])
    rows = _rows(client, state, detect_deletions=False)

    assert rows == []  # no delete marker for the vanished paper
    # Only the incremental pass ran, so the sweep's extra request never happened.
    assert len(client.requests) == 1


# ---------------------------------------------------------------------------
# Paging + rate limiting
# ---------------------------------------------------------------------------
def test_paging_walks_past_the_first_page():
    papers = [_paper(f"2608.{i:05d}", published=f"2026-08-01T00:{i:02d}:00Z") for i in range(1, 26)]
    client = FakeArxivClient(papers)
    entries = list(iter_entries(client, "cat:cs.AI", page_size=10))

    assert len(entries) == 25
    assert [int(r["start"]) for r in client.requests] == [0, 10, 20]


def test_paging_stops_at_max_papers():
    papers = [_paper(f"2608.{i:05d}", published=f"2026-08-01T00:{i:02d}:00Z") for i in range(1, 26)]
    client = FakeArxivClient(papers)
    entries = list(iter_entries(client, "cat:cs.AI", page_size=10, max_papers=15))

    assert len(entries) == 15


def test_rate_limiter_keeps_requests_min_interval_apart(monkeypatch):
    """The 3s courtesy delay must be applied before a request, not after a 429."""
    now = {"t": 0.0}
    slept = []
    monkeypatch.setattr("time.monotonic", lambda: now["t"])
    monkeypatch.setattr("time.sleep", lambda s: (slept.append(s), now.update(t=now["t"] + s)))

    client = _ArxivClient(min_interval=3.0)
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)

        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"<feed/>"

        return _Ctx()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client.get({"search_query": "cat:cs.AI", "start": 0, "max_results": 1})
    client.get({"search_query": "cat:cs.AI", "start": 1, "max_results": 1})

    assert len(calls) == 2
    # First request goes straight out; the second waits out the interval.
    assert slept == [3.0]


# ---------------------------------------------------------------------------
# Source wiring
# ---------------------------------------------------------------------------
def test_arxiv_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")
    resource = arxiv_source(categories=["cs.AI"], client=FakeArxivClient([]))
    schema = resource.compute_table_schema()

    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_arxiv_source_requires_a_scope():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="needs a scope"):
        arxiv_source()


def test_arxiv_source_rejects_track_revisions_without_deletion_detection():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="requires detect_deletions"):
        arxiv_source(categories=["cs.AI"], detect_deletions=False, track_revisions=True)


def test_arxiv_source_validates_page_size():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="page_size"):
        arxiv_source(categories=["cs.AI"], page_size=0)


# ---------------------------------------------------------------------------
# End-to-end through a real dlt merge
# ---------------------------------------------------------------------------
def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("duckdb")

    pipeline = dlt.pipeline(
        pipeline_name="test_arxiv_e2e",
        destination=dlt.destinations.duckdb(str(tmp_path / "arxiv.duckdb")),
        dataset_name="papers",
    )

    papers = [
        _paper("2608.00001", published="2026-08-01T00:00:00Z"),
        _paper("2608.00002", published="2026-08-02T00:00:00Z"),
    ]

    # Sync #1: two live papers land in the destination.
    pipeline.run(arxiv_source(categories=["cs.AI"], client=FakeArxivClient(papers)))
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM arxiv_papers")[0][0] == 2

    # Sync #2: paper 2608.00002 withdrawn upstream. The connector emits a
    # hard-delete marker; dlt's merge removes it from the destination.
    pipeline.run(arxiv_source(categories=["cs.AI"], client=FakeArxivClient(papers[:1])))
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM arxiv_papers")
    assert [r[0] for r in rows] == ["2608.00001"]
