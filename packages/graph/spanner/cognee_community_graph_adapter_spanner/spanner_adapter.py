"""Google Cloud Spanner Graph adapter for Cognee.

Maps Cognee graph operations to Spanner Graph (GQL) and SQL DML on
CogneeNode / CogneeEdge tables. Requires a pre-created database with
the Cognee graph schema and property graph (see README).
"""

import asyncio
import json
import uuid
from typing import Any
from uuid import UUID

from cognee.infrastructure.databases.exceptions.exceptions import (
    NodesetFilterNotSupportedError,
)
from cognee.infrastructure.databases.graph.graph_db_interface import GraphDBInterface
from cognee.infrastructure.engine import DataPoint
from cognee.modules.storage.utils import JSONEncoder
from cognee.shared.logging_utils import ERROR, get_logger

logger = get_logger("SpannerGraphAdapter", level=ERROR)

# Table and graph names used by the Cognee Spanner schema
NODE_TABLE = "CogneeNode"
EDGE_TABLE = "CogneeEdge"
GRAPH_NAME = "CogneeGraph"


def _run_sync(coro_fn, *args, **kwargs):
    """Run a synchronous Spanner call in a thread so we don't block the event loop."""
    return asyncio.to_thread(coro_fn, *args, **kwargs)


class SpannerGraphAdapter(GraphDBInterface):
    """Graph backend for Cognee using Google Cloud Spanner with Spanner Graph (GQL).

    Expects the database to already have the Cognee graph schema:
    - CogneeNode (id STRING(MAX), properties JSON) PRIMARY KEY (id)
    - CogneeEdge (source_id, target_id, edge_id, relationship_type, properties)
      PRIMARY KEY (source_id, target_id, edge_id)
    - PROPERTY GRAPH CogneeGraph with NODE TABLES (CogneeNode) and
      EDGE TABLES (CogneeEdge SOURCE KEY (source_id) DESTINATION KEY (target_id) LABEL Related)

    Config keys: project_id, instance_id, database_id; optional: credentials.
    """

    def __init__(
        self,
        graph_database_url: str | None = None,
        project_id: str | None = None,
        instance_id: str | None = None,
        database_id: str | None = None,
        credentials: Any | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Spanner client.

        Either pass graph_database_url as 'project_id/instance_id/database_id'
        or pass project_id, instance_id, database_id separately.
        """
        if graph_database_url:
            parts = graph_database_url.strip("/").split("/")
            if len(parts) >= 3:
                self._project_id = parts[-3]
                self._instance_id = parts[-2]
                self._database_id = parts[-1]
            else:
                raise ValueError(
                    "graph_database_url must be of the form project_id/instance_id/database_id"
                )
        elif project_id and instance_id and database_id:
            self._project_id = project_id
            self._instance_id = instance_id
            self._database_id = database_id
        else:
            raise ValueError(
                "Provide either graph_database_url (project_id/instance_id/database_id) "
                "or project_id, instance_id, and database_id"
            )
        self._credentials = credentials
        self._client: Any = None
        self._database: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud.spanner import Client

            self._client = Client(
                project=self._project_id,
                credentials=self._credentials,
            )
        return self._client

    def _get_database(self) -> Any:
        if self._database is None:
            instance = self._get_client().instance(self._instance_id)
            self._database = instance.database(self._database_id)
        return self._database

    @staticmethod
    def _serialize_properties(properties: dict[str, Any] | None) -> dict[str, Any]:
        if properties is None:
            return {}
        out: dict[str, Any] = {}
        for key, value in properties.items():
            if isinstance(value, UUID):
                out[key] = str(value)
            elif isinstance(value, dict):
                out[key] = json.dumps(value, cls=JSONEncoder)
            else:
                out[key] = value
        return out

    @staticmethod
    def _properties_to_json_string(properties: dict[str, Any]) -> str:
        return json.dumps(properties, cls=JSONEncoder)

    @staticmethod
    def _parse_json_properties(raw: Any) -> dict[str, Any]:
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"_raw": raw}
        return {"_raw": raw}

    async def query(self, query: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Execute a GQL or SQL query. For GQL use 'Graph CogneeGraph MATCH ... RETURN ...'."""
        database = self._get_database()
        params = params or {}

        def _run() -> list[dict[str, Any]]:
            with database.snapshot() as snapshot:
                results = list(snapshot.execute_sql(query, params=params))
            if not results:
                return []
            first = results[0]
            keys = list(first.keys()) if hasattr(first, "keys") else list(range(len(first)))
            rows = []
            for row in results:
                vals = list(row.values()) if hasattr(row, "values") else list(row)
                rows.append(dict(zip(keys, vals, strict=False)))
            return rows

        return await _run_sync(_run)

    async def add_node(
        self,
        node: DataPoint | str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        if isinstance(node, DataPoint):
            node_id = str(node.id)
            props = self._serialize_properties(node.model_dump())
        else:
            node_id = str(node)
            props = self._serialize_properties(properties or {})
        if "id" not in props:
            props["id"] = node_id
        props_json = self._properties_to_json_string(props)
        database = self._get_database()

        def _run() -> None:
            def _tx(transaction: Any) -> None:
                # Upsert: try insert, then update if already exists
                transaction.execute_update(
                    f"""
                    INSERT INTO {NODE_TABLE} (id, properties)
                    VALUES (@id, @properties)
                    """,
                    params={"id": node_id, "properties": props_json},
                    param_types={"id": "STRING", "properties": "JSON"},
                )

            try:
                database.run_in_transaction(_tx)
            except Exception as e:
                if "ALREADY_EXISTS" in str(e) or "already exists" in str(e).lower():
                    # Update existing node
                    def _tx_update(txn: Any) -> None:
                        txn.execute_update(
                            f"""
                            UPDATE {NODE_TABLE}
                            SET properties = @properties
                            WHERE id = @id
                            """,
                            params={"id": node_id, "properties": props_json},
                            param_types={"id": "STRING", "properties": "JSON"},
                        )

                    database.run_in_transaction(_tx_update)
                else:
                    raise

        await _run_sync(_run)

    async def add_nodes(
        self,
        nodes: list[tuple[str, dict[str, Any]]] | list[DataPoint],
        source_ref_key: str | None = None,
        pipeline_run_id: str | None = None,
    ) -> None:
        if not nodes:
            return
        database = self._get_database()

        def _run() -> None:
            def _tx(transaction: Any) -> None:
                for node in nodes:
                    if isinstance(node, DataPoint):
                        node_id = str(node.id)
                        props = self._serialize_properties(node.model_dump())
                    else:
                        node_id, props = node[0], node[1]
                        props = self._serialize_properties(props or {})
                    if "id" not in props:
                        props["id"] = node_id
                    props_json = self._properties_to_json_string(props)
                    transaction.execute_update(
                        f"""
                        INSERT INTO {NODE_TABLE} (id, properties)
                        VALUES (@id, @properties)
                        """,
                        params={"id": node_id, "properties": props_json},
                        param_types={"id": "STRING", "properties": "JSON"},
                    )

            database.run_in_transaction(_tx)

        await _run_sync(_run)

    async def delete_node(self, node_id: str) -> None:
        database = self._get_database()

        def _run() -> None:
            def _tx(transaction: Any) -> None:
                transaction.execute_update(
                    f"DELETE FROM {EDGE_TABLE} WHERE source_id = @id OR target_id = @id",
                    params={"id": node_id},
                    param_types={"id": "STRING"},
                )
                transaction.execute_update(
                    f"DELETE FROM {NODE_TABLE} WHERE id = @id",
                    params={"id": node_id},
                    param_types={"id": "STRING"},
                )

            database.run_in_transaction(_tx)

        await _run_sync(_run)

    async def delete_nodes(self, node_ids: list[str]) -> None:
        if not node_ids:
            return
        for nid in node_ids:
            await self.delete_node(nid)

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        database = self._get_database()

        def _run() -> dict[str, Any] | None:
            with database.snapshot() as snapshot:
                results = list(
                    snapshot.execute_sql(
                        f"SELECT id, properties FROM {NODE_TABLE} WHERE id = @id",
                        params={"id": node_id},
                        param_types={"id": "STRING"},
                    )
                )
            if not results:
                return None
            row = results[0]
            props = self._parse_json_properties(
                row[1] if hasattr(row, "__getitem__") else getattr(row, "properties", None)
            )
            props["id"] = row[0] if hasattr(row, "__getitem__") else getattr(row, "id", node_id)
            return props

        return await _run_sync(_run)

    async def get_nodes(self, node_ids: list[str]) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        out: list[dict[str, Any]] = []
        for nid in node_ids:
            node = await self.get_node(nid)
            if node is not None:
                out.append(node)
        return out

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        relationship_name: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        edge_id = str(uuid.uuid4())
        props = dict(properties or {})
        props["source_node_id"] = source_id
        props["target_node_id"] = target_id
        props["relationship_name"] = relationship_name
        props = self._serialize_properties(props)
        props_json = self._properties_to_json_string(props)
        database = self._get_database()

        def _run() -> None:
            def _tx(transaction: Any) -> None:
                transaction.execute_update(
                    f"""
                    INSERT INTO {EDGE_TABLE}
                    (source_id, target_id, edge_id, relationship_type, properties)
                    VALUES (@source_id, @target_id, @edge_id, @relationship_type, @properties)
                    """,
                    params={
                        "source_id": source_id,
                        "target_id": target_id,
                        "edge_id": edge_id,
                        "relationship_type": relationship_name,
                        "properties": props_json,
                    },
                    param_types={
                        "source_id": "STRING",
                        "target_id": "STRING",
                        "edge_id": "STRING",
                        "relationship_type": "STRING",
                        "properties": "JSON",
                    },
                )

            database.run_in_transaction(_tx)

        await _run_sync(_run)

    async def add_edges(
        self,
        edges: list[tuple[str, str, str, dict[str, Any]]]
        | list[tuple[str, str, str, dict[str, Any] | None]],
        source_ref_key: str | None = None,
        pipeline_run_id: str | None = None,
    ) -> None:
        if not edges:
            return
        database = self._get_database()

        def _run() -> None:
            def _tx(transaction: Any) -> None:
                for edge in edges:
                    src, tgt, rel, props = (
                        edge[0],
                        edge[1],
                        edge[2],
                        edge[3] if len(edge) > 3 else None,
                    )
                    edge_id = str(uuid.uuid4())
                    p = dict(props or {})
                    p["source_node_id"] = str(src)
                    p["target_node_id"] = str(tgt)
                    p["relationship_name"] = rel
                    p = self._serialize_properties(p)
                    props_json = self._properties_to_json_string(p)
                    transaction.execute_update(
                        f"""
                        INSERT INTO {EDGE_TABLE}
                        (source_id, target_id, edge_id, relationship_type, properties)
                        VALUES (@source_id, @target_id, @edge_id, @relationship_type, @properties)
                        """,
                        params={
                            "source_id": str(src),
                            "target_id": str(tgt),
                            "edge_id": edge_id,
                            "relationship_type": rel,
                            "properties": props_json,
                        },
                        param_types={
                            "source_id": "STRING",
                            "target_id": "STRING",
                            "edge_id": "STRING",
                            "relationship_type": "STRING",
                            "properties": "JSON",
                        },
                    )

            database.run_in_transaction(_tx)

        await _run_sync(_run)

    async def delete_graph(self) -> None:
        database = self._get_database()

        def _run() -> None:
            def _tx(transaction: Any) -> None:
                transaction.execute_update(f"DELETE FROM {EDGE_TABLE}")
                transaction.execute_update(f"DELETE FROM {NODE_TABLE}")

            database.run_in_transaction(_tx)

        await _run_sync(_run)

    async def get_graph_data(
        self,
    ) -> tuple[
        list[tuple[str, dict[str, Any]]],
        list[tuple[str, str, str, dict[str, Any]]],
    ]:
        database = self._get_database()

        def _run_nodes() -> list[tuple[str, dict[str, Any]]]:
            with database.snapshot() as snapshot:
                results = list(snapshot.execute_sql(f"SELECT id, properties FROM {NODE_TABLE}"))
            nodes: list[tuple[str, dict[str, Any]]] = []
            for row in results:
                nid = row[0] if hasattr(row, "__getitem__") else getattr(row, "id", None)
                props = self._parse_json_properties(
                    row[1] if hasattr(row, "__getitem__") else getattr(row, "properties", None)
                )
                if "id" not in props:
                    props["id"] = nid
                nodes.append((str(nid), props))
            return nodes

        def _run_edges() -> list[tuple[str, str, str, dict[str, Any]]]:
            with database.snapshot() as snapshot:
                results = list(
                    snapshot.execute_sql(
                        f"""
                        SELECT source_id, target_id, relationship_type, properties
                        FROM {EDGE_TABLE}
                        """
                    )
                )
            edges: list[tuple[str, str, str, dict[str, Any]]] = []
            for row in results:
                src = row[0] if hasattr(row, "__getitem__") else getattr(row, "source_id", None)
                tgt = row[1] if hasattr(row, "__getitem__") else getattr(row, "target_id", None)
                rel = (
                    row[2] if hasattr(row, "__getitem__") else getattr(row, "relationship_type", "")
                )
                props = self._parse_json_properties(
                    row[3] if hasattr(row, "__getitem__") else getattr(row, "properties", None)
                )
                edges.append((str(src), str(tgt), str(rel), props))
            return edges

        nodes_result, edges_result = await asyncio.gather(
            _run_sync(_run_nodes),
            _run_sync(_run_edges),
        )
        return nodes_result, edges_result

    async def is_empty(self) -> bool:
        """Return True if the graph has no nodes."""
        database = self._get_database()

        def _run() -> bool:
            with database.snapshot() as snapshot:
                results = list(snapshot.execute_sql(f"SELECT 1 FROM {NODE_TABLE} LIMIT 1"))
            return len(results) == 0

        return await _run_sync(_run)

    async def get_filtered_graph_data(
        self,
        attribute_filters: list[dict[str, list[str | int]]],
    ) -> tuple[
        list[tuple[str, dict[str, Any]]],
        list[tuple[str, str, str, dict[str, Any]]],
    ]:
        """Return nodes and edges filtered by the given attribute filters."""
        if not attribute_filters:
            return [], []
        if await self.is_empty():
            return [], []
        nodes, edges = await self.get_graph_data()
        filters = attribute_filters[0]
        filtered_ids: set[str] = set()
        for nid, props in nodes:
            if all(
                str(props.get(attr)) in [str(v) for v in values] for attr, values in filters.items()
            ):
                filtered_ids.add(str(nid))
        filtered_nodes = [(nid, p) for nid, p in nodes if str(nid) in filtered_ids]
        filtered_edges = [
            (s, t, r, p)
            for s, t, r, p in edges
            if str(s) in filtered_ids and str(t) in filtered_ids
        ]
        return filtered_nodes, filtered_edges

    async def get_graph_metrics(self, include_optional: bool = False) -> dict[str, Any]:
        nodes, edges = await self.get_graph_data()
        num_nodes = len(nodes)
        num_edges = len(edges)
        mean_degree = (2 * num_edges) / num_nodes if num_nodes > 0 else 0.0
        edge_density = num_edges / (num_nodes * (num_nodes - 1)) if num_nodes > 1 else 0.0
        # Connected components (undirected)
        adj: dict[str, set[str]] = {}
        for nid, _ in nodes:
            adj[str(nid)] = set()
        for src, tgt, _rel, _ in edges:
            adj.setdefault(str(src), set()).add(str(tgt))
            adj.setdefault(str(tgt), set()).add(str(src))
        visited: set[str] = set()
        component_sizes: list[int] = []
        for nid in adj:
            if nid in visited:
                continue
            stack = [nid]
            size = 0
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                size += 1
                stack.extend(adj.get(cur, []))
            component_sizes.append(size)
        metrics: dict[str, Any] = {
            "num_nodes": num_nodes,
            "num_edges": num_edges,
            "mean_degree": mean_degree,
            "edge_density": edge_density,
            "num_connected_components": len(component_sizes),
            "sizes_of_connected_components": component_sizes,
        }
        if not include_optional:
            metrics["num_selfloops"] = -1
            metrics["diameter"] = -1
            metrics["avg_shortest_path_length"] = -1
            metrics["avg_clustering"] = -1
        else:
            num_selfloops = sum(1 for s, t, _, _ in edges if s == t)
            metrics["num_selfloops"] = num_selfloops
            # Optional: diameter and avg path (expensive for large graphs)
            metrics["diameter"] = -1
            metrics["avg_shortest_path_length"] = -1
            metrics["avg_clustering"] = -1
        return metrics

    async def has_edge(self, source_id: str, target_id: str, relationship_name: str) -> bool:
        database = self._get_database()

        def _run() -> bool:
            with database.snapshot() as snapshot:
                results = list(
                    snapshot.execute_sql(
                        f"""
                        SELECT 1 FROM {EDGE_TABLE}
                        WHERE source_id = @source_id
                          AND target_id = @target_id
                          AND relationship_type = @rel
                        LIMIT 1
                        """,
                        params={
                            "source_id": source_id,
                            "target_id": target_id,
                            "rel": relationship_name,
                        },
                        param_types={
                            "source_id": "STRING",
                            "target_id": "STRING",
                            "rel": "STRING",
                        },
                    )
                )
            return len(results) > 0

        return await _run_sync(_run)

    async def has_edges(
        self, edges: list[tuple[str, str, str, dict[str, Any]]]
    ) -> list[tuple[str, str, str, dict[str, Any]]]:
        found: list[tuple[str, str, str, dict[str, Any]]] = []
        for edge in edges:
            src, tgt, rel = edge[0], edge[1], edge[2]
            if await self.has_edge(src, tgt, rel):
                found.append(edge)
        return found

    async def get_edges(self, node_id: str) -> list[tuple[str, str, str, dict[str, Any]]]:
        database = self._get_database()

        def _run() -> list[tuple[str, str, str, dict[str, Any]]]:
            with database.snapshot() as snapshot:
                results = list(
                    snapshot.execute_sql(
                        f"""
                        SELECT source_id, target_id, relationship_type, properties
                        FROM {EDGE_TABLE}
                        WHERE source_id = @id OR target_id = @id
                        """,
                        params={"id": node_id},
                        param_types={"id": "STRING"},
                    )
                )
            out: list[tuple[str, str, str, dict[str, Any]]] = []
            for row in results:
                src = row[0] if hasattr(row, "__getitem__") else getattr(row, "source_id", None)
                tgt = row[1] if hasattr(row, "__getitem__") else getattr(row, "target_id", None)
                rel = (
                    row[2] if hasattr(row, "__getitem__") else getattr(row, "relationship_type", "")
                )
                props = self._parse_json_properties(
                    row[3] if hasattr(row, "__getitem__") else getattr(row, "properties", None)
                )
                out.append((str(src), str(tgt), str(rel), props))
            return out

        return await _run_sync(_run)

    async def get_neighbors(self, node_id: str) -> list[dict[str, Any]]:
        edges = await self.get_edges(node_id)
        neighbor_ids: set[str] = set()
        for src, tgt, _rel, _ in edges:
            other = tgt if src == node_id else src
            neighbor_ids.add(other)
        if not neighbor_ids:
            return []
        return await self.get_nodes(list(neighbor_ids))

    async def get_neighborhood(
        self,
        node_ids: list[str],
        depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> tuple[
        list[tuple[str, dict[str, Any]]],
        list[tuple[str, str, str, dict[str, Any]]],
    ]:
        """Get the k-hop neighborhood subgraph around seed nodes.

        Performs an iterative BFS using the adapter's existing get_edges
        primitive, collecting all node IDs within `depth` hops of any seed
        node. If edge_types is provided, only edges of those relationship
        types are traversed. Returns (nodes, edges) in the same format as
        get_graph_data().
        """
        if not node_ids:
            return [], []

        edge_type_set: set[str] | None = set(edge_types) if edge_types else None

        # Iterative BFS over the graph using get_edges as the traversal primitive.
        reachable_ids: set[str] = {str(nid) for nid in node_ids}
        frontier: set[str] = set(reachable_ids)
        for _ in range(max(0, depth)):
            if not frontier:
                break
            next_frontier: set[str] = set()
            for current in frontier:
                for src, tgt, rel, _props in await self.get_edges(current):
                    if edge_type_set is not None and str(rel) not in edge_type_set:
                        continue
                    other = str(tgt) if str(src) == current else str(src)
                    if other not in reachable_ids:
                        next_frontier.add(other)
            reachable_ids.update(next_frontier)
            frontier = next_frontier

        # Collect nodes for all reachable ids.
        nodes: list[tuple[str, dict[str, Any]]] = []
        for nid in reachable_ids:
            node = await self.get_node(nid)
            if node is not None:
                nodes.append((str(nid), node))

        # Collect edges whose both endpoints are within the neighborhood,
        # honoring the edge_types filter.
        seen_edges: set[tuple[str, str, str]] = set()
        edges: list[tuple[str, str, str, dict[str, Any]]] = []
        for nid in reachable_ids:
            for src, tgt, rel, props in await self.get_edges(nid):
                s, t, r = str(src), str(tgt), str(rel)
                if s not in reachable_ids or t not in reachable_ids:
                    continue
                if edge_type_set is not None and r not in edge_type_set:
                    continue
                key = (s, t, r)
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                edges.append((s, t, r, props))

        return nodes, edges

    async def get_nodeset_subgraph(
        self,
        node_type: type[Any],
        node_name: list[str],
        node_name_filter_operator: str = "OR",
    ) -> tuple[
        list[tuple[int, dict[str, Any]]],
        list[tuple[int, int, str, dict[str, Any]]],
    ]:
        """Fetch a subgraph of matching primary nodes, their direct neighbors,
        and all edges interconnecting that node set.

        Primary nodes are those whose properties match the given node_type
        (DataPoint subclass name, stored in the "type" property) and whose
        "name" property is in node_name. With operator "OR" a neighbor of any
        primary node qualifies; with "AND" a neighbor must be connected to
        every primary node. Returns (nodes, edges) in get_graph_data() format.
        """
        if node_name_filter_operator not in ("OR", "AND"):
            raise NodesetFilterNotSupportedError

        label = node_type.__name__
        wanted_names = set(node_name)

        all_nodes, all_edges = await self.get_graph_data()

        # Identify primary nodes by type label and name.
        primary_ids: set[str] = set()
        for nid, props in all_nodes:
            if str(props.get("type")) == label and props.get("name") in wanted_names:
                primary_ids.add(str(nid))

        if not primary_ids:
            return [], []

        # Count, per neighbor, how many distinct primary nodes it connects to.
        neighbor_primary_links: dict[str, set[str]] = {}
        for src, tgt, _rel, _props in all_edges:
            s, t = str(src), str(tgt)
            if s in primary_ids and t not in primary_ids:
                neighbor_primary_links.setdefault(t, set()).add(s)
            elif t in primary_ids and s not in primary_ids:
                neighbor_primary_links.setdefault(s, set()).add(t)

        if node_name_filter_operator == "OR":
            neighbor_ids = set(neighbor_primary_links.keys())
        else:
            neighbor_ids = {
                nbr for nbr, primaries in neighbor_primary_links.items() if primaries == primary_ids
            }

        all_ids = primary_ids | neighbor_ids

        nodes = [(nid, props) for nid, props in all_nodes if str(nid) in all_ids]
        edges = [
            (s, t, r, p) for s, t, r, p in all_edges if str(s) in all_ids and str(t) in all_ids
        ]
        return nodes, edges

    async def get_connections(
        self, node_id: str | UUID
    ) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        nid = str(node_id)
        node = await self.get_node(nid)
        if node is None:
            return []
        edges = await self.get_edges(nid)
        connections: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for src, tgt, rel, rel_props in edges:
            other_id = tgt if src == nid else src
            other = await self.get_node(other_id)
            if other is None:
                continue
            connections.append((node, {"relationship_name": rel, **rel_props}, other))
        return connections
