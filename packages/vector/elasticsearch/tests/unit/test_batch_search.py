"""Search semantics and batch parity against the in-memory Elasticsearch fake."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cognee_community_vector_adapter_elasticsearch.elasticsearch_adapter import IndexSchema
from fake_elasticsearch import FakeAsyncElasticsearch, make_adapters


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
async def test_scores_are_cosine_distances_in_ascending_order(adapters):
    a, _ = adapters
    east, north, west = uuid4(), uuid4(), uuid4()
    await a.create_data_points(
        "Entity_name",
        [
            IndexSchema(id=west, text="west"),
            IndexSchema(id=north, text="north"),
            IndexSchema(id=east, text="east"),
        ],
    )
    hits = await a.search("Entity_name", query_text="east", limit=None)

    assert [hit.id for hit in hits] == [east, north, west]
    assert [hit.score for hit in hits] == pytest.approx([0.0, 1.0, 2.0])


@pytest.mark.asyncio
@pytest.mark.parametrize("include_payload", [False, True])
@pytest.mark.parametrize("limit", [1, None])
@pytest.mark.parametrize(
    "operator,names", [("OR", None), ("OR", ["red", "blue"]), ("AND", ["red", "blue"])]
)
async def test_batch_matches_individual_searches(adapters, include_payload, limit, operator, names):
    a, b = adapters
    first, second = uuid4(), uuid4()
    await a.create_collection("Entity_name")
    await a.create_data_points(
        "Entity_name",
        [
            IndexSchema(id=first, text="east", belongs_to_set=["red"]),
            IndexSchema(id=second, text="north", belongs_to_set=["red", "blue"]),
        ],
    )
    await b.create_data_points(
        "Entity_name",
        [IndexSchema(id=uuid4(), text="west", belongs_to_set=["red", "blue"])],
    )
    kwargs = dict(
        limit=limit,
        include_payload=include_payload,
        node_name=names,
        node_name_filter_operator=operator,
    )
    queries = ["east", "west"]
    expected = [await a.search("Entity_name", query_text=query, **kwargs) for query in queries]
    a.embedding_engine.embed_text = AsyncMock(wraps=a.embedding_engine.embed_text)
    actual = await a.batch_search("Entity_name", queries, with_vectors=True, **kwargs)
    a.embedding_engine.embed_text.assert_awaited_once_with(queries)
    assert len(actual) == 2
    for batch, single in zip(actual, expected, strict=True):
        assert len(batch) == len(single) > 0
        assert [hit.id for hit in batch] == [hit.id for hit in single]
        assert [hit.score for hit in batch] == pytest.approx([hit.score for hit in single])
        assert [hit.payload for hit in batch] == [hit.payload for hit in single]
        assert all(hit.id in (first, second) for hit in batch)
    # The west query has no similarity above zero, but its matches must remain.
    assert all(hit.score >= 1 for hit in actual[1])
    if operator == "AND":
        assert [hit.id for hit in actual[0]] == [second]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "empty", "zero", "filtered", "other_dataset"])
async def test_no_matches_preserve_query_count(adapters, case):
    a, b = adapters
    kwargs = {}
    if case != "missing":
        await a.create_collection("Entity_name")
    if case in ("zero", "filtered", "other_dataset"):
        owner = b if case == "other_dataset" else a
        await owner.create_data_points("Entity_name", [IndexSchema(id=uuid4(), text="east")])
    if case == "zero":
        kwargs["limit"] = 0
    if case == "filtered":
        kwargs["node_name"] = ["absent"]
    assert await a.batch_search("Entity_name", ["east", "west"], **kwargs) == [[], []]


@pytest.mark.asyncio
async def test_empty_queries_skip_client_and_embeddings(adapters, client):
    a, _ = adapters
    a.embedding_engine.embed_text = AsyncMock(side_effect=AssertionError("must not embed"))
    assert await a.batch_search("missing", []) == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_missing_collection_does_not_embed(adapters):
    a, _ = adapters
    a.embedding_engine.embed_text = AsyncMock(side_effect=AssertionError("must not embed"))
    assert await a.batch_search("Entity_name", ["east"]) == [[]]


@pytest.mark.asyncio
async def test_single_round_trip_per_batch(adapters, client):
    a, _ = adapters
    await a.create_data_points("Entity_name", [IndexSchema(id=uuid4(), text="east")])
    client.calls.clear()
    await a.batch_search("Entity_name", ["east", "north", "west"], limit=5)
    assert client.calls.count("msearch") == 1
    assert "search" not in client.calls


@pytest.mark.asyncio
async def test_limit_is_capped_at_the_result_window(adapters, client):
    a, _ = adapters
    await a.create_data_points("Entity_name", [IndexSchema(id=uuid4(), text="east")])
    hits = await a.search("Entity_name", query_text="east", limit=50_000)
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_backend_failure_propagates(adapters, monkeypatch):
    a, _ = adapters
    await a.create_data_points("Entity_name", [IndexSchema(id=uuid4(), text="east")])
    monkeypatch.setattr(a.client, "msearch", AsyncMock(side_effect=RuntimeError("backend down")))
    with pytest.raises(RuntimeError, match="backend down"):
        await a.batch_search("Entity_name", ["east"], limit=1)
