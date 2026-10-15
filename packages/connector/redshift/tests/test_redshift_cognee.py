"""End-to-end: Redshift changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify ->
orphan cleanup) against the SQLite-backed fake, with a mocked LLM and mocked
embeddings, so no network, AWS account or API key is needed. Each row
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
from fake_redshift import FakeRedshift

from cognee_community_connector_redshift import RedshiftTable, redshift_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "redshift_forget_test"
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"
CHARLIE = "Charliecorp"
DELTA = "Deltacorp"
TOKENS = (ALPHA, BRAVO, CHARLIE, DELTA)

ACCOUNTS = RedshiftTable("public.accounts", primary_key="id", cursor_column="updated_at")
VENDORS = RedshiftTable("public.vendors", primary_key="id")


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
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


@pytest.fixture
def fake(tmp_path):
    fake = FakeRedshift(tmp_path)
    fake.create_table(
        "accounts",
        {"id": "INTEGER", "name": "TEXT", "notes": "TEXT", "updated_at": "TIMESTAMP"},
        comment="Customer accounts.",
    )
    fake.insert(
        "accounts",
        {"id": 1, "name": ALPHA, "notes": "Renews in March.", "updated_at": "2026-01-01"},
        {"id": 2, "name": BRAVO, "notes": "Churn risk.", "updated_at": "2026-01-01"},
    )
    fake.create_table("vendors", {"id": "INTEGER", "name": "TEXT"})
    fake.insert("vendors", {"id": 1, "name": CHARLIE})
    return fake


async def _sync(fake, selections):
    await cognee.add(
        redshift_source(selections, connection=fake.connection()),
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
async def test_rows_are_ingested_and_deleted_rows_are_forgotten(clean_environment, fake):
    await _sync(fake, [ACCOUNTS, VENDORS])

    for token in (ALPHA, BRAVO, CHARLIE):
        assert await _graph_has(token), f"{token} should be in the graph after the first sync"
    documents = await _documents()
    assert "redshift:table:public.accounts" in documents
    assert documents["redshift:public.accounts:1"]["source"] == "redshift"
    assert documents["redshift:public.accounts:1"]["table_name"] == "redshift_public_accounts"

    # Upstream: one account deleted, one edited, one vendor added.
    fake.delete("accounts", "id", 2)
    fake.update("accounts", "id", 1, name=DELTA, updated_at="2026-02-01")
    await _sync(fake, [ACCOUNTS, VENDORS])

    assert not await _graph_has(BRAVO), "a deleted row must be forgotten from the graph"
    assert await _graph_has(DELTA), "an edited row must be re-ingested"
    assert await _graph_has(CHARLIE), "an untouched table must stay"
    assert "redshift:public.accounts:2" not in await _documents()


@pytest.mark.asyncio
async def test_dropped_table_is_forgotten_but_other_tables_stay(clean_environment, fake):
    await _sync(fake, [ACCOUNTS, VENDORS])
    assert await _graph_has(CHARLIE)

    fake.drop_table("vendors")
    await _sync(fake, [ACCOUNTS, VENDORS])

    assert not await _graph_has(CHARLIE), "rows of a dropped table must be forgotten"
    assert await _graph_has(ALPHA), "another table's rows must stay"
    documents = await _documents()
    assert "redshift:table:public.vendors" not in documents
    assert "redshift:table:public.accounts" in documents


@pytest.mark.asyncio
async def test_rows_and_their_table_card_share_a_node_set(clean_environment, fake):
    await _sync(fake, [ACCOUNTS])

    nodes, _ = await (await get_graph_engine()).get_graph_data()
    node_sets = {props.get("name") for _id, props in nodes if props.get("type") == "NodeSet"}
    assert "redshift:table:public.accounts" in node_sets
