"""Opt-in live Miro mutation test for shape rendering and deletion propagation."""

from __future__ import annotations

import asyncio
import importlib
import os
import time
import uuid

import pytest
import requests

from cognee_community_connector_miro.miro import _board_documents, _MiroRESTSource, miro_source

KEEP_MARKER = "MiroKeep4553"
DELETE_MARKER = "MiroDelete4553"
SHAPE_LABEL = f"{KEEP_MARKER} shape label"


def _enabled() -> bool:
    return os.getenv("MIRO_RUN_LIVE_WRITE_TESTS", "").strip().lower() in {"1", "true", "yes"}


def _board_id() -> str | None:
    if board_id := os.getenv("MIRO_BOARD_ID", "").strip():
        return board_id
    return next(
        (value.strip() for value in os.getenv("MIRO_BOARD_IDS", "").split(",") if value.strip()),
        None,
    )


def _access_token() -> str | None:
    import dlt

    return dlt.secrets.get("sources.miro.access_token") or os.getenv("MIRO_ACCESS_TOKEN")


def _request(method: str, path: str, *, token: str, board_id: str, json=None):
    response = requests.request(
        method,
        f"https://api.miro.com/v2/boards/{board_id}/{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=json,
        timeout=30,
    )
    response.raise_for_status()
    return response.json() if response.content else None


