"""Unit tests for the Letterboxd connector.

The export is faked by real ZIP files written by ``fake_letterboxd.py`` (no
network, no account), so these run in CI. Coverage:

  - film rendering merges watched + ratings + reviews + diary + lists
  - the 10-scale rating renders as stars (7.5 -> 3.5 stars, half-steps allowed)
  - rows are deterministic: the same export renders identical rows
  - the rewatch timeline appears in content, in date order, with tags
  - list rows and their ordered rosters
  - node sets: list slugs plus the decade on films, empty on lists
  - first run yields every row; an edited review changes only that row
  - a film removed from a newer export becomes a ``_deleted`` row
  - a removed list deletes its row and its films lose the node-set entry
  - the state is written only after the last row
  - tolerant parsing: unknown extra columns, missing optional columns,
    an export of only watched.csv, a missing watched.csv, extracted folders
  - factory validation and the dlt resource wiring (merge, id PK, hard delete,
    DOCUMENT_SOURCE_ATTR, stats counts, ``check_active``, service injection)
"""

import pytest
from fake_letterboxd import (
    EDITED_GET_OUT_REVIEW,
    GET_OUT,
    GET_OUT_REVIEW,
    HEAT,
    MAD_MAX,
    MOONLIGHT,
    PARASITE,
    WHIPLASH,
    FakeLetterboxdService,
    default_export,
    to_export_data,
    with_edited_review,
    with_extra_columns,
    with_watchlist,
    without_film,
    without_list,
    without_tags_column,
    without_watched,
    write_dir,
    write_zip,
)

from cognee_community_connector_letterboxd.letterboxd import (
    NODE_SET_COLUMN,
    Film,
    LetterboxdExportError,
    LetterboxdExportService,
    film_to_row,
    letterboxd_source,
    rating_to_stars,
    sync_export,
)


def run_sync(files, tmp_path, state=None, stats=None):
    """Sync one export (as fake files) through a real ZIP in tmp_path."""
    path = write_zip(tmp_path / "export.zip", files)
    data = LetterboxdExportService(path).read_export()
    if state is None:
        state = {}
    rows = list(sync_export(data, state, stats))
    return rows, state


def by_id(rows):
    return {row["id"]: row for row in rows}


# --------------------------------------------------------------------- rendering
def test_film_row_merges_watched_ratings_reviews_diary_and_lists(tmp_path):
    rows, _ = run_sync(default_export(), tmp_path)
    parasite = by_id(rows)[PARASITE]
    assert parasite["id"] == PARASITE  # the Letterboxd URI is the stable key
    assert parasite["title"] == "Parasite (2019)"
    assert parasite["url"] == PARASITE
    assert parasite["_deleted"] is False
    lines = parasite["content"].splitlines()
    for line in ("Film: Parasite", "Year: 2019", f"URI: {PARASITE}"):
        assert line in lines
    assert "My rating: ★★★½ (7.5/10)" in lines
    assert "My lists: Favourites, Korean Cinema" in lines
    assert "Diary:" in lines and "- 2024-01-05" in lines
    assert "My review" not in parasite["content"]  # rated but never reviewed


@pytest.mark.parametrize(
    "rating, stars",
    [
        (10, "★★★★★"),
        (9, "★★★★½"),
        (8, "★★★★"),
        (7.5, "★★★½"),
        (7, "★★★½"),
        (5, "★★½"),
        (1, "½"),
    ],
)
def test_a_10_scale_rating_renders_as_half_step_stars(rating, stars):
    assert rating_to_stars(rating) == stars


def test_rows_are_deterministic(tmp_path):
    first, _ = run_sync(default_export(), tmp_path)
    second, _ = run_sync(default_export(), tmp_path)
    assert first == second  # same export, byte-identical rows, same content hash


def test_rewatch_timeline_appears_in_content_in_date_order(tmp_path):
    rows, _ = run_sync(default_export(), tmp_path)
    content = by_id(rows)[MAD_MAX]["content"]
    first_watch = content.index("2024-05-01")
    rewatch = content.index("2024-06-10 (rewatch)")
    assert first_watch < rewatch
    assert "tags: action, rewatch" in content
    # Mad Max is not in ratings.csv: the newest diary rating and comment stand in.
    assert "My rating: ★★★★ (8/10)" in content
    assert "Even better the second time." in content


def test_full_review_text_is_included(tmp_path):
    rows, _ = run_sync(default_export(), tmp_path)
    content = by_id(rows)[GET_OUT]["content"]
    assert "My review:" in content
    assert GET_OUT_REVIEW in content


def test_film_only_in_watched_csv_gets_a_minimal_row(tmp_path):
    rows, _ = run_sync(default_export(), tmp_path)
    heat = by_id(rows)[HEAT]
    assert heat["title"] == "Heat (1995)"
    assert heat["content"] == f"Film: Heat\nYear: 1995\nURI: {HEAT}\nDiary:\n- 2024-03-01"


