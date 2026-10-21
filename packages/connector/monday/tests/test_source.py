"""monday_source(): the factory users call."""

import sqlite3

import dlt
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_monday import FakeMonday

from cognee_community_connector_monday import MondayAuthError, monday_source


@pytest.fixture(autouse=True)
def no_monday_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MONDAY_API_TOKEN", raising=False)
    monkeypatch.delenv("MONDAY_BOARD_IDS", raising=False)
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))


@pytest.fixture
def pipeline(tmp_path):
    database = tmp_path / "staging.db"
    destination = dlt.destinations.sqlalchemy(f"sqlite:///{database}")
    return dlt.pipeline("monday_test", destination=destination, dataset_name="main"), database


def run(pipeline, source):
    pipeline[0].run(source)
    return staged(pipeline, source.name)


def staged(pipeline, table="monday_items"):
    with sqlite3.connect(pipeline[1]) as conn:
        return sorted(row[0] for row in conn.execute(f"SELECT id FROM {table}"))


def account(account_id, board_id, *item_ids):
    """A monday.com account with one board and a few items."""
    monday = FakeMonday(account_id=account_id)
    monday.add_board(board_id, f"Board {board_id}")
    for item_id in item_ids:
        monday.add_item(item_id, board_id, f"Item {item_id}")
    return monday


def test_resource_opts_into_document_mode_merge_and_pipeline_scope():
    resource = monday_source(board_ids=[1], client=FakeMonday())

    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "monday"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "monday_items"
    assert resource.write_disposition == "merge"
    assert resource.columns["_deleted"]["hard_delete"] is True
    assert resource.cognee_sync_stats == {}


def test_a_second_account_under_the_same_name_is_refused_and_forgets_nothing(pipeline):
    first = run(pipeline, monday_source(board_ids=[100], client=account("1", "100", "1", "2")))
    other = account("2", "200", "9")

    with pytest.raises(Exception, match=r"another monday\.com account"):
        run(pipeline, monday_source(board_ids=[200], client=other))

    # Refused before any board was read, and the first account keeps every
    # item: without the check, its board would look deselected.
    assert other.calls == ["Account"]
    assert staged(pipeline) == first == ["monday:1", "monday:2"]


def test_two_accounts_with_their_own_names_stay_apart_in_one_dataset(pipeline):
    first = account("1", "100", "1", "2")
    run(pipeline, monday_source(board_ids=[100], client=first, resource_name="monday_a"))
    run(
        pipeline,
        monday_source(board_ids=[200], client=account("2", "200", "9"), resource_name="monday_b"),
    )
    first.delete("2")

    run(pipeline, monday_source(board_ids=[100], client=first, resource_name="monday_a"))

    assert staged(pipeline, "monday_a") == ["monday:1"]
    assert staged(pipeline, "monday_b") == ["monday:9"]


def test_a_missing_token_fails_when_the_source_is_built():
    with pytest.raises(MondayAuthError, match="MONDAY_API_TOKEN"):
        monday_source(board_ids=[1])


def test_the_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("MONDAY_API_TOKEN", "env-token")

    assert monday_source(board_ids=[1]) is not None


def test_a_malformed_token_fails_without_echoing_it():
    with pytest.raises(MondayAuthError) as excinfo:
        monday_source(board_ids=[1], token="abc def")
    assert "abc def" not in str(excinfo.value)


@pytest.mark.parametrize("empty", [[], [""], "  "])
def test_an_empty_board_list_is_refused_instead_of_forgetting_everything(empty):
    with pytest.raises(ValueError, match="board_ids is empty"):
        monday_source(board_ids=empty, client=FakeMonday())


def test_board_ids_come_from_the_environment_and_are_deduplicated(monkeypatch):
    from cognee_community_connector_monday.monday import _normalize_board_ids

    monkeypatch.setenv("MONDAY_BOARD_IDS", " 111, 222 ,111,")

    assert _normalize_board_ids(None) == ["111", "222"]
    assert _normalize_board_ids([111, "111", 222]) == ["111", "222"]


def test_board_ids_must_be_numeric():
    with pytest.raises(ValueError, match="numeric"):
        monday_source(board_ids=["my board"], client=FakeMonday())


def test_list_boards_hides_subitem_boards_and_pages_through_all():
    from cognee_community_connector_monday import list_boards

    monday = FakeMonday()
    monday.add_board("100", "Launch plan", subitems_board="101")
    monday.add_board("101", "Subitems of Launch plan")
    for n in range(150):
        monday.add_board(str(1000 + n), f"Board {n}")

    boards = list_boards(client=monday)

    assert boards[0] == {"id": "100", "name": "Launch plan", "workspace": "Main workspace"}
    assert len(boards) == 151
    assert "101" not in {board["id"] for board in boards}
    assert monday.calls == ["ListBoards", "ListBoards"]


def test_list_boards_needs_a_token():
    from cognee_community_connector_monday import list_boards

    with pytest.raises(MondayAuthError, match="MONDAY_API_TOKEN"):
        list_boards()
