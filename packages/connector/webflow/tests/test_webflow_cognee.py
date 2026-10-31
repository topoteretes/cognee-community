"""End-to-end: Webflow changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify ->
orphan cleanup) against the fake Webflow API, with a mocked LLM and mocked
embeddings, so no network, Webflow account or API key is needed. Each item
carries one distinctive company name, which the mocked LLM turns into exactly
one graph entity.
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
from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination
from fake_webflow import TOKEN, FakeWebflow

from cognee_community_connector_webflow import WebflowClient, webflow_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "webflow_forget_test"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"
DELTA = "Deltacorp"
ECHO = "Echocorp"
TOKENS = (ALPHA, BRAVO, CHARLIE, DELTA, ECHO)

FIELDS = [
    {"slug": "name", "displayName": "Name", "type": "PlainText"},
    {"slug": "body", "displayName": "Body", "type": "PlainText"},
]

# Chunks the mocked LLM extracted a graph from, in call order.
EXTRACTED: list[str] = []


def _with_required(model, **values):
    """Build a cognee data model, filling any other required text field.

    cognee swaps in Node and KnowledgeGraph models with extra required fields
    when the configured LLM provider is Gemini, so the mock must work with
    both variants.
    """
    for field_name, field in model.model_fields.items():
        if field.is_required() and field_name not in values:
            values[field_name] = "mock"
    return model(**values)


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
        EXTRACTED.append(text_input or "")
        name = next((t for t in TOKENS if text_input and t in text_input), None)
        nodes = (
            [
                _with_required(
                    KGNode, id=name, name=name, type="Company", description=f"{name} entity"
                )
            ]
            if name
            else []
        )
        return _with_required(KnowledgeGraph, nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    # dlt keeps pipeline state (the cursors) outside cognee's directories.
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    # The dlt staging destination is cached per process; without this a test
    # would keep loading into the previous test's (deleted) system directory.
    get_dlt_destination.cache_clear()

    async def _noop_index(*_a, **_k):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    EXTRACTED.clear()
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


@pytest.fixture
def fake():
    fake = FakeWebflow()
    fake.site = fake.add_site("Acme", "acme")
    fake.posts = fake.add_collection(fake.site, "posts", "Posts", "Post", FIELDS)
    fake.alpha = fake.add_item(fake.posts, ALPHA, body="Renews in March.")
    fake.bravo = fake.add_item(fake.posts, BRAVO, body="Churn risk.")
    fake.docs = fake.add_collection(fake.site, "docs", "Docs", "Doc", FIELDS)
    fake.charlie = fake.add_item(fake.docs, CHARLIE, body="Partner.")
    return fake


async def _sync(fake, **options):
    client = WebflowClient(TOKEN, transport=fake.transport(), sleep=lambda _s: None)
    await cognee.add(
        webflow_source(client=client, **options),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


async def _documents() -> dict[str, dict]:
    from cognee.modules.data.methods import get_dataset_data, get_datasets_by_name
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    (dataset,) = await get_datasets_by_name(DATASET, user.id)
    return {
        data.system_metadata.get("external_id"): data.system_metadata
        for data in await get_dataset_data(dataset.id)
    }


@pytest.mark.asyncio
async def test_items_are_ingested_and_deleted_items_are_forgotten(clean_environment, fake):
    await _sync(fake)

    for token in (ALPHA, BRAVO, CHARLIE):
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"
    documents = await _documents()
    assert f"webflow:collection:{fake.posts}" in documents
    assert documents[f"webflow:item:{fake.alpha}"]["source"] == "webflow"
    assert documents[f"webflow:item:{fake.alpha}"]["table_name"] == f"webflow_items_{fake.posts}"

    # Upstream: one post deleted, one edited, one added.
    fake.delete_item(fake.posts, fake.bravo)
    fake.edit_item(fake.posts, fake.alpha, name=DELTA)
    fake.add_item(fake.posts, ECHO, body="New.")
    await _sync(fake)

    assert not await _graph_has(BRAVO), "a deleted item must be forgotten from the graph"
    assert await _graph_has(DELTA), "an edited item must be re-ingested"
    assert await _graph_has(ECHO), "a new item must be ingested"
    assert await _graph_has(CHARLIE), "an untouched collection must stay"
    assert f"webflow:item:{fake.bravo}" not in await _documents()


@pytest.mark.asyncio
async def test_unpublishing_an_item_forgets_it(clean_environment, fake):
    await _sync(fake)
    assert await _graph_has(BRAVO)

    fake.unpublish_item(fake.posts, fake.bravo)
    await _sync(fake)

    assert not await _graph_has(BRAVO)
    assert await _graph_has(ALPHA)


@pytest.mark.asyncio
async def test_a_deleted_collection_is_forgotten_but_others_stay(clean_environment, fake):
    await _sync(fake)
    assert await _graph_has(CHARLIE)

    fake.delete_collection(fake.docs)
    await _sync(fake)

    assert not await _graph_has(CHARLIE), "items of a deleted collection must be forgotten"
    assert await _graph_has(ALPHA), "another collection's items must stay"
    documents = await _documents()
    assert f"webflow:collection:{fake.docs}" not in documents
    assert f"webflow:collection:{fake.posts}" in documents


@pytest.mark.asyncio
async def test_items_and_their_collection_share_a_node_set(clean_environment, fake):
    await _sync(fake, collections=["posts"])

    nodes, _ = await (await get_graph_engine()).get_graph_data()
    node_sets = {props.get("name") for _id, props in nodes if props.get("type") == "NodeSet"}
    assert "webflow:acme/posts" in node_sets


@pytest.mark.asyncio
async def test_unchanged_resync_does_not_call_the_llm_again(clean_environment, fake):
    await _sync(fake)
    assert EXTRACTED, "the first sync should extract a graph"

    EXTRACTED.clear()
    await _sync(fake)
    assert EXTRACTED == [], "unchanged items must not be cognified again"

    # A save that changes no text bumps lastUpdated: the item is sent again,
    # but its document is identical, so cognee's content hash skips the LLM.
    fake.touch_item(fake.posts, fake.alpha)
    await _sync(fake)
    assert EXTRACTED == [], "an item whose text did not change must not be cognified again"

    fake.edit_item(fake.posts, fake.alpha, body="Renewed early.")
    await _sync(fake)
    assert len(EXTRACTED) == 1 and "Renewed early." in EXTRACTED[0]
