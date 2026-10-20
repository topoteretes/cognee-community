"""Read-only response evidence. This module never interprets records as sync data."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import validators
from jsonschema.exceptions import SchemaError, ValidationError

from cognee_community_connector_logrocket.client import CapabilitySnapshot, LogRocketMCPError

ALLOWED_TOOLS = frozenset({"find_sessions", "find_issues"})
_ANNOTATION_KEYS = frozenset({"description", "examples", "title"})


def _validation_contract(value: Any) -> Any:
    """Remove JSON Schema annotations that do not affect request validation."""
    if isinstance(value, dict):
        return {
            key: _validation_contract(item)
            for key, item in value.items()
            if key not in _ANNOTATION_KEYS
        }
    if isinstance(value, list):
        return [_validation_contract(item) for item in value]
    return value


def response_shape(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Keep field names, types, and lengths, never scalar record values.

    Field names can themselves be sensitive in dynamic maps. Review reports
    before sharing them. Depth and array sampling bound the size of reports.
    """
    if depth >= 12:
        return {"type": "depth_limit"}
    if isinstance(value, dict):
        return {
            "type": "object",
            "fields": {key: response_shape(item, depth=depth + 1) for key, item in value.items()},
        }
    if isinstance(value, list):
        shapes = {}
        for item in value[:100]:
            shape = response_shape(item, depth=depth + 1)
            shapes[json.dumps(shape, sort_keys=True)] = shape
        return {
            "type": "array",
            "length": len(value),
            "sampled": min(len(value), 100),
            "item_shapes": list(shapes.values()),
        }
    kind = (
        "null"
        if value is None
        else "boolean"
        if isinstance(value, bool)
        else "string"
        if isinstance(value, str)
        else "number"
    )
    return {"type": kind}


def validate_request(snapshot: CapabilitySnapshot, name: str, arguments: dict[str, Any]):
    if name not in ALLOWED_TOOLS:
        raise ValueError("Only find_sessions and find_issues may be probed.")
    matches = [tool for tool in snapshot.tools if tool.name == name]
    if len(matches) != 1:
        raise ValueError("The selected tool must be advertised exactly once.")
    tool = matches[0]
    # Require an explicit small budget even if the server advertises a larger default.
    limit = arguments.get("limit")
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Probes require an explicit integer limit between 1 and 5.")
    try:
        validator = validators.validator_for(tool.input_schema)
        validator.check_schema(tool.input_schema)
        validator(tool.input_schema).validate(arguments)
    except (SchemaError, ValidationError):
        # ValidationError text includes the rejected values, which may be sensitive.
        raise ValueError("Probe arguments do not conform to the advertised input schema.") from None
    return tool


async def probe_tool(
    client: Any,
    snapshot: CapabilitySnapshot,
    baseline: CapabilitySnapshot,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    tool = validate_request(snapshot, name, arguments)
    previous = [item for item in baseline.tools if item.name == name]
    if len(previous) != 1 or (
        _validation_contract(tool.input_schema) != _validation_contract(previous[0].input_schema)
        or _validation_contract(tool.output_schema)
        != _validation_contract(previous[0].output_schema)
    ):
        raise ValueError("Tool definition changed; inspect and review a fresh baseline first.")
    result = await client.call_tool(name, arguments)
    if result.is_error:
        raise LogRocketMCPError("LogRocket returned a tool error; response content withheld.")
    payload = result.structured_content
    encoding = "structured_content"
    if payload is None:
        blocks = result.content
        if len(blocks) != 1 or getattr(blocks[0], "type", None) != "text":
            raise LogRocketMCPError("Probe response is not a single JSON text or structured value.")
        try:
            payload = json.loads(blocks[0].text)
        except (ValueError, TypeError):
            raise LogRocketMCPError("Probe response text is not JSON; content withheld.") from None
        encoding = "json_text"
    return {
        "tool": name,
        "encoding": encoding,
        "response_shape": response_shape(payload),
        "sync_implementation_allowed": False,
        "note": "Shapes do not prove stable identity, complete pagination, or deletion semantics.",
    }
