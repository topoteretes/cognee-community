"""Forget-on-delete must remove a cohort's cognified graph content, not just its Data record.

Ingests two cohorts, cognifies them with the LLM mocked, archives one in Amplitude and
checks that only the archived cohort's entity leaves the graph.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from conftest import ENTITY_TOKENS
from fake_amplitude import FakeAmplitude

from cognee_community_connector_amplitude import amplitude_source

DATASET = "amplitude_forget_test"
ALPHA, BRAVO = ENTITY_TOKENS


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return any(
        token.lower() in str(value).lower()
        for _, props in nodes
        for value in (props or {}).values()
    )


async def _sync(fake: FakeAmplitude) -> None:
    await cognee.add(
        amplitude_source(service=fake, include=("cohorts",), resource_name="amplitude_forget"),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_archiving_a_cohort_forgets_its_graph_content(mocked_llm):
    fake = FakeAmplitude()
    fake.add_cohort("c1", f"{ALPHA} paying users")
    fake.add_cohort("c2", f"{BRAVO} churn risk")

    await _sync(fake)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)

    fake.cohorts["c2"]["archived"] = True
    await _sync(fake)

    assert await _graph_has(ALPHA), "the surviving cohort must stay in the graph"
    assert not await _graph_has(BRAVO), "the archived cohort's entity must leave the graph"
