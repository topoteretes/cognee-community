"""DLT source for Drizzle ORM schemas (TypeScript database models & relations).

Parses Drizzle ORM schema files (pgTable, mysqlTable, sqliteTable, relations, enums)
into structured Markdown documents optimized for Cognee's entity extraction and knowledge
graph indexing.

Like the Notion and Prisma connectors, this connector operates in Cognee's "document-mode":
it tags the dlt source with ``DOCUMENT_SOURCE_ATTR = "drizzle"`` so each table and enum
flows through the standard ``cognify()`` entity-extraction pipeline into memory.

Each sync is a full snapshot (``write_disposition="replace"``). When tables, relations,
or enums are modified, unchanged schemas keep stable deterministic IDs, while deleted
or renamed models are automatically pruned by Cognee's orphan cleanup.
"""

import glob
import hashlib
import re
from pathlib import Path
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("drizzle_connector")

DRIZZLE_TABLE_NAME = "drizzle_schemas"
DRIZZLE_SOURCE_NAME = "drizzle"

_EXTRA_HINT = 'The Drizzle connector requires dlt: pip install "dlt[sqlalchemy]"'


class DrizzleParserError(RuntimeError):
    """Raised when parsing a Drizzle schema fails."""


def _stable_id(prefix: str, name: str) -> str:
    """Generate a deterministic, stable identifier for Cognee graph deduplication."""
    return f"drizzle:{prefix}:{name}"


def _content_hash(text: str) -> str:
    """SHA-256 hash to prevent unnecessary re-cognification of unchanged schemas."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _extract_balanced_block(
    text: str, start_pos: int, open_ch: str = "{", close_ch: str = "}"
) -> tuple[str, int]:
    """Extract content within balanced opening and closing delimiters."""
    depth = 0
    in_string = False
    str_ch = ""
    start_idx = -1
    for i in range(start_pos, len(text)):
        ch = text[i]
        if in_string:
            if ch == str_ch and text[i - 1] != "\\":
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            str_ch = ch
            continue
        if ch == open_ch:
            if depth == 0:
                start_idx = i + 1
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start_idx:i], i
    return "", -1


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split string by a separator only when not inside brackets, parens, or strings."""
    items: list[str] = []
    current: list[str] = []
    depth_brace = 0
    depth_paren = 0
    depth_bracket = 0
    in_str = False
    str_ch = ""
    for i, ch in enumerate(text):
        if in_str:
            current.append(ch)
            if ch == str_ch and text[i - 1] != "\\":
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            str_ch = ch
            current.append(ch)
            continue
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket -= 1

        if ch == sep and depth_brace == 0 and depth_paren == 0 and depth_bracket == 0:
            item = "".join(current).strip()
            if item:
                items.append(item)
            current = []
        else:
            current.append(ch)
    if current:
        item = "".join(current).strip()
        if item:
            items.append(item)
    return items


def _extract_enums(content: str) -> list[dict[str, Any]]:
    """Extract pgEnum and mysqlEnum declarations."""
    enums = []
    pattern = re.compile(
        r"(?:export\s+const\s+(\w+)\s*=\s*)?(pgEnum|mysqlEnum)\s*\(\s*[\"']([^\"']+)[\"']\s*,\s*\[([\s\S]*?)\]\s*\)",
        re.MULTILINE,
    )
    for match in pattern.finditer(content):
        var_name = match.group(1) or ""
        dialect_fn = match.group(2)
        enum_name = match.group(3)
        values_raw = match.group(4)

        values = [v.strip().strip("\"'") for v in re.findall(r"[\"']([^\"']+)[\"']", values_raw)]
        dialect = "PostgreSQL" if dialect_fn == "pgEnum" else "MySQL"

        enums.append(
            {
                "name": enum_name,
                "variable_name": var_name,
                "dialect": dialect,
                "values": values,
            }
        )
    return enums


def _extract_relations(content: str) -> dict[str, list[dict[str, Any]]]:
    """Extract Drizzle relations() mappings grouped by table variable name."""
    relations_by_table: dict[str, list[dict[str, Any]]] = {}

    pattern = re.compile(
        r"(?:export\s+const\s+(\w+)\s*=\s*)?relations\s*\(\s*(\w+)\s*,[\s\S]*?=>",
        re.MULTILINE,
    )

    for match in pattern.finditer(content):
        table_var = match.group(2)
        body, _ = _extract_balanced_block(content, match.end(), "{", "}")
        if not body:
            continue

        rel_list = relations_by_table.setdefault(table_var, [])
        rel_entries = _split_top_level(body)

        for entry in rel_entries:
            colon_idx = entry.find(":")
            if colon_idx == -1:
                continue
            rel_name = entry[:colon_idx].strip()
            rel_expr = entry[colon_idx + 1 :].strip()

            if rel_expr.startswith("one("):
                target_match = re.search(r"one\s*\(\s*(\w+)", rel_expr)
                target_table = target_match.group(1) if target_match else ""

                fields = re.findall(r"fields\s*:\s*\[([\w\.,\s]+)\]", rel_expr)
                references = re.findall(r"references\s*:\s*\[([\w\.,\s]+)\]", rel_expr)

                fields_clean = [f.strip() for f in fields[0].split(",")] if fields else []
                refs_clean = [r.strip() for r in references[0].split(",")] if references else []

                rel_list.append(
                    {
                        "type": "one",
                        "name": rel_name,
                        "target_table": target_table,
                        "fields": fields_clean,
                        "references": refs_clean,
                    }
                )
            elif rel_expr.startswith("many("):
                target_match = re.search(r"many\s*\(\s*(\w+)", rel_expr)
                target_table = target_match.group(1) if target_match else ""

                rel_list.append(
                    {
                        "type": "many",
                        "name": rel_name,
                        "target_table": target_table,
                    }
                )

    return relations_by_table


