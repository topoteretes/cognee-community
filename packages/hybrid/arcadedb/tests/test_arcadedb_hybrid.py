"""Tests for ArcadeDB hybrid adapter (graph + vector).

These tests require a running ArcadeDB instance with Bolt (7687) and HTTP (2480).
Set environment variables before running:
    ARCADEDB_URL=bolt://localhost:7687
    ARCADEDB_HTTP_URL=http://localhost:2480
    ARCADEDB_USERNAME=root
    ARCADEDB_PASSWORD=test
"""

import os
import uuid

import pytest
import pytest_asyncio

# Skip all tests if ArcadeDB is not available
ARCADEDB_URL = os.environ.get("ARCADEDB_URL", "bolt://localhost:7687")
ARCADEDB_USERNAME = os.environ.get("ARCADEDB_USERNAME", "root")
ARCADEDB_PASSWORD = os.environ.get("ARCADEDB_PASSWORD", "test")


def arcadedb_available():
    """Check if ArcadeDB is reachable."""
    try:
        import socket

        host = ARCADEDB_URL.split("://")[-1].split(":")[0]
        sock = socket.create_connection((host, 7687), timeout=2)
        sock.close()
        return True
    except (OSError, ConnectionRefusedError):
        return False


pytestmark = pytest.mark.skipif(
    not arcadedb_available(), reason="ArcadeDB not available"
)


@pytest_asyncio.fixture
async def adapter():
    from cognee_community_hybrid_adapter_arcadedb import ArcadeDBAdapter

    adapter = ArcadeDBAdapter(
        graph_database_url=ARCADEDB_URL,
        graph_database_username=ARCADEDB_USERNAME,
        graph_database_password=ARCADEDB_PASSWORD,
        database_name="cognee_test",
    )
    yield adapter
    await adapter.delete_graph()


# ---- Graph Tests ----


@pytest.mark.asyncio
async def test_is_empty(adapter):
    assert await adapter.is_empty()


@pytest.mark.asyncio
async def test_add_and_get_node(adapter):
    from cognee.infrastructure.engine import DataPoint

    class TestNode(DataPoint):
        name: str

    node = TestNode(id=uuid.uuid4(), name="test_node")
    await adapter.add_node(node)

    result = await adapter.get_node(str(node.id))
    assert result is not None
    assert result["name"] == "test_node"


@pytest.mark.asyncio
async def test_add_and_get_edge(adapter):
    from cognee.infrastructure.engine import DataPoint

    class TestNode(DataPoint):
        name: str

    node1 = TestNode(id=uuid.uuid4(), name="node1")
    node2 = TestNode(id=uuid.uuid4(), name="node2")

    await adapter.add_node(node1)
    await adapter.add_node(node2)
    await adapter.add_edge(node1.id, node2.id, "KNOWS")

    has = await adapter.has_edge(node1.id, node2.id, "KNOWS")
    assert has is True


@pytest.mark.asyncio
async def test_delete_node(adapter):
    from cognee.infrastructure.engine import DataPoint

    class TestNode(DataPoint):
        name: str

    node = TestNode(id=uuid.uuid4(), name="to_delete")
    await adapter.add_node(node)
    await adapter.delete_node(str(node.id))

    result = await adapter.get_node(str(node.id))
    assert result is None


@pytest.mark.asyncio
async def test_graph_metrics(adapter):
    metrics = await adapter.get_graph_metrics()
    assert "num_nodes" in metrics
    assert "num_edges" in metrics


# ---- Vector Tests ----


@pytest.mark.asyncio
async def test_has_collection_nonexistent(adapter):
    result = await adapter.has_collection("nonexistent_type")
    assert result is False


@pytest.mark.asyncio
async def test_create_collection(adapter):
    await adapter.create_collection("VectorTest")
    result = await adapter.has_collection("VectorTest")
    assert result is True


@pytest.mark.asyncio
async def test_embed_data(adapter):
    vectors = await adapter.embed_data(["hello world"])
    assert len(vectors) == 1
    assert len(vectors[0]) > 0
    assert isinstance(vectors[0][0], float)


@pytest.mark.asyncio
async def test_embed_data_empty(adapter):
    vectors = await adapter.embed_data([])
    assert vectors == []


@pytest.mark.asyncio
async def test_embed_data_blank(adapter):
    vectors = await adapter.embed_data(["", "  "])
    assert len(vectors) == 2
    assert vectors[0] == []
    assert vectors[1] == []
