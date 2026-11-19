"""Forget-on-delete must remove a row's cognified graph content, not just its Data record.

Ingests two ClickHouse rows, cognifies (LLM + embeddings mocked, so no API key and
no network), deletes one row upstream, re-syncs, and asserts the deleted row's
extracted entity is gone from the graph while the surviving row's entity remains.

This is the acceptance criterion the connector's own unit tests cannot reach:
``orphan_cleanup`` is what turns dlt's hard-delete marker into a removal from the
graph and vector stores.
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

from cognee_community_connector_clickhouse import clickhouse_source

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

DATASET = "clickhouse_forget_test"
# Distinctive tokens so each row maps to exactly one graph entity.
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"


class FakeResult:
    def __init__(self, column_names, result_rows):
        self.column_names = column_names
        self.result_rows = result_rows


class FakeTable:
    def __init__(self, columns, rows, *, primary_key=(), comment=""):
        self.columns = columns
        self.rows = rows
        self.primary_key = list(primary_key)
        self.comment = comment

    @classmethod
    def events(cls, rows):
        """The one table shape these tests need, with its identity declared."""
        return cls(
            {
                "event_id": "UInt64",
                "kind": "LowCardinality(String)",
                "payload": "String",
                "updated_at": "DateTime64(3)",
            },
            rows,
            primary_key=["event_id"],
            comment="Raw events.",
        )


class FakeClickHouseClient:
    """In-memory ClickHouse over the one table shape these tests need."""

    def __init__(self, rows):
        self.table = FakeTable.events(rows)
        self.calls = []

    def query(self, sql, parameters=None):
        self.calls.append(sql)
        parameters = parameters or {}

        if "system.columns" in sql:
            columns = list(self.table.columns.items())
            if "is_in_primary_key = 1" in sql:
                columns = [(n, t) for n, t in columns if n in self.table.primary_key]
            elif "is_in_sorting_key = 1" in sql:
                columns = []
            return FakeResult(["name", "type"], [list(pair) for pair in columns])

        if "system.tables" in sql:
            return FakeResult(["comment"], [[self.table.comment]])

        select = sql[len("SELECT ") :].split(" FROM ")[0]
        columns = [column.strip() for column in select.split(",")]
        tail = (
            sql.split(" FROM ")[1].split(" ", 1)[1].strip() if " " in sql.split(" FROM ")[1] else ""
        )

        rows = list(self.table.rows)
        if tail.startswith("WHERE "):
            clause = tail[len("WHERE ") :].split(" ORDER BY ")[0]
            for term in clause.split(" AND "):
                term = term.strip().strip("()")
                if ">=" in term and "{cursor:" in term:
                    column = term.split(">=")[0].strip()
                    # A NULL cursor can never satisfy >=, which is why the connector
                    # rejects a nullable cursor column outright.
                    rows = [
                        row
                        for row in rows
                        if row.get(column) is not None and row[column] >= parameters["cursor"]
                    ]
        if "ORDER BY" in tail:
            column = tail.split("ORDER BY ")[1].replace(" ASC", "").strip()
            rows = sorted(rows, key=lambda row: row[column])

        return FakeResult(columns, [[row.get(column) for column in columns] for row in rows])


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
        name = next((token for token in (ALPHA, BRAVO) if text_input and token in text_input), None)
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
    for _node_id, properties in nodes:
        if any(token in str(value).lower() for value in (properties or {}).values()):
            return True
    return False


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    async def _noop_index(*_args, **_kwargs):
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


def _source(client):
    return clickhouse_source(
        database="analytics",
        tables=["events"],
        key_columns={"events": "event_id"},
        cursor_columns={"events": "updated_at"},
        client=client,
    )


async def _sync(client):
    await cognee.add(_source(client), dataset_name=DATASET, write_disposition="merge")
    await cognee.cognify(datasets=[DATASET])


def _rows():
    import datetime

    return [
        {
            "event_id": 1,
            "kind": "signup",
            "payload": f"{ALPHA} onboarded through SSO and is a logistics company.",
            "updated_at": datetime.datetime(2026, 1, 1, 10, 0, 0),
        },
        {
            "event_id": 2,
            "kind": "login",
            "payload": f"{BRAVO} had a 2FA failure and is a finance company.",
            "updated_at": datetime.datetime(2026, 1, 2, 10, 0, 0),
        },
    ]


@pytest.mark.asyncio
async def test_deleting_a_row_forgets_its_graph_content(clean_environment):
    client = FakeClickHouseClient(_rows())

    await _sync(client)
    assert await _graph_has(ALPHA), "row 1's entity should be in the graph after ingest"
    assert await _graph_has(BRAVO), "row 2's entity should be in the graph after ingest"

    # Delete row 2 upstream (a ClickHouse lightweight delete or a mutation); the next
    # sync must forget its content.
    client.table.rows = [row for row in client.table.rows if row["event_id"] != 2]
    await _sync(client)

    assert await _graph_has(ALPHA), "surviving row 1 must remain after deleting row 2"
    assert not await _graph_has(BRAVO), (
        "deleted row 2's entity must be removed from the graph (forget-on-delete), "
        "not just its Data record"
    )


@pytest.mark.asyncio
async def test_unchanged_resync_does_not_reingest(clean_environment):
    """A no-op re-sync must leave the graph alone: only changed rows re-enter."""
    client = FakeClickHouseClient(_rows()[:1])

    await _sync(client)
    nodes_before, _ = await (await get_graph_engine()).get_graph_data()
    count_before = len(nodes_before)

    await _sync(client)

    nodes_after, _ = await (await get_graph_engine()).get_graph_data()
    assert len(nodes_after) == count_before, (
        "an unchanged re-sync must not add nodes; the content-hash data_id has to be stable"
    )


@pytest.mark.asyncio
async def test_the_incremental_cursor_limits_the_second_sync_to_changed_rows(clean_environment):
    import datetime

    client = FakeClickHouseClient(_rows())

    await _sync(client)
    read_calls = len(client.calls)

    # A third row arrives; the next sync must read it rather than backfilling.
    client.table.rows.append(
        {
            "event_id": 3,
            "kind": "purchase",
            "payload": "Globex bought a plan.",
            "updated_at": datetime.datetime(2026, 1, 3, 10, 0, 0),
        }
    )
    await _sync(client)

    delta_queries = [sql for sql in client.calls if "{cursor:" in sql]
    assert delta_queries, "the second sync must push the cursor down into SQL"
    assert any(call > read_calls for call in range(len(client.calls)))