def test_list_rows_and_their_rosters(tmp_path):
    rows, _ = run_sync(default_export(), tmp_path)
    favourites = by_id(rows)["list:favourites"]
    assert favourites["title"] == "Favourites"
    assert favourites["_deleted"] is False
    assert favourites["url"] is None
    assert favourites["content"] == (
        "List: Favourites\nFilms:\n"
        "1. Parasite (2019)\n2. Mad Max: Fury Road (2015)\n3. Whiplash (2014)"
    )
    korean = by_id(rows)["list:korean-cinema"]
    assert korean["title"] == "Korean Cinema"
    assert "1. Parasite (2019)" in korean["content"]
    assert "Details" not in korean["content"]  # unknown column ignored


def test_film_node_sets_carry_list_slugs_and_the_decade(tmp_path):
    rows, _ = run_sync(default_export(), tmp_path)
    films = by_id(rows)
    assert NODE_SET_COLUMN == "cognee_node_set"
    assert films[PARASITE][NODE_SET_COLUMN] == [
        "decade:2010s",
        "list:favourites",
        "list:korean-cinema",
    ]
    assert films[HEAT][NODE_SET_COLUMN] == ["decade:1990s"]
    assert films[GET_OUT][NODE_SET_COLUMN] == ["decade:2010s"]
    assert films["list:favourites"][NODE_SET_COLUMN] == []


def test_watchlist_films_are_marked(tmp_path):
    rows, _ = run_sync(with_watchlist(default_export()), tmp_path)
    moonlight = by_id(rows)[MOONLIGHT]
    assert moonlight["title"] == "Moonlight (2016)"
    assert "On my watchlist" in moonlight["content"]
    assert moonlight[NODE_SET_COLUMN] == ["decade:2010s"]


# --------------------------------------------------------------------- sync
def test_first_run_yields_every_row_and_counts(tmp_path):
    stats: dict = {}
    rows, state = run_sync(default_export(), tmp_path, stats=stats)
    assert len(rows) == 7  # 5 films + 2 lists
    assert stats == {"films": 5, "lists": 2, "deletions": 0, "skipped": 0, "failed": 0}
    assert state["known_films"] == sorted([PARASITE, GET_OUT, MAD_MAX, HEAT, WHIPLASH])
    assert state["known_lists"] == ["list:favourites", "list:korean-cinema"]


def test_an_edited_review_changes_only_that_row(tmp_path):
    first, state = run_sync(default_export(), tmp_path)
    second, _ = run_sync(with_edited_review(default_export()), tmp_path, state=state)
    assert [row["id"] for row in first] == [row["id"] for row in second]
    changed = [a["id"] for a, b in zip(first, second, strict=True) if a != b]
    assert changed == [GET_OUT]
    assert EDITED_GET_OUT_REVIEW in by_id(second)[GET_OUT]["content"]


def test_a_film_removed_from_a_newer_export_is_forgotten(tmp_path):
    _, state = run_sync(default_export(), tmp_path)
    stats: dict = {}
    rows, state = run_sync(without_film(default_export(), HEAT), tmp_path, state=state, stats=stats)
    assert [row for row in rows if row["_deleted"]] == [{"id": HEAT, "_deleted": True}]
    assert HEAT not in state["known_films"]
    assert (stats["films"], stats["deletions"]) == (4, 1)


def test_a_removed_list_deletes_its_row_and_updates_its_films(tmp_path):
    _, state = run_sync(default_export(), tmp_path)
    newer = without_list(default_export(), "lists/Korean Cinema.csv")
    rows, state = run_sync(newer, tmp_path, state=state)
    assert [row["id"] for row in rows if row["_deleted"]] == ["list:korean-cinema"]
    assert "list:korean-cinema" not in state["known_lists"]
    parasite = by_id(rows)[PARASITE]
    assert "list:korean-cinema" not in parasite[NODE_SET_COLUMN]
    assert "My lists: Favourites" in parasite["content"]
    assert "Korean Cinema" not in parasite["content"]


def test_state_is_written_only_after_the_last_row(tmp_path):
    path = write_zip(tmp_path / "export.zip")
    data = LetterboxdExportService(path).read_export()
    state: dict = {}
    rows = sync_export(data, state)
    next(rows)
    rows.close()  # what happens when check_active raises between rows
    assert state == {}, "the known ids only move once every row is out"


def test_a_failed_run_replays_the_same_rows(tmp_path):
    rows, persisted = run_sync(default_export(), tmp_path)
    replayed, _ = run_sync(default_export(), tmp_path, state=dict(persisted))
    assert replayed == rows  # the load failed: dlt keeps the old state


# --------------------------------------------------------------------- tolerance
def test_unknown_extra_columns_are_ignored(tmp_path):
    base, _ = run_sync(default_export(), tmp_path)
    extra, _ = run_sync(with_extra_columns(default_export()), tmp_path)
    assert extra == base


