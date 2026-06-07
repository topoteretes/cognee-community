"""Serialization helpers for the TurboPuffer graph adapter.

Ported from the TurboPuffer vector adapter so the graph adapter encodes node and
edge attributes into TurboPuffer-compatible scalar/array values, infers a stable
write schema, and respects the 4096-byte filterable-attribute limit.
"""

from datetime import datetime
from typing import List, Union, get_args, get_origin
from uuid import UUID

# TurboPuffer filterable attribute size limit is 4096 bytes. Larger string
# attributes are truncated so writes do not get rejected.
_MAX_ATTR_BYTES = 4096


def _serialize_value(value):
    """Recursively serialize complex types to turbopuffer-compatible values."""
    if isinstance(value, datetime):
        return value.isoformat()
    elif isinstance(value, UUID):
        return str(value)
    elif isinstance(value, dict):
        return str({k: _serialize_value(v) for k, v in value.items()})
    elif isinstance(value, list):
        # Keep lists as native arrays for ContainsAny filtering support.
        serialized = [_serialize_value(v) for v in value]
        # Only keep as list if all elements are strings.
        if all(isinstance(v, str) for v in serialized):
            return serialized
        return str(serialized)
    return value


def _truncate_large_values(payload: dict) -> dict:
    """Truncate string values exceeding turbopuffer's filterable attribute limit."""
    result = {}
    for key, value in payload.items():
        if isinstance(value, str) and len(value.encode("utf-8")) > _MAX_ATTR_BYTES:
            encoded = value.encode("utf-8")[: _MAX_ATTR_BYTES - 3]
            result[key] = encoded.decode("utf-8", errors="ignore") + "..."
        else:
            result[key] = value
    return result


def _strip_optional(annotation):
    origin = get_origin(annotation)
    if origin is Union:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _annotation_to_turbopuffer_type(annotation):
    annotation = _strip_optional(annotation)
    origin = get_origin(annotation)

    if origin in (list, List):
        args = get_args(annotation)
        if len(args) != 1:
            return None
        item_type = _strip_optional(args[0])
        if item_type is str:
            return "[]string"
        if item_type is int:
            return "[]int"
        if item_type is float:
            return "[]float"
        if item_type is bool:
            return "[]bool"
        if item_type is UUID:
            return "[]uuid"
        if item_type is datetime:
            return "[]datetime"
        return None

    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is str:
        return "string"
    if annotation is UUID:
        return "uuid"
    if annotation is datetime:
        return "datetime"

    return None


def _infer_turbopuffer_attribute_type(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return "[]string"
        if all(isinstance(item, bool) for item in value):
            return "[]bool"
        if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            return "[]int"
        if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
            return "[]float"
    return None


def _merge_turbopuffer_types(existing_type: str, new_type: str) -> str:
    if existing_type == new_type:
        return existing_type

    # Promote numeric types to avoid int schemas rejecting float values later.
    if {existing_type, new_type} == {"int", "float"}:
        return "float"
    if {existing_type, new_type} == {"[]int", "[]float"}:
        return "[]float"

    # Keep existing type if no safe merge is known.
    return existing_type


def _build_row_schema(rows: list[dict]) -> dict:
    """Infer a TurboPuffer write schema from row values alone.

    The graph adapter writes plain dict rows (not DataPoint instances), so the
    schema is inferred from the actual values, widening numeric types when a
    field appears with mixed int/float values across rows.
    """
    schema: dict = {}
    for row in rows:
        for key, value in row.items():
            if key in ("id", "vector"):
                continue
            if value is None:
                continue
            inferred_type = _infer_turbopuffer_attribute_type(value)
            if inferred_type is None:
                continue
            if key not in schema:
                schema[key] = inferred_type
            else:
                schema[key] = _merge_turbopuffer_types(schema[key], inferred_type)
    return schema
