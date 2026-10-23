"""DLT source for AsyncAPI specifications (full-snapshot sync + forget-on-delete).

Discovers, parses, and ingests AsyncAPI specifications (asyncapi.yaml / asyncapi.json)
defining event-driven architectures (Kafka topics, WebSockets, RabbitMQ, MQTT)
as standard documents for Cognee's ingestion pipeline.

Declares ``cognee_document_source = "asyncapi"`` so each event channel, topic, and message
schema flows through the standard cognify entity-extraction pipeline (for architecture
and event payloads) rather than the tabular dlt schema path.

Uses a full-snapshot model (``write_disposition="replace"``). Deletions propagate
automatically: any channel, message, or spec removed upstream falls out of the snapshot,
allowing Cognee's ``orphan_cleanup`` to prune it from the knowledge graph.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("asyncapi_connector")
except ImportError:
    logger = logging.getLogger("asyncapi_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

ASYNCAPI_TABLE_NAME = "asyncapi_specs"
ASYNCAPI_SOURCE_NAME = "asyncapi"

_EXTRA_HINT = (
    'The AsyncAPI connector requires "dlt" and "pyyaml": '
    'pip install "dlt[sqlalchemy]" pyyaml'
)


def _load_spec_file(file_path: Path) -> dict[str, Any]:
    """Loads and parses an AsyncAPI YAML or JSON specification file."""
    try:
        content = file_path.read_text(encoding="utf-8")
        if file_path.suffix.lower() == ".json":
            return json.loads(content)
        return yaml.safe_load(content) or {}
    except Exception as exc:
        logger.warning("Failed to parse AsyncAPI file %s: %s", file_path, exc)
        return {}


def _render_payload_properties(payload_schema: Any) -> list[str]:
    """Formats payload JSON schema properties into readable Markdown."""
    lines: list[str] = []
    if not isinstance(payload_schema, dict):
        return [f"Schema: `{payload_schema}`"]

    props = payload_schema.get("properties") or {}
    required = set(payload_schema.get("required") or [])

    if not props:
        schema_type = payload_schema.get("type", "object")
        return [f"Type: `{schema_type}`"]

    for prop_name, prop_def in props.items():
        if isinstance(prop_def, dict):
            p_type = prop_def.get("type", "string")
            p_desc = prop_def.get("description", "")
            req_flag = " *(required)*" if prop_name in required else ""
            desc_str = f" - {p_desc}" if p_desc else ""
            lines.append(f"- **{prop_name}** (`{p_type}`){req_flag}{desc_str}")
        else:
            lines.append(f"- **{prop_name}**: `{prop_def}`")

    return lines


def _render_channel_row(
    app_title: str,
    channel_name: str,
    operation_type: str,
    operation_data: dict[str, Any],
    server_names: list[str],
) -> dict[str, Any]:
    """Renders a single channel operation into a normalized document row."""
    op_summary = operation_data.get("summary", "")
    op_desc = operation_data.get("description", "")
    message_data = operation_data.get("message") or {}

    lines = [
        f"# {app_title}",
        f"## Channel / Topic: `{channel_name}`",
        f"**Operation:** `{operation_type.upper()}`",
    ]
    if server_names:
        lines.append(f"**Target Brokers/Servers:** {', '.join(server_names)}")
    if op_summary:
        lines.append(f"> **Summary:** {op_summary}")
    if op_desc:
        lines.append(f"> {op_desc}")

    # Message Schema
    if isinstance(message_data, dict):
        msg_name = message_data.get("name") or message_data.get("title") or "EventMessage"
        content_type = message_data.get("contentType", "application/json")
        lines.append(f"\n### Message: {msg_name}")
        lines.append(f"- **Content-Type:** `{content_type}`")

        msg_summary = message_data.get("summary")
        if msg_summary:
            lines.append(f"- **Summary:** {msg_summary}")

        payload = message_data.get("payload")
        if payload:
            lines.append("\n### Payload Schema Properties")
            lines.extend(_render_payload_properties(payload))

    content = "\n".join(lines)
    return {
        "id": f"asyncapi:{channel_name}:{operation_type.lower()}",
        "url": f"/{channel_name.strip().lstrip('/')}",
        "title": f"AsyncAPI: {operation_type.upper()} {channel_name}",
        "content": content,
    }


def asyncapi_source(
    spec_path: str | Path | None = None,
    client: Any = None,
):
    """Create a dlt source that yields AsyncAPI channel and message definitions.

    Args:
        spec_path: Path to an asyncapi.yaml/json file or directory containing specs.
                   Falls back to current directory or ASYNCAPI_PATH env var.
        client: Optional pre-injected client for testing.

    Returns:
        A dlt source suitable for cognee.add(...) / cognee.remember(...).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_path = (
        Path(spec_path)
        if spec_path
        else Path(os.environ.get("ASYNCAPI_PATH", "."))
    ).resolve()

    if not resolved_path.exists():
        raise ValueError(
            f"Valid AsyncAPI file or directory required. '{resolved_path}' does not exist."
        )

    spec_files: list[Path] = []
    if resolved_path.is_file():
        spec_files = [resolved_path]
    else:
        for ext in ("*.yaml", "*.yml", "*.json"):
            for p in resolved_path.rglob(ext):
                if "asyncapi" in p.name.lower():
                    spec_files.append(p)
        if not spec_files:
            # Fallback to any yaml/json in the folder
            for ext in ("*.yaml", "*.yml"):
                spec_files.extend(resolved_path.glob(ext))

    @dlt.resource(name=ASYNCAPI_TABLE_NAME, primary_key="id", write_disposition="replace")
    def asyncapi_specs() -> Iterator[dict[str, Any]]:
        synced_count = 0

        for file_path in spec_files:
            spec = _load_spec_file(file_path)
            if not isinstance(spec, dict) or not spec:
                continue

            app_title = spec.get("info", {}).get("title") or file_path.stem.title()
            servers = list(spec.get("servers", {}).keys())

            channels = spec.get("channels") or {}
            for channel_name, channel_def in channels.items():
                if not isinstance(channel_def, dict):
                    continue

                # V2 style: subscribe / publish under channel
                for op_key in ("publish", "subscribe"):
                    if op_key in channel_def and isinstance(channel_def[op_key], dict):
                        synced_count += 1
                        yield _render_channel_row(
                            app_title=app_title,
                            channel_name=channel_name,
                            operation_type=op_key,
                            operation_data=channel_def[op_key],
                            server_names=servers,
                        )

                # Direct message definition (or V3 messages)
                if "publish" not in channel_def and "subscribe" not in channel_def:
                    synced_count += 1
                    yield _render_channel_row(
                        app_title=app_title,
                        channel_name=channel_name,
                        operation_type="channel",
                        operation_data=channel_def,
                        server_names=servers,
                    )

        logger.info("AsyncAPI: synced %d event channel operation(s).", synced_count)

    @dlt.source(name=ASYNCAPI_SOURCE_NAME)
    def _asyncapi():
        return asyncapi_specs

    source = _asyncapi()
    setattr(source, DOCUMENT_SOURCE_ATTR, ASYNCAPI_SOURCE_NAME)
    return source
