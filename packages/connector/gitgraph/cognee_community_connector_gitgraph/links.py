"""Row-to-row edges in Cognee's graph, written after ``cognee.remember``.

Cognee draws row-to-row edges only from foreign keys it reads off the staging database, and dlt's
sqlalchemy destination never creates any, so ingested rows come out linked to their table node
and to nothing else. ``link_rows`` adds the missing edges between the row nodes cognee already
wrote. The edges are named the way cognee names its own foreign-key edges,
``{column}_references_{table}``:

- ``sha_references_commits``: file_changes -> commits (``file_changes.sha == commits.id``)
- ``from_symbol_references_symbols``: references -> the symbol that makes the reference
  (none for an import at module level, where ``from_symbol`` is null)
- ``to_symbol_references_symbols``: references -> the symbol being referenced
- ``commit_sha_references_commits``: ci_failures -> commits, when ``commit_sha`` is a commit id
  or the unique prefix of one (CI systems often report a short sha)
"""

import logging
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)


async def get_graph_engine() -> Any:
    # Imported here: cognee logs to stderr on import, and only the remember path needs it.
    from cognee.infrastructure.databases.graph import get_graph_engine as engine

    return await engine()


Row = Mapping[str, Any]
RowKey = tuple[str, str]  # (table, id)
Edge = tuple[RowKey, RowKey, str]  # (source row, target row, relationship)

_ID_PREFIX = "  id: "


def build_edges(tables: Mapping[str, Iterable[Row]]) -> list[Edge]:
    """The sorted, deduplicated edges between the rows of ``tables`` (table name -> rows).

    An edge is only made when both ends exist, so a dangling reference or an unknown commit
    gives nothing. Tables that are absent (``ci_failures`` without CI logs) give no edges.
    """
    rows = {name: list(table) for name, table in tables.items()}
    commits = {r["id"] for r in rows.get("commits", [])}
    symbols = {r["id"] for r in rows.get("symbols", [])}
    edges: set[Edge] = set()

    def commit_for(sha: str | None) -> str | None:
        if not sha:
            return None
        if sha in commits:
            return sha
        matches = [c for c in commits if c.startswith(sha)]
        return matches[0] if len(matches) == 1 else None

    for row in rows.get("file_changes", []):
        if row["sha"] in commits:
            key, target = ("file_changes", row["id"]), ("commits", row["sha"])
            edges.add((key, target, "sha_references_commits"))
    for row in rows.get("references", []):
        key = ("references", row["id"])
        if row.get("from_symbol") in symbols:
            edges.add((key, ("symbols", row["from_symbol"]), "from_symbol_references_symbols"))
        if row.get("to_symbol") in symbols:
            edges.add((key, ("symbols", row["to_symbol"]), "to_symbol_references_symbols"))
    for row in rows.get("ci_failures", []):
        commit = commit_for(row.get("commit_sha"))
        if commit is not None:
            key = ("ci_failures", row["id"])
            edges.add((key, ("commits", commit), "commit_sha_references_commits"))
    return sorted(edges)


def _row_id(text: str, table: str) -> str | None:
    """The id of a row node's text, or None unless ``id`` is the very first Row Data line.

    Cognee renders ``Table: <name>``, the schema and a blank line (cognee 1.6.5; 1.6.6 drops
    both), ``Row Data:`` and then one ``  <column>: <value>`` line per column in column order,
    values unescaped. Row values come from untrusted repos and may contain newlines, so only
    the line right after the first ``Row Data:`` header (everything before it is schema, not
    row data) can be trusted, and only because ``id`` is the first column of every table.
    """
    head, found, data = text.partition("\nRow Data:\n")
    title = f"Table: {table}"
    if not found or (head != title and not head.startswith(title + "\n")):
        return None
    first = data.split("\n", 1)[0]
    return first[len(_ID_PREFIX) :] if first.startswith(_ID_PREFIX) else None


