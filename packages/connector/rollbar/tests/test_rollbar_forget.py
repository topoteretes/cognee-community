"""Forget-on-delete must remove an item's cognified graph content, not just its Data record.

Ingests two items, cognifies them with the LLM mocked, deletes every occurrence of one in
Rollbar and checks that only that item's entity leaves the graph.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from conftest import ENTITY_TOKENS
from fake_rollbar import PROJECT, TOKEN, FakeRollbar

from cognee_community_connector_rollbar import rollbar_source

DATASET = "rollbar_forget_test"
ALPHA, BRAVO = ENTITY_TOKENS


async def _graph_values() -> list[str]:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return [str(value).lower() for _, props in nodes for value in (props or {}).values()]


async def _graph_has(token: str) -> bool:
    return any(token.lower() in value for value in await _graph_values())


async def _sync(fake: FakeRollbar) -> None:
    await cognee.add(
        rollbar_source(
            PROJECT, access_token=TOKEN, http_client=fake.client(), resource_name="rb_forget"
        ),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_an_items_occurrences_forgets_its_graph_content(mocked_llm):
    fake = FakeRollbar()
    fake.add_item(f"ValueError: {ALPHA} code rejected")
    bravo = fake.add_item(f"KeyError: {BRAVO} total missing", environment="staging")

    await _sync(fake)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    # each document sits in its project's and environment's node sets
    assert f"rollbar:{PROJECT}:staging" in await _graph_values()

    # deleting every occurrence drops the item from rollbar's listing
    fake.delete_item_occurrences(bravo)
    await _sync(fake)

    assert await _graph_has(ALPHA), "the surviving item must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted item's entity must leave the graph"
