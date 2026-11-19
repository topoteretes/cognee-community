"""GraphQL Schema Definition Language (schema.graphql) data-source connector for cognee.

Parses GraphQL SDL files (object types, queries, mutations, subscriptions, inputs,
interfaces, unions, enums, fields with arguments, and docstrings) into structured
documents ready for :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_graphql import graphql_source

    await cognee.remember(
        graphql_source("/path/to/schema.graphql"),
        dataset_name="api_schema",
        max_rows_per_table=0,  # ingest the full schema snapshot
    )

Design
------
* **Primary key** — ``id = "graphql:{kind}:{name}"``. Stable across re-syncs so
  incremental updates only re-cognify changed schema definitions.
* **Snapshot sync (``replace``)** — Schemas represent the current contract of an API.
  Using ``write_disposition="replace"`` ensures that any type, field, or mutation removed
  from the schema is automatically purged from the graph and vector index via cognee's
  orphan cleanup.
* **Agent-Optimized SDL Representation** — Arguments, nullability (``!``), list wrappers
  (``[...]``), and descriptions (``\"\"\"...\"\"\"``) are structured so AI coding agents
  can accurately construct queries, mutations, and resolvers without schema hallucinations.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("graphql_connector")
except ImportError:
    import logging
    logger = logging.getLogger("graphql_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

GRAPHQL_TABLE_NAME = "graphql_schema_entities"
GRAPHQL_SOURCE_NAME = "graphql"

_BLOCK_HEADER_RE = re.compile(
    r"^(?P<kind>type|input|interface|enum|union)\s+(?P<name>[A-Za-z0-9_]+)"
    r"(?:\s+implements\s+(?P<implements>[A-Za-z0-9_,\s&]+))?"
    r"(?:\s*=\s*(?P<union_members>[A-Za-z0-9_|\s]+))?"
    r"\s*(?:\{)?"
)


def parse_graphql_sdl_content(sdl_text: str) -> list[dict[str, Any]]:
    """Parse GraphQL SDL text into structured entities (types, inputs, enums, unions)."""
    entities: list[dict[str, Any]] = []
    lines = sdl_text.splitlines()

    pending_desc: list[str] = []
    in_triple_quote = False
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Handle multi-line docstring: """ ... """
        if '"""' in line:
            if line.startswith('"""') and line.endswith('"""') and len(line) > 6:
                pending_desc.append(line[3:-3].strip())
                i += 1
                continue
            elif in_triple_quote:
                clean_part = line.split('"""')[0].strip()
                if clean_part:
                    pending_desc.append(clean_part)
                in_triple_quote = False
                i += 1
                continue
            else:
                in_triple_quote = True
                clean_part = line.split('"""', 1)[1].strip()
                if clean_part:
                    pending_desc.append(clean_part)
                i += 1
                continue

        if in_triple_quote:
            pending_desc.append(line)
            i += 1
            continue

        # Handle single-line comment # ...
        if line.startswith("#"):
            pending_desc.append(line.lstrip("#").strip())
            i += 1
            continue

        if not line:
            pending_desc = []
            i += 1
            continue

        match = _BLOCK_HEADER_RE.match(line)
        if match:
            kind = match.group("kind")
            name = match.group("name")
            implements = match.group("implements")
            union_members = match.group("union_members")
            description = " ".join(part for part in pending_desc if part).strip()
            pending_desc = []

            # Handle union definition (e.g. union Result = User | Error)
            if kind == "union" and union_members:
                members = [m.strip() for m in union_members.split("|") if m.strip()]
                entities.append({
                    "kind": "union",
                    "name": name,
                    "description": description,
                    "members": members,
                    "fields": [],
                })
                i += 1
                continue

            # Read block lines until closing '}'
            block_lines: list[str] = []
            if "{" in line and not line.endswith("{"):
                # single line block: type Foo { id: ID }
                inner = line[line.find("{") + 1 : line.rfind("}")].strip()
                if inner:
                    block_lines.append(inner)
            else:
                i += 1
                while i < len(lines):
                    b_line = lines[i].strip()
                    if b_line == "}" or b_line.endswith("}"):
                        break
                    block_lines.append(b_line)
                    i += 1

            fields: list[dict[str, Any]] = []
            enum_values: list[str] = []

            for b in block_lines:
                if not b or b.startswith("#"):
                    continue
                if kind == "enum":
                    enum_values.append(b.split()[0].strip())
                else:
                    # Parse field line e.g., "users(limit: Int = 10): [User!]! @auth"
                    if ")" in b:
                        paren_idx = b.rfind(")")
                        colon_idx = b.find(":", paren_idx)
                    else:
                        colon_idx = b.find(":")

                    if colon_idx != -1:
                        field_name = b[:colon_idx].strip()
                        field_type = b[colon_idx + 1 :].strip()
                        fields.append({
                            "signature": b,
                            "name": field_name,
                            "type": field_type,
                        })

            entities.append({
                "kind": kind,
                "name": name,
                "description": description,
                "implements": implements.strip() if implements else "",
                "fields": fields,
                "enum_values": enum_values,
                "members": [],
            })

        i += 1

    return entities


