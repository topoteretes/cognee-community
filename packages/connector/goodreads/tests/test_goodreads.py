"""Unit tests for the Goodreads connector.

The export is faked by a real CSV file written by ``fake_goodreads.py`` (no
network, no account), so these run in CI. Coverage:

  - book rendering: status line per shelf, shelves as a comma list, year
    (original preferred), cleaned ISBN/ISBN13, average rating, publisher,
    binding, pages, owned copies, private notes
  - the re-read timeline: sorted by date, per-read rating (``★ x/5``) and
    review, the single-date-plus-count case, no rating when unrated, no
    review block when a read has none
  - review HTML stripped to plain text; rows deterministic; title truncated
    at 80 characters; url and ``book:<id>`` key; node sets (shelves + author)
  - tolerant parsing: UTF-8 BOM, unknown extra columns, snake_case numbered
    re-read columns, lowercase headers, footer junk and trailing empty lines
  - first run and counts; an edited review changes only that row; a removed
    book becomes a ``_deleted`` row; a failed run replays the same rows; the
    state is written only after the last row
  - the shelves filter: exclusive + custom matching, case-insensitive, and
    deletion semantics (a book that stops matching is forgotten, one that
    still matches another selected shelf stays)
  - factory validation and the dlt resource wiring (merge, id PK, hard
    delete, DOCUMENT_SOURCE_ATTR, stats counts, ``check_active``,
    service injection)
"""

import pytest
from fake_goodreads import (
    CLEAN_CODE,
    DDIA,
    DDIA_REVIEW,
    DUNE,
    EDITED_DDIA_REVIEW,
    K_AND_R,
    PRAGMATIC,
    SNAKE_CASE_COLUMNS,
    SRE,
    SYS_PERF,
    FakeGoodreadsService,
    default_rows,
    with_bookshelves,
    with_edited_review,
    with_long_title,
    without_book,
    write_export,
)

from cognee_community_connector_goodreads.goodreads import (
    NODE_SET_COLUMN,
    GoodreadsExportError,
    GoodreadsExportService,
    book_to_row,
    goodreads_source,
    parse_export,
    sync_export,
)


def run_sync(rows, tmp_path, columns=None, state=None, stats=None, shelves=None, bom=True):
    """Sync one export (as fake rows) through a real CSV in tmp_path."""
    path = write_export(tmp_path / "export.csv", rows=rows, columns=columns, bom=bom)
    parsed = parse_export(path)
    if state is None:
        state = {}
    return list(sync_export(parsed, state, shelves, stats)), state


def by_id(rows):
    return {row["id"]: row for row in rows}


def content_lines(row):
    return row["content"].splitlines()


# --------------------------------------------------------------------- rendering
def test_read_book_renders_the_full_document(tmp_path):
    rows, _ = run_sync(default_rows(), tmp_path)
    ddia = by_id(rows)[f"book:{DDIA}"]
    assert ddia["id"] == f"book:{DDIA}"  # the Goodreads book id is the stable key
    assert ddia["title"] == "Designing Data-Intensive Applications"
    assert ddia["url"] == f"https://www.goodreads.com/book/show/{DDIA}"
    assert ddia["_deleted"] is False
    assert content_lines(ddia) == [
        "Book: Designing Data-Intensive Applications",
        "Author: Martin Kleppmann",
        "Year: 2015",  # Original Publication Year preferred over Year Published (2017)
        "ISBN: 9781449373320",  # the ="..." escaping stripped, ISBN13 preferred
        "My shelves: read, favorites, systems",
        "Status: Read",
        "Read timeline:",
        "- Read on 2023/01/15, ★ 5/5",
        "  Review: A masterwork on systems design.",  # <br/> became a newline
        "  Full notes here",  # the <a href> tag stripped, its text kept
        "Average rating: 4.72/5",
        "Publisher: O'Reilly Media",
        "Binding: Paperback",
        "Pages: 616",
        "Owned copies: 1",
        "Private notes: Re-read chapters 5 and 6 annually.",
    ]


def test_reread_timeline_is_sorted_and_pairs_rating_and_review_per_read(tmp_path):
    rows, _ = run_sync(default_rows(), tmp_path)
    lines = content_lines(by_id(rows)[f"book:{PRAGMATIC}"])
    assert "Additional authors: Andrew Hunt" in lines
    assert "Read count:" not in lines  # multiple Date Read columns: the timeline says it
    first_read = lines.index("- Read on 2019/03/01, ★ 4/5")  # out of order in the file
    second_read = lines.index("- Read on 2021/05/10, ★ 3/5")
    assert first_read < second_read
    # the first read has no review: no empty review block after it
    assert lines[first_read + 1] == lines[second_read]
    assert lines[second_read + 1] == "  Review: Good, but dated in places."


