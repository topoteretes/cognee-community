"""Forget-on-delete must reach cognee's graph, not just the dlt rows.

Runs the real cognee pipeline (dlt merge, document ingestion, cognify) against
FakeHubSpot, with the LLM and embeddings mocked, so no token or API key is needed.
Follows the google-drive connector's forget test: each fact carries one
distinctive company name, which the mocked LLM turns into exactly one entity.

The HubSpot-specific part is the contact: her name also sits in the company's
document. HubSpot drops a deleted record's associations, so unless the connector
re-renders the company without her, her name would stay in the graph.
"""

import importlib

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_hubspot import DAY, NOW, FakeHubSpot, iso

from cognee_community_connector_hubspot import hubspot_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "hubspot_forget_test"
ALPHA = "Alphacorp"  # in a note that stays
BRAVO = "Bravocorp"  # in a note that is deleted
CHARLIE = "Charliecorp"  # a contact's surname, also listed on her company's document
TOKENS = (ALPHA, BRAVO, CHARLIE)


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity named after whichever token appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        name = next((t for t in TOKENS if text_input and t in text_input), None)
        nodes = (
            [KGNode(id=name, name=name, type="Company", description=f"{name} entity")]
            if name
            else []
        )
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


def _reset_cached_engines() -> None:
    """Drop cognee's cached engines and dlt's active pipeline, so each test starts
    from its own storage and sync state."""
    from cognee.context_global_variables import graph_db_config, vector_db_config
    from cognee.infrastructure.databases.graph.get_graph_engine import _create_graph_engine
    from cognee.infrastructure.databases.relational.create_relational_engine import (
        create_relational_engine,
    )
    from cognee.infrastructure.databases.vector.create_vector_engine import _create_vector_engine
    from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    _create_graph_engine.cache_clear()
    _create_vector_engine.cache_clear()
    create_relational_engine.cache_clear()
    get_dlt_destination.cache_clear()
    graph_db_config.set(None)
    vector_db_config.set(None)


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "databases"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("PIPELINES_DIR", str(tmp_path / "dlt" / "pipelines"))
    _reset_cached_engines()
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_a, **_k):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        vectors = [[0.0] * self.get_vector_size() for _ in text]
        try:
            # newer cognee reads the token count off the result
            from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
                EmbeddingResult,
            )
        except ImportError:
            return vectors
        return EmbeddingResult(vectors, tokens=0)

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    _reset_cached_engines()


async def _documents() -> dict[tuple[str, str], object]:
    """The dataset's Data records, keyed by (table, HubSpot id)."""
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[DATASET]
    )
    return {
        (data.system_metadata["table_name"], data.system_metadata["external_id"]): data.id
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict)
    }


async def _sync(fake, portal_id="4242", resource_name="hubspot"):
    await cognee.add(
        hubspot_source(client=fake, portal_id=portal_id, resource_name=resource_name),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_deleted_note_and_contact_leave_the_graph(clean_environment):
    fake = FakeHubSpot()
    then = NOW - 9 * DAY
    fake.put("companies", 10, then, name="Acme Corp", domain="acme.com")
    fake.put("contacts", 1, then, firstname="Jane", lastname="Doe", email="jane@acme.com")
    fake.put("contacts", 2, then, firstname="Sam", lastname=CHARLIE, email="sam@acme.com")
    fake.put("notes", 40, then, hs_note_body=f"{ALPHA} signed.", hs_timestamp=iso(then))
    fake.put("notes", 41, then, hs_note_body=f"{BRAVO} wants 15% off.", hs_timestamp=iso(then))
    fake.link("contacts", 1, "companies", 10)
    fake.link("contacts", 2, "companies", 10)
    fake.link("notes", 40, "contacts", 1)
    fake.link("notes", 41, "contacts", 1)

    await _sync(fake)
    for token in TOKENS:
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"
    before = await _documents()
    deleted = {("hubspot_notes", "41"), ("hubspot_contacts", "2")}
    assert deleted <= set(before)

    fake.archive("notes", 41, NOW - DAY)  # deleting in HubSpot archives the record
    fake.archive("contacts", 2, NOW - DAY)
    await _sync(fake)

    after = await _documents()
    assert not deleted & set(after), "the deleted note's and contact's Data records must go"
    kept = ("hubspot_notes", "40")
    assert after[kept] == before[kept], "a note that was not touched must keep its id"

    assert await _graph_has(ALPHA), "a note that was not deleted must stay"
    assert not await _graph_has(BRAVO), "a deleted note must be forgotten from the graph"
    assert not await _graph_has(CHARLIE), (
        "a deleted contact must be forgotten, including from her company's document"
    )


@pytest.mark.asyncio
async def test_two_accounts_with_their_own_resource_name_stay_apart(clean_environment):
    # Record ids are only unique inside one account: both have a note 40.
    then = NOW - 9 * DAY
    first, second = FakeHubSpot(), FakeHubSpot()
    first.put("notes", 40, then, hs_note_body=f"{ALPHA} signed.", hs_timestamp=iso(then))
    second.put("notes", 40, then, hs_note_body=f"{BRAVO} wants 15% off.", hs_timestamp=iso(then))

    await _sync(first, "1001", "hubspot_eu")
    await _sync(second, "2002", "hubspot_us")
    both = {("hubspot_eu_notes", "40"), ("hubspot_us_notes", "40")}
    assert set(await _documents()) == both

    second.archive("notes", 40, NOW - DAY)
    await _sync(second, "2002", "hubspot_us")
    await _sync(first, "1001", "hubspot_eu")

    assert set(await _documents()) == {("hubspot_eu_notes", "40")}
    assert await _graph_has(ALPHA), "deleting one account's note 40 must not touch the other's"
    assert not await _graph_has(BRAVO)