def _format_graphql_document(entity: dict[str, Any], schema_file: str) -> dict[str, Any]:
    kind = entity["kind"]
    name = entity["name"]
    desc = entity.get("description", "")
    implements = entity.get("implements", "")
    fields = entity.get("fields", [])
    enum_values = entity.get("enum_values", [])
    members = entity.get("members", [])

    lines: list[str] = [
        f"GraphQL {kind.capitalize()}: {name}",
        f"Schema File: {schema_file}",
    ]

    if desc:
        lines.append(f"Description: {desc}")
    if implements:
        lines.append(f"Implements: {implements}")

    if kind == "enum" and enum_values:
        lines.append(f"Allowed Values: {', '.join(enum_values)}")
    elif kind == "union" and members:
        lines.append(f"Union Members: {', '.join(members)}")
    elif fields:
        lines.append("Fields / Operations:")
        for f in fields:
            lines.append(f"  - {f['signature']}")

    content_str = "\n".join(lines)
    primary_key = f"graphql:{kind.lower()}:{name.lower()}"
    title = f"[GraphQL {kind.capitalize()}] {name}"

    return {
        "id": primary_key,
        "title": title,
        "content": content_str,
        "kind": kind,
        "name": name,
        "schema_file": schema_file,
    }


def iter_graphql_schema_entities(schema_path: str | os.PathLike) -> Iterator[dict[str, Any]]:
    """Yield parsed GraphQL entities from an SDL schema file or directory."""
    path = Path(schema_path)
    if not path.exists():
        raise FileNotFoundError(f"GraphQL schema path does not exist: {path}")

    files: list[Path] = []
    if path.is_file():
        files.append(path)
    elif path.is_dir():
        files.extend(sorted(path.glob("**/*.graphql")))
        files.extend(sorted(path.glob("**/*.gql")))
        files = list(dict.fromkeys(files))

    if not files:
        logger.warning("GraphQL connector: no .graphql or .gql schema files found in %s", path)
        return

    count = 0
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("GraphQL connector: failed to read file %s: %s", f, exc)
            continue

        entities = parse_graphql_sdl_content(content)
        for ent in entities:
            count += 1
            yield _format_graphql_document(ent, f.name)

    logger.info("GraphQL connector: successfully parsed %d entity(ies) from %s", count, path)


def graphql_source(schema_path: str | os.PathLike):
    """Return a ``dlt`` resource over a GraphQL SDL schema for :func:`cognee.remember`.

    Args:
        schema_path: Path to a ``.graphql`` file or directory containing schema files.

    Returns:
        A ``dlt`` resource (``graphql_schema_entities``) with ``write_disposition="replace"``
        and document source marker (``cognee_document_source = "graphql"``).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The GraphQL connector requires the dlt extra: pip install "cognee[dlt]".'
        ) from exc

    source_path = str(schema_path)

    @dlt.resource(
        name=GRAPHQL_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def graphql_schema_entities():
        yield from iter_graphql_schema_entities(source_path)

    setattr(graphql_schema_entities, DOCUMENT_SOURCE_ATTR, GRAPHQL_SOURCE_NAME)
    return graphql_schema_entities
