"""Unit tests for node metadata deserialization on read.

FalkorDB stores only primitive property values, so the adapter ``json.dumps``
any dict-valued property (notably ``metadata``) on write (``add_node`` /
``_coerce_stored_value``). cognee core, however, treats ``metadata`` as a dict
— e.g. ``DataPoint.get_embeddable_property_names`` does
``metadata["index_fields"]``. The read path therefore has to restore it.

The cognee 1.2.0 ``namespace_entity_type_node_ids`` migration is the first code
path to round-trip ``get_graph_data`` output back through ``add_nodes``; before
this fix it crashed with ``TypeError: string indices must be integers, not str``
because ``metadata`` came back as a JSON string.

No FalkorDB connection is required.
"""

import json

from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter


def test_metadata_json_string_is_deserialized():
    stored = {
        "id": "n1",
        "name": "Entity A",
        "metadata": json.dumps({"index_fields": ["name"], "type": "Entity"}),
    }
    result = FalkorDBAdapter._deserialize_node_properties(stored)
    assert result["metadata"] == {"index_fields": ["name"], "type": "Entity"}
    # The exact access that crashed the 1.2.0 migration now works:
    assert result["metadata"]["index_fields"] == ["name"]


def test_non_string_metadata_is_untouched():
    # Idempotent / forward-compatible: a dict metadata (future adapter) is left as-is.
    already = {"id": "n1", "metadata": {"index_fields": ["name"]}}
    result = FalkorDBAdapter._deserialize_node_properties(already)
    assert result["metadata"] == {"index_fields": ["name"]}


def test_missing_metadata_is_untouched():
    props = {"id": "n1", "name": "Entity A"}
    result = FalkorDBAdapter._deserialize_node_properties(props)
    assert result == {"id": "n1", "name": "Entity A"}


def test_malformed_metadata_is_left_as_is():
    # Not valid JSON -> tolerate rather than raise.
    props = {"id": "n1", "metadata": "not json {"}
    result = FalkorDBAdapter._deserialize_node_properties(props)
    assert result["metadata"] == "not json {"


def test_only_metadata_is_parsed():
    # Scoped to metadata: a string property that happens to look like JSON is untouched.
    props = {
        "id": "n1",
        "name": "{not metadata}",
        "metadata": json.dumps({"index_fields": []}),
    }
    result = FalkorDBAdapter._deserialize_node_properties(props)
    assert result["name"] == "{not metadata}"
    assert result["metadata"] == {"index_fields": []}
