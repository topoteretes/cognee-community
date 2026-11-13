"""End-to-end: Snowflake changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge, document ingestion, cognify) against
the fake Snowflake account, with a mocked LLM and mocked embeddings, so no
network or API key is needed. Each row carries one distinctive company name,
which the mocked LLM turns into exactly one graph entity.

A deleted row must leave every store: its entity leaves the graph and its Data
record is removed, rather than replaced by an emptied document, while the rows
that stay keep their Data records.

CUSTOMERS has no change tracking, so a change there means reading the table
and comparing keys; ORDERS has change tracking, so only its change feed is read.
Both paths are exercised.
"""

import datetime
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
from fake_snowflake import FakeAccount

from cognee_community_connector_snowflake import snowflake_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "snowflake_forget_test"
TABLES = ["SALES_DB.SALES.CUSTOMERS", "SALES_DB.SALES.ORDERS"]
ALPHA, BRAVO, CHARLIE, DELTA, ECHO = (
    "Alphacorp",
    "Bravocorp",
    "Charliecorp",
    "Deltacorp",
    "Echocorp",
)
TOKENS = (ALPHA, BRAVO, CHARLIE, DELTA, ECHO)
CUSTOMER_1, CUSTOMER_2 = (f"snowflake:row:SALES_DB.SALES.CUSTOMERS:{key}" for key in (1, 2))
ORDER_101, ORDER_102 = (f"snowflake:row:SALES_DB.SALES.ORDERS:{key}" for key in (101, 102))
TABLE_DOCUMENTS = {f"snowflake:table:{table}" for table in TABLES}
WHEN = datetime.datetime(2026, 10, 20, 9, 0)


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


def _account():
    account = FakeAccount()
    account.insert("CUSTOMERS", CUSTOMER_ID=1, NAME=ALPHA)
    account.insert("CUSTOMERS", CUSTOMER_ID=2, NAME=BRAVO)
    account.insert(
        "ORDERS", ORDER_ID=101, CUSTOMER_ID=1, STATUS=f"{CHARLIE} license", UPDATED_AT=WHEN
    )
    account.insert("ORDERS", ORDER_ID=102, CUSTOMER_ID=1, STATUS=f"{DELTA} audit", UPDATED_AT=WHEN)
    return account


async def _sync(source):
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[DATASET])


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
async def test_snowflake_changes_reach_the_graph(clean_environment):
    account = _account()

    # Run 1: every row comes in.
    source = snowflake_source(TABLES, client=account)
    await _sync(source)
    assert await _in_graph(ALPHA, BRAVO, CHARLIE, DELTA) == dict.fromkeys(
        (ALPHA, BRAVO, CHARLIE, DELTA), True
    )
    before = await _documents()
    assert set(before) == {CUSTOMER_1, CUSTOMER_2, ORDER_101, ORDER_102} | TABLE_DOCUMENTS

    # Run 2: a customer deleted (read in full, compared by key) and an order
    # updated (read from the change feed).
    account.delete("CUSTOMERS", (2,))
    account.update("ORDERS", (101,), STATUS=f"{ECHO} license")
    source = snowflake_source(TABLES, client=account)
    await _sync(source)
    assert await _in_graph(ALPHA, BRAVO, CHARLIE, DELTA, ECHO) == {
        ALPHA: True,  # untouched row stays
        BRAVO: False,  # deleted customer is forgotten
        CHARLIE: False,  # the old value of the updated order is gone
        DELTA: True,
        ECHO: True,  # the new value reached the graph
    }
    assert source.cognee_sync_stats == {"rows": 2, "deleted": 1, "from_changes": 1}
    # The deleted customer's Data record is gone, not emptied, and the
    # untouched rows keep theirs.
    after = await _documents()
    assert set(after) == set(before) - {CUSTOMER_2}
    assert after[CUSTOMER_1] == before[CUSTOMER_1]
    assert after[ORDER_102] == before[ORDER_102]

    # Run 3: a sync where a deletion is the only change.
    account.delete("ORDERS", (102,))
    source = snowflake_source(TABLES, client=account)
    await _sync(source)
    assert await _in_graph(ALPHA, DELTA, ECHO) == {ALPHA: True, DELTA: False, ECHO: True}
    assert source.cognee_sync_stats == {"unchanged": 1, "deleted": 1, "from_changes": 1}
    assert set(await _documents()) == set(after) - {ORDER_102}