def test_status_line_per_shelf_and_custom_shelves_rendered(tmp_path):
    rows, _ = run_sync(default_rows(), tmp_path)
    reading = by_id(rows)[f"book:{SRE}"]
    assert "Status: Currently reading" in content_lines(reading)
    assert "My shelves: currently-reading" in content_lines(reading)
    assert "Read timeline:" not in reading["content"]  # no date read yet
    assert "★" not in reading["content"]
    to_read = by_id(rows)[f"book:{SYS_PERF}"]
    assert "Status: To read" in content_lines(to_read)
    assert "Read timeline:" not in to_read["content"]
    dune = by_id(rows)[f"book:{DUNE}"]
    assert "My shelves: to-read, sci-fi, favorites" in content_lines(dune)


def test_unrated_book_renders_no_rating_and_no_review_block(tmp_path):
    rows, _ = run_sync(default_rows(), tmp_path)
    kernighan = by_id(rows)[f"book:{K_AND_R}"]
    assert content_lines(kernighan) == [
        "Book: The C Programming Language",
        "Author: Brian Kernighan",
        "Additional authors: Dennis Ritchie",
        "Year: 1978",
        "My shelves: read",
        "Status: Read",
        "Read timeline:",
        "- Read on 2020/09/09",  # My Rating 0: no "rated 0/5" anywhere
        "Average rating: 4.44/5",
        "Publisher: Prentice Hall",
        "Pages: 272",
    ]


def test_read_count_with_a_single_date_renders_the_count(tmp_path):
    rows, _ = run_sync(default_rows(), tmp_path)
    clean_code = by_id(rows)[f"book:{CLEAN_CODE}"]
    lines = content_lines(clean_code)
    assert "- Read on 2022/08/20, ★ 4/5 (read 3 times)" in lines
    assert "ISBN: 9780132350884" in lines  # escaped ISBN13 beats the plain ISBN
    assert "Owned copies: 0" in lines  # non-empty, so rendered


def test_rows_are_deterministic(tmp_path):
    first, _ = run_sync(default_rows(), tmp_path)
    second, _ = run_sync(default_rows(), tmp_path)
    assert first == second  # same export, byte-identical rows, same content hash


def test_titles_are_truncated_at_80_characters(tmp_path):
    rows, _ = run_sync(with_long_title(default_rows()), tmp_path)
    long_book = by_id(rows)["book:1008"]
    assert len(long_book["title"]) == 80 and long_book["title"].endswith("…")
    assert long_book["content"].startswith("Book: " + "A" * 120)  # content keeps it all


def test_node_sets_carry_shelf_and_author_slugs(tmp_path):
    rows, _ = run_sync(default_rows(), tmp_path)
    books = by_id(rows)
    assert NODE_SET_COLUMN == "cognee_node_set"
    assert books[f"book:{DDIA}"][NODE_SET_COLUMN] == [
        "author:martin-kleppmann",  # Author l-f "Kleppmann, Martin" -> first-last slug
        "shelf:favorites",
        "shelf:read",
        "shelf:systems",
    ]
    assert books[f"book:{DUNE}"][NODE_SET_COLUMN] == [
        "author:frank-herbert",
        "shelf:favorites",
        "shelf:sci-fi",
        "shelf:to-read",
    ]
    assert books[f"book:{CLEAN_CODE}"][NODE_SET_COLUMN] == [
        "author:robert-c-martin",
        "shelf:read",
        "shelf:work",
    ]


# --------------------------------------------------------------------- tolerance
def test_snake_case_numbered_reread_columns_parse_identically(tmp_path):
    from fake_goodreads import COLUMNS

    titled, _ = run_sync(default_rows(), tmp_path, columns=COLUMNS)
    snaked, _ = run_sync(default_rows(), tmp_path, columns=SNAKE_CASE_COLUMNS)
    assert snaked == titled  # "date_read 2" and "My Rating" are the same column


def test_the_bom_is_stripped_and_a_file_without_one_works_too(tmp_path):
    with_bom, _ = run_sync(default_rows(), tmp_path, bom=True)
    without_bom, _ = run_sync(default_rows(), tmp_path, bom=False)
    assert without_bom == with_bom


