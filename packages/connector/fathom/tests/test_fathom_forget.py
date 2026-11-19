"""Forget-on-delete must hard-delete a meeting: its row, its Data record and its graph content.

Ingests two meetings, cognifies them with the LLM mocked, deletes one in Fathom and checks
that only the deleted meeting leaves every store, rather than being replaced by an
emptied document.
"""

import cognee
import pytest
import responses
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_fathom import NOW, FakeFathom

from cognee_community_connector_fathom import FathomClient, fathom_source
from cognee_community_connector_fathom import fathom as fathom_module

DATASET = "fathom_forget_test"
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


async def _sync(fake: FakeFathom) -> None:
    source = fathom_source(
        client=FathomClient("key", sleep=lambda _seconds: None),
        include_action_items=False,
        resource_name="fathom_forget",
    )
    # only the Fathom calls are faked: cognify must still reach its own (local) resources
    with responses.RequestsMock() as rsps:
        fake.install(rsps)
        await cognee.add(source, dataset_name=DATASET, primary_key="id", write_disposition="merge")
    await cognee.cognify(datasets=[DATASET])


def _summary(text: str) -> dict:
    return {"default_summary": {"markdown_formatted": text}}


@pytest.mark.asyncio
async def test_deleting_a_meeting_forgets_its_graph_content(mocked_llm, monkeypatch):
    monkeypatch.setattr(fathom_module, "_now", lambda: NOW)
    fake = FakeFathom()
    fake.add(1, days_ago=60, **_summary(f"{ALPHA} agreed a ten percent discount."))
    fake.add(2, days_ago=50, **_summary(f"{BRAVO} asked for annual billing."))

    await _sync(fake)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    before = await _documents()
    assert set(before) == {"1", "2"}

    fake.remove(2)
    await _sync(fake)

    assert await _documents() == {"1": before["1"]}, (
        "the deleted meeting's Data record must go and the survivor must keep its id"
    )
    assert await _graph_has(ALPHA), "the surviving meeting must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted meeting's entity must leave the graph"
