"""Dataset isolation on a shared index, against the in-memory Elasticsearch fake."""

from uuid import uuid4

import pytest
from cognee.infrastructure.databases.vector.exceptions import CollectionNotFoundError
from cognee_community_vector_adapter_elasticsearch.elasticsearch_adapter import IndexSchema
from contract_suite import FakeEmbeddingEngine
from fake_elasticsearch import FakeAsyncElasticsearch, make_adapters

COLLECTION = "Entity_name"


@pytest.fixture
def client():
    return FakeAsyncElasticsearch()


@pytest.fixture
def adapters(client):
    return make_adapters(client, ("dataset-a", "dataset-b"), FakeEmbeddingEngine)


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_method", ["delete_data_points", "prune"])
async def test_shared_entity_survives_other_dataset_write_and_deletion(adapters, delete_method):
    a, b = adapters
    point_id = uuid4()
    await a.create_collection(COLLECTION)
    for adapter, text in ((a, "Alice in A"), (b, "Alice in B")):
        point = IndexSchema(id=point_id, text=text)
        # Repeated ingestion must remain an upsert within each dataset.
        await adapter.create_data_points(COLLECTION, [point])
        await adapter.create_data_points(COLLECTION, [point])

    for adapter, text in ((a, "Alice in A"), (b, "Alice in B")):
        records = await adapter.retrieve(COLLECTION, [str(point_id), str(uuid4())])
        assert len(records) == 1
        assert str(records[0].id) == str(point_id)
        assert records[0].payload["text"] == text
        for include_payload in (False, True):
            hits = await adapter.search(
                COLLECTION, query_text=text, include_payload=include_payload
            )
            assert len(hits) == 1
            assert str(hits[0].id) == str(point_id)
            assert hits[0].score == pytest.approx(0.0, abs=1e-9)
            if include_payload:
                assert hits[0].payload["id"] == str(point_id)
                assert hits[0].payload["text"] == text
            else:
                assert hits[0].payload is None

    if delete_method == "prune":
        await a.prune()
    else:
        await a.delete_data_points(COLLECTION, [str(point_id)])
    assert await a.retrieve(COLLECTION, [str(point_id)]) == []
    assert await a.search(COLLECTION, query_text="Alice") == []
    remaining = await b.retrieve(COLLECTION, [str(point_id)])
    assert len(remaining) == 1
    assert remaining[0].payload["text"] == "Alice in B"


@pytest.mark.asyncio
async def test_prune_only_drops_indices_left_empty(adapters, client):
    a, b = adapters
    await a.create_data_points(COLLECTION, [IndexSchema(id=uuid4(), text="a")])
    await b.create_data_points(COLLECTION, [IndexSchema(id=uuid4(), text="b")])
    await a.create_data_points("TextSummary_text", [IndexSchema(id=uuid4(), text="only a")])

    await a.prune()

    assert await a.get_collection_names() == []
    assert await b.get_collection_names() == [COLLECTION]
    assert "cognee-textsummary_text" not in client.store
    assert "cognee-entity_name" in client.store

    await b.prune()
    assert client.store == {}


@pytest.mark.asyncio
async def test_collection_names_keep_cognee_casing_and_only_own_rows(adapters):
    a, b = adapters
    await a.create_data_points("DocumentChunk_text", [IndexSchema(id=uuid4(), text="chunk")])
    await b.create_collection("Entity_name")

    assert await a.get_collection_names() == ["DocumentChunk_text"]
    assert await b.get_collection_names() == []
    assert await a.has_collection("Entity_name")


@pytest.mark.asyncio
async def test_empty_ids_do_not_delete_points(adapters):
    a, _ = adapters
    await a.create_collection(COLLECTION)
    point_id = uuid4()
    await a.create_data_points(COLLECTION, [IndexSchema(id=point_id, text="Alice")])
    assert await a.retrieve(COLLECTION, []) == []
    await a.delete_data_points(COLLECTION, [])
    assert len(await a.retrieve(COLLECTION, [str(point_id)])) == 1


@pytest.mark.asyncio
async def test_missing_collection_behaviour(adapters):
    a, _ = adapters
    assert await a.retrieve("Missing_text", [str(uuid4())]) == []
    await a.delete_data_points("Missing_text", [str(uuid4())])
    assert await a.score_by_ids("Missing_text", [str(uuid4())], [1.0] * 8) == []
    with pytest.raises(CollectionNotFoundError):
        await a.search("Missing_text", query_text="anything")


@pytest.mark.asyncio
async def test_create_collection_is_idempotent_and_records_the_name(adapters, client):
    a, _ = adapters
    await a.create_collection(COLLECTION)
    await a.create_collection(COLLECTION)
    mappings = client.store["cognee-entity_name"]["mappings"]
    assert mappings["_meta"]["collection_name"] == COLLECTION
    assert mappings["properties"]["vector"] == {
        "type": "dense_vector",
        "dims": 8,
        "index": True,
        "similarity": "cosine",
        "index_options": {"type": "int8_hnsw"},
    }
    assert mappings["properties"]["payload"] == {"type": "object", "enabled": False}


@pytest.mark.asyncio
async def test_bulk_item_errors_surface(adapters):
    a, _ = adapters
    await a.create_collection(COLLECTION)
    a.embedding_engine.vector_size = 3  # vectors no longer match the index mapping
    with pytest.raises(RuntimeError, match="document_parsing_exception"):
        await a.create_data_points(COLLECTION, [IndexSchema(id=uuid4(), text="bad")])
