"""Forget-on-delete must remove a person from every store, and from their relatives' documents.

Syncs a small tree through ``cognee.remember`` with the LLM mocked, deletes one person
in Gramps and checks that their document and their graph entity are gone while the
others stay under the same ids.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_gramps import FakeGramps, date
from helpers import client_for

from cognee_community_connector_gramps import gramps_source

DATASET = "gramps_forget_test"
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


async def _sync(fake: FakeGramps) -> dict:
    source = gramps_source(client=client_for(fake), resource_name="gramps_forget")
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    return source.cognee_sync_stats


@pytest.mark.asyncio
async def test_deleting_a_person_forgets_them_everywhere(mocked_llm):
    fake = FakeGramps()
    fake.add_event("e1", "Birth", date(1900))
    fake.add_person("p1", "Ada", ALPHA, events=["e1"], birth=0)
    fake.add_person("p2", "Ben", BRAVO)

    stats = await _sync(fake)
    assert stats["synced"] == 2
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    before = await _documents()
    assert set(before) == {"gramps:person:p1", "gramps:person:p2"}

    fake.delete("people", "p2")
    stats = await _sync(fake)
    assert stats["deleted"] == 1

    assert await _documents() == {"gramps:person:p1": before["gramps:person:p1"]}, (
        "the deleted person's Data record must go and the survivor must keep its id"
    )
    assert await _graph_has(ALPHA)
    assert not await _graph_has(BRAVO), "the deleted person's entity must leave the graph"
