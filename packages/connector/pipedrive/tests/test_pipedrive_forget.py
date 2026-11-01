"""Forget-on-delete must hard-delete a note: its row, its Data record and its graph content.

Ingests two notes, cognifies them with the LLM mocked, deletes one in Pipedrive and
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
from fake_pipedrive import FakePipedrive

from cognee_community_connector_pipedrive import pipedrive_source

DATASET = "pipedrive_forget_test"
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


async def _sync(fake: FakePipedrive) -> None:
    await cognee.add(
        pipedrive_source(service=fake, include=("notes",), resource_name="pipedrive_forget"),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_a_note_forgets_its_graph_content(mocked_llm):
    fake = FakePipedrive()
    fake.add_note(1, f"<p>Call with {ALPHA} went well.</p>")
    fake.add_note(2, f"<p>{BRAVO} asked for a discount.</p>")

    await _sync(fake)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    before = await _documents()
    assert set(before) == {"note:1", "note:2"}

    del fake.notes[2]
    await _sync(fake)

    assert await _documents() == {"note:1": before["note:1"]}, (
        "the deleted note's Data record must go and the survivor must keep its id"
    )
    assert await _graph_has(ALPHA), "the surviving note must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted note's entity must leave the graph"
