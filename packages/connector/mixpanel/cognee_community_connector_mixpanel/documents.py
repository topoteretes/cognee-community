"""Deterministic definition documents. Provider data is never evaluated as code."""

import hashlib
import json
import re
from typing import Any

from .errors import ResponseContractError

RENDERER_VERSION = 1
_DENIED = {
    "creator",
    "creator_email",
    "created_by",
    "owned_by",
    "contacts",
    "teamContacts",
    "results",
    "result",
    "rows",
    "members",
    "profiles",
    "samples",
    "sample",
    "examples",
    "sample_values",
    "users",
    "access_token",
    "secret",
    "password",
}
_SENSITIVE_PROPERTY = re.compile(
    r"(?:email|phone|distinct[_ ]?id|user[_ ]?id|account[_ ]?id|address|ssn|password|token)", re.I
)
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
# Top-level semantic report parameters observed in the official SDK's builders.
# Unknown roots fail rather than silently dropping a new analytical definition.
_REPORT_KEYS = {
    # Released bookmark_schema.py / workspace.py canonical bookmark roots.
    "sections",
    "displayOptions",
    "sorting",
    "columnWidths",
    "forecastComparison",
    "legend",
    "liftComparison",
    "timeComparison",
    "versions",
    "flows_merge_type",
    "count_type",
    "cardinality_threshold",
    "anchor_position",
    "alignment",
    "collapse_repeated",
    "show_custom_events",
    "hidden_events",
    "filter_by_cohort",
    "filter_by_event",
    "aggregate_by",
    "time_percentiles_enabled",
    "chartType",
    "events",
    "metrics",
    "formula",
    "formulas",
    "filters",
    "filter",
    "where",
    "group_by",
    "group",
    "groupings",
    "breakdown",
    "breakdowns",
    "segments",
    "segment",
    "steps",
    "funnel",
    "funnel_order",
    "funnel_window",
    "conversion_window",
    "conversion_window_unit",
    "from_date",
    "to_date",
    "date",
    "date_range",
    "date_unit",
    "unit",
    "interval",
    "time",
    "time_unit",
    "time_comparison",
    "cohort",
    "cohorts",
    "retention",
    "retention_type",
    "retention_mode",
    "retention_unit",
    "retention_buckets",
    "born_event",
    "event",
    "return_event",
    "start_event",
    "end_event",
    "exclusions",
    "exclude",
    "include",
    "math",
    "math_property",
    "property",
    "properties",
    "count",
    "limit",
    "sort",
    "order",
    "mode",
    "type",
    "query",
    "queries",
    "clauses",
    "show",
    "show_clause",
    "showClauses",
    "filter_by",
    "on",
    "by",
    "chart_type",
    "display",
    "viz_type",
    "visualization",
    "frequency",
    "frequency_type",
    "per_user",
    "sampling_factor",
    "rollup",
    "behavior",
    "behaviors",
    "data_group_id",
    "project_id",
    "workspace_id",
    "version",
    "v",
    "last",
    "time_zone",
    "timezone",
    "use_project_timezone",
    "relative_date",
    "date_picker",
    "funnel_steps",
    "global_filters",
    "compare",
    "comparison",
    "analysis",
    "bucket",
}
_REPORT_METADATA = {"name", "modified", "user", "user_id", "id", "executedMigrations"}


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def semantic_hash(row: dict) -> str:
    return hashlib.sha256(canonical(row).encode()).hexdigest()


def _text(item: dict, key: str, *, required=False) -> str:
    value = item.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ResponseContractError(f"Definition has invalid {key}; no batch was published.")
    return value


def _sensitive_reference(value: Any, depth=0, reference=False) -> bool:
    if depth > 32:
        raise ResponseContractError("Definition exceeds maximum nesting depth.")
    if isinstance(value, dict):
        return any(
            _sensitive_reference(
                v,
                depth + 1,
                reference
                or k in {"property", "name", "key", "field", "propertyName", "property_name"},
            )
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_sensitive_reference(v, depth + 1, reference) for v in value)
    return isinstance(value, str) and reference and bool(_SENSITIVE_PROPERTY.search(value))


