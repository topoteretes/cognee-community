"""Exercise dataset isolation with the real Qdrant local engine, without a server."""

from uuid import uuid4

import pytest
from cognee_community_vector_adapter_qdrant.qdrant_adapter import IndexSchema, QDrantAdapter
from contract_suite import FakeEmbeddingEngine
from qdrant_client import models

COLLECTION = "Entity_name"


@pytest.fixture
def adapters(tmp_path):
    return [
        QDrantAdapter(
            url=None,
            api_key=None,
            embedding_engine=FakeEmbeddingEngine(),
            qdrant_path=str(tmp_path / "qdrant"),
            database_name=dataset,
        )
        for dataset in ("dataset-a", "dataset-b")
    ]


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


async def seed_legacy(adapter, point_id):
    client = adapter.get_qdrant_client()
    try:
        await client.upsert(
            COLLECTION,
            [
                models.PointStruct(
                    id=str(point_id),
                    vector={"text": (await adapter.embed_data(["legacy Alice"]))[0]},
                    payload={
                        "id": str(point_id),
                        "text": "legacy Alice",
                        "database_name": adapter.database_name,
                    },
                )
            ],
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_legacy_reads_and_deletes_are_dataset_scoped(adapters):
    a, b = adapters
    point_id = uuid4()
    await a.create_collection(COLLECTION)
    await seed_legacy(b, point_id)
    assert await a.retrieve(COLLECTION, [str(point_id)]) == []
    await a.delete_data_points(COLLECTION, [str(point_id)])
    assert len(await b.retrieve(COLLECTION, [str(point_id)])) == 1
    await b.delete_data_points(COLLECTION, [str(point_id)])
    assert await b.retrieve(COLLECTION, [str(point_id)]) == []


@pytest.mark.asyncio
async def test_reingestion_migrates_only_owned_legacy_points(adapters):
    a, b = adapters
    point_id = uuid4()
    await a.create_collection(COLLECTION)
    await seed_legacy(b, point_id)
    await a.create_data_points(COLLECTION, [IndexSchema(id=point_id, text="Alice in A")])
    assert (await b.retrieve(COLLECTION, [str(point_id)]))[0].payload["text"] == "legacy Alice"
    await b.create_data_points(COLLECTION, [IndexSchema(id=point_id, text="Alice in B")])
    client = b.get_qdrant_client()
    try:
        assert await client.retrieve(COLLECTION, [str(point_id)]) == []
        assert (await client.count(COLLECTION)).count == 2
    finally:
        await client.close()
    for adapter in (a, b):
        assert len(await adapter.search(COLLECTION, query_text="Alice")) == 1


@pytest.mark.asyncio
async def test_empty_ids_do_not_delete_points(adapters):
    a, _ = adapters
    await a.create_collection(COLLECTION)
    point_id = uuid4()
    await a.create_data_points(COLLECTION, [IndexSchema(id=point_id, text="Alice")])
    assert await a.retrieve(COLLECTION, []) == []
    await a.delete_data_points(COLLECTION, [])
    assert len(await a.retrieve(COLLECTION, [str(point_id)])) == 1