def _create_frame(token: str, board_id: str, title: str, x: int) -> dict:
    payload = {
        "data": {"title": title},
        "position": {"x": x, "y": 1800, "origin": "center"},
        "geometry": {"width": 600, "height": 400},
    }
    last_error: requests.HTTPError | None = None
    for attempt in range(3):
        try:
            return _request(
                "POST",
                "frames",
                token=token,
                board_id=board_id,
                json=payload,
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status != 429 and (status is None or status < 500):
                raise
            last_error = exc

        # A failed POST response can be ambiguous. Check whether Miro created
        # the uniquely titled frame before retrying the non-idempotent request.
        time.sleep(attempt + 1)
        frame = next(
            (
                item
                for item in _MiroRESTSource(token).list_items(board_id)
                if item.get("type") == "frame"
                and (item.get("data") or {}).get("title") == title
            ),
            None,
        )
        if frame is not None:
            return frame

    assert last_error is not None
    raise last_error


def _create_shape(token: str, board_id: str, frame_id: str) -> dict:
    return _request(
        "POST",
        "shapes",
        token=token,
        board_id=board_id,
        json={
            "data": {"content": SHAPE_LABEL, "shape": "rectangle"},
            "parent": {"id": frame_id},
            "position": {"x": 0, "y": 0, "origin": "center"},
            "geometry": {"width": 320, "height": 120},
        },
    )


def _create_sticky(token: str, board_id: str, frame_id: str) -> dict:
    return _request(
        "POST",
        "sticky_notes",
        token=token,
        board_id=board_id,
        json={
            "data": {
                "content": f"{DELETE_MARKER} is a temporary deletion entity.",
                "shape": "square",
            },
            "parent": {"id": frame_id},
            "position": {"x": 0, "y": 0, "origin": "center"},
        },
    )


def _delete_item(token: str, board_id: str, item_type: str, item_id: str) -> None:
    try:
        _request("DELETE", f"{item_type}/{item_id}", token=token, board_id=board_id)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise


def _wait_for_marker(client: _MiroRESTSource, board_id: str, marker: str, present: bool) -> None:
    for _ in range(30):
        items = client.list_items(board_id)
        found = any(marker in str((item.get("data") or {}).get("content", "")) for item in items)
        if found is present:
            return
        time.sleep(1)
    expectation = "appear" if present else "disappear"
    raise AssertionError(f"timed out waiting for {marker!r} to {expectation} on the live board")


def _wait_for_board_change(
    client: _MiroRESTSource, board_id: str, previous_modified_at: str
) -> None:
    """Wait until Miro's board-level incremental cursor reflects an item mutation."""
    for _ in range(30):
        if str(client.get_board(board_id).get("modifiedAt")) != previous_modified_at:
            return
        time.sleep(1)
    raise AssertionError("timed out waiting for the live board modifiedAt cursor to advance")


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        names = [
            marker
            for marker in (KEEP_MARKER, DELETE_MARKER)
            if text_input and marker in text_input
        ]
        return KnowledgeGraph(
            nodes=[
                KGNode(id=name, name=name, type="LiveMiroTest", description=f"{name} entity")
                for name in names
            ],
            edges=[],
        )
    return response_model()


@pytest.mark.skipif(
    not _enabled() or not _access_token() or not _board_id(),
    reason=(
        "set MIRO_RUN_LIVE_WRITE_TESTS=1 and MIRO_BOARD_ID, then configure a Miro token "
        "with boards:read and boards:write"
    ),
)
def test_live_shape_and_deleted_frame_reach_cognee_cleanup(tmp_path, monkeypatch) -> None:
    """Create isolated items, prove shape rendering and deletion, then clean up the board."""
    import cognee
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
        LiteLLMEmbeddingEngine,
    )
    from cognee.infrastructure.llm import LLMGateway
    from cognee.modules.data.methods import get_authorized_existing_datasets
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.users.methods import get_default_user

    add_data_points = importlib.import_module("cognee.tasks.storage.add_data_points")
    token = _access_token()
    board_id = _board_id()
    assert token is not None and board_id is not None

    created: list[tuple[str, str]] = []
    run_id = uuid.uuid4().hex
    keep_frame_title = f"{KEEP_MARKER}-{run_id} frame"
    delete_frame_title = f"{DELETE_MARKER}-{run_id} frame"
    frame_titles = {keep_frame_title, delete_frame_title}
    dataset_name = "miro_live_deletion_test"
    client = _MiroRESTSource(token)

    async def no_index(*_args, **_kwargs):
        return None

    async def mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    async def graph_has(marker: str) -> bool:
        nodes, _ = await (await get_graph_engine()).get_graph_data()
        return any(
            marker.lower() in str(value).lower()
            for _, properties in nodes
            for value in (properties or {}).values()
        )

    async def external_ids() -> set[str]:
        user = await get_default_user()
        datasets = await get_authorized_existing_datasets(
            user=user,
            permission_type="write",
            datasets=[dataset_name],
        )
        if not datasets:
            return set()
        data = await get_dataset_data(datasets[0].id)
        return {
            str(item.system_metadata["external_id"])
            for item in data
            if isinstance(item.system_metadata, dict)
            and item.system_metadata.get("source") == "miro"
        }

    async def sync() -> None:
        await cognee.add(
            miro_source(access_token=token, board_ids=[board_id]),
            dataset_name=dataset_name,
            primary_key="id",
            write_disposition="replace",
            max_rows_per_table=0,
        )
        await cognee.cognify(datasets=[dataset_name])

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.chdir(tmp_path)
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    monkeypatch.setattr(add_data_points, "index_data_points", no_index)
    monkeypatch.setattr(add_data_points, "index_graph_edges", no_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)
    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", mock_embed_text)

    async def scenario() -> None:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        try:
            keep_frame = _create_frame(token, board_id, keep_frame_title, 1800)
            created.append(("frames", str(keep_frame["id"])))
            delete_frame = _create_frame(token, board_id, delete_frame_title, 2500)
            created.append(("frames", str(delete_frame["id"])))
            shape = _create_shape(token, board_id, str(keep_frame["id"]))
            created.append(("shapes", str(shape["id"])))
            sticky = _create_sticky(token, board_id, str(delete_frame["id"]))
            created.append(("sticky_notes", str(sticky["id"])))

            _wait_for_marker(client, board_id, SHAPE_LABEL, True)
            _wait_for_marker(client, board_id, DELETE_MARKER, True)
            rendered = _board_documents(client.get_board(board_id), client.list_items(board_id))
            assert SHAPE_LABEL in "\n".join(row["content"] for row in rendered)

            await sync()
            ids_before = await external_ids()
            keep_document_id = f"{board_id}:frame:{keep_frame['id']}"
            delete_document_id = f"{board_id}:frame:{delete_frame['id']}"
            assert {keep_document_id, delete_document_id} <= ids_before
            assert await graph_has(KEEP_MARKER)
            assert await graph_has(DELETE_MARKER)
            previous_modified_at = str(client.get_board(board_id)["modifiedAt"])

            _delete_item(token, board_id, "sticky_notes", str(sticky["id"]))
            created.remove(("sticky_notes", str(sticky["id"])))
            _delete_item(token, board_id, "frames", str(delete_frame["id"]))
            created.remove(("frames", str(delete_frame["id"])))
            _wait_for_marker(client, board_id, DELETE_MARKER, False)
            _wait_for_board_change(client, board_id, previous_modified_at)

            await sync()
            ids_after = await external_ids()
            assert keep_document_id in ids_after
            assert delete_document_id not in ids_after
            assert await graph_has(KEEP_MARKER)
            assert not await graph_has(DELETE_MARKER)
        finally:
            for item_type, item_id in reversed(created):
                _delete_item(token, board_id, item_type, item_id)
            # Remove any duplicate frame from an ambiguous POST retry. Exact,
            # per-run titles avoid touching pre-existing board content.
            for item in client.list_items(board_id):
                if (
                    item.get("type") == "frame"
                    and (item.get("data") or {}).get("title") in frame_titles
                ):
                    _delete_item(token, board_id, "frames", str(item["id"]))
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)

    asyncio.run(scenario())