def test_a_missing_optional_tags_column_does_not_crash(tmp_path):
    rows, _ = run_sync(without_tags_column(default_export()), tmp_path)
    content = by_id(rows)[MAD_MAX]["content"]
    assert "2024-06-10 (rewatch)" in content
    assert "tags:" not in content


def test_an_export_of_only_watched_csv_still_works(tmp_path):
    files = {"watched.csv": default_export()["watched.csv"]}
    rows, _ = run_sync(files, tmp_path)
    assert len(rows) == 5
    parasite = by_id(rows)[PARASITE]
    assert "My rating" not in parasite["content"]
    assert "My lists" not in parasite["content"]


def test_a_missing_watched_csv_is_a_clear_error(tmp_path):
    path = write_zip(tmp_path / "broken.zip", without_watched(default_export()))
    with pytest.raises(LetterboxdExportError, match=r"watched\.csv"):
        LetterboxdExportService(path).read_export()
    with pytest.raises(LetterboxdExportError, match=r"watched\.csv"):
        letterboxd_source(export=path)


def test_an_extracted_directory_works_like_the_zip(tmp_path):
    from_zip, _ = run_sync(default_export(), tmp_path)
    path = write_dir(tmp_path / "extracted")
    rows = list(sync_export(LetterboxdExportService(path).read_export(), {}))
    assert rows == from_zip


# --------------------------------------------------------------------- factory / wiring
def test_a_missing_export_path_raises_a_clear_error(tmp_path):
    with pytest.raises(LetterboxdExportError, match=r"letterboxd\.com"):
        letterboxd_source(export=tmp_path / "nope.zip")


def test_a_missing_export_argument_raises_a_clear_error():
    with pytest.raises(LetterboxdExportError, match="export="):
        letterboxd_source()


@pytest.mark.parametrize("name", ["Film-Diary", "film diary", "1film"])
def test_a_resource_name_dlt_would_rename_is_refused(name, tmp_path):
    path = write_zip(tmp_path / "export.zip")
    with pytest.raises(ValueError, match="resource_name"):
        letterboxd_source(export=path, resource_name=name)


def test_resource_is_configured_for_merge_and_hard_delete(tmp_path):
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    path = write_zip(tmp_path / "export.zip")
    source = letterboxd_source(export=path)
    resource = source.resources["letterboxd_default"]
    assert resource.write_disposition == "merge"
    assert resource.compute_table_schema()["columns"]["_deleted"]["hard_delete"] is True
    assert resource.compute_table_schema()["columns"]["_deleted"]["data_type"] == "bool"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "letterboxd"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "letterboxd:default"

    named = letterboxd_source(export=path, resource_name="film_diary")
    assert set(named.resources) == {"film_diary"}
    assert getattr(named, PIPELINE_SCOPE_ATTR) == "letterboxd:film_diary"


def test_check_active_runs_around_every_row(tmp_path):
    path = write_zip(tmp_path / "export.zip")
    checks = []
    source = letterboxd_source(export=path, check_active=lambda: checks.append(1))
    rows = list(source.resources["letterboxd_default"])
    assert len(rows) == 7
    # once before reading the export, then guarded_rows: before and after each
    # row, and twice at the end.
    assert len(checks) == 1 + 2 * 7 + 2


def test_source_stats_are_counts_only(tmp_path):
    path = write_zip(tmp_path / "export.zip")
    source = letterboxd_source(export=path)
    list(source.resources["letterboxd_default"])
    assert source.cognee_sync_stats == {
        "films": 5,
        "lists": 2,
        "deletions": 0,
        "skipped": 0,
        "failed": 0,
    }
    assert all(type(value) is int for value in source.cognee_sync_stats.values())


def test_service_injection_avoids_the_filesystem():
    service = FakeLetterboxdService(to_export_data(default_export()))
    source = letterboxd_source(service=service)
    rows = list(source.resources["letterboxd_default"])
    assert len(rows) == 7
    assert service.calls == 1


def test_row_becomes_a_letterboxd_document_in_cognee():
    # The row -> document mapping is cognee's (the same check the Notion tests do).
    pytest.importorskip("cognee")
    from types import SimpleNamespace
    from uuid import NAMESPACE_OID, uuid5

    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = film_to_row(Film(uri=PARASITE, name="Parasite", year="2019", rating=7.5))
    data_id = uuid5(NAMESPACE_OID, row["id"])
    item = _build_document_data_item(
        SimpleNamespace(row_data=row, table_name="letterboxd_default"), data_id, "letterboxd"
    )
    # source="letterboxd" (not "dlt") routes the row through normal cognify.
    assert item.system_metadata["source"] == "letterboxd"
    assert item.system_metadata["external_id"] == PARASITE
    assert item.system_metadata["url"] == PARASITE
    assert item.data.startswith("# Parasite (2019)")
