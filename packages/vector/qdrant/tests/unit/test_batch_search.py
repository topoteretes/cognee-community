"""Batch retrieval parity against Qdrant's real local engine."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cognee_community_vector_adapter_qdrant.qdrant_adapter import IndexSchema, QDrantAdapter


class Embeddings:
    async def embed_text(self, texts):
        vectors = {"east": [1.0, 0.0], "north": [0.0, 1.0], "west": [-1.0, 0.0]}
        return [vectors[text] for text in texts]

    def get_vector_size(self):
        return 2


@pytest.fixture
def adapters(tmp_path):
    return [
        QDrantAdapter(
            url=None,
            api_key=None,
            embedding_engine=Embeddings(),
            qdrant_path=str(tmp_path / "qdrant"),
            database_name=dataset,
        )
        for dataset in ("a", "b")
    ]


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
        [
            IndexSchema(id=uuid4(), text="west", belongs_to_set=["red", "blue"]),
        ],
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
    # The west query has no similarity above 0.9, but its matches must remain.
    assert all(hit.score >= 1 for hit in actual[1])


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
async def test_empty_queries_skip_client_and_embeddings(adapters, monkeypatch):
    a, _ = adapters

    def unexpected_client():
        pytest.fail("empty queries should not open a client")

    monkeypatch.setattr(a, "get_qdrant_client", unexpected_client)
    assert await a.batch_search("missing", []) == []


@pytest.mark.asyncio
async def test_backend_failure_propagates_and_closes_client(adapters, monkeypatch):
    a, _ = adapters
    monkeypatch.setattr(a, "has_collection", AsyncMock(return_value=True))
    client = AsyncMock()
    client.query_batch_points.side_effect = RuntimeError("backend unavailable")
    monkeypatch.setattr(a, "get_qdrant_client", lambda: client)
    with pytest.raises(RuntimeError, match="backend unavailable"):
        await a.batch_search("Entity_name", ["east"], limit=1)
    client.close.assert_awaited_once()
