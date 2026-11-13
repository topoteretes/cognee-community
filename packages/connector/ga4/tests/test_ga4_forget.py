"""Forget-on-delete must remove an audience's cognified graph content, not just its Data record.

Ingests two audiences, cognifies them with the LLM mocked, archives one in GA4 and
checks that only the archived audience's entity leaves the graph.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from conftest import ENTITY_TOKENS
from fake_ga4 import PROPERTY, FakeGA4

from cognee_community_connector_ga4 import ga4_source

DATASET = "ga4_forget_test"
ALPHA, BRAVO = ENTITY_TOKENS


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return any(
        token.lower() in str(value).lower()
        for _, props in nodes
        for value in (props or {}).values()
    )


async def _sync(fake: FakeGA4) -> None:
    await cognee.add(
        ga4_source(PROPERTY, service=fake, include=("audiences",), resource_name="ga4_forget"),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_archiving_an_audience_forgets_its_graph_content(mocked_llm):
    fake = FakeGA4()
    fake.add_audience("41", f"{ALPHA} purchasers", "Users who bought something.")
    fake.add_audience("42", f"{BRAVO} churn risk", "Users who stopped visiting.")

    await _sync(fake)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)

    # an archived audience drops out of the listing
    del fake.records["audiences"]["42"]
    await _sync(fake)

    assert await _graph_has(ALPHA), "the surviving audience must stay in the graph"
    assert not await _graph_has(BRAVO), "the archived audience's entity must leave the graph"
