"""Unit tests for Cypher parameter sanitization.

These tests verify that Enum values in query parameters are properly
converted to their underlying values before being sent to FalkorDB.
No FalkorDB connection is required.
"""

from enum import Enum

from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter


class Color(Enum):
    RED = "red"
    GREEN = "green"


class Priority(Enum):
    LOW = 1
    HIGH = 2


def test_enum_string_value():
    result = FalkorDBAdapter._sanitize_cypher_params({"color": Color.RED})
    assert result == {"color": "red"}


def test_enum_int_value():
    result = FalkorDBAdapter._sanitize_cypher_params({"priority": Priority.HIGH})
    assert result == {"priority": 2}


def test_plain_values_unchanged():
    params = {"name": "Alice", "age": 30, "active": True}
    result = FalkorDBAdapter._sanitize_cypher_params(params)
    assert result == params


def test_nested_dict_with_enum():
    params = {"meta": {"color": Color.GREEN, "label": "test"}}
    result = FalkorDBAdapter._sanitize_cypher_params(params)
    assert result == {"meta": {"color": "green", "label": "test"}}


def test_list_with_enums():
    params = {"colors": [Color.RED, Color.GREEN, "blue"]}
    result = FalkorDBAdapter._sanitize_cypher_params(params)
    assert result == {"colors": ["red", "green", "blue"]}


def test_empty_dict():
    assert FalkorDBAdapter._sanitize_cypher_params({}) == {}


def test_mixed_complex():
    params = {
        "name": "node1",
        "status": Color.RED,
        "tags": [Color.GREEN, "manual"],
        "nested": {"priority": Priority.LOW, "value": 42},
    }
    result = FalkorDBAdapter._sanitize_cypher_params(params)
    assert result == {
        "name": "node1",
        "status": "red",
        "tags": ["green", "manual"],
        "nested": {"priority": 1, "value": 42},
    }


def test_bytes_value_decoded():
    # bytes would otherwise make FalkorDB reject the whole query with
    # "Failed to parse query parameter 'properties' value".
    result = FalkorDBAdapter._sanitize_cypher_params({"blob": b"abc"})
    assert result == {"blob": "abc"}


def test_bytes_value_in_property_map_decoded():
    result = FalkorDBAdapter._sanitize_cypher_params(
        {"properties": {"name": "ok", "raw": b"\xe2\x9c\x93 done"}}
    )
    assert result == {"properties": {"name": "ok", "raw": "✓ done"}}


def test_primitive_array_preserved():
    params = {"nums": [1, 2, 3], "words": ["a", "b"]}
    result = FalkorDBAdapter._sanitize_cypher_params(params)
    assert result == params


def test_bound_list_of_maps_preserved_for_unwind():
    # A list of maps bound as a param (e.g. the `UNWIND $items AS item ... SET
    # edge += item` edge batch) MUST stay a list of maps — JSON-stringifying the
    # elements makes FalkorDB fail with "Type mismatch: expected Map ... but was
    # String". The element maps' values are still primitivized (they get stored).
    result = FalkorDBAdapter._sanitize_cypher_params(
        {"items": [{"edge_index": 0, "props": {"w": 1}}, {"edge_index": 1}]}
    )
    # props stays a map so `SET r += item.props` works (issue #3324)
    assert result == {"items": [{"edge_index": 0, "props": {"w": 1}}, {"edge_index": 1}]}


def test_stored_map_value_json_encoded():
    # A map *value* inside the stored $properties bag can't be a property value,
    # so it is JSON-encoded.
    result = FalkorDBAdapter._sanitize_cypher_params(
        {"properties": {"meta": {"a": 1}, "name": "ok"}}
    )
    # With the fix, nested maps preserve their shape (issue #3324).
    # add_node pre-flattens stored property values before binding, so the
    # sanitizer no longer needs to stringify nested maps.
    assert result == {"properties": {"meta": {"a": 1}, "name": "ok"}}


def test_stored_array_of_maps_json_encoded():
    # An array of maps as a stored property value becomes an array of JSON strings
    # (a valid array-of-primitives).
    result = FalkorDBAdapter._sanitize_cypher_params({"properties": {"objs": [{"k": 1}, {"k": 2}]}})
    # Nested maps in arrays also preserve their shape (issue #3324)
    assert result == {"properties": {"objs": [{"k": 1}, {"k": 2}]}}


def test_stored_array_with_null_json_encoded():
    # FalkorDB rejects null elements inside a stored array; it can't be made
    # all-primitive, so the whole array becomes a JSON string.
    result = FalkorDBAdapter._sanitize_cypher_params({"properties": {"tags": ["a", None]}})
    assert result == {"properties": {"tags": '["a", null]'}}


def test_coerce_param_value_helper():
    coerce = FalkorDBAdapter._coerce_param_value
    assert coerce(b"hi") == "hi"
    assert coerce("x") == "x"
    assert coerce(7) == 7
    assert coerce(None) is None
    assert coerce([1, 2]) == [1, 2]
    assert coerce(Color.RED) == "red"
    # a bound map keeps its structure; nested maps also preserve shape (issue #3324)
    assert coerce({"a": 1, "b": {"c": 2}}) == {"a": 1, "b": {"c": 2}}


def test_edge_props_preserved_as_map_for_unwind_set():
    """Regression for cognee issue #3324: edge ``props`` must stay a map so
    ``SET r += item.props`` works in FalkorDB.  Previously ``_coerce_param_value``
    routed dict values through ``_coerce_stored_value`` which ``json.dumps``'d
    them, turning ``props`` into a string and causing
    ``Property values can only be of primitive types``.
    """
    params = {
        "items": [
            {
                "source_id": "node_a",
                "target_id": "node_b",
                "props": {"relationship_name": "CONNECTS", "weight": 1},
            }
        ]
    }
    result = FalkorDBAdapter._sanitize_cypher_params(params)
    props = result["items"][0]["props"]
    assert isinstance(props, dict), f"props should be a dict, got {type(props)}: {props!r}"
    assert props == {"relationship_name": "CONNECTS", "weight": 1}


def test_coerce_stored_value_helper():
    stored = FalkorDBAdapter._coerce_stored_value
    assert stored(b"hi") == "hi"
    assert stored({"a": 1}) == '{"a": 1}'
    assert stored([1, 2]) == [1, 2]
    assert stored([{"k": 1}]) == ['{"k": 1}']
    assert stored(Color.RED) == "red"
