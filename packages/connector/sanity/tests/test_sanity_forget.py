"""End-to-end: Sanity changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify) against
the fake dataset, with a mocked LLM and mocked embeddings, so no network or API
key is needed.  Each document carries one distinctive word, which the mocked LLM
turns into exactly one graph entity.
"""

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_sanity import FakeSanity, block, ref

from cognee_community_connector_sanity import sanity_source

DATASET = "sanity_forget_test"
ALPHA, BRAVO, CHARLIE = ENTITY_TOKENS


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


async def _sync(fake: FakeSanity) -> None:
    await cognee.add(
        sanity_source("proj1", "production", ["article"], client=fake),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleting_an_article_forgets_it_and_a_renamed_author_is_re_rendered(mocked_llm):
    fake = FakeSanity()
    fake.put("author-1", "author", name=f"Ada {CHARLIE}")
    fake.put(
        "a1",
        "article",
        title="Billing",
        author=ref("author-1"),
        body=[block(f"{ALPHA} owns billing.")],
    )
    fake.put("a2", "article", title="Rockets", body=[block(f"{BRAVO} builds rockets.")])

    await _sync(fake)
    assert await _graph_has(ALPHA) and await _graph_has(BRAVO)
    assert await _graph_has(CHARLIE), "the author's name reaches the graph through the article"
    before = await _documents()
    assert set(before) == {"sanity:proj1:production:a1", "sanity:proj1:production:a2"}

    fake.delete("a2")
    fake.patch("author-1", name="Ada Lovelace")
    await _sync(fake)

    after = await _documents()
    assert set(after) == {"sanity:proj1:production:a1"}, (
        "the deleted article's Data record goes, and the re-rendered one is not duplicated"
    )
    assert await _graph_has(ALPHA), "the surviving article stays in the graph"
    assert not await _graph_has(BRAVO), "the deleted article's entity leaves the graph"
    assert not await _graph_has(CHARLIE), "the old author name leaves with the re-render"
