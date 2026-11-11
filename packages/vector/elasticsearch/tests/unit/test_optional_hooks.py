"""The optional cognee 1.6 hooks: score_by_ids, update_payload, tag removal, raw vectors."""

from uuid import uuid4

import pytest
from cognee_community_vector_adapter_elasticsearch.elasticsearch_adapter import IndexSchema
from fake_elasticsearch import FakeAsyncElasticsearch, make_adapters
from pydantic import BaseModel


class Embeddings:
    async def embed_text(self, texts):
        vectors = {"east": [1.0, 0.0], "north": [0.0, 1.0], "west": [-1.0, 0.0]}
        return [vectors[text] for text in texts]

    def get_vector_size(self):
        return 2

    def get_batch_size(self):
        return 16


@pytest.fixture
def client():
    return FakeAsyncElasticsearch()


@pytest.fixture
def adapters(client):
    return make_adapters(client, ("a", "b"), Embeddings)


@pytest.mark.asyncio
async def test_score_by_ids_scores_only_requested_rows_exactly(adapters):
    a, b = adapters
    east, north, west = uuid4(), uuid4(), uuid4()
    await a.create_data_points(
        "Entity_name",
        [
            IndexSchema(id=east, text="east"),
            IndexSchema(id=north, text="north"),
            IndexSchema(id=west, text="west"),
        ],
    )
    await b.create_data_points("Entity_name", [IndexSchema(id=west, text="east")])

    scores = await a.score_by_ids(
        "Entity_name", [str(west), str(north), str(west), str(uuid4())], [1.0, 0.0]
    )
    by_id = {score.id: score.score for score in scores}

    # Duplicates collapse, missing ids are omitted, the nearest row (east) is not
    # returned because it was not asked for, and b's copy of west is ignored.
    assert set(by_id) == {west, north}
    assert by_id[west] == pytest.approx(2.0)
    assert by_id[north] == pytest.approx(1.0)
    assert all(score.payload is None for score in scores)
    assert await a.score_by_ids("Entity_name", [], [1.0, 0.0]) == []


@pytest.mark.asyncio
async def test_update_payload_keeps_vectors_and_skips_missing_ids(adapters, client):
    a, b = adapters
    chunk, other = uuid4(), uuid4()
    await a.create_data_points(
        "DocumentChunk_text", [IndexSchema(id=chunk, text="east", belongs_to_set=["red"])]
    )
    await b.create_data_points("DocumentChunk_text", [IndexSchema(id=chunk, text="north")])
    a.embedding_engine.embed_text = None  # any re-embedding would blow up

    await a.update_payload(
        "DocumentChunk_text",
        {str(chunk): {"chunk_index": 3, "belongs_to_set": ["blue"]}, str(other): {"x": 1}},
    )

    (record,) = await a.retrieve("DocumentChunk_text", [str(chunk)])
    assert record.payload["chunk_index"] == 3
    assert record.payload["text"] == "east"
    assert record.payload["belongs_to_set"] == ["blue"]
    east = [1.0, 0.0]
    assert await a.search("DocumentChunk_text", query_vector=east, node_name=["blue"]) != []
    assert await a.search("DocumentChunk_text", query_vector=east, node_name=["red"]) == []
    stored = client.store["cognee-documentchunk_text"]["docs"][a._doc_id(chunk)]
    assert stored["vector"] == [1.0, 0.0]
    (b_record,) = await b.retrieve("DocumentChunk_text", [str(chunk)])
    assert b_record.payload["chunk_index"] is None
    await a.update_payload("Missing_text", {str(chunk): {"x": 1}})
    assert a.supports_payload_update is True


@pytest.mark.asyncio
async def test_update_payload_refuses_unknown_fields_and_changes_nothing(adapters):
    a, _ = adapters
    chunk = uuid4()
    await a.create_data_points("DocumentChunk_text", [IndexSchema(id=chunk, text="east")])
    await a.update_payload("DocumentChunk_text", {str(chunk): {"chunk_index": 41}})

    with pytest.raises(ValueError, match="not_a_payload_field"):
        await a.update_payload(
            "DocumentChunk_text",
            {str(chunk): {"chunk_index": 7, "not_a_payload_field": 1}},
        )

    (record,) = await a.retrieve("DocumentChunk_text", [str(chunk)])
    assert record.payload["chunk_index"] == 41, "a refused update must change nothing"
    assert "not_a_payload_field" not in record.payload
    # A missing id is skipped, not refused, whatever its fields.
    await a.update_payload("DocumentChunk_text", {str(uuid4()): {"not_a_payload_field": 1}})


