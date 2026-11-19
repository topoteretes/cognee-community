"""Forget-on-delete must hard-delete an activity: its row, its Data record and its graph content.

Ingests two notes, cognifies them with the LLM mocked, deletes one in Copper and
checks that only the deleted note leaves every store, rather than being replaced
by an emptied document.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_copper import FakeCopper

from cognee_community_connector_copper import copper_source

DATASET = "copper_forget_test"
ALPHA, BRAVO = ENTITY_TOKENS


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return any(
        token.lower() in str(value).lower()
        for _, props in nodes
        for value in (props or {}).values()
    )


async def _documents() -> dict[str, object]:
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[DATASET]
    )
    return {
        data.system_metadata["external_id"]: data.id
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict)
    }


async def _sync(fake: FakeCopper) -> None:
    await cognee.add(
        copper_source(
            service=fake, include=("people", "activities"), resource_name="copper_forget"
        ),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_an_activity_forgets_its_graph_content(mocked_llm):
    fake = FakeCopper()
    fake.add_person(9, "Ada Lovelace")
    fake.add_activity(1, f"Call with {ALPHA} went well.", ("person", 9))
    fake.add_activity(2, f"{BRAVO} asked for a discount.", ("person", 9))

    await _sync(fake)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    before = await _documents()
    assert set(before) == {"person:9", "activity:1", "activity:2"}

    del fake.activities[2]
    await _sync(fake)

    assert await _documents() == {k: v for k, v in before.items() if k != "activity:2"}, (
        "the deleted activity's Data record must go and the survivors must keep their ids"
    )
    assert await _graph_has(ALPHA), "the surviving activity must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted activity's entity must leave the graph"
