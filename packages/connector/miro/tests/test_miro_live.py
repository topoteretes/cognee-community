"""Opt-in smoke test for Miro's live board-items endpoint."""

from __future__ import annotations

import os

import pytest

from cognee_community_connector_miro.miro import _MiroRESTSource


def _board_id() -> str | None:
    """Return one explicitly configured board without logging credentials."""
    if board_id := os.getenv("MIRO_BOARD_ID", "").strip():
        return board_id

    board_ids = os.getenv("MIRO_BOARD_IDS", "")
    return next((value.strip() for value in board_ids.split(",") if value.strip()), None)


@pytest.mark.skipif(
    not os.getenv("MIRO_ACCESS_TOKEN") or not _board_id(),
    reason="set MIRO_ACCESS_TOKEN and MIRO_BOARD_ID (or MIRO_BOARD_IDS) for the live test",
)
def test_live_board_items_endpoint_returns_complete_item_records() -> None:
    """Exercise the same stable endpoint and cursor paginator used in production."""
    board_id = _board_id()
    assert board_id is not None

    items = _MiroRESTSource(os.environ["MIRO_ACCESS_TOKEN"]).list_items(board_id)

    assert isinstance(items, list)
    assert all(isinstance(item, dict) for item in items)
    assert all(item.get("id") and item.get("type") for item in items)
