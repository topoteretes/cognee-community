"""Forget-on-delete must hard-delete a monitor: its row, its Data record and its graph content.

Ingests two monitors, cognifies them with the LLM mocked, deletes one in Datadog and
checks that only the deleted monitor leaves every store, rather than being replaced
by an emptied document.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_datadog import FakeDatadog, client_for

from cognee_community_connector_datadog import datadog_source

DATASET = "datadog_forget_test"
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


async def _sync(fake: FakeDatadog) -> None:
    await cognee.add(
        datadog_source(
            service=client_for(fake), include=("monitors",), resource_name="datadog_forget"
        ),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_a_monitor_forgets_its_graph_content(mocked_llm):
    fake = FakeDatadog()
    fake.add_monitor(1, "Checkout latency", f"Escalate to {ALPHA}.")
    fake.add_monitor(2, "Disk full", f"Escalate to {BRAVO}.")

    await _sync(fake)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    before = await _documents()
    assert set(before) == {"monitor:1", "monitor:2"}

    del fake.monitors["2"]
    await _sync(fake)

    assert await _documents() == {"monitor:1": before["monitor:1"]}, (
        "the deleted monitor's Data record must go and the survivor must keep its id"
    )
    assert await _graph_has(ALPHA), "the surviving monitor must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted monitor's entity must leave the graph"
