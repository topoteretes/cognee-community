"""Offline tests for the cognee-community LodeDB vector adapter.

These drive the adapter directly with a deterministic one-hot embedding engine, so they
run with no LLM or API key. Importing ``register`` also verifies the provider registers
with cognee's vector-adapter registry.
"""

import asyncio

import pytest

pytest.importorskip("lodedb")
pytest.importorskip("cognee")

from cognee.infrastructure.databases.vector.supported_databases import supported_databases
from cognee.infrastructure.engine import DataPoint
from cognee_community_vector_adapter_lodedb import register  # noqa: F401
from cognee_community_vector_adapter_lodedb.lodedb_adapter import LodeDBAdapter

DIM = 8


class OneHotEmbeddingEngine:
    def __init__(self) -> None:
        self._index: dict[str, int] = {}

    async def embed_text(self, text: list[str]) -> list[list[float]]:
        vectors = []
        for value in text:
            self._index.setdefault(value, len(self._index))
            vector = [0.0] * DIM
            vector[self._index[value]] = 1.0
            vectors.append(vector)
        return vectors

    def get_vector_size(self) -> int:
        return DIM

    def get_batch_size(self) -> int:
        return 32


class Note(DataPoint):
    text: str
    metadata: dict = {"index_fields": ["text"]}


def test_register_registers_lodedb_provider():
    assert "lodedb" in supported_databases
    assert supported_databases["lodedb"] is LodeDBAdapter


def test_create_search_retrieve_and_node_name(tmp_path):
    async def body():
        adapter = LodeDBAdapter(url=str(tmp_path), embedding_engine=OneHotEmbeddingEngine())
        a = Note(text="alice likes espresso", belongs_to_set=["people"])
        b = Note(text="the project uses lodedb", belongs_to_set=["project"])
        await adapter.create_data_points("Note_text", [a, b])

        hits = await adapter.search(
            "Note_text", query_text="alice likes espresso", limit=2, include_payload=True
        )
        assert hits[0].id == a.id
        assert hits[0].payload["text"] == "alice likes espresso"
        assert hits[0].score <= hits[1].score

        scoped = await adapter.search(
            "Note_text", query_text="alice likes espresso", limit=5, node_name=["project"]
        )
        assert {hit.id for hit in scoped} == {b.id}

        got = await adapter.retrieve("Note_text", [str(a.id)])
        assert got[0].payload["text"] == "alice likes espresso"
        adapter.close()

    asyncio.run(body())
