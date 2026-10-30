"""Parity with the built-in adapters: feedback weights, truth state, triplet paging."""

import pytest
from cognee.modules.engine.utils import generate_edge_object_id
from support import Concept


async def test_node_feedback_weights_round_trip(seeded):
    adapter = seeded.adapter
    # 0.5 is DataPoint's own default, serialized into properties-json on add.
    assert await adapter.get_node_feedback_weights([seeded.ai, "ghost"]) == {seeded.ai: 0.5}

    result = await adapter.set_node_feedback_weights({seeded.ai: 0.9, "ghost": 0.1})
    assert result == {seeded.ai: True, "ghost": False}
    assert await adapter.get_node_feedback_weights([seeded.ai]) == {seeded.ai: 0.9}

    # The weight lives in the projected properties, where CogneeGraph reads it.
    node = await adapter.get_node(seeded.ai)
    assert node["feedback_weight"] == 0.9
    assert node["name"] == "artificial intelligence"  # other properties untouched

    assert await adapter.get_node_feedback_weights([]) == {}
    assert await adapter.set_node_feedback_weights({}) == {}


async def test_node_truth_state_round_trip(seeded):
    adapter = seeded.adapter
    empty = {"truth_alignment": [], "truth_epoch": None}
    assert await adapter.get_node_truth_state([seeded.ml]) == {seeded.ml: empty}

    result = await adapter.set_node_truth_state(
        {seeded.ml: {"truth_alignment": ["a", "b"], "truth_epoch": 3}, "ghost": {}}
    )
    assert result == {seeded.ml: True, "ghost": False}
    assert await adapter.get_node_truth_state([seeded.ml]) == {
        seeded.ml: {"truth_alignment": ["a", "b"], "truth_epoch": 3}
    }

    # A None epoch leaves the stored epoch alone (Ladybug semantics).
    await adapter.set_node_truth_state({seeded.ml: {"truth_alignment": ["c"], "truth_epoch": None}})
    assert await adapter.get_node_truth_state([seeded.ml]) == {
        seeded.ml: {"truth_alignment": ["c"], "truth_epoch": 3}
    }


async def test_edge_feedback_weights_round_trip(seeded):
    adapter = seeded.adapter
    edge_id = generate_edge_object_id(seeded.ml, seeded.ai, "is_subset_of")
    assert await adapter.get_edge_feedback_weights([edge_id, "ghost"]) == {edge_id: 0.5}

    result = await adapter.set_edge_feedback_weights({edge_id: 0.2, "ghost": 0.3})
    assert result == {edge_id: True, "ghost": False}
    assert await adapter.get_edge_feedback_weights([edge_id]) == {edge_id: 0.2}

    # Re-adding the edge with fresh properties resets it, as on other adapters.
    await adapter.add_edge(seeded.ml, seeded.ai, "is_subset_of", {"weight": 1})
    assert await adapter.get_edge_feedback_weights([edge_id]) == {edge_id: 0.5}

    # A caller-supplied edge_object_id wins over the derived one.
    await adapter.add_edge(seeded.dl, seeded.ai, "related_to", {"edge_object_id": "custom"})
    assert await adapter.set_edge_feedback_weights({"custom": 0.7}) == {"custom": True}
    assert await adapter.get_edge_feedback_weights(["custom"]) == {"custom": 0.7}


async def test_get_triplets_batch_pages_in_stable_order(seeded):
    adapter = seeded.adapter
    first = await adapter.get_triplets_batch(0, 2)
    rest = await adapter.get_triplets_batch(2, 10)
    assert len(first) == 2 and len(rest) == 1
    triplets = first + rest
    assert {
        (
            t["start_node"]["id"],
            t["relationship_properties"]["relationship_name"],
            t["end_node"]["id"],
        )
        for t in triplets
    } == {
        (seeded.ml, "is_subset_of", seeded.ai),
        (seeded.dl, "is_subset_of", seeded.ml),
        (seeded.dl, "related_to", seeded.ai),
    }
    # memify's triplet consumer skips any node dict without a "type".
    assert all(t["start_node"]["name"] and t["start_node"]["type"] == "Concept" for t in triplets)
    assert all(t["end_node"]["type"] == "Concept" for t in triplets)
    weighted = next(t for t in triplets if t["start_node"]["id"] == seeded.ml)
    assert weighted["relationship_properties"]["weight"] == 1

    assert await adapter.get_triplets_batch(10, 5) == []
    assert await adapter.get_triplets_batch(0, 0) == []
    with pytest.raises(ValueError):
        await adapter.get_triplets_batch(-1, 5)
    with pytest.raises(ValueError):
        await adapter.get_triplets_batch(0, -1)


async def test_get_triplets_batch_on_missing_database(adapter):
    assert await adapter.get_triplets_batch(0, 5) == []
    await adapter.add_nodes([Concept(name="solo")])
    assert await adapter.get_triplets_batch(0, 5) == []
