"""Per-dataset isolation: writes to one dataset must never be visible from
another. Isolation is at the TurboPuffer namespace level (each dataset gets its
own ``{database_name}_graph_node`` / ``_graph_edge`` namespaces), so this is the
graph analog of the vector adapter's dataset handler guarantee.
"""

import uuid

import pytest

from conftest import demo_node_tuples, demo_edge_tuples, requires_turbopuffer
from conftest import _make_adapter

pytestmark = [pytest.mark.asyncio, requires_turbopuffer]


async def test_two_datasets_are_isolated():
    a = _make_adapter(f"tpuf_iso_a_{uuid.uuid4().hex[:8]}")
    b = _make_adapter(f"tpuf_iso_b_{uuid.uuid4().hex[:8]}")
    await a.initialize()
    await b.initialize()
    try:
        await a.add_nodes(demo_node_tuples())
        await a.add_edges(demo_edge_tuples())

        # b shares no data with a.
        assert await b.is_empty() is True
        assert await b.get_node("Alice") is None
        assert await b.has_edge("Alice", "WhiteRabbit", "follows") is False

        # a still intact.
        assert await a.get_node("Alice") is not None
    finally:
        await a.delete_graph()
        await b.delete_graph()


async def test_delete_graph_does_not_affect_other_dataset():
    a = _make_adapter(f"tpuf_iso_c_{uuid.uuid4().hex[:8]}")
    b = _make_adapter(f"tpuf_iso_d_{uuid.uuid4().hex[:8]}")
    await a.initialize()
    await b.initialize()
    try:
        await a.add_nodes(demo_node_tuples())
        await b.add_nodes(demo_node_tuples())

        await a.delete_graph()

        assert await a.is_empty() is True
        assert await b.is_empty() is False  # b untouched
    finally:
        await a.delete_graph()
        await b.delete_graph()
