"""Evidence-based analysis of LogRocket MCP tool definitions.

The inspector distinguishes schema evidence from assumptions. A ``limit``
argument alone does not prove pagination, and a list-like response does not
prove that absence means deletion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from cognee_community_connector_logrocket.client import CapabilitySnapshot, ToolDefinition

REQUIRED_TOOLS = ("find_sessions", "find_issues")

_CURSOR_INPUT_NAMES = {
    "after",
    "before",
    "cursor",
    "offset",
    "page",
    "page_cursor",
    "page_token",
    "starting_after",
}
_LIMIT_INPUT_NAMES = {"first", "limit", "page_size", "per_page", "size"}
_CONTINUATION_OUTPUT_NAMES = {
    "has_more",
    "next",
    "next_cursor",
    "next_page",
    "next_page_token",
    "next_token",
    "page_info",
}
_IDENTITY_NAMES = {"id", "issue_id", "session_id", "recording_id"}
_DELETION_NAMES = {
    "archived",
    "deleted",
    "deleted_at",
    "deletion",
    "deletions",
    "is_archived",
    "is_deleted",
    "removed",
    "removed_at",
    "tombstone",
    "tombstones",
}
_TIME_NAMES = {
    "created_at",
    "end_time",
    "first_seen",
    "last_seen",
    "start_time",
    "timestamp",
    "updated_at",
}

EvidenceStatus = Literal["schema_evidence", "partial", "unverified", "missing"]


@dataclass(frozen=True)
class ToolCapability:
    name: str
    present: bool
    response_schema: bool
    pagination: EvidenceStatus
    pagination_evidence: tuple[str, ...]
    stable_identity: EvidenceStatus
    identity_evidence: tuple[str, ...]
    timestamps: EvidenceStatus
    timestamp_evidence: tuple[str, ...]
    deletion_visibility: EvidenceStatus
    deletion_evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "present": self.present,
            "response_schema": self.response_schema,
            "pagination": self.pagination,
            "pagination_evidence": list(self.pagination_evidence),
            "stable_identity": self.stable_identity,
            "identity_evidence": list(self.identity_evidence),
            "timestamps": self.timestamps,
            "timestamp_evidence": list(self.timestamp_evidence),
            "deletion_visibility": self.deletion_visibility,
            "deletion_evidence": list(self.deletion_evidence),
        }


@dataclass(frozen=True)
class CapabilityReport:
    endpoint: str
    protocol_version: str | None
    tools: tuple[ToolCapability, ...]
    sync_implementation_allowed: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "protocol_version": self.protocol_version,
            "tools": [tool.to_dict() for tool in self.tools],
            "sync_implementation_allowed": self.sync_implementation_allowed,
            "blockers": list(self.blockers),
        }


def _property_paths(schema: dict[str, Any] | None) -> set[str]:
    """Return dotted paths for every JSON Schema property, including branches."""
    if not schema:
        return set()
    paths: set[str] = set()
    visited: set[int] = set()

    def visit(node: Any, prefix: str = "") -> None:
        if isinstance(node, dict):
            marker = id(node)
            if marker in visited:
                return
            visited.add(marker)
            properties = node.get("properties")
            if isinstance(properties, dict):
                for name, child in properties.items():
                    path = f"{prefix}.{name}" if prefix else str(name)
                    paths.add(path)
                    visit(child, path)
            for key in ("items", "additionalProperties", "contains", "not"):
                visit(node.get(key), prefix)
            for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
                branches = node.get(key)
                if isinstance(branches, list):
                    for branch in branches:
                        visit(branch, prefix)
            definitions = node.get("$defs", node.get("definitions"))
            if isinstance(definitions, dict):
                for child in definitions.values():
                    visit(child, prefix)
        elif isinstance(node, list):
            for child in node:
                visit(child, prefix)

    visit(schema)
    return paths


def _matching_paths(paths: set[str], names: set[str]) -> tuple[str, ...]:
    return tuple(sorted(path for path in paths if path.rsplit(".", 1)[-1].lower() in names))


def _deletion_tools_for(resource_name: str, tools: tuple[ToolDefinition, ...]) -> tuple[str, ...]:
    resource = resource_name.removeprefix("find_").removesuffix("s")
    evidence: list[str] = []
    for tool in tools:
        lowered = tool.name.lower()
        output_paths = _property_paths(tool.output_schema)
        deletion_paths = _matching_paths(output_paths, _DELETION_NAMES)
        identity_paths = _matching_paths(output_paths, _IDENTITY_NAMES)
        if resource in lowered and deletion_paths and identity_paths:
            evidence.extend(f"tool:{tool.name}:output:{path}" for path in deletion_paths)
    return tuple(sorted(evidence))


def _inspect_tool(name: str, tools: tuple[ToolDefinition, ...]) -> ToolCapability:
    tool = next((candidate for candidate in tools if candidate.name == name), None)
    if tool is None:
        return ToolCapability(
            name=name,
            present=False,
            response_schema=False,
            pagination="missing",
            pagination_evidence=(),
            stable_identity="missing",
            identity_evidence=(),
            timestamps="missing",
            timestamp_evidence=(),
            deletion_visibility="missing",
            deletion_evidence=(),
        )

    input_paths = _property_paths(tool.input_schema)
    output_paths = _property_paths(tool.output_schema)
    cursor_inputs = _matching_paths(input_paths, _CURSOR_INPUT_NAMES)
    limit_inputs = _matching_paths(input_paths, _LIMIT_INPUT_NAMES)
    continuations = _matching_paths(output_paths, _CONTINUATION_OUTPUT_NAMES)
    pagination_evidence = tuple(
        [*(f"input:{path}" for path in cursor_inputs), *(f"input:{path}" for path in limit_inputs)]
        + [f"output:{path}" for path in continuations]
    )
    if cursor_inputs and continuations:
        pagination: EvidenceStatus = "schema_evidence"
    elif cursor_inputs or limit_inputs or continuations:
        pagination = "partial"
    else:
        pagination = "unverified"

    identities = _matching_paths(output_paths, _IDENTITY_NAMES)
    timestamps = _matching_paths(output_paths, _TIME_NAMES)
    deletion_fields = _matching_paths(output_paths, _DELETION_NAMES)
    deletion_tools = _deletion_tools_for(name, tools)
    deletion_evidence = (
        *(f"output:{path}" for path in deletion_fields),
        *deletion_tools,
    )

    return ToolCapability(
        name=name,
        present=True,
        response_schema=tool.output_schema is not None,
        pagination=pagination,
        pagination_evidence=pagination_evidence,
        stable_identity="schema_evidence" if identities else "unverified",
        identity_evidence=tuple(f"output:{path}" for path in identities),
        timestamps="schema_evidence" if timestamps else "unverified",
        timestamp_evidence=tuple(f"output:{path}" for path in timestamps),
        deletion_visibility="schema_evidence" if deletion_evidence else "unverified",
        deletion_evidence=deletion_evidence,
    )


def inspect_snapshot(snapshot: CapabilitySnapshot) -> CapabilityReport:
    """Assess whether captured schemas are sufficient to design safe sync."""
    inspected = tuple(_inspect_tool(name, snapshot.tools) for name in REQUIRED_TOOLS)
    blockers: list[str] = []
    for tool in inspected:
        if not tool.present:
            blockers.append(f"{tool.name}: tool is not advertised")
            continue
        if not tool.response_schema:
            blockers.append(f"{tool.name}: no MCP outputSchema is advertised")
        if tool.pagination != "schema_evidence":
            blockers.append(
                f"{tool.name}: exhaustive pagination is not proven by input and output schemas"
            )
        else:
            blockers.append(
                f"{tool.name}: live calls have not proven cursor progress, termination, or limits"
            )
        if tool.stable_identity != "schema_evidence":
            blockers.append(f"{tool.name}: stable record identity is not proven")
        if tool.deletion_visibility != "schema_evidence":
            blockers.append(
                f"{tool.name}: explicit deletion visibility or a deletion/audit tool is not proven"
            )
        else:
            blockers.append(
                f"{tool.name}: live behavior has not proven that deletions are observable"
            )

    return CapabilityReport(
        endpoint=snapshot.endpoint,
        protocol_version=snapshot.protocol_version,
        tools=inspected,
        # tools/list is necessary evidence, not sufficient evidence. Even rich
        # JSON Schemas cannot prove cursor termination or that deleted records
        # are actually returned by the server.
        sync_implementation_allowed=False,
        blockers=tuple(blockers),
    )
