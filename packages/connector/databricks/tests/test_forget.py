"""End-to-end: Databricks changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge, document ingestion, cognify) against
the fake workspace, with a mocked LLM and mocked embeddings, so no network or
API key is needed. Each notebook and table carries one distinctive company
name, which the mocked LLM turns into exactly one graph entity.

A deleted notebook or dropped table must leave every store: its entity leaves
the graph and its Data record is removed, rather than replaced by an emptied
document, while the documents that stay keep their Data records.
"""

import importlib

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)

try:
    from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
        EmbeddingResult,
    )
except ImportError:  # cognee before 1.6.5 returns plain lists
    EmbeddingResult = None
from cognee.infrastructure.llm import LLMGateway
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_databricks import HOME, FakeWorkspace, source

from cognee_community_connector_databricks import databricks_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "databricks_forget_test"
ROOT, SCHEMA = f"{HOME}/analytics", "workspace.sales"
ORDERS, CUSTOMERS, REGIONS = (f"{SCHEMA}.{name}" for name in ("orders", "customers", "regions"))
ALPHA, BRAVO, CHARLIE, DELTA, ECHO, FOXTROT = (
    "Alphacorp",
    "Bravocorp",
    "Charliecorp",
    "Deltacorp",
    "Echocorp",
    "Foxtrotcorp",
)
TOKENS = (ALPHA, BRAVO, CHARLIE, DELTA, ECHO, FOXTROT)


def _with_required(model, **values):
    """Build a cognee data model, filling any other required text field.

    cognee swaps in Node and KnowledgeGraph models with extra required fields
    when the configured LLM provider is Gemini, so the mock works with both.
    """
    for field_name, field in model.model_fields.items():
        if field.is_required() and field_name not in values:
            values[field_name] = "mock"
    return model(**values)


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity per company name that appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        names = [t for t in TOKENS if text_input and t in text_input]
        nodes = [
            _with_required(KGNode, id=name, name=name, type="Company", description=name)
            for name in names
        ]
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
    # dlt keeps each pipeline's state under ~/.dlt by default, which would carry
    # the connector's saved stamps from one test (and one test run) to the next.
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
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
        # From cognee 1.6.5 an engine returns its vectors with the token count.
        return EmbeddingResult(vectors, tokens=0) if EmbeddingResult else vectors

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


def _workspace():
    fake = FakeWorkspace()
    fake.notebook(f"{ROOT}/build_revenue", source(f"# MAGIC %md\n# MAGIC Revenue for {ALPHA}"))
    fake.notebook(f"{ROOT}/report", source(f"# MAGIC %md\n# MAGIC Weekly report for {BRAVO}"))
    fake.schema(SCHEMA, "Sales")
    fake.table(ORDERS, [("order_id", "bigint", "")], comment=f"Orders from {CHARLIE}")
    fake.table(CUSTOMERS, [("customer_id", "bigint", "")], comment=f"Customers like {DELTA}")
    fake.table(REGIONS, [("region", "string", "")], comment=f"Regions of {FOXTROT}")
    fake.ran(f"{ROOT}/build_revenue", reads=[ORDERS])
    return fake


def _ids(fake):
    notebook = {
        p.rsplit("/", 1)[1]: o["object_id"]
        for p, o in fake.objects.items()
        if o["object_type"] == "NOTEBOOK"
    }
    return (
        {name: f"databricks:notebook:{i}" for name, i in notebook.items()},
        {name: f"databricks:table:{t['table_id']}" for name, t in fake.tables.items()},
    )


async def _sync(fake):
    resource = databricks_source(paths=[ROOT], tables=[SCHEMA], client=fake)
    await cognee.add(
        resource,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])
    return resource


async def _in_graph(*tokens):
    return {token: await _graph_has(token) for token in tokens}


async def _documents() -> dict[str, object]:
    """The connector's document id -> cognee's Data record id, for the dataset."""
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[DATASET]
    )
    return {
        data.system_metadata["external_id"]: data.id
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict)
    }


# One test with several syncs, like the Google Calendar connector's: cognee
# caches its database engines per process, so a second test in the same
# process would still talk to the first test's (already removed) databases.
@pytest.mark.asyncio
async def test_databricks_changes_reach_the_graph(clean_environment):
    fake = _workspace()
    notebooks, tables = _ids(fake)

    # Run 1: every notebook and table comes in.
    await _sync(fake)
    assert await _in_graph(ALPHA, BRAVO, CHARLIE, DELTA, FOXTROT) == dict.fromkeys(
        (ALPHA, BRAVO, CHARLIE, DELTA, FOXTROT), True
    )
    before = await _documents()
    assert set(before) == {*notebooks.values(), *tables.values()}

    # Run 2: a notebook deleted, a table dropped, and a notebook edited.
    fake.delete(f"{ROOT}/report")
    fake.drop(CUSTOMERS)
    fake.edit(f"{ROOT}/build_revenue", source(f"# MAGIC %md\n# MAGIC Revenue for {ECHO}"))
    resource = await _sync(fake)
    assert await _in_graph(ALPHA, BRAVO, CHARLIE, DELTA, ECHO, FOXTROT) == {
        ALPHA: False,  # the old text of the edited notebook is gone
        BRAVO: False,  # deleted notebook is forgotten
        CHARLIE: True,
        DELTA: False,  # dropped table is forgotten
        ECHO: True,  # the new text reached the graph
        FOXTROT: True,
    }
    assert resource.cognee_sync_stats == {
        "notebooks": 1,
        "notebooks_changed": 1,
        "notebooks_deleted": 1,
        "tables": 2,
        "tables_deleted": 1,
    }
    # The Data records of what is gone are removed, not emptied, and the
    # untouched documents keep theirs.
    after = await _documents()
    assert set(after) == set(before) - {notebooks["report"], tables[CUSTOMERS]}
    assert after[tables[REGIONS]] == before[tables[REGIONS]]
    assert after[tables[ORDERS]] == before[tables[ORDERS]]

    # Run 3: a sync where a dropped table is the only change. The notebook that
    # read it is sent again too, since its "Reads tables" line came from the
    # dropped table's lineage.
    fake.drop(ORDERS)
    resource = await _sync(fake)
    assert await _in_graph(CHARLIE, ECHO, FOXTROT) == {CHARLIE: False, ECHO: True, FOXTROT: True}
    assert resource.cognee_sync_stats == {
        "notebooks": 1,
        "notebooks_changed": 1,
        "tables": 1,
        "tables_deleted": 1,
    }
    assert set(await _documents()) == set(after) - {tables[ORDERS]}
