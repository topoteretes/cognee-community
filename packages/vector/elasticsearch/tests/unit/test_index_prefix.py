"""An index prefix bounds what prune, tag removal and listing touch, even when prefixes nest."""

from uuid import uuid4

import pytest
from cognee_community_vector_adapter_elasticsearch.elasticsearch_adapter import IndexSchema
from fake_elasticsearch import FakeAsyncElasticsearch, make_adapters


class Embeddings:
    async def embed_text(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def get_vector_size(self):
        return 2

    def get_batch_size(self):
        return 16


@pytest.fixture
def client():
    return FakeAsyncElasticsearch()


@pytest.fixture
def prod_and_staging(client):
    # Same database_name (access control off): only the prefix keeps them apart.
    (prod,) = make_adapters(client, ("",), Embeddings, index_prefix="cognee")
    (staging,) = make_adapters(client, ("",), Embeddings, index_prefix="cognee-staging")
    return prod, staging


async def _seed(prod, staging, client):
    prod_id, staging_id = uuid4(), uuid4()
    await prod.create_data_points(
        "Entity_name", [IndexSchema(id=prod_id, text="p", belongs_to_set=["A"])]
    )
    await staging.create_data_points(
        "Entity_name", [IndexSchema(id=staging_id, text="s", belongs_to_set=["A"])]
    )
    # An index nobody manages that happens to start with the prefix.
    client.store["cognee-foo"] = {"mappings": {}, "dims": 2, "docs": {}}
    return prod_id, staging_id


@pytest.mark.asyncio
async def test_prune_leaves_a_nested_prefix_and_unmanaged_indices_alone(prod_and_staging, client):
    prod, staging = prod_and_staging
    _, staging_id = await _seed(prod, staging, client)

    await prod.prune()

    assert "cognee-entity_name" not in client.store
    assert "cognee-foo" in client.store
    assert [r.id for r in await staging.retrieve("Entity_name", [str(staging_id)])] == [staging_id]


@pytest.mark.asyncio
async def test_listing_and_detag_stay_within_the_prefix(prod_and_staging, client):
    prod, staging = prod_and_staging
    _, staging_id = await _seed(prod, staging, client)

    assert await prod.get_collection_names() == ["Entity_name"]
    assert await staging.get_collection_names() == ["Entity_name"]

    await prod.remove_belongs_to_set_tags(["A"])

    (record,) = await staging.retrieve("Entity_name", [str(staging_id)])
    assert record.payload["belongs_to_set"] == ["A"]


@pytest.mark.asyncio
async def test_delete_collection_drops_only_its_own_managed_index(prod_and_staging, client):
    prod, staging = prod_and_staging
    _, staging_id = await _seed(prod, staging, client)
    (other_dataset,) = make_adapters(client, ("other",), Embeddings, index_prefix="cognee")
    await other_dataset.create_data_points(
        "Entity_name", [IndexSchema(id=uuid4(), text="o", belongs_to_set=["A"])]
    )

    await prod.delete_collection("Entity_name")

    # The whole index goes, other datasets' rows included: it is the collection.
    assert "cognee-entity_name" not in client.store
    assert not await other_dataset.has_collection("Entity_name")
    # A nested prefix and an unmanaged index with a matching name stay.
    assert "cognee-foo" in client.store
    assert [r.id for r in await staging.retrieve("Entity_name", [str(staging_id)])] == [staging_id]


@pytest.mark.asyncio
async def test_delete_collection_ignores_missing_and_unmanaged_indices(prod_and_staging, client):
    prod, _ = prod_and_staging
    client.store["cognee-foo"] = {"mappings": {}, "dims": 2, "docs": {}}

    await prod.delete_collection("Never_created")
    await prod.delete_collection("foo")

    assert "cognee-foo" in client.store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index_options", "expected"),
    [(None, {"type": "int8_hnsw"}), ("hnsw", {"type": "hnsw"}), ("server", None)],
)
async def test_new_indices_get_the_configured_index_options(
    client, monkeypatch, index_options, expected
):
    monkeypatch.delenv("ELASTICSEARCH_INDEX_OPTIONS", raising=False)
    (adapter,) = make_adapters(client, ("",), Embeddings, index_options=index_options)

    await adapter.create_collection("Entity_name")

    vector = client.store["cognee-entity_name"]["mappings"]["properties"]["vector"]
    assert vector.get("index_options") == expected
    assert vector["similarity"] == "cosine" and vector["dims"] == 2
