"""Opt-in smoke test for Miro's live board-items endpoint."""

from __future__ import annotations

import os

import pytest

from cognee_community_connector_miro.miro import _board_documents, _MiroRESTSource, miro_source


def _live_enabled() -> bool:
    return os.getenv("MIRO_RUN_LIVE_TESTS", "").strip().lower() in {"1", "true", "yes"}


def _board_id() -> str | None:
    """Return one explicitly configured board without logging credentials."""
    if board_id := os.getenv("MIRO_BOARD_ID", "").strip():
        return board_id

    board_ids = os.getenv("MIRO_BOARD_IDS", "")
    return next((value.strip() for value in board_ids.split(",") if value.strip()), None)


def _access_token() -> str | None:
    """Resolve the live credential without prescribing an env-file location."""
    import dlt

    return dlt.secrets.get("sources.miro.access_token") or os.getenv("MIRO_ACCESS_TOKEN")


@pytest.mark.skipif(
    not _live_enabled() or not _access_token() or not _board_id(),
    reason=(
        "set MIRO_RUN_LIVE_TESTS=1 and MIRO_BOARD_ID (or MIRO_BOARD_IDS), then "
        "configure sources.miro.access_token through a dlt secret provider"
    ),
)
def test_live_board_discovery_and_items_endpoint() -> None:
    """Exercise discovery, item retrieval, and document rendering with real content."""
    board_id = _board_id()
    assert board_id is not None

    access_token = _access_token()
    assert access_token is not None
    client = _MiroRESTSource(access_token)
    boards = client.list_boards()
    selected_board = next((board for board in boards if str(board.get("id")) == board_id), None)
    assert selected_board is not None
    assert selected_board.get("modifiedAt")

    items = client.list_items(board_id)

    assert isinstance(items, list)
    assert items, "configured live board is empty; add a frame with a sticky note, shape, or text"
    assert all(isinstance(item, dict) for item in items)
    assert all(item.get("id") and item.get("type") for item in items)

    documents = _board_documents(selected_board, items)
    assert documents, "live board has no supported text content to render"
    assert len({document["id"] for document in documents}) == len(documents)
    assert all(document["content"].strip() for document in documents)


@pytest.mark.skipif(
    not _live_enabled() or not _access_token() or not _board_id(),
    reason=(
        "set MIRO_RUN_LIVE_TESTS=1 and MIRO_BOARD_ID (or MIRO_BOARD_IDS), then "
        "configure sources.miro.access_token through a dlt secret provider"
    ),
)
def test_live_dlt_sync_persists_documents_and_skips_unchanged_board(tmp_path) -> None:
    """Load real Miro documents through dlt and verify the modifiedAt checkpoint."""
    import dlt

    board_id = _board_id()
    assert board_id is not None
    access_token = _access_token()
    assert access_token is not None

    preflight_client = _MiroRESTSource(access_token)
    selected_board = next(
        (board for board in preflight_client.list_boards() if str(board.get("id")) == board_id),
        None,
    )
    assert selected_board is not None
    items = preflight_client.list_items(board_id)
    assert items, "configured live board is empty; add a frame with a sticky note, shape, or text"
    assert _board_documents(selected_board, items), "live board has no supported text to sync"

    class CountingClient:
        def __init__(self) -> None:
            self.client = _MiroRESTSource(access_token)
            self.item_calls = 0

        def list_boards(self, **kwargs):
            return self.client.list_boards(**kwargs)

        def list_items(self, selected_board_id: str):
            self.item_calls += 1
            return self.client.list_items(selected_board_id)

    client = CountingClient()
    pipeline = dlt.pipeline(
        pipeline_name="miro_live_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'miro-live.db'}"),
        dataset_name="miro_live",
        pipelines_dir=str(tmp_path / "pipelines"),
    )

    pipeline.run(miro_source(board_ids=[board_id], client=client))
    with pipeline.sql_client() as sql_client:
        first_ids = [
            row[0] for row in sql_client.execute_sql("SELECT id FROM miro_documents ORDER BY id")
        ]

    assert first_ids, "live board produced no supported frame documents"
    assert client.item_calls == 1

    pipeline.run(miro_source(board_ids=[board_id], client=client))
    with pipeline.sql_client() as sql_client:
        second_ids = [
            row[0] for row in sql_client.execute_sql("SELECT id FROM miro_documents ORDER BY id")
        ]

    assert second_ids == first_ids
    assert client.item_calls == 1
