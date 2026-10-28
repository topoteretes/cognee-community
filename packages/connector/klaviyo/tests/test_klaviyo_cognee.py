"""The Klaviyo source through the real cognee pipeline, with the account faked.

``cognee.add()`` (the step ``remember()`` runs before ``cognify()``) needs no LLM,
so most tests stop at the stored Data records. The last one cognifies with the
LLM mocked and checks a deleted campaign leaves the graph too.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_klaviyo import FakeKlaviyo, email_step

from cognee_community_connector_klaviyo import klaviyo_source

DATASET = "klaviyo_integration_test"
ALPHA, BRAVO = ENTITY_TOKENS
KEY = "pk_test_0123456789abcdef"


def _account() -> FakeKlaviyo:
    fake = FakeKlaviyo()
    fake.add_template("T1", "Welcome", html="<p>Welcome aboard.</p>")
    fake.add_template("T2", "Launch", text="Meet the new collection.")
    fake.add_campaign("C1", f"Spring launch {ALPHA}", subject="It's here", template_id="T2")
    fake.add_campaign("C2", f"Winter clearance {BRAVO}", subject="Last chance", template_id="T2")
    fake.add_flow("F1", "Welcome series", [email_step("a1", "Welcome", "Hi there", "T1")])
    return fake


async def _sync(fake: FakeKlaviyo, cognify: bool = False):
    source = klaviyo_source(api_key=KEY, resources=["campaigns", "flows"], service=fake)
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
        and data.system_metadata.get("source") == "klaviyo"
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
    fake = _account()
    await _sync(fake)
    first = await _documents()
    assert set(first) == {"campaign:C1", "campaign:C2", "flow:F1"}
    assert {d.system_metadata["table_name"] for d in first.values()} == {"klaviyo_documents"}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["rendered"] == 0
    assert {k: d.id for k, d in (await _documents()).items()} == {k: d.id for k, d in first.items()}


@pytest.mark.asyncio
async def test_a_template_edit_replaces_only_the_documents_that_use_it(clean_environment):
    fake = _account()
    await _sync(fake)
    before = {k: d.id for k, d in (await _documents()).items()}

    fake.edit_template("T1", html="<p>Welcome aboard, now with free shipping.</p>")
    await _sync(fake)
    after = {k: d.id for k, d in (await _documents()).items()}

    assert set(after) == set(before)
    assert after["flow:F1"] != before["flow:F1"]
    assert {k: v for k, v in after.items() if k != "flow:F1"} == {
        k: v for k, v in before.items() if k != "flow:F1"
    }


@pytest.mark.asyncio
async def test_deleted_and_archived_records_are_forgotten(clean_environment):
    fake = _account()
    await _sync(fake)
    fake.delete_campaign("C2")
    fake.edit_flow("F1", archived=True)
    await _sync(fake)
    assert set(await _documents()) == {"campaign:C1"}


@pytest.mark.asyncio
async def test_a_deleted_campaign_leaves_the_graph(mocked_llm):
    fake = _account()
    await _sync(fake, cognify=True)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    survivor = (await _documents())["campaign:C1"].id

    fake.delete_campaign("C2")
    await _sync(fake, cognify=True)

    documents = await _documents()
    assert "campaign:C2" not in documents
    assert documents["campaign:C1"].id == survivor
    assert await _graph_has(ALPHA), "the surviving campaign must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted campaign's entity must leave the graph"