def index_rows(nodes: Iterable[Any], edges: Iterable[Any]) -> tuple[dict[RowKey, str], set[RowKey]]:
    """Map each row key to its graph node id, from ``get_graph_data`` nodes and edges.

    A row node is found through its ``is_row_of`` edge to the table node. A key that more than
    one node claims (another dataset's table, or a forged row) is refused: it is returned in the
    second set and absent from the first, so nothing links to a node that may be the wrong one.
    """
    props = {str(node_id): p for node_id, p in nodes}
    claims: dict[RowKey, set[str]] = {}
    for source, target, relationship, *_ in edges:
        if relationship != "is_row_of" or str(source) not in props or str(target) not in props:
            continue
        table = props[str(target)].get("name")
        row_id = _row_id(props[str(source)].get("text") or "", table) if table else None
        if row_id is not None:
            claims.setdefault((table, row_id), set()).add(str(source))
    found = {key: next(iter(ids)) for key, ids in claims.items() if len(ids) == 1}
    return found, {key for key, ids in claims.items() if len(ids) > 1}


def graph_tables(nodes: Iterable[Any], edges: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """The ``symbols`` and ``references`` tables, read back from the graph after ``link_rows``.

    Rows come from ``index_rows`` (ambiguous keys refused) and the reference -> symbol edges
    ``link_rows`` wrote. Only ids are trusted, so every other column is derived from them: a
    symbol id is ``{path}::{qualname}`` and a qualname never holds ``::``; a reference id is
    ``{path}:{owner}->{target}:{kind}``, so one made at module level (no owner) ends in
    ``:->{target}:{kind}`` and gives ``from_file``. Symbols carry no ``line``, so blast_radius
    maps a ``path:line`` item to every symbol in the file.
    """
    nodes = list(nodes)
    edges = list(edges)
    found, _ = index_rows(nodes, edges)
    key_of = {node: key for key, node in found.items()}
    ends: dict[tuple[str, str], str] = {}  # (reference id, relationship) -> symbol id
    for source, target, relationship, *_ in edges:
        ref, symbol = key_of.get(str(source)), key_of.get(str(target))
        if ref and symbol and ref[0] == "references" and symbol[0] == "symbols":
            ends[(ref[1], relationship)] = symbol[1]
    symbols = sorted(key[1] for key in found if key[0] == "symbols")
    references = []
    for rid in sorted(key[1] for key in found if key[0] == "references"):
        target = ends.get((rid, "to_symbol_references_symbols"))
        if target is None:
            continue
        owner = ends.get((rid, "from_symbol_references_symbols"))
        head = rid.rpartition(":")[0]
        if owner is not None:
            from_file: str | None = owner.rpartition("::")[0]
        elif head.endswith(f":->{target}"):
            from_file = head[: -len(f":->{target}")]
        else:
            from_file = None  # an owned reference whose owner edge is missing
        references.append(
            {"id": rid, "from_symbol": owner, "to_symbol": target, "from_file": from_file}
        )
    return {
        "symbols": [{"id": sid, "file": sid.rpartition("::")[0]} for sid in symbols],
        "references": references,
    }


async def link_rows(source: Any) -> dict[str, int]:
    """Write the edges of ``build_edges`` between the row nodes of an already remembered ``source``.

    Safe to run again: cognee merges an edge on (from, to, relationship), so a second run changes
    nothing. Returns the edges written per relationship plus ``"missing"``, the edges skipped
    because a row has no node in the graph, and ``"ambiguous"``, the edges skipped because
    several nodes claim one end (also logged as a warning). Neither is an error.
    """
    tables = {name: list(resource) for name, resource in source.resources.items()}
    nodes, edges = await (await get_graph_engine()).get_graph_data()
    node_ids, ambiguous = index_rows(nodes, edges)
    if ambiguous:
        logger.warning("ambiguous row nodes, not linked: %s", sorted(ambiguous)[:10])
    summary: dict[str, int] = {"missing": 0, "ambiguous": 0}
    batch = []
    for from_row, to_row, relationship in build_edges(tables):
        if from_row in ambiguous or to_row in ambiguous:
            summary["ambiguous"] += 1
            continue
        if from_row not in node_ids or to_row not in node_ids:
            summary["missing"] += 1
            continue
        from_id, to_id = node_ids[from_row], node_ids[to_row]
        batch.append(
            (
                from_id,
                to_id,
                relationship,
                {
                    "source_node_id": from_id,
                    "target_node_id": to_id,
                    "relationship_name": relationship,
                    "edge_text": relationship.replace("_", " "),
                    "source_table": from_row[0],
                    "target_table": to_row[0],
                },
            )
        )
        summary[relationship] = summary.get(relationship, 0) + 1
    if batch:
        await (await get_graph_engine()).add_edges(batch)
    return summary
