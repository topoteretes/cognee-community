"""
Integration test for ArcadeDB hybrid adapter.

Requires a running ArcadeDB instance. Set environment variables:
  ARCADEDB_URL, ARCADEDB_HTTP_PORT, ARCADEDB_USERNAME, ARCADEDB_PASSWORD
"""

import os

import pytest

# Skip all tests in this module if ArcadeDB is not available
pytestmark = pytest.mark.skipif(
    os.getenv("ARCADEDB_URL") is None,
    reason="ArcadeDB not available (set ARCADEDB_URL to run)",
)


@pytest.fixture
def adapter():
    from cognee_community_hybrid_adapter_arcadedb.arcadedb_adapter import (
        ArcadeDBAdapter,
    )

    return ArcadeDBAdapter(
        graph_database_url=os.getenv("ARCADEDB_URL", "localhost"),
        graph_database_username=os.getenv("ARCADEDB_USERNAME", "root"),
        graph_database_password=os.getenv("ARCADEDB_PASSWORD", ""),
    )


@pytest.mark.asyncio
async def test_health_check(adapter):
    """Verify the adapter can connect to ArcadeDB."""
    result = await adapter._sql("SELECT 1 AS ok")
    assert result.get("result") is not None


@pytest.mark.asyncio
async def test_vector_storage_type_auto_detect(adapter):
    """Verify auto-detection of vertex type casing."""
    storage_type = await adapter._vector_storage_type()
    assert storage_type.lower() == "vertex"
