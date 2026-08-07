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
    # String". A dict-valued record field like `props` (spread via
    # `SET r += e.props`) must ALSO stay a map: FalkorDB rejects a JSON string
    # there with "Property values can only be of primitive types or arrays of
    # primitive types". Only the props map's own values are primitivized.
    result = FalkorDBAdapter._sanitize_cypher_params(
        {"items": [{"edge_index": 0, "props": {"w": 1}}, {"edge_index": 1}]}
    )
    assert result == {"items": [{"edge_index": 0, "props": {"w": 1}}, {"edge_index": 1}]}


def test_bound_record_props_values_primitivized():
    # Values inside a record's props sub-map get stored as edge properties, so
    # they are primitivized (a nested dict there becomes a JSON string).
    result = FalkorDBAdapter._sanitize_cypher_params(
        {"items": [{"props": {"meta": {"a": 1}, "raw": b"x", "kind": Color.RED}}]}
    )
    assert result == {"items": [{"props": {"meta": '{"a": 1}', "raw": "x", "kind": "red"}}]}


def test_stored_map_value_json_encoded():
    # A map *value* inside the stored $properties bag can't be a property value,
    # so it is JSON-encoded.
    result = FalkorDBAdapter._sanitize_cypher_params(
        {"properties": {"meta": {"a": 1}, "name": "ok"}}
    )
    assert result == {"properties": {"meta": '{"a": 1}', "name": "ok"}}


def test_stored_array_of_maps_json_encoded():
    # An array of maps as a stored property value becomes an array of JSON strings
    # (a valid array-of-primitives).
    result = FalkorDBAdapter._sanitize_cypher_params({"properties": {"objs": [{"k": 1}, {"k": 2}]}})
    assert result == {"properties": {"objs": ['{"k": 1}', '{"k": 2}']}}


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
    # a bound map keeps its structure; its values are primitivized for storage
    assert coerce({"a": 1, "b": {"c": 2}}) == {"a": 1, "b": '{"c": 2}'}


def test_coerce_stored_value_helper():
    stored = FalkorDBAdapter._coerce_stored_value
    assert stored(b"hi") == "hi"
    assert stored({"a": 1}) == '{"a": 1}'
    assert stored([1, 2]) == [1, 2]
    assert stored([{"k": 1}]) == ['{"k": 1}']
    assert stored(Color.RED) == "red"
