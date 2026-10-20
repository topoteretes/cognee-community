"""End-to-end: Ramp changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify) against
the fake Ramp API, with a mocked LLM and mocked embeddings (see conftest.py).
Each transaction's memo names one company, which the mocked LLM turns into exactly
one graph entity.
"""

import cognee
import httpx
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_dataset_data, get_datasets_by_name
from cognee.modules.users.methods import get_default_user
from fake_ramp import FakeRamp, ago

from cognee_community_connector_ramp import RampClient, ramp_source

DATASET = "ramp_forget_test"


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


async def _documents() -> set[str]:
    """External ids of the documents in the dataset, from the relational store."""
    user = await get_default_user()
    dataset = (await get_datasets_by_name([DATASET], user.id))[0]
    return {
        (data.system_metadata or {}).get("external_id")
        for data in await get_dataset_data(dataset.id)
    }


async def _sync(fake: FakeRamp) -> None:
    client = RampClient("client", "secret", http=fake.client(), sleep=lambda _s: None)
    await cognee.remember(
        ramp_source(resource_name="ramp_test", service=client),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        self_improvement=False,
    )


@pytest.mark.asyncio
async def test_forgotten_transactions_leave_the_graph(mocked_llm):
    fake = FakeRamp()
    hold = fake.add("t1", ago(3), memo="Hotel hold for the Alphacorp visit", state="PENDING")
    fake.add("t2", ago(2), memo="Dinner with Bravocorp")

    await _sync(fake)
    assert await _graph_has("Alphacorp")
    assert await _graph_has("Bravocorp")
    assert await _documents() == {"transaction:t1", "transaction:t2"}

    # a failing API must not forget anything
    fake.queued = [httpx.Response(500, content=b"{}")] * 10
    with pytest.raises(Exception):  # noqa: B017 - cognee wraps the source error
        await _sync(fake)
    fake.queued = []
    assert await _graph_has("Alphacorp")

    # the hold expired: Ramp no longer lists or serves it
    del fake.transactions[hold["id"]]
    await _sync(fake)
    assert not await _graph_has("Alphacorp"), "an expired authorization must be forgotten"
    assert await _graph_has("Bravocorp"), "an untouched transaction must stay"
    # the document itself is removed (the _deleted hard delete), not left behind empty
    assert await _documents() == {"transaction:t2"}
