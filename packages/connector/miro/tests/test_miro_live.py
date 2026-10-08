"""Opt-in smoke test for Miro's live board-items endpoint."""

from __future__ import annotations

import os

import pytest

from cognee_community_connector_miro.miro import _MiroRESTSource


def _live_enabled() -> bool:
    return os.getenv("MIRO_RUN_LIVE_TESTS", "").strip().lower() in {"1", "true", "yes"}


def _board_id() -> str | None:
    """Return one explicitly configured board without logging credentials."""
    if board_id := os.getenv("MIRO_BOARD_ID", "").strip():
        return board_id

    board_ids = os.getenv("MIRO_BOARD_IDS", "")
    return next((value.strip() for value in board_ids.split(",") if value.strip()), None)


@pytest.mark.skipif(
    not _live_enabled() or not os.getenv("MIRO_ACCESS_TOKEN") or not _board_id(),
    reason=(
        "set MIRO_RUN_LIVE_TESTS=1, MIRO_ACCESS_TOKEN, and MIRO_BOARD_ID "
        "(or MIRO_BOARD_IDS) for the live test"
    ),
)
def test_live_board_discovery_and_items_endpoint() -> None:
    """Exercise the board discovery and stable items endpoint used in production."""
    board_id = _board_id()
    assert board_id is not None

    client = _MiroRESTSource(os.environ["MIRO_ACCESS_TOKEN"])
    boards = client.list_boards()
    selected_board = next((board for board in boards if str(board.get("id")) == board_id), None)
    assert selected_board is not None
    assert selected_board.get("modifiedAt")

    items = client.list_items(board_id)

    assert isinstance(items, list)
    assert all(isinstance(item, dict) for item in items)
    assert all(item.get("id") and item.get("type") for item in items)