def test_footer_junk_and_trailing_empty_lines_are_skipped(tmp_path):
    stats: dict = {}
    rows, state = run_sync(default_rows(), tmp_path, stats=stats)
    assert len(rows) == 7
    assert stats["skipped"] == 1  # the footer line; the empty line is not counted
    assert all(row["id"].startswith("book:") for row in rows)
    assert len(state["known_books"]) == 7


# --------------------------------------------------------------------- sync
def test_first_run_yields_every_book_and_counts(tmp_path):
    stats: dict = {}
    _, state = run_sync(default_rows(), tmp_path, stats=stats)
    assert stats == {"books": 7, "deletions": 0, "skipped": 1, "failed": 0}
    expected = (DDIA, PRAGMATIC, SRE, SYS_PERF, K_AND_R, CLEAN_CODE, DUNE)
    assert state["known_books"] == sorted(f"book:{id_}" for id_ in expected)


def test_an_edited_review_changes_only_that_row(tmp_path):
    first, state = run_sync(default_rows(), tmp_path)
    second, _ = run_sync(with_edited_review(default_rows()), tmp_path, state=state)
    assert [row["id"] for row in first] == [row["id"] for row in second]
    changed = [a["id"] for a, b in zip(first, second, strict=True) if a != b]
    assert changed == [f"book:{DDIA}"]
    new_content = by_id(second)[f"book:{DDIA}"]["content"]
    assert EDITED_DDIA_REVIEW.split("<br/>")[0] in new_content
    assert DDIA_REVIEW not in new_content


def test_a_book_removed_from_a_newer_export_is_forgotten(tmp_path):
    _, state = run_sync(default_rows(), tmp_path)
    stats: dict = {}
    rows, state = run_sync(
        without_book(default_rows(), K_AND_R), tmp_path, state=state, stats=stats
    )
    assert [row for row in rows if row["_deleted"]] == [{"id": f"book:{K_AND_R}", "_deleted": True}]
    assert f"book:{K_AND_R}" not in state["known_books"]
    assert (stats["books"], stats["deletions"]) == (6, 1)


def test_a_failed_run_replays_the_same_rows(tmp_path):
    rows, persisted = run_sync(default_rows(), tmp_path)
    replayed, _ = run_sync(default_rows(), tmp_path, state=dict(persisted))
    assert replayed == rows  # the load failed: dlt keeps the old state


def test_state_is_written_only_after_the_last_row(tmp_path):
    path = write_export(tmp_path / "export.csv")
    state: dict = {}
    rows = sync_export(parse_export(path), state)
    next(rows)
    rows.close()  # what happens when check_active raises between rows
    assert state == {}, "the known ids only move once every row is out"


# --------------------------------------------------------------------- shelves filter
def test_the_filter_matches_exclusive_and_custom_shelves_case_insensitively(tmp_path):
    custom, _ = run_sync(default_rows(), tmp_path, shelves=["FAVORITES"])
    assert sorted(row["id"] for row in custom) == [f"book:{DDIA}", f"book:{DUNE}"]
    exclusive, _ = run_sync(default_rows(), tmp_path, shelves=["read"])
    assert sorted(row["id"] for row in exclusive) == [
        f"book:{DDIA}",
        f"book:{PRAGMATIC}",
        f"book:{K_AND_R}",
        f"book:{CLEAN_CODE}",
    ]


def test_a_book_that_stops_matching_the_filter_is_forgotten_but_one_that_still_matches_stays(
    tmp_path,
):
    _, state = run_sync(default_rows(), tmp_path, shelves=["favorites", "systems"])
    assert set(state["known_books"]) == {f"book:{DDIA}", f"book:{DUNE}"}

    newer = with_bookshelves(with_bookshelves(default_rows(), DDIA, "systems"), DUNE, "classics")
    stats: dict = {}
    rows, state = run_sync(
        newer, tmp_path, state=state, shelves=["favorites", "systems"], stats=stats
    )
    kept = by_id(rows)[f"book:{DDIA}"]
    assert kept["_deleted"] is False  # it left "favorites" but still matches "systems"
    assert "My shelves: read, systems" in content_lines(kept)
    assert by_id(rows)[f"book:{DUNE}"] == {"id": f"book:{DUNE}", "_deleted": True}
    assert stats["deletions"] == 1


def test_switching_the_filter_forgets_every_previously_ingested_non_match(tmp_path):
    _, state = run_sync(default_rows(), tmp_path)  # no filter: everything ingested
    rows, state = run_sync(default_rows(), tmp_path, state=state, shelves=["sci-fi"])
    assert [row["id"] for row in rows if not row["_deleted"]] == [f"book:{DUNE}"]
    assert len([row for row in rows if row["_deleted"]]) == 6  # the other books left the filter
    assert state["known_books"] == [f"book:{DUNE}"]