@pytest.mark.asyncio
async def test_reindexing_merges_node_set_tags(adapters):
    a, b = adapters
    entity = uuid4()
    await a.create_data_points(
        "Entity_name", [IndexSchema(id=entity, text="east", belongs_to_set=["A"])]
    )
    await b.create_data_points(
        "Entity_name", [IndexSchema(id=entity, text="east", belongs_to_set=["other"])]
    )
    await a.create_data_points(
        "Entity_name", [IndexSchema(id=entity, text="east", belongs_to_set=["B"])]
    )

    (record,) = await a.retrieve("Entity_name", [str(entity)])
    assert record.payload["belongs_to_set"] == ["A", "B"]
    east = [1.0, 0.0]
    assert [h.id for h in await a.search("Entity_name", query_vector=east, node_name=["A"])] == [
        entity
    ]
    # Another dataset's tags never leak in.
    (b_record,) = await b.retrieve("Entity_name", [str(entity)])
    assert b_record.payload["belongs_to_set"] == ["other"]

    # Deleting node set B keeps the row: node set A still owns it.
    await a.remove_belongs_to_set_tags(["B"])
    (record,) = await a.retrieve("Entity_name", [str(entity)])
    assert record.payload["belongs_to_set"] == ["A"]


@pytest.mark.asyncio
async def test_duplicate_ids_in_one_batch_union_their_tags(adapters):
    a, _ = adapters
    entity = uuid4()
    await a.create_data_points(
        "Entity_name",
        [
            IndexSchema(id=entity, text="east", belongs_to_set=["A"]),
            IndexSchema(id=entity, text="east", belongs_to_set=["B"]),
        ],
    )

    (record,) = await a.retrieve("Entity_name", [str(entity)])
    assert record.payload["belongs_to_set"] == ["A", "B"]


@pytest.mark.asyncio
async def test_remove_tags_detags_rows_and_deletes_empty_ones(adapters):
    a, b = adapters
    both, only_red, scoped = uuid4(), uuid4(), uuid4()
    await a.create_data_points(
        "Entity_name",
        [
            IndexSchema(id=both, text="east", belongs_to_set=["red", "blue"]),
            IndexSchema(id=only_red, text="north", belongs_to_set=["red"]),
        ],
    )
    await a.create_data_points(
        "DocumentChunk_text", [IndexSchema(id=scoped, text="west", belongs_to_set=["red"])]
    )
    await b.create_data_points(
        "Entity_name", [IndexSchema(id=only_red, text="east", belongs_to_set=["red"])]
    )

    # Scoped detag: only the named node loses the tag.
    await a.remove_belongs_to_set_tags(["red"], node_ids=[str(scoped)])
    assert await a.retrieve("DocumentChunk_text", [str(scoped)]) == []
    assert len(await a.retrieve("Entity_name", [str(both), str(only_red)])) == 2

    await a.remove_belongs_to_set_tags(["red"])
    (survivor,) = await a.retrieve("Entity_name", [str(both), str(only_red)])
    assert survivor.id == both
    assert survivor.payload["belongs_to_set"] == ["blue"]
    assert await a.search("Entity_name", query_text="east", node_name=["red"]) == []
    assert [
        hit.id for hit in await a.search("Entity_name", query_text="east", node_name=["blue"])
    ] == [both]
    # Another dataset's rows never change.
    (b_row,) = await b.retrieve("Entity_name", [str(only_red)])
    assert b_row.payload["belongs_to_set"] == ["red"]

    await a.remove_belongs_to_set_tags([])
    await a.remove_belongs_to_set_tags(["blue"], node_ids=[])
    assert len(await a.retrieve("Entity_name", [str(both)])) == 1


class CentroidPayload(BaseModel):
    dataset_id: str
    slot: int
    label: str = "centroid"


@pytest.mark.asyncio
async def test_upsert_raw_vectors_round_trip(adapters):
    a, _ = adapters
    a.embedding_engine.embed_text = None  # raw writes must not embed
    centroid = uuid4()
    points = [
        {
            "id": centroid,
            "vector": [0.0, 1.0],
            "payload": {"dataset_id": "ds", "slot": 0},
        }
    ]

    await a.upsert_raw_vectors("TruthCentroid_vector", points, payload_schema=CentroidPayload)
    await a.upsert_raw_vectors("TruthCentroid_vector", points, payload_schema=CentroidPayload)

    (record,) = await a.retrieve("TruthCentroid_vector", [str(centroid)])
    assert record.payload == {"dataset_id": "ds", "slot": 0, "label": "centroid"}
    hits = await a.search("TruthCentroid_vector", query_vector=[0.0, 1.0], limit=None)
    assert [hit.id for hit in hits] == [centroid]
    assert hits[0].score == pytest.approx(0.0)

    with pytest.raises(ValueError, match="size"):
        await a.upsert_raw_vectors(
            "TruthCentroid_vector", [{"id": uuid4(), "vector": [1.0, 0.0, 0.0], "payload": {}}]
        )
    with pytest.raises(ValueError, match="missing id"):
        await a.upsert_raw_vectors("TruthCentroid_vector", [{"vector": [1.0, 0.0]}])
    await a.upsert_raw_vectors("TruthCentroid_vector", [])


@pytest.mark.asyncio
async def test_close_closes_the_client(adapters, client):
    a, _ = adapters
    await a.close()
    assert client.closed
