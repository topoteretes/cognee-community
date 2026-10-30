"""Forget-on-delete through the real cognee pipeline, with the Attio API faked.

Ingests two people, cognifies them with the LLM mocked, deletes one in Attio and checks that
only the deleted person leaves every store (its Data record and its graph content), while the
survivor keeps its id. This is the end-to-end form of the snapshot model: nothing in the
connector deletes anything, a record that stops being listed is reconciled out by cognee.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_attio import FakeAttio, attribute, name, record, text

from cognee_community_connector_attio import attio_source

DATASET = "attio_forget_test"
ALPHA, BRAVO = ENTITY_TOKENS


def _workspace() -> FakeAttio:
    fake = FakeAttio()
    fake.add_object(
        "people",
        "Person",
        [attribute("name", "personal-name", "Name"), attribute("job_title", "text", "Job title")],
    )
    fake.records["people"] = [
        record("people", "p1", {"name": [name("Ada", ALPHA)], "job_title": [text("Engineer")]}),
        record("people", "p2", {"name": [name("Ben", BRAVO)], "job_title": [text("Analyst")]}),
    ]
    return fake


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
    if not datasets:
        return {}
    return {
        data.system_metadata["external_id"]: data.id
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict)
    }


async def _sync(fake: FakeAttio) -> None:
    await cognee.add(attio_source(client=fake), dataset_name=DATASET)
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_a_record_forgets_its_graph_content(mocked_llm):
    fake = _workspace()

    await _sync(fake)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    before = await _documents()
    assert set(before) == {"people:p1", "people:p2"}

    del fake.records["people"][1]
    await _sync(fake)

    assert await _documents() == {"people:p1": before["people:p1"]}, (
        "the deleted record's Data must go and the survivor must keep its id"
    )
    assert await _graph_has(ALPHA), "the surviving record must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted record's entity must leave the graph"


@pytest.mark.asyncio
async def test_an_unchanged_workspace_keeps_every_document_id(mocked_llm):
    fake = _workspace()

    await _sync(fake)
    first = await _documents()
    await _sync(fake)

    assert await _documents() == first, "an unchanged record must not be re-ingested"
