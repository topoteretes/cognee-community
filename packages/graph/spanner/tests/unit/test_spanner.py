"""Tests for the Spanner Graph adapter.

Unit tests use mocks so no real Spanner instance is required.
Integration tests (when SPANNER_DATABASE env is set) run against a live database.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from cognee.infrastructure.engine import DataPoint

from cognee_community_graph_adapter_spanner import SpannerGraphAdapter


class Person(DataPoint):
    name: str


def _make_row(keys, values):
    row = MagicMock()
    row.keys.return_value = keys
    row.values.return_value = values

    def getitem(self, key):
        if isinstance(key, int):
            return values[key] if key < len(values) else None
        idx = keys.index(key) if key in keys else -1
        return values[idx] if idx >= 0 else None

    row.__getitem__ = getitem
    return row


@pytest.fixture
def mock_database():
    """Mock Spanner database with in-memory state for node/edge tables."""
    state = {"nodes": {}, "edges": []}

    def run_in_transaction(fn):
        mock_txn = MagicMock()

        def execute_update(sql, params=None, param_types=None):
            params = params or {}
            if "INSERT INTO CogneeNode" in sql or "UPDATE CogneeNode" in sql:
                state["nodes"][params["id"]] = params.get("properties", "{}")
            elif "DELETE FROM CogneeEdge" in sql and "WHERE source_id" not in sql:
                state["edges"] = []
            elif "DELETE FROM CogneeNode" in sql and "WHERE id" in sql:
                nid = params.get("id")
                if nid:
                    state["nodes"].pop(nid, None)
                    state["edges"] = [
                        e
                        for e in state["edges"]
                        if e.get("source_id") != nid and e.get("target_id") != nid
                    ]
            elif "INSERT INTO CogneeEdge" in sql:
                state["edges"].append(
                    {
                        "source_id": params.get("source_id"),
                        "target_id": params.get("target_id"),
                        "edge_id": params.get("edge_id"),
                        "relationship_type": params.get("relationship_type"),
                        "properties": params.get("properties", "{}"),
                    }
                )
            return 1

        mock_txn.execute_update.side_effect = execute_update
        fn(mock_txn)

    def snapshot():
        snap = MagicMock()

        def execute_sql(sql, params=None, param_types=None):
            params = params or {}
            if "FROM CogneeNode WHERE id" in sql:
                nid = params.get("id")
                if nid in state["nodes"]:
                    props_raw = state["nodes"][nid]
                    return [_make_row(["id", "properties"], [nid, props_raw])]
                return []
            if "FROM CogneeNode" in sql and "WHERE" not in sql:
                rows = []
                for nid, props_raw in state["nodes"].items():
                    rows.append(_make_row(["id", "properties"], [nid, props_raw]))
                return rows
            if "FROM CogneeEdge" in sql and "WHERE source_id = @source_id" in sql:
                src = params.get("source_id")
                tgt = params.get("target_id")
                rel = params.get("rel") or params.get("relationship_type")
                for e in state["edges"]:
                    if (
                        e["source_id"] == src
                        and e["target_id"] == tgt
                        and e["relationship_type"] == rel
                    ):
                        return [_make_row([1], [1])]
                return []
            if "FROM CogneeEdge" in sql and "WHERE source_id = @id OR target_id = @id" in sql:
                nid = params.get("id")
                rows = []
                for e in state["edges"]:
                    if e["source_id"] == nid or e["target_id"] == nid:
                        rows.append(
                            _make_row(
                                ["source_id", "target_id", "relationship_type", "properties"],
                                [
                                    e["source_id"],
                                    e["target_id"],
                                    e["relationship_type"],
                                    e.get("properties", "{}"),
                                ],
                            )
                        )
                return rows
            if "FROM CogneeEdge" in sql:
                rows = []
                for e in state["edges"]:
                    rows.append(
                        _make_row(
                            ["source_id", "target_id", "relationship_type", "properties"],
                            [
                                e["source_id"],
                                e["target_id"],
                                e["relationship_type"],
                                e.get("properties", "{}"),
                            ],
                        )
                    )
                return rows
            return []

        snap.execute_sql.side_effect = execute_sql
        snap.__enter__ = MagicMock(return_value=snap)
        snap.__exit__ = MagicMock(return_value=None)
        return snap

    db = MagicMock()
    db.run_in_transaction.side_effect = run_in_transaction
    db.snapshot.side_effect = snapshot
    return db, state


@pytest.fixture
def adapter(mock_database):
    mock_db, _ = mock_database
    with patch.object(SpannerGraphAdapter, "_get_database", return_value=mock_db):
        adp = SpannerGraphAdapter(
            project_id="test-project",
            instance_id="test-instance",
            database_id="test-db",
        )
        yield adp


@pytest.mark.asyncio
async def test_add_node_and_get_node(adapter):
    node_id = str(uuid4())
    await adapter.add_node(
        Person(id=node_id, name="Alice"),
    )
    node = await adapter.get_node(node_id)
    assert node is not None
    assert node.get("id") == node_id
    assert node.get("name") == "Alice"


@pytest.mark.asyncio
async def test_add_edge_and_has_edge(adapter):
    a, b = str(uuid4()), str(uuid4())
    await adapter.add_node(Person(id=a, name="A"))
    await adapter.add_node(Person(id=b, name="B"))
    await adapter.add_edge(a, b, "KNOWS", {"since": "2025"})
    assert await adapter.has_edge(a, b, "KNOWS") is True
    assert await adapter.has_edge(a, b, "OTHER") is False


@pytest.mark.asyncio
async def test_get_graph_data(adapter):
    a, b = str(uuid4()), str(uuid4())
    await adapter.add_node(Person(id=a, name="A"))
    await adapter.add_node(Person(id=b, name="B"))
    await adapter.add_edge(a, b, "LINK", {})
    nodes, edges = await adapter.get_graph_data()
    assert len(nodes) == 2
    assert len(edges) == 1
    assert edges[0][0] == a and edges[0][1] == b and edges[0][2] == "LINK"


@pytest.mark.asyncio
async def test_delete_node(adapter):
    a = str(uuid4())
    await adapter.add_node(Person(id=a, name="A"))
    assert await adapter.get_node(a) is not None
    await adapter.delete_node(a)
    assert await adapter.get_node(a) is None


@pytest.mark.asyncio
async def test_init_accepts_url():
    adp = SpannerGraphAdapter(graph_database_url="p/i/d")
    assert adp._project_id == "p"
    assert adp._instance_id == "i"
    assert adp._database_id == "d"


def test_init_requires_url_or_ids():
    with pytest.raises(ValueError, match="Provide either"):
        SpannerGraphAdapter()
    with pytest.raises(ValueError, match="project_id/instance_id/database_id"):
        SpannerGraphAdapter(graph_database_url="invalid")
