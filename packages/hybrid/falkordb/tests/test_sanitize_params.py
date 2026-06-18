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


def test_dict_array_elements_stringified():
    # dict elements aren't valid FalkorDB array members -> each becomes a JSON
    # string, keeping a (now-primitive) array FalkorDB accepts.
    result = FalkorDBAdapter._sanitize_cypher_params({"items": [{"k": 1}, {"k": 2}]})
    assert result == {"items": ['{"k": 1}', '{"k": 2}']}


def test_array_with_null_json_encoded():
    # FalkorDB rejects null elements inside arrays; the array can't be made
    # all-primitive, so the whole array is stored as a JSON string instead.
    result = FalkorDBAdapter._sanitize_cypher_params({"tags": ["a", None]})
    assert result == {"tags": '["a", null]'}


def test_coerce_param_value_helper():
    coerce = FalkorDBAdapter._coerce_param_value
    assert coerce(b"hi") == "hi"
    assert coerce("x") == "x"
    assert coerce(7) == 7
    assert coerce(None) is None
    assert coerce([1, 2]) == [1, 2]
    assert coerce(Color.RED) == "red"
