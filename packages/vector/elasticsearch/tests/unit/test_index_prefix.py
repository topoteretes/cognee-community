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