def _parse_column_item(item: str) -> dict[str, Any] | None:
    """Parse a single column item from a table body."""
    colon_idx = item.find(":")
    if colon_idx == -1:
        return None
    prop_name = item[:colon_idx].strip()
    rest = item[colon_idx + 1 :].strip()

    paren_idx = rest.find("(")
    if paren_idx == -1:
        return None
    fn_name = rest[:paren_idx].strip()

    first_str = re.search(r"^\(\s*[\"']([^\"']+)[\"']", rest[paren_idx:])
    sql_name = first_str.group(1) if first_str else prop_name

    is_pk = ".primaryKey(" in rest
    is_nn = ".notNull(" in rest
    is_uq = ".unique(" in rest
    is_ai = ".autoincrement(" in rest or "autoIncrement: true" in rest or fn_name == "serial"

    ref_match = re.search(
        r"\.references\s*\(\s*\(\s*\)\s*=>\s*([\w\.]+)(?:\s*,\s*\{([\s\S]*?)\})?\s*\)", rest
    )
    ref = ref_match.group(1) if ref_match else None
    on_del = None
    if ref_match and ref_match.group(2):
        on_del_m = re.search(r"onDelete\s*:\s*[\"']([^\"']+)[\"']", ref_match.group(2))
        if on_del_m:
            on_del = on_del_m.group(1)

    def_val = None
    if ".defaultNow(" in rest:
        def_val = "now()"
    else:
        def_m = re.search(r"\.default\s*\(\s*([^)]+)\s*\)", rest)
        if def_m:
            def_val = def_m.group(1).strip()

    return {
        "property_name": prop_name,
        "sql_name": sql_name,
        "type_function": fn_name,
        "is_primary_key": is_pk,
        "is_not_null": is_nn,
        "is_unique": is_uq,
        "is_autoincrement": is_ai,
        "references": ref,
        "on_delete": on_del,
        "default_value": def_val,
    }


def _extract_tables(content: str) -> list[dict[str, Any]]:
    """Extract pgTable, mysqlTable, and sqliteTable definitions."""
    tables = []

    pattern = re.compile(
        r"(?:export\s+const\s+(\w+)\s*=\s*)?(pgTable|mysqlTable|sqliteTable)\s*\(\s*[\"']([^\"']+)[\"']\s*,\s*",
        re.MULTILINE,
    )

    for match in pattern.finditer(content):
        var_name = match.group(1) or ""
        dialect_fn = match.group(2)
        table_name = match.group(3)

        body, _ = _extract_balanced_block(content, match.end(), "{", "}")
        if not body:
            continue

        dialect_map = {
            "pgTable": "PostgreSQL",
            "mysqlTable": "MySQL",
            "sqliteTable": "SQLite",
        }
        dialect = dialect_map.get(dialect_fn, "SQL")

        raw_col_items = _split_top_level(body)
        columns = []
        for it in raw_col_items:
            parsed = _parse_column_item(it)
            if parsed:
                columns.append(parsed)

        tables.append(
            {
                "name": table_name,
                "variable_name": var_name,
                "dialect": dialect,
                "columns": columns,
            }
        )

    return tables


def _format_table_document(
    table: dict[str, Any], file_path: str, relations: list[dict[str, Any]]
) -> str:
    """Render a table and its relations into a structured Markdown document."""
    lines = [
        f"# Database Table: `{table['name']}`",
        f"**Dialect**: {table['dialect']}",
        f"**Source File**: `{file_path}`",
        f"**Variable Name**: `{table.get('variable_name', table['name'])}`",
        "",
        "## Columns",
    ]

    for col in table["columns"]:
        attributes = []
        if col["is_primary_key"]:
            attributes.append("PRIMARY KEY")
        if col["is_not_null"]:
            attributes.append("NOT NULL")
        if col["is_unique"]:
            attributes.append("UNIQUE")
        if col["is_autoincrement"]:
            attributes.append("AUTO-INCREMENT")
        if col["default_value"]:
            attributes.append(f"DEFAULT({col['default_value']})")
        if col["references"]:
            ref_str = f"REFERENCES `{col['references']}`"
            if col["on_delete"]:
                ref_str += f" [ON DELETE: {col['on_delete']}]"
            attributes.append(ref_str)

        attr_str = f" — {', '.join(attributes)}" if attributes else ""
        lines.append(f"- `{col['sql_name']}` ({col['type_function']}){attr_str}")

    if relations:
        lines.extend(["", "## Relations"])
        for rel in relations:
            if rel["type"] == "one":
                fields_str = (
                    f" (via {', '.join(rel['fields'])} -> {', '.join(rel['references'])})"
                    if rel.get("fields")
                    else ""
                )
                lines.append(f"- **One** `{rel['name']}` -> `{rel['target_table']}`{fields_str}")
            elif rel["type"] == "many":
                lines.append(f"- **Many** `{rel['name']}` -> `{rel['target_table']}`")

    return "\n".join(lines)


