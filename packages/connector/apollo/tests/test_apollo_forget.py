"""Forget-on-delete must hard-delete a contact: its row, its Data record and its graph content.

Ingests two contacts, cognifies them with the LLM mocked, deletes one in Apollo and
checks that only the deleted contact leaves every store, rather than being replaced
by an emptied document.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_apollo import FakeApollo

from cognee_community_connector_apollo import apollo_source

DATASET = "apollo_forget_test"
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


async def _sync(fake: FakeApollo) -> None:
    await cognee.add(
        apollo_source(service=fake, include=("contacts",), resource_name="apollo_forget"),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_a_contact_forgets_its_graph_content(mocked_llm):
    fake = FakeApollo()
    fake.add_contact("c1", f"Ada {ALPHA}")
    fake.add_contact("c2", f"Ben {BRAVO}")

    await _sync(fake)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    before = await _documents()
    assert set(before) == {"contact:c1", "contact:c2"}

    del fake.contacts["c2"]
    await _sync(fake)

    assert await _documents() == {"contact:c1": before["contact:c1"]}, (
        "the deleted contact's Data record must go and the survivor must keep its id"
    )
    assert await _graph_has(ALPHA), "the surviving contact must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted contact's entity must leave the graph"
