"""Forget-on-delete must hard-delete a page: its row, its Data record and its graph content.

Ingests two pages, cognifies them with the LLM mocked, deletes one in Moodle and checks
that only the deleted page leaves every store, rather than being replaced by an emptied
document.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_moodle import FakeMoodle

from cognee_community_connector_moodle import moodle_source

DATASET = "moodle_forget_test"
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


async def _sync(fake: FakeMoodle) -> None:
    await cognee.add(
        moodle_source(service=fake, include=("pages",), resource_name="moodle_forget"),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_a_page_forgets_its_graph_content(mocked_llm):
    fake = FakeMoodle()
    fake.add_course(7, "Algorithms")
    keep = fake.add_page(7, "Guest lecture", f"<p>{ALPHA} spoke about sorting.</p>")
    gone = fake.add_page(7, "Office hours", f"<p>{BRAVO} holds office hours on Tuesdays.</p>")

    await _sync(fake)
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    before = await _documents()
    section = f"section:7:{fake.courses[7]['sections'][0]['id']}"
    assert set(before) == {
        "course:7",
        section,
        f"page:7:{keep['coursemodule']}",
        f"page:7:{gone['coursemodule']}",
    }

    fake.remove(7, gone["coursemodule"])
    await _sync(fake)

    after = await _documents()
    assert f"page:7:{gone['coursemodule']}" not in after, "the deleted page's Data record must go"
    assert after["course:7"] == before["course:7"]
    assert after[f"page:7:{keep['coursemodule']}"] == before[f"page:7:{keep['coursemodule']}"], (
        "the surviving page must keep its id"
    )
    assert await _graph_has(ALPHA), "the surviving page must stay in the graph"
    assert not await _graph_has(BRAVO), "the deleted page's entity must leave the graph"