def _format_enum_document(enum: dict[str, Any], file_path: str) -> str:
    """Render an enum into a structured Markdown document."""
    values_str = ", ".join(f"`{v}`" for v in enum["values"])
    lines = [
        f"# Database Enum: `{enum['name']}`",
        f"**Dialect**: {enum['dialect']}",
        f"**Source File**: `{file_path}`",
        f"**Variable Name**: `{enum.get('variable_name', enum['name'])}`",
        "",
        f"**Allowed Values**: [{values_str}]",
    ]
    return "\n".join(lines)


def parse_drizzle_files(schema_path: str | Path | list[str | Path]) -> list[dict[str, Any]]:
    """Parse Drizzle TypeScript schema file(s) into document rows.

    Args:
        schema_path: Path to a schema file, a directory containing schema files,
            a glob pattern, or a list of file paths.

    Returns:
        List of dicts with fields: id, url, title, content.
    """
    files_to_parse: list[Path] = []

    if isinstance(schema_path, list):
        for item in schema_path:
            p = Path(item)
            if p.is_file():
                files_to_parse.append(p)
            elif p.is_dir():
                files_to_parse.extend(p.glob("**/*.ts"))
                files_to_parse.extend(p.glob("**/*.js"))
            else:
                for match in glob.glob(str(item), recursive=True):
                    files_to_parse.append(Path(match))
    else:
        p = Path(schema_path)
        if p.is_file():
            files_to_parse.append(p)
        elif p.is_dir():
            files_to_parse.extend(p.glob("**/*.ts"))
            files_to_parse.extend(p.glob("**/*.js"))
        else:
            for match in glob.glob(str(schema_path), recursive=True):
                files_to_parse.append(Path(match))

    valid_files = [
        f
        for f in files_to_parse
        if not any(part in f.parts for part in ("node_modules", ".git", "dist", ".next"))
    ]

    if not valid_files:
        logger.warning("Drizzle: No valid schema files found for path: %s", schema_path)
        return []

    documents: list[dict[str, Any]] = []

    for file_path in sorted(set(valid_files)):
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("Drizzle: Failed to read schema file %s: %s", file_path, exc)
            continue

        enums = _extract_enums(content)
        relations_map = _extract_relations(content)
        tables = _extract_tables(content)

        # Ingest Enums
        for enum in enums:
            doc_content = _format_enum_document(enum, str(file_path))
            stable_id = _stable_id("enum", enum["name"])
            documents.append(
                {
                    "id": stable_id,
                    "url": f"file://{file_path.resolve()}#{enum['name']}",
                    "title": f"Enum: {enum['name']}",
                    "content": doc_content,
                }
            )

        # Ingest Tables
        for table in tables:
            tbl_rels = (
                relations_map.get(table["variable_name"]) or relations_map.get(table["name"]) or []
            )

            doc_content = _format_table_document(table, str(file_path), tbl_rels)
            stable_id = _stable_id("table", table["name"])
            documents.append(
                {
                    "id": stable_id,
                    "url": f"file://{file_path.resolve()}#{table['name']}",
                    "title": f"Table: {table['name']}",
                    "content": doc_content,
                }
            )

    return documents


def drizzle_source(
    schema_path: str | Path | list[str | Path] = "schema.ts",
):
    """Create a dlt source that yields Drizzle ORM schemas as structured documents.

    Args:
        schema_path: Path to schema file, directory, glob pattern, or list of paths.
            Defaults to "schema.ts" in the current directory.

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    @dlt.resource(name=DRIZZLE_TABLE_NAME, primary_key="id", write_disposition="replace")
    def drizzle_schemas():
        docs = parse_drizzle_files(schema_path)
        count = 0
        for doc in docs:
            count += 1
            yield doc
        logger.info("Drizzle: Synced %d schema document(s) into Cognee.", count)

    @dlt.source(name=DRIZZLE_SOURCE_NAME)
    def _drizzle():
        return drizzle_schemas

    source = _drizzle()
    setattr(source, DOCUMENT_SOURCE_ATTR, DRIZZLE_SOURCE_NAME)
    return source
