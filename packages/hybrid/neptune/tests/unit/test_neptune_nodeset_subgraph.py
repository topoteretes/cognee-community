"""get_nodeset_subgraph must accept the filter operator cognee always passes.

cognee's CogneeGraph calls
``get_nodeset_subgraph(node_type=..., node_name=..., node_name_filter_operator=...)``
on every node-set filtered search. The adapter used to declare only the first two
parameters, so that call raised TypeError.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cognee_community_hybrid_adapter_neptune.neptune_graph_adapter import NeptuneGraphDB


class NodeSet:
    pass


ROWS = [
    {
        "rawNodes": [{"id": "a", "properties": {"name": "a"}}],
        "rawRels": [{"source_id": "a", "target_id": "b", "type": "REL", "properties": {}}],
    }
]


def _stub():
    return SimpleNamespace(_GRAPH_NODE_LABEL="COGNEE_NODE", query=AsyncMock(return_value=ROWS))


@pytest.mark.asyncio
async def test_default_operator_keeps_neighbours_of_any_named_node():
    stub = _stub()
    nodes, edges = await NeptuneGraphDB.get_nodeset_subgraph(stub, NodeSet, ["x", "y"])

    query, params = stub.query.await_args.args
    assert "OPTIONAL MATCH (p)-[r]-(nbr:COGNEE_NODE)" in query
    assert "matched_count" not in query
    assert params == {"names": ["x", "y"], "type": "NodeSet"}
    assert nodes == [("a", {"name": "a"})]
    assert edges == [("a", "b", "REL", {})]


@pytest.mark.asyncio
async def test_or_operator_is_the_default_query():
    default, explicit = _stub(), _stub()
    await NeptuneGraphDB.get_nodeset_subgraph(default, NodeSet, ["x"])
    await NeptuneGraphDB.get_nodeset_subgraph(
        explicit, node_type=NodeSet, node_name=["x"], node_name_filter_operator="OR"
    )
    assert default.query.await_args.args == explicit.query.await_args.args


@pytest.mark.asyncio
async def test_and_operator_keeps_only_neighbours_of_every_named_node():
    stub = _stub()
    await NeptuneGraphDB.get_nodeset_subgraph(
        stub, node_type=NodeSet, node_name=["x", "y"], node_name_filter_operator="AND"
    )

    query = stub.query.await_args.args[0]
    assert "OPTIONAL MATCH" not in query
    assert "count(DISTINCT p) AS matched_count" in query
    assert "WHERE matched_count = size(primary)" in query
