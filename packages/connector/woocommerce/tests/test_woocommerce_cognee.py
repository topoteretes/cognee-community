"""The WooCommerce source through the real cognee pipeline, with the store faked.

``cognee.add()`` (the step ``remember()`` runs before ``cognify()``) needs no LLM,
so most tests stop at the stored Data records. The last one cognifies with the
LLM mocked and checks a deleted product leaves the graph too.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_woocommerce import FakeWooCommerce

from cognee_community_connector_woocommerce import woocommerce_source

DATASET = "woocommerce_integration_test"
ALPHA, BRAVO = ENTITY_TOKENS


def _store() -> FakeWooCommerce:
    fake = FakeWooCommerce()
    fake.add_product(10, f"Linen Shirt {ALPHA}")
    fake.add_product(11, f"Stoneware Mug {BRAVO}")
    fake.add_review(1, 10, "Runs small.")
    fake.add_order(20)
    fake.add_note(20, 1, "Ships tomorrow.", to_customer=True, author="admin")
    return fake


async def _sync(fake: FakeWooCommerce, cognify: bool = False):
    source = woocommerce_source("http://shop.test", "ck_test_key", "cs_test_secret", service=fake)
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    if cognify:
        await cognee.cognify(datasets=[DATASET])
    return source


async def _documents() -> dict[str, object]:
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[DATASET]
    )
    if not datasets:
        return {}
    return {
        data.system_metadata["external_id"]: data
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict)
        and data.system_metadata.get("source") == "woocommerce"
    }


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return any(
        token.lower() in str(value).lower()
        for _, props in nodes
        for value in (props or {}).values()
    )


@pytest.mark.asyncio
async def test_a_first_sync_stores_documents_and_a_quiet_one_changes_nothing(clean_environment):
    fake = _store()
    await _sync(fake)
    first = await _documents()
    assert set(first) == {"product:10", "product:11", "review:1", "order:20"}
    assert {d.system_metadata["table_name"] for d in first.values()} == {"woocommerce_documents"}
    assert first["product:10"].system_metadata["url"] == "http://shop.test/product/10/"

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["rendered"] == 0
    assert {k: d.id for k, d in (await _documents()).items()} == {k: d.id for k, d in first.items()}


@pytest.mark.asyncio
async def test_an_edit_replaces_only_that_document(clean_environment):
    fake = _store()
    await _sync(fake)
    before = {k: d.id for k, d in (await _documents()).items()}

    fake.edit_product(10, description="<p>Now true to size.</p>")
    await _sync(fake)
    after = {k: d.id for k, d in (await _documents()).items()}

    assert set(after) == set(before)
    assert after["product:10"] != before["product:10"]
    assert {k: v for k, v in after.items() if k != "product:10"} == {
        k: v for k, v in before.items() if k != "product:10"
    }


@pytest.mark.asyncio
async def test_trashed_and_deleted_records_are_forgotten(clean_environment):
    fake = _store()
    await _sync(fake)
    fake.products[11]["status"] = "trash"
    del fake.orders[20]
    await _sync(fake)
    assert set(await _documents()) == {"product:10", "review:1"}


@pytest.mark.asyncio
async def test_a_deleted_product_leaves_the_graph(mocked_llm):
    fake = _store()
    await _sync(fake, cognify=True)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    survivor = (await _documents())["product:10"].id

    del fake.products[11]
    await _sync(fake, cognify=True)

    documents = await _documents()
    assert "product:11" not in documents
    assert documents["product:10"].id == survivor
    assert await _graph_has(ALPHA), "the surviving product must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted product's entity must leave the graph"
