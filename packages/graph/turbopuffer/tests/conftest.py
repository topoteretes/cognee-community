"""Shared fixtures and helpers for the TurboPuffer graph adapter tests.

Test tiers (gated by env vars so CI can run the cheap ones without a network):

  - Unit/contract  (no gate): construction, registration, signatures. Run always.
  - Integration    (COGNEE_TURBOPUFFER_GRAPH_TESTS=1 + TURBOPUFFER_API_KEY):
                    per-method behavior against a real TurboPuffer namespace.
  - E2E            (COGNEE_TURBOPUFFER_GRAPH_E2E=1 + TURBOPUFFER_API_KEY + LLM_API_KEY):
                    full add -> cognify -> search pipeline on Alice in Wonderland.

The demo graph (test_kg.json) is the Wonderland fixture used by the integration
tier so per-method assertions stay deterministic (no LLM involved).
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

TESTS_DIR = Path(__file__).parent
DEMO_KG_PATH = TESTS_DIR / "test_kg.json"

# Make the package importable when running from source without `pip install -e`.
PACKAGE_ROOT = TESTS_DIR.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

# Make packages/shared (contract_suite) importable.
sys.path.insert(0, str((TESTS_DIR.parents[2] / "shared").resolve()))

# --- env gates -------------------------------------------------------------

INTEGRATION = os.getenv("COGNEE_TURBOPUFFER_GRAPH_TESTS", "").lower() in ("1", "true", "yes")
E2E = os.getenv("COGNEE_TURBOPUFFER_GRAPH_E2E", "").lower() in ("1", "true", "yes")
HAS_TPUF_KEY = bool(os.getenv("TURBOPUFFER_API_KEY"))
HAS_LLM_KEY = bool(os.getenv("LLM_API_KEY"))

requires_turbopuffer = pytest.mark.skipif(
    not (INTEGRATION and HAS_TPUF_KEY),
    reason="Set COGNEE_TURBOPUFFER_GRAPH_TESTS=1 and TURBOPUFFER_API_KEY to run integration tests",
)
requires_e2e = pytest.mark.skipif(
    not (E2E and HAS_TPUF_KEY and HAS_LLM_KEY),
    reason="Set COGNEE_TURBOPUFFER_GRAPH_E2E=1, TURBOPUFFER_API_KEY and LLM_API_KEY to run e2e",
)

# --- demo graph helpers ----------------------------------------------------


def load_demo_kg() -> dict:
    return json.loads(DEMO_KG_PATH.read_text(encoding="utf-8"))


def demo_node_tuples() -> list[tuple[str, dict]]:
    """Nodes in the (node_id, properties) form accepted by add_nodes."""
    kg = load_demo_kg()
    return [
        (n["id"], {"name": n["name"], "type": n["type"], "description": n["description"]})
        for n in kg["nodes"]
    ]


def demo_edge_tuples() -> list[tuple[str, str, str, dict]]:
    """Edges in the (source, target, relationship_name, properties) form."""
    kg = load_demo_kg()
    return [
        (e["source_node_id"], e["target_node_id"], e["relationship_name"], {}) for e in kg["edges"]
    ]


# --- adapter fixtures ------------------------------------------------------


def _make_adapter(database_name: str):
    """Construct a TurbopufferGraphAdapter the way cognee's factory does:
    keyword args including ``database_name`` (the per-dataset prefix)."""
    from cognee_community_graph_adapter_turbopuffer import TurbopufferGraphAdapter

    return TurbopufferGraphAdapter(
        graph_database_url=os.getenv("TURBOPUFFER_REGION", ""),
        graph_database_username="",
        graph_database_password="",
        graph_database_port="",
        graph_database_key=os.getenv("TURBOPUFFER_API_KEY", ""),
        database_name=database_name,
    )


@pytest_asyncio.fixture
async def adapter():
    """A fresh, isolated adapter bound to a unique namespace prefix.

    Each test gets its own ``database_name`` so parallel/repeat runs never
    collide, and the graph is dropped on teardown.
    """
    db_name = f"tpuf_graph_test_{uuid.uuid4().hex[:8]}"
    inst = _make_adapter(db_name)
    await inst.initialize()
    try:
        yield inst
    finally:
        try:
            await inst.delete_graph()
        except Exception:
            pass
