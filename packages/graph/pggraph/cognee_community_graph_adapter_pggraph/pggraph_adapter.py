"""pgGraph-backed graph adapter for Cognee community package."""

import json
import os
from typing import Any, Dict, List, Optional, Tuple, Union

from sqlalchemy import text

from cognee.infrastructure.databases.graph.postgres.adapter import PostgresAdapter
from cognee.infrastructure.engine import DataPoint
from cognee.shared.logging_utils import get_logger

from . import queries
from .connection import resolve_connection_string

logger = get_logger()

_BUILD_MODE_MANUAL = "manual"
_BUILD_MODE_ON_WRITE = "on_write"
_BUILD_MODE_SCHEDULED = "scheduled"


class PgGraphAdapter(PostgresAdapter):
    """
    Graph adapter using Cognee's Postgres graph_node/graph_edge tables with pgGraph
    as an optional derived traversal index.

    CRUD uses PostgresAdapter. Traversal prefers pgGraph and falls back to SQL.
    """

    def __init__(
        self,
        connection_string: Optional[str] = None,
        graph_database_url: str = "",
        graph_database_username: str = "",
        graph_database_password: str = "",
        graph_database_host: str = "",
        graph_database_port: str = "",
        graph_database_name: str = "",
        **kwargs: Any,
    ) -> None:
        # cognee's _create_graph_engine factory passes the database name as
        # `database_name` (not `graph_database_name`) and does not pass a host
        # at all. Accept both names from kwargs so factory-driven construction
        # works the same as direct construction.
        if not graph_database_name:
            graph_database_name = kwargs.pop("database_name", "") or ""
        if not graph_database_host:
            graph_database_host = kwargs.pop("graph_database_host", "") or ""

        if connection_string is None:
            connection_string = resolve_connection_string(
                graph_database_url=graph_database_url,
                graph_database_username=graph_database_username,
                graph_database_password=graph_database_password,
                graph_database_host=graph_database_host,
                graph_database_port=str(graph_database_port) if graph_database_port else "",
                graph_database_name=graph_database_name,
            )
        super().__init__(connection_string=connection_string)
        self._pggraph_ready = False
        self._build_mode = os.getenv("PGGRAPH_BUILD_MODE", _BUILD_MODE_MANUAL).lower()

    async def initialize(self) -> None:
        await super().initialize()
        await self._install_pggraph_extension()
        if self._pggraph_ready:
            await self._register_pggraph_tables()

    async def build_graph(self) -> Optional[List[Dict[str, Any]]]:
        """Run pgGraph's build step. Returns one dict per row produced by
        ``graph.build()`` (typically per registered table/edge), or ``None`` if
        pgGraph isn't available.
        """
        if not self._pggraph_ready:
            logger.warning("pgGraph is not available; skipping build_graph()")
            return None

        async with self._session() as session:
            result = await session.execute(text(queries.BUILD_GRAPH))
            await session.commit()
            rows = result.fetchall()
            return [dict(row._mapping) for row in rows]

    async def add_nodes(self, nodes: Union[List[Tuple[str, Dict]], List[DataPoint]]) -> None:
        await super().add_nodes(nodes)
        await self._maybe_rebuild_on_write()

    async def add_edges(
        self, edges: Union[List[Tuple[str, str, str, Optional[Dict[str, Any]]]], List]
    ) -> None:
        await super().add_edges(edges)
        await self._maybe_rebuild_on_write()

    async def delete_graph(self) -> None:
        await super().delete_graph()
        if self._pggraph_ready:
            try:
                async with self._session() as session:
                    await session.execute(text(queries.RESET_GRAPH))
                    await session.commit()
                await self._register_pggraph_tables()
                await self.build_graph()
            except Exception as exc:
                logger.warning(
                    "pgGraph reset after delete_graph failed; Postgres tables were cleared: %s",
                    exc,
                )

    async def get_neighbors(self, node_id: str) -> List[Dict[str, Any]]:
        if self._pggraph_ready:
            try:
                return await self._pggraph_get_neighbors(node_id)
            except Exception as exc:
                # Disable pgGraph for the rest of this adapter's lifetime so we
                # don't double-query and log on every call when the extension
                # is in a bad state. The user can recreate the adapter (and
                # rerun initialize) once the underlying issue is fixed.
                self._pggraph_ready = False
                logger.warning(
                    "pgGraph traversal failed; disabling pgGraph and falling back to "
                    "PostgresAdapter for this session: %s",
                    exc,
                )
        return await super().get_neighbors(node_id)

    async def get_neighborhood(
        self,
        node_ids: List[str],
        depth: int = 1,
        edge_types: Optional[List[str]] = None,
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Tuple[str, str, str, Dict[str, Any]]]]:
        if self._pggraph_ready:
            try:
                return await self._pggraph_get_neighborhood(node_ids, depth, edge_types)
            except Exception as exc:
                self._pggraph_ready = False
                logger.warning(
                    "pgGraph neighborhood failed; disabling pgGraph and falling back to "
                    "PostgresAdapter for this session: %s",
                    exc,
                )
        return await self._postgres_get_neighborhood(node_ids, depth, edge_types)

    async def _install_pggraph_extension(self) -> None:
        self._pggraph_ready = False
        try:
            async with self._session() as session:
                available = (
                    await session.execute(text(queries.EXTENSION_AVAILABLE))
                ).scalar()
                if not available:
                    logger.info(
                        "pgGraph extension not installed on this Postgres server; "
                        "using Postgres SQL traversal"
                    )
                    return

                await session.execute(text(queries.CREATE_EXTENSION))
                enabled = await session.execute(text(queries.PGGRAPH_ENABLED))
                await session.commit()
                self._pggraph_ready = bool(enabled.scalar())
                if self._pggraph_ready:
                    logger.info("pgGraph extension loaded")
        except Exception as exc:
            # The async session's __aexit__ rolls back any open transaction;
            # we just need to record the failure and continue with SQL-only mode.
            logger.warning(
                "pgGraph setup failed; using Postgres-only graph traversal: %s",
                exc,
            )

    async def _register_pggraph_tables(self) -> None:
        async with self._session() as session:
            tables = {
                row[0]
                for row in (await session.execute(text(queries.REGISTERED_TABLES))).fetchall()
            }
            if queries.NODE_TABLE not in tables:
                await session.execute(text(queries.REGISTER_NODE_TABLE))

            edges = {
                row[0]
                for row in (await session.execute(text(queries.REGISTERED_EDGES))).fetchall()
            }
            if "cognee_edge" not in edges:
                await session.execute(text(queries.REGISTER_EDGE_TABLE))

            await session.commit()

        if self._build_mode != _BUILD_MODE_SCHEDULED:
            await self.build_graph()

    async def _maybe_rebuild_on_write(self) -> None:
        if self._pggraph_ready and self._build_mode == _BUILD_MODE_ON_WRITE:
            await self.build_graph()

    async def _pggraph_get_neighbors(self, node_id: str) -> List[Dict[str, Any]]:
        if not self._pggraph_ready:
            raise RuntimeError("pgGraph is not initialized")

        async with self._session() as session:
            result = await session.execute(
                text(queries.NEIGHBORS_TRAVERSE),
                {"seed_id": node_id, "edge_types": None},
            )
            neighbors = []
            for row in result.fetchall():
                parsed = self._parse_pggraph_node(row.node, row.node_id)
                if parsed.get("id") != node_id:
                    neighbors.append(parsed)
            return neighbors

    async def _pggraph_get_neighborhood(
        self,
        node_ids: List[str],
        depth: int,
        edge_types: Optional[List[str]],
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Tuple[str, str, str, Dict[str, Any]]]]:
        if not self._pggraph_ready:
            raise RuntimeError("pgGraph is not initialized")
        if not node_ids:
            return [], []

        start_tables = [queries.NODE_TABLE] * len(node_ids)
        async with self._session() as session:
            result = await session.execute(
                text(queries.MULTI_START_TRAVERSE),
                {
                    "start_tables": start_tables,
                    "start_ids": list(node_ids),
                    "max_depth": depth,
                    "edge_types": edge_types,
                },
            )
            reached_ids = {row.node_id for row in result.fetchall()}

        # graph.traverse() may omit seed nodes that have no outgoing edges
        # under the configured edge_types. The SQL fallback always includes
        # seeds (its CTE base case is `unnest(seeds), 0`); union them in so
        # both code paths return the same shape for isolated seeds.
        reached_ids.update(node_ids)

        return await self._fetch_subgraph_for_ids(list(reached_ids), edge_types)

    async def _fetch_subgraph_for_ids(
        self,
        node_ids: List[str],
        edge_types: Optional[List[str]] = None,
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Tuple[str, str, str, Dict[str, Any]]]]:
        nodes_out: List[Tuple[str, Dict[str, Any]]] = []
        for node in await self.get_nodes(node_ids):
            node_id = node.pop("id", None)
            if node_id is not None:
                nodes_out.append((node_id, node))

        async with self._session() as session:
            edge_sql = queries.SUBGRAPH_EDGES
            params: Dict[str, Any] = {"ids": node_ids}
            if edge_types:
                edge_sql += " AND ge.relationship_name = ANY(:edge_types)"
                params["edge_types"] = edge_types
            result = await session.execute(text(edge_sql), params)

            edges_out = []
            for row in result.fetchall():
                props = {}
                if row.properties is not None:
                    props = (
                        row.properties
                        if isinstance(row.properties, dict)
                        else json.loads(row.properties)
                    )
                edges_out.append((row.source_id, row.target_id, row.relationship_name, props))

        return nodes_out, edges_out

    async def _postgres_get_neighborhood(
        self,
        node_ids: List[str],
        depth: int = 1,
        edge_types: Optional[List[str]] = None,
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Tuple[str, str, str, Dict[str, Any]]]]:
        """SQL neighborhood with explicit text[] cast (Postgres 16 + asyncpg)."""
        if not node_ids:
            return [], []

        edge_filter = ""
        if edge_types:
            placeholders = ", ".join(f":et_{i}" for i in range(len(edge_types)))
            edge_filter = f"AND e.relationship_name IN ({placeholders})"

        query_str = f"""
            WITH RECURSIVE neighborhood(id, hops) AS (
                SELECT unnest(CAST(:seeds AS text[])), 0
              UNION
                SELECT CASE WHEN e.source_id = n.id THEN e.target_id
                            ELSE e.source_id END,
                       n.hops + 1
                FROM neighborhood n
                JOIN graph_edge e ON (e.source_id = n.id OR e.target_id = n.id)
                    {edge_filter}
                WHERE n.hops < :depth
            ),
            ids AS (SELECT DISTINCT id FROM neighborhood)

            SELECT 'node' AS kind,
                   gn.id, gn.name, gn.type, gn.properties,
                   NULL AS source_id, NULL AS target_id,
                   NULL AS relationship_name, NULL AS edge_properties
            FROM graph_node gn
            JOIN ids ON gn.id = ids.id

            UNION ALL

            SELECT 'edge' AS kind,
                   NULL, NULL, NULL, NULL,
                   ge.source_id, ge.target_id,
                   ge.relationship_name, ge.properties
            FROM graph_edge ge
            WHERE ge.source_id IN (SELECT id FROM ids)
              AND ge.target_id IN (SELECT id FROM ids)
        """

        params: Dict[str, Any] = {"seeds": list(node_ids), "depth": depth}
        if edge_types:
            for i, et in enumerate(edge_types):
                params[f"et_{i}"] = et

        async with self._session() as session:
            result = await session.execute(text(query_str), params)

            nodes = []
            edges = []
            for row in result.fetchall():
                if row.kind == "node":
                    data = self._parse_node_row(row)
                    data.pop("id", None)
                    nodes.append((row.id, data))
                else:
                    props = {}
                    if row.edge_properties is not None:
                        props = (
                            row.edge_properties
                            if isinstance(row.edge_properties, dict)
                            else json.loads(row.edge_properties)
                        )
                    edges.append((row.source_id, row.target_id, row.relationship_name, props))

            return nodes, edges

    def _parse_pggraph_node(self, node_payload: Any, node_id: str) -> Dict[str, Any]:
        if node_payload is None:
            return {"id": node_id}

        if isinstance(node_payload, str):
            data = json.loads(node_payload)
        elif isinstance(node_payload, dict):
            data = dict(node_payload)
        else:
            data = dict(node_payload)

        node_key = data.get("id", node_id)
        result = {
            "id": str(node_key),
            "name": str(data.get("name", "")),
            "type": str(data.get("type", "")),
        }
        properties = data.get("properties")
        if properties is not None:
            if isinstance(properties, str):
                properties = json.loads(properties)
            if isinstance(properties, dict):
                result.update(properties)
        for key in ("name", "type", "properties", "created_at", "updated_at"):
            data.pop(key, None)
        result.update({k: v for k, v in data.items() if k != "id"})
        return result
