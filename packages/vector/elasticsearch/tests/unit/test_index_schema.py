"""index_data_points carries cognee core's reference fields into the search payload."""

from uuid import uuid4

import pytest
from cognee.infrastructure.engine import DataPoint
from cognee_community_vector_adapter_elasticsearch.elasticsearch_adapter import IndexSchema
from fake_elasticsearch import FakeAsyncElasticsearch, make_adapters

REFERENCE_FIELDS = (
    "document_id",
    "document_name",
    "chunk_index",
    "source_chunk_id",
    "importance_weight",
    "external_metadata",
)


class Embeddings:
    async def embed_text(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def get_vector_size(self):
        return 2

    def get_batch_size(self):
        return 16


class Chunk(DataPoint):
    """Stand-in for cognee's DocumentChunk: the indexed text plus reference scalars."""

    text: str
    document_id: str | None = None
    document_name: str | None = None
    chunk_index: int | None = None
    importance_weight: float | None = 0.5
    external_metadata: str | None = None
    metadata: dict = {"index_fields": ["text"]}


class Entity(DataPoint):
    name: str
    metadata: dict = {"index_fields": ["name"]}


@pytest.fixture
def adapter():
    (a,) = make_adapters(FakeAsyncElasticsearch(), ("a",), Embeddings)
    return a


def test_index_schema_mirrors_core_reference_fields():
    assert set(REFERENCE_FIELDS) <= set(IndexSchema.model_fields)


@pytest.mark.asyncio
async def test_reference_fields_come_back_in_search_payloads(adapter):
    chunk = Chunk(
        id=uuid4(),
        text="quantum computers",
        document_id="doc-1",
        document_name="Quantum_computers.txt",
        chunk_index=3,
        importance_weight=0.8,
        external_metadata='{"source": "wiki"}',
    )
    await adapter.index_data_points("Chunk", "text", [chunk])

    (hit,) = await adapter.search("Chunk_text", query_vector=[1.0, 0.0], include_payload=True)
    assert hit.payload["text"] == "quantum computers"
    assert hit.payload["document_id"] == "doc-1"
    assert hit.payload["document_name"] == "Quantum_computers.txt"
    assert hit.payload["chunk_index"] == 3
    assert hit.payload["importance_weight"] == 0.8
    assert hit.payload["external_metadata"] == '{"source": "wiki"}'
    assert hit.payload["source_chunk_id"] is None


@pytest.mark.asyncio
async def test_data_points_without_reference_fields_index_with_none(adapter):
    await adapter.index_data_points("Entity", "name", [Entity(id=uuid4(), name="Berlin")])

    (hit,) = await adapter.search("Entity_name", query_vector=[1.0, 0.0], include_payload=True)
    assert hit.payload["text"] == "Berlin"
    # importance_weight lives on every DataPoint (default 0.5); the rest are chunk-only.
    assert hit.payload["importance_weight"] == 0.5
    for field in set(REFERENCE_FIELDS) - {"importance_weight"}:
        assert hit.payload[field] is None