# --------------------------------------------------------------------- factory errors
def test_a_missing_export_file_raises_a_clear_error(tmp_path):
    with pytest.raises(GoodreadsExportError, match=r"goodreads\.com"):
        goodreads_source(export=tmp_path / "nope.csv")


def test_a_missing_export_argument_raises_a_clear_error():
    with pytest.raises(GoodreadsExportError, match="export="):
        goodreads_source()


def test_a_csv_without_book_id_and_title_is_refused(tmp_path):
    path = tmp_path / "not_goodreads.csv"
    path.write_text("Name,Rating\nMoby Dick,5\n", encoding="utf-8")
    with pytest.raises(GoodreadsExportError, match="Book Id"):
        GoodreadsExportService(path).read_export()
    with pytest.raises(GoodreadsExportError, match="Book Id"):
        goodreads_source(export=path)


@pytest.mark.parametrize("name", ["My-Library", "my library", "1library"])
def test_a_resource_name_dlt_would_rename_is_refused(name, tmp_path):
    path = write_export(tmp_path / "export.csv")
    with pytest.raises(ValueError, match="resource_name"):
        goodreads_source(export=path, resource_name=name)


def test_shelves_must_be_a_list_of_names(tmp_path):
    path = write_export(tmp_path / "export.csv")
    with pytest.raises(ValueError, match="shelves"):
        goodreads_source(export=path, shelves="read")


# --------------------------------------------------------------------- dlt wiring
def test_resource_is_configured_for_merge_and_hard_delete(tmp_path):
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    path = write_export(tmp_path / "export.csv")
    source = goodreads_source(export=path)
    resource = source.resources["goodreads_default"]
    assert resource.write_disposition == "merge"
    assert resource.compute_table_schema()["columns"]["_deleted"]["hard_delete"] is True
    assert resource.compute_table_schema()["columns"]["_deleted"]["data_type"] == "bool"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "goodreads"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "goodreads:default"

    named = goodreads_source(export=path, resource_name="my_library")
    assert set(named.resources) == {"my_library"}
    assert getattr(named, PIPELINE_SCOPE_ATTR) == "goodreads:my_library"


def test_check_active_runs_around_every_row(tmp_path):
    path = write_export(tmp_path / "export.csv")
    checks = []
    source = goodreads_source(export=path, check_active=lambda: checks.append(1))
    rows = list(source.resources["goodreads_default"])
    assert len(rows) == 7
    # once before reading the export, then guarded_rows: before and after each
    # row, and twice at the end.
    assert len(checks) == 1 + 2 * 7 + 2


def test_source_stats_are_counts_only(tmp_path):
    path = write_export(tmp_path / "export.csv")
    source = goodreads_source(export=path)
    list(source.resources["goodreads_default"])
    assert source.cognee_sync_stats == {
        "books": 7,
        "deletions": 0,
        "skipped": 1,
        "failed": 0,
    }
    assert all(type(value) is int for value in source.cognee_sync_stats.values())


def test_service_injection_avoids_the_filesystem(tmp_path):
    path = write_export(tmp_path / "export.csv")
    service = FakeGoodreadsService(parse_export(path))
    source = goodreads_source(service=service)
    rows = list(source.resources["goodreads_default"])
    assert len(rows) == 7
    assert service.calls == 1


def test_row_becomes_a_goodreads_document_in_cognee(tmp_path):
    # The row -> document mapping is cognee's (the same check the Notion tests do).
    pytest.importorskip("cognee")
    from types import SimpleNamespace
    from uuid import NAMESPACE_OID, uuid5

    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    path = write_export(tmp_path / "export.csv")
    parsed = parse_export(path)
    row = book_to_row(next(r for r in parsed if r["book_id"] == DDIA))
    data_id = uuid5(NAMESPACE_OID, row["id"])
    item = _build_document_data_item(
        SimpleNamespace(row_data=row, table_name="goodreads_default"), data_id, "goodreads"
    )
    # source="goodreads" (not "dlt") routes the row through normal cognify.
    assert item.system_metadata["source"] == "goodreads"
    assert item.system_metadata["external_id"] == f"book:{DDIA}"
    assert item.system_metadata["url"] == f"https://www.goodreads.com/book/show/{DDIA}"
    assert item.data.startswith("# Designing Data-Intensive Applications")
    assert "Martin Kleppmann" in item.data
