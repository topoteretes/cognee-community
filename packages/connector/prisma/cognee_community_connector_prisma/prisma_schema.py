"""Prisma ORM schema (schema.prisma) data-source connector for cognee.

Parses Prisma schema files (models, scalar fields, cross-model relations, composite keys,
and enums) into rich, structured documents ready for :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_prisma import prisma_source

    await cognee.remember(
        prisma_source("/path/to/schema.prisma"),
        dataset_name="database_schema",
        max_rows_per_table=0,  # ingest the full schema snapshot
    )

Design
------
* **Primary key** — ``id = "prisma:model:{model_name}"`` or ``"prisma:enum:{enum_name}"``.
  Stable across schema edits so incremental updates only re-cognify changed entities.
* **Snapshot sync (``replace``)** — Database schemas represent the current state of a database.
  Using ``write_disposition="replace"`` ensures that any model or enum removed from
  ``schema.prisma`` is automatically purged from the graph and vector index via cognee's
  orphan cleanup.
* **Agent-Optimized Relational Graph** — Foreign key relations (``@relation(fields: [...], references: [...])``),
  cardinalities (one-to-one, one-to-many), unique constraints, and documentation comments
  are structured so AI coding agents can generate queries without hallucinating schemas.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("prisma_connector")
except ImportError:
    import logging
    logger = logging.getLogger("prisma_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

PRISMA_TABLE_NAME = "prisma_models"
PRISMA_SOURCE_NAME = "prisma"

_BLOCK_START_RE = re.compile(r"^(model|enum|datasource|generator)\s+([A-Za-z0-9_]+)\s*\{")
_RELATION_ATTR_RE = re.compile(
    r"@relation\((?:name:\s*[\"'](?P<name>[^\"']+)[\"'],?\s*)?"
    r"(?:fields:\s*\[(?P<fields>[^\]]+)\],?\s*)?"
    r"(?:references:\s*\[(?P<references>[^\]]+)\])?.*?\)"
)


def _clean_tokens(s: str) -> list[str]:
    """Parse comma/space separated list of field names."""
    return [t.strip() for t in s.split(",") if t.strip()]


def parse_prisma_schema_content(schema_text: str) -> dict[str, Any]:
    """Parse schema.prisma text into structured models, enums, and datasource metadata."""
    models: dict[str, Any] = {}
    enums: dict[str, Any] = {}
    datasource: dict[str, str] = {}

    lines = schema_text.splitlines()
    pending_doc: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Collect doc comments: /// Description
        if line.startswith("///"):
            doc = line[3:].strip()
            pending_doc.append(doc)
            i += 1
            continue

        # Skip regular comments and blank lines
        if not line or line.startswith("//"):
            if not line:
                pending_doc = []
            i += 1
            continue

        block_match = _BLOCK_START_RE.match(line)
        if block_match:
            block_type = block_match.group(1)
            block_name = block_match.group(2)
            doc_comment = " ".join(pending_doc).strip()
            pending_doc = []

            # Gather all lines inside block until closing '}'
            block_lines: list[str] = []
            i += 1
            while i < len(lines):
                b_line = lines[i].strip()
                if b_line == "}" or b_line.endswith("}"):
                    break
                block_lines.append(b_line)
                i += 1

            if block_type == "model":
                models[block_name] = _parse_model_block(block_name, block_lines, doc_comment)
            elif block_type == "enum":
                enums[block_name] = _parse_enum_block(block_name, block_lines, doc_comment)
            elif block_type == "datasource":
                for b in block_lines:
                    if "=" in b:
                        k, v = b.split("=", 1)
                        datasource[k.strip()] = v.strip().strip('"')

        i += 1

    return {"models": models, "enums": enums, "datasource": datasource}


def _parse_model_block(model_name: str, lines: list[str], doc_comment: str) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    block_attributes: list[str] = []
    table_name = model_name

    for line in lines:
        if not line or line.startswith("//"):
            continue

        # Block-level attributes: @@id, @@index, @@map, @@unique
        if line.startswith("@@"):
            block_attributes.append(line)
            if line.startswith("@@map"):
                map_match = re.search(r'@@map\(["\']([^"\']+)["\']\)', line)
                if map_match:
                    table_name = map_match.group(1)
            continue

        # Field line: name type attributes...
        # e.g., "authorId  String  @relation(...) // inline comment"
        code_part = line.split("//")[0].strip()
        parts = code_part.split()
        if len(parts) < 2:
            continue

        field_name = parts[0]
        raw_type = parts[1]
        attributes = parts[2:]

        is_optional = raw_type.endswith("?")
        is_list = raw_type.endswith("[]")
        clean_type = raw_type.rstrip("?").rstrip("[]")

        # Check for relation attribute
        relation_match = _RELATION_ATTR_RE.search(code_part)
        relation_info = None
        if relation_match:
            fields_val = relation_match.group("fields")
            refs_val = relation_match.group("references")
            relation_info = {
                "name": relation_match.group("name") or "",
                "fields": _clean_tokens(fields_val) if fields_val else [],
                "references": _clean_tokens(refs_val) if refs_val else [],
            }

        fields.append({
            "name": field_name,
            "type": clean_type,
            "is_optional": is_optional,
            "is_list": is_list,
            "attributes": attributes,
            "relation_info": relation_info,
            "is_id": any(attr.startswith("@id") for attr in attributes),
            "is_unique": any(attr.startswith("@unique") for attr in attributes),
        })

    return {
        "name": model_name,
        "table_name": table_name,
        "description": doc_comment,
        "fields": fields,
        "block_attributes": block_attributes,
    }


def _parse_enum_block(enum_name: str, lines: list[str], doc_comment: str) -> dict[str, Any]:
    values: list[str] = []
    for line in lines:
        val = line.split("//")[0].strip()
        if val and not val.startswith("@@"):
            values.append(val)
    return {
        "name": enum_name,
        "description": doc_comment,
        "values": values,
    }


def _format_model_document(model: dict[str, Any], all_models: dict[str, Any], provider: str) -> dict[str, Any]:
    name = model["name"]
    table_name = model["table_name"]
    desc = model["description"]

    lines: list[str] = [
        f"Prisma Database Model: {name}",
        f"Database Table: {table_name}",
    ]
    if provider:
        lines.append(f"Database Provider: {provider}")
    if desc:
        lines.append(f"Description: {desc}")

    field_lines: list[str] = []
    relations_summary: list[str] = []
    pk_fields: list[str] = []
    unique_fields: list[str] = []

    for f in model["fields"]:
        type_str = f["type"]
        if f["is_list"]:
            type_str = f"{type_str}[] (List)"
        elif f["is_optional"]:
            type_str = f"{type_str}? (Optional)"

        notes: list[str] = []
        if f["is_id"]:
            notes.append("Primary Key")
            pk_fields.append(f["name"])
        if f["is_unique"]:
            notes.append("Unique")
            unique_fields.append(f["name"])

        # Check if type points to another model (relational field)
        target_model = f["type"]
        if target_model in all_models and target_model != name:
            if f["is_list"]:
                relations_summary.append(f"- Has many {target_model} (via field '{f['name']}')")
            else:
                rel = f.get("relation_info")
                if rel and rel.get("fields"):
                    relations_summary.append(
                        f"- Belongs to {target_model} (foreign key: {', '.join(rel['fields'])} -> {', '.join(rel.get('references', []))})"
                    )
                else:
                    relations_summary.append(f"- References {target_model} (via field '{f['name']}')")

        for attr in f["attributes"]:
            if attr.startswith("@default"):
                notes.append(attr)

        notes_str = f" [{', '.join(notes)}]" if notes else ""
        field_lines.append(f"  - {f['name']}: {type_str}{notes_str}")

    if pk_fields:
        lines.append(f"Primary Key: {', '.join(pk_fields)}")
    if unique_fields:
        lines.append(f"Unique Constraints: {', '.join(unique_fields)}")

    lines.append("Fields:")
    lines.extend(field_lines)

    if relations_summary:
        lines.append("Relations:")
        lines.extend(relations_summary)

    if model["block_attributes"]:
        lines.append("Model Attributes & Indexes:")
        for attr in model["block_attributes"]:
            lines.append(f"  - {attr}")

    content_str = "\n".join(lines)
    primary_key = f"prisma:model:{name.lower()}"
    title = f"[Prisma Model] {name} ({table_name})"

    return {
        "id": primary_key,
        "title": title,
        "content": content_str,
        "entity_type": "model",
        "name": name,
        "table_name": table_name,
        "field_count": len(model["fields"]),
    }


def _format_enum_document(enum_data: dict[str, Any]) -> dict[str, Any]:
    name = enum_data["name"]
    desc = enum_data["description"]
    values = enum_data["values"]

    lines = [
        f"Prisma Database Enum: {name}",
        f"Values: {', '.join(values)}",
    ]
    if desc:
        lines.append(f"Description: {desc}")

    content_str = "\n".join(lines)
    primary_key = f"prisma:enum:{name.lower()}"
    title = f"[Prisma Enum] {name}"

    return {
        "id": primary_key,
        "title": title,
        "content": content_str,
        "entity_type": "enum",
        "name": name,
        "values": values,
    }


def iter_prisma_schema_entities(schema_path: str | os.PathLike) -> Iterator[dict[str, Any]]:
    """Yield parsed models and enums from a schema.prisma file or directory containing .prisma files."""
    path = Path(schema_path)
    if not path.exists():
        raise FileNotFoundError(f"Prisma schema path does not exist: {path}")

    files: list[Path] = []
    if path.is_file():
        files.append(path)
    elif path.is_dir():
        files.extend(sorted(path.glob("**/*.prisma")))

    if not files:
        logger.warning("Prisma connector: no .prisma schema files found in %s", path)
        return

    count = 0
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Prisma connector: failed to read file %s: %s", f, exc)
            continue

        parsed = parse_prisma_schema_content(content)
        all_models = parsed["models"]
        provider = parsed["datasource"].get("provider", "")

        for enum_data in parsed["enums"].values():
            count += 1
            yield _format_enum_document(enum_data)

        for model_data in all_models.values():
            count += 1
            yield _format_model_document(model_data, all_models, provider)

    logger.info("Prisma connector: successfully parsed %d entity(ies) from %s", count, path)


def prisma_source(schema_path: str | os.PathLike):
    """Return a ``dlt`` resource over a Prisma schema for :func:`cognee.remember`.

    Args:
        schema_path: Path to a ``schema.prisma`` file or directory containing schema files.

    Returns:
        A ``dlt`` resource (``prisma_models``) with ``write_disposition="replace"``
        and document source marker (``cognee_document_source = "prisma"``).
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Prisma connector requires the dlt extra: pip install "cognee[dlt]".'
        ) from exc

    source_path = str(schema_path)

    @dlt.resource(
        name=PRISMA_TABLE_NAME,
        primary_key="id",
        write_disposition="replace",
    )
    def prisma_models():
        yield from iter_prisma_schema_entities(source_path)

    setattr(prisma_models, DOCUMENT_SOURCE_ATTR, PRISMA_SOURCE_NAME)
    return prisma_models