def _sanitize(value: Any, include_sensitive: bool, depth=0, sensitive=False) -> Any:
    if depth > 32:
        raise ResponseContractError("Definition exceeds maximum nesting depth.")
    if isinstance(value, dict):
        # A selector commonly puts its property and literal in sibling nodes.
        if any(not isinstance(k, str) for k in value):
            raise ResponseContractError("Definition keys must be strings.")
        # Comparisons can split property and literal across sibling children.
        # Boolean containers leave sensitivity to their individual predicates.
        direct = any(
            _sensitive_reference(v, reference=True)
            for k, v in value.items()
            if k in {"property", "name", "key", "field", "propertyName", "property_name"}
        )
        direct = direct or (
            "filterValue" in value and _sensitive_reference(value.get("value"), reference=True)
        )
        comparison = value.get("operator") not in (None, "and", "or", "not")
        sensitive = sensitive or direct or (comparison and _sensitive_reference(value))
        output = {}
        for key, child in sorted(value.items()):
            if not isinstance(key, str):
                raise ResponseContractError("Definition keys must be strings.")
            if key in _DENIED:
                output[key] = "[omitted non-definition data]"
                continue
            if (
                not include_sensitive
                and sensitive
                and key in {"value", "values", "literal", "enum", "const", "default", "filterValue"}
                and not (key == "value" and "filterValue" in value)
            ):
                output[key] = "[redacted sensitive literal]"
            else:
                property_map = key == "properties" and isinstance(child, dict)
                if property_map:
                    output[key] = {
                        name: _sanitize(
                            prop,
                            include_sensitive,
                            depth + 1,
                            sensitive or bool(_SENSITIVE_PROPERTY.search(name)),
                        )
                        for name, prop in child.items()
                    }
                else:
                    output[key] = _sanitize(child, include_sensitive, depth + 1, sensitive)
        return output
    if isinstance(value, list):
        return [_sanitize(v, include_sensitive, depth + 1, sensitive) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        # Reject NaN/infinity, which have no deterministic JSON representation.
        canonical(value)
        return value
    if isinstance(value, str):
        if (
            not include_sensitive
            and _SENSITIVE_PROPERTY.search(value)
            and ("properties[" in value or "user[" in value)
        ):
            return "[redacted opaque sensitive predicate]"
        return value if include_sensitive else _EMAIL.sub("[redacted email]", value)
    raise ResponseContractError("Definition contains a non-JSON value.")


def report_definition(item: dict) -> dict:
    params = item.get("params")
    if not isinstance(params, dict) or not params:
        raise ResponseContractError("Saved report is missing its query definition (params).")
    if set(params) - _REPORT_KEYS - _REPORT_METADATA:
        raise ResponseContractError(
            "Report has unsupported parameter fields. Extend the renderer from verified "
            "provider fixtures before syncing this report."
        )
    return {k: v for k, v in params.items() if k not in _REPORT_METADATA}


def cohort_definition(item: dict) -> dict:
    # The SDK's Cohort model preserves these extras without requiring them.
    # Do not assume a universal `definition` field or ingest membership counts.
    root = item.get("definition", item)
    if not isinstance(root, dict):
        raise ResponseContractError("Cohort definition must be an object.")
    if isinstance(root.get("selector"), (dict, str)):
        if not root["selector"] or (
            isinstance(root["selector"], str) and not root["selector"].strip()
        ):
            raise ResponseContractError("Cohort selector is empty.")
        result = {"selector": root["selector"]}
        if "behaviors" in root:
            if not isinstance(root["behaviors"], (list, dict)):
                raise ResponseContractError("Cohort behaviors have an unsupported shape.")
            result["behaviors"] = root["behaviors"]
        return result
    if isinstance(root.get("groups"), list) and root["groups"]:
        return {k: root[k] for k in ("groups", "operator", "group_operator") if k in root}
    raise ResponseContractError(
        "Cohort is missing supported predicates (selector/behaviors or groups). "
        "Check definition access; names and member counts are insufficient."
    )


def _schema_definition(item: dict) -> dict:
    schema = item.get("schemaJson")
    if not isinstance(schema, dict):
        raise ResponseContractError("Event schema is missing schemaJson.")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ResponseContractError("schemaJson.properties must be an object map.")
    approved = {
        k: schema[k] for k in ("description", "required", "additionalProperties") if k in schema
    }
    approved["properties"] = {}
    for name, prop in sorted(properties.items()):
        if not isinstance(prop, dict) or not isinstance(name, str):
            raise ResponseContractError("Event property definition is malformed.")
        approved["properties"][name] = {
            k: prop[k]
            for k in (
                "type",
                "description",
                "required",
                "items",
                "properties",
                "additionalProperties",
                "enum",
            )
            if k in prop
        }
        meta = _schema_metadata(prop)
        if meta:
            approved["properties"][name]["metadata"] = meta
    meta = _schema_metadata(schema)
    if meta:
        approved["metadata"] = meta
    return approved


def _schema_metadata(schema: dict) -> dict:
    metadata = schema.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ResponseContractError("Schema metadata must be an object.")
    provider = metadata.get("com.mixpanel", {})
    if not isinstance(provider, dict):
        raise ResponseContractError("Mixpanel schema metadata must be an object.")
    result = {k: provider[k] for k in ("displayName", "hidden", "dropped") if k in provider}
    if "tags" in provider:
        if not isinstance(provider["tags"], list) or not all(
            isinstance(t, str) for t in provider["tags"]
        ):
            raise ResponseContractError("Schema tags must be strings.")
        result["tags"] = sorted(set(provider["tags"]))
    return result


def document(kind: str, item: dict, scope: str, project_url: str, include_sensitive=False) -> dict:
    name = _text(item, "name", required=True)
    if kind == "schema":
        if item.get("entityType") != "event":
            raise ResponseContractError("Only registered event schemas are supported.")
        remote_id = name
        definition = _schema_definition(item)
    else:
        remote_id = item.get("id")
        if isinstance(remote_id, bool) or not isinstance(remote_id, int) or remote_id <= 0:
            raise ResponseContractError("Report/cohort ID must be a positive integer.")
        definition = report_definition(item) if kind == "report" else cohort_definition(item)
    description = "" if kind == "schema" else _text(item, "description")
    semantic = {
        "name": name,
        "description": description,
        "definition": _sanitize(definition, include_sensitive),
        "renderer_version": RENDERER_VERSION,
    }
    if kind == "report":
        semantic["report_type"] = _text(item, "type", required=True)
    # Metadata never includes arbitrary SDK extras or dynamic counts/timestamps.
    label = {"schema": "Event schema", "report": "Saved report", "cohort": "Cohort"}[kind]
    safe_name = _sanitize(name, include_sensitive)
    safe_description = _sanitize(description, include_sensitive)
    content = f"{label}: {safe_name}\n"
    if safe_description:
        content += f"\n{safe_description}\n"
    if kind == "report":
        content += f"\nReport type: {semantic['report_type']}\n"
    content += (
        "\nStructured definition (preserves ordering and boolean grouping; "
        "referenced IDs are not expanded):\n\n```json\n"
        + json.dumps(
            semantic["definition"], sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
        )
        + "\n```"
    )
    return {
        "id": canonical([scope, kind, remote_id]),
        "title": f"Mixpanel {label}: {safe_name}",
        "content": content,
        "url": project_url,
        "_deleted": False,
        "renderer_version": RENDERER_VERSION,
    }
