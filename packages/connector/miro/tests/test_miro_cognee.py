"""Cognee graph integration tests for the Miro connector."""

from __future__ import annotations

import asyncio
import importlib
from copy import deepcopy

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway

from cognee_community_connector_miro import miro_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET_NAME = "miro_graph_integration_test"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"


class FakeMiroClient:
    def __init__(self) -> None:
        self.modified_at = "2026-10-01T10:00:00Z"
        self.items = [
            _frame("f1", "Keep"),
            _frame("f2", "Delete"),
            _item("a", f"{ALPHA} is a logistics company.", "f1"),
            _item("b", f"{BRAVO} is a finance company.", "f2"),
        ]

    def list_boards(self, **_kwargs):
        return [
            {
                "id": "board-1",
                "name": "Company research",
                "modifiedAt": self.modified_at,
                "viewLink": "https://miro.com/app/board/board-1/",
            }
        ]

    def list_items(self, _board_id: str):
        return deepcopy(self.items)


def _frame(frame_id: str, title: str) -> dict:
    return {
        "id": frame_id,
        "type": "frame",
        "data": {"title": title},
        "position": {"x": 0, "y": 0},
    }


def _item(item_id: str, content: str, parent_id: str) -> dict:
    return {
        "id": item_id,
        "type": "sticky_note",
        "data": {"content": content},
        "parent": {"id": parent_id},
        "position": {"x": 0, "y": 0},
    }


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
        name = next((token for token in (ALPHA, BRAVO) if text_input and token in text_input), None)
        nodes = (
            [KGNode(id=name, name=name, type="Company", description=f"{name} entity")]
            if name
            else []
        )
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _, props in nodes for value in (props or {}).values()
    )


def test_deleted_miro_frame_is_removed_from_cognified_graph(tmp_path, monkeypatch) -> None:
    """Prove deletion reaches graph content, not only dlt staging."""
    pytest.importorskip("dlt")
    pytest.importorskip("ladybug")

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.chdir(tmp_path)
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def no_index(*_args, **_kwargs):
        return None

    async def mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(add_data_points_module, "index_data_points", no_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", no_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)
    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", mock_embed_text)

    async def sync(client: FakeMiroClient) -> None:
        await cognee.add(
            miro_source(client=client),
            dataset_name=DATASET_NAME,
            primary_key="id",
            write_disposition="merge",
            max_rows_per_table=0,
        )
        await cognee.cognify(datasets=[DATASET_NAME])

    async def scenario() -> None:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        try:
            client = FakeMiroClient()
            await sync(client)
            assert await _graph_has(ALPHA)
            assert await _graph_has(BRAVO)

            client.modified_at = "2026-10-02T10:00:00Z"
            client.items = [
                _frame("f1", "Keep"),
                _item("a", f"{ALPHA} is a logistics company.", "f1"),
            ]
            await sync(client)

            assert await _graph_has(ALPHA)
            assert not await _graph_has(BRAVO)
        finally:
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)

    asyncio.run(scenario())
