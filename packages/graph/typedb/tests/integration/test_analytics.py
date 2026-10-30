"""Analytics tier: neighborhood, components, metrics, subgraphs, filters."""

import pytest
from support import Concept


async def test_get_neighborhood_depth_and_edge_types(seeded):
    adapter = seeded.adapter

    nodes, edges = await adapter.get_neighborhood([seeded.dl], depth=1)
    assert {node_id for node_id, _ in nodes} == {seeded.dl, seeded.ml, seeded.ai}
    assert len(edges) == 3  # all three edges touch the 1-hop set

    isolated = Concept(name="isolated")
    await adapter.add_nodes([isolated])
    nodes, _ = await adapter.get_neighborhood([str(isolated.id)], depth=2)
    assert [node_id for node_id, _ in nodes] == [str(isolated.id)]

    # ml is 1 hop from ai; at depth 1 with only is_subset_of edges, related_to
    # is neither traversed nor returned.
    nodes, edges = await adapter.get_neighborhood([seeded.ai], depth=1, edge_types=["is_subset_of"])
    assert {node_id for node_id, _ in nodes} == {seeded.ai, seeded.ml}
    assert {label for _, _, label, _ in edges} == {"is_subset_of"}

    assert await adapter.get_neighborhood([]) == ([], [])


async def test_get_disconnected_nodes_returns_only_isolated(seeded):
    adapter = seeded.adapter
    assert await adapter.get_disconnected_nodes() == []

    # Only degree-zero nodes count: cognee's remove_disconnected_chunks
    # deletes every id returned here, so a connected pair — even one in its
    # own small component — must NOT be reported.
    lonely = Concept(name="lonely")
    lonelier = Concept(name="lonelier")
    isolated = Concept(name="isolated")
    await adapter.add_nodes([lonely, lonelier, isolated])
    await adapter.add_edge(str(lonely.id), str(lonelier.id), "commiserates_with")

    assert await adapter.get_disconnected_nodes() == [str(isolated.id)]


async def test_get_graph_metrics(seeded):
    metrics = await seeded.adapter.get_graph_metrics()
    assert metrics["num_nodes"] == 3
    assert metrics["num_edges"] == 3
    assert metrics["mean_degree"] == 2.0
    assert metrics["num_connected_components"] == 1
    assert metrics["sizes_of_connected_components"] == [3]
    assert metrics["num_selfloops"] == -1  # optional metrics off by default

    await seeded.adapter.add_edge(seeded.ai, seeded.ai, "self_referential")
    metrics = await seeded.adapter.get_graph_metrics(include_optional=True)
    assert metrics["num_selfloops"] == 1
    assert metrics["diameter"] == -1  # all-pairs metrics unsupported


async def test_get_nodeset_subgraph_or_and(seeded):
    adapter = seeded.adapter

    nodes, edges = await adapter.get_nodeset_subgraph(Concept, ["machine learning"])
    # OR: seed (ml) plus its neighbours (ai via is_subset_of, dl via is_subset_of).
    assert {node_id for node_id, _ in nodes} == {seeded.ml, seeded.ai, seeded.dl}
    assert len(edges) == 3  # dl->ai also joins: both endpoints are in the set

    nodes, _ = await adapter.get_nodeset_subgraph(
        Concept, ["machine learning", "artificial intelligence"], "AND"
    )
    # AND: both seeds, plus only neighbours connected to every seed (dl).
    assert {node_id for node_id, _ in nodes} == {seeded.ml, seeded.ai, seeded.dl}

    nodes, edges = await adapter.get_nodeset_subgraph(Concept, ["no such name"])
    assert (nodes, edges) == ([], [])


async def test_get_filtered_graph_data(seeded):
    # "name" is a promoted attribute, so this exercises the server-side path.
    nodes, edges = await seeded.adapter.get_filtered_graph_data(
        [{"name": ["machine learning", "artificial intelligence"]}]
    )
    assert {node_id for node_id, _ in nodes} == {seeded.ml, seeded.ai}
    assert {(source, target) for source, target, _, _ in edges} == {(seeded.ml, seeded.ai)}


async def test_get_filtered_graph_data_client_side_fallback(seeded):
    # "description" is not promoted, so this exercises the client-side scan.
    nodes, _ = await seeded.adapter.get_filtered_graph_data(
        [{"description": ['the "broad" field']}]
    )
    assert {node_id for node_id, _ in nodes} == {seeded.ai}


async def test_get_model_independent_graph_data(seeded):
    nodes_result, edges_result = await seeded.adapter.get_model_independent_graph_data()
    assert len(nodes_result[0]["nodes"]) == 3
    elements = edges_result[0]["elements"]
    assert [seeded.ml, "is_subset_of", seeded.ai] in elements


async def test_get_id_filtered_graph_data(seeded):
    adapter = seeded.adapter
    nodes, edges = await adapter.get_id_filtered_graph_data([seeded.ml])
    # ml plus its direct neighbours; only edges touching ml (dl->ai is excluded).
    assert {node_id for node_id, _ in nodes} == {seeded.ml, seeded.ai, seeded.dl}
    assert {(source, target) for source, target, _, _ in edges} == {
        (seeded.ml, seeded.ai),
        (seeded.dl, seeded.ml),
    }
    assert all(props.get("name") for _, props in nodes)

    assert await adapter.get_id_filtered_graph_data([]) == ([], [])
    assert await adapter.get_id_filtered_graph_data(["no-such-node"]) == ([], [])
    from cognee.exceptions import CogneeValidationError

    with pytest.raises(CogneeValidationError):
        await adapter.get_id_filtered_graph_data([123])
