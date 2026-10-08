"""Forget-on-delete must remove a contact's cognified graph content, not just its Data record.

Ingests two contacts, cognifies them with the LLM mocked, deletes one in Apollo and
checks that only the deleted contact's entity leaves the graph.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
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

    del fake.contacts["c2"]
    await _sync(fake)

    assert await _graph_has(ALPHA), "the surviving contact must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted contact's entity must leave the graph"
