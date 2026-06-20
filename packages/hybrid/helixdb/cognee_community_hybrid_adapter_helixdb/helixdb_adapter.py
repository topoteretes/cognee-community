# This module embeds HelixQL query/schema definitions as string literals whose
# lines necessarily exceed the 100-character limit; suppress E501 file-wide.
# ruff: noqa: E501
import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import UUID

from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.engine.utils import parse_id

if TYPE_CHECKING:
    from cognee.infrastructure.databases.graph.graph_db_interface import (
        GraphDBInterface,
    )
    from cognee.infrastructure.databases.vector.vector_db_interface import (
        VectorDBInterface,
    )

import helix
from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
from cognee.infrastructure.databases.graph.graph_db_interface import (
    EdgeData,
    Node,
    NodeData,
)
from cognee.infrastructure.databases.vector.embeddings import get_embedding_engine
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
    EmbeddingEngine,
)
from cognee.infrastructure.engine import DataPoint

# ---------------------------------------------------------------------------
# HQL Schema – written to schema.hx and deployed on first connect
# ---------------------------------------------------------------------------
SCHEMA_HX = """\
N::CogneeNode {
    UNIQUE INDEX node_id: String,
    INDEX node_type: String,
    INDEX name: String,
    properties_json: String,
    updated_at: String
}

E::CogneeEdge {
    From: CogneeNode,
    To: CogneeNode,
    Properties: {
        relationship_name: String,
        source_node_id: String,
        target_node_id: String,
        properties_json: String,
        updated_at: String
    }
}

V::CogneeVector {
    node_id: String,
    collection_name: String,
    text: String,
    properties_json: String
}
"""

# ---------------------------------------------------------------------------
# HQL Queries – written to queries.hx and deployed on first connect
# ---------------------------------------------------------------------------
QUERIES_HX = """\
// ── Node CRUD ──

QUERY upsert_node(node_id: String, node_type: String, name: String, properties_json: String, updated_at: String) =>
    existing <- N<CogneeNode>({node_id: node_id})
    node <- existing::UpsertN({node_id: node_id, node_type: node_type, name: name, properties_json: properties_json, updated_at: updated_at})
    RETURN node

QUERY get_node(node_id: String) =>
    node <- N<CogneeNode>({node_id: node_id})
    RETURN node

QUERY get_all_nodes() =>
    nodes <- N<CogneeNode>
    RETURN nodes

QUERY delete_node(node_id: String) =>
    DROP N<CogneeNode>({node_id: node_id})

// ── Edge CRUD ──

QUERY add_edge(source_node_id: String, target_node_id: String, relationship_name: String, properties_json: String, updated_at: String) =>
    src <- N<CogneeNode>({node_id: source_node_id})
    dst <- N<CogneeNode>({node_id: target_node_id})
    edge <- AddE<CogneeEdge>({relationship_name: relationship_name, source_node_id: source_node_id, target_node_id: target_node_id, properties_json: properties_json, updated_at: updated_at})::From(src)::To(dst)
    RETURN edge

QUERY get_edges_out(node_id: String) =>
    node <- N<CogneeNode>({node_id: node_id})
    edges <- node::OutE<CogneeEdge>
    RETURN edges

QUERY get_edges_in(node_id: String) =>
    node <- N<CogneeNode>({node_id: node_id})
    edges <- node::InE<CogneeEdge>
    RETURN edges

QUERY get_all_edges() =>
    edges <- E<CogneeEdge>
    RETURN edges

// ── Traversal ──

QUERY get_outgoing_neighbors(node_id: String) =>
    node <- N<CogneeNode>({node_id: node_id})
    neighbors <- node::Out<CogneeEdge>
    RETURN neighbors

QUERY get_incoming_neighbors(node_id: String) =>
    node <- N<CogneeNode>({node_id: node_id})
    neighbors <- node::In<CogneeEdge>
    RETURN neighbors

// ── Vector ──

QUERY add_vector(vector: [F64], node_id: String, collection_name: String, text: String, properties_json: String) =>
    vec <- AddV<CogneeVector>(vector, {node_id: node_id, collection_name: collection_name, text: text, properties_json: properties_json})
    RETURN vec

QUERY search_vectors(vector: [F64], limit: I64) =>
    results <- SearchV<CogneeVector>(vector, limit)
    RETURN results

// ── Utility ──

QUERY is_empty_check() =>
    nodes <- N<CogneeNode>
    RETURN nodes
"""


class IndexSchema(DataPoint):
    text: str
    metadata: dict = {"index_fields": ["text"]}


class HelixDBAdapter:
    """Hybrid graph-vector adapter for HelixDB.

    Implements both GraphDBInterface and VectorDBInterface so that a single
    HelixDB instance can serve as the graph *and* vector backend for cognee.

    HelixDB uses pre-compiled HQL queries. The adapter deploys a generic
    schema (CogneeNode / CogneeEdge / CogneeVector) and a fixed set of
    query endpoints at initialisation time.  Dynamic node types are stored
    as a ``node_type`` property; arbitrary properties are JSON-serialised
    into ``properties_json``.
    """

    def __init__(
        self,
        graph_database_url: str | None = None,
        graph_database_port: int | None = 6969,
        graph_database_username: str | None = None,
        graph_database_password: str | None = None,
        graph_database_key: str | None = None,
        embedding_engine: EmbeddingEngine | None = None,
        url: str | None = None,
        api_key: str | None = None,
        database_name: str | None = "cognee_graph",
        **kwargs: Any,
    ):
        host = url or graph_database_url or "localhost"
        port = graph_database_port or 6969
        self.database_name = database_name or "cognee_graph"

        self._schema_deployed = False
        self._deploy_schema(port)

        if host in ("localhost", "127.0.0.1"):
            self.client = helix.Client(local=True, port=port)
        else:
            self.client = helix.Client(api_endpoint=f"http://{host}:{port}")

        self.embedding_engine = embedding_engine or get_embedding_engine()
        self._collections: set[str] = set()

    # ------------------------------------------------------------------
    # Schema / query deployment
    # ------------------------------------------------------------------

    def _deploy_schema(self, port: int) -> None:
        """Write schema.hx + queries.hx and deploy via helix.Instance."""
        try:
            config_dir = os.path.join(tempfile.gettempdir(), f"helixdb_cognee_{self.database_name}")
            os.makedirs(config_dir, exist_ok=True)

            with open(os.path.join(config_dir, "schema.hx"), "w") as fh:
                fh.write(SCHEMA_HX)
            with open(os.path.join(config_dir, "queries.hx"), "w") as fh:
                fh.write(QUERIES_HX)

            from helix.instance import Instance

            instance = Instance(config_dir, port, verbose=False)
            instance.deploy()
            self._schema_deployed = True
        except Exception:
            # Schema may already be deployed, or Instance is unavailable for
            # remote connections.  The adapter will still work if the schema
            # was previously deployed.
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_query(self, query_name: str, params: dict | None = None) -> Any:
        """Execute a compiled HQL query asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, partial(self.client.query, query_name, params or {})
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _serialize_properties(properties: dict[str, Any]) -> str:
        clean: dict[str, Any] = {}
        for k, v in properties.items():
            if v is None:
                continue
            if isinstance(v, UUID):
                clean[k] = str(v)
            else:
                clean[k] = v
        return json.dumps(clean, default=str)

    @staticmethod
    def _deserialize_properties(properties_json: str) -> dict[str, Any]:
        if not properties_json:
            return {}
        try:
            return json.loads(properties_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    def _node_to_dict(self, raw: Any) -> dict[str, Any]:
        """Convert a raw HelixDB node result to a flat properties dict."""
        if isinstance(raw, dict):
            props = self._deserialize_properties(raw.get("properties_json", "{}"))
            props["id"] = raw.get("node_id", "")
            props["name"] = raw.get("name", "")
            props["type"] = raw.get("node_type", "")
            props["updated_at"] = raw.get("updated_at", "")
            return props
        if isinstance(raw, (list, tuple)) and len(raw) > 0:
            return self._node_to_dict(raw[0])
        return {}

    @staticmethod
    def _edge_to_tuple(raw: Any) -> EdgeData:
        """Convert a raw HelixDB edge result to an EdgeData tuple."""
        if isinstance(raw, dict):
            props_json = raw.get("properties_json", "{}")
            try:
                extra = json.loads(props_json) if isinstance(props_json, str) else {}
            except (json.JSONDecodeError, TypeError):
                extra = {}
            props: dict[str, Any] = {**extra}
            props["relationship_name"] = raw.get("relationship_name", "")
            return (
                raw.get("source_node_id", ""),
                raw.get("target_node_id", ""),
                raw.get("relationship_name", ""),
                props,
            )
        return ("", "", "", {})

    # ==================================================================
    # GraphDBInterface
    # ==================================================================

    async def query(self, query: str, params: dict | None = None) -> list[Any]:
        result = await self._run_query(query, params or {})
        if result is None:
            return []
        if isinstance(result, list):
            return result
        return [result]

    async def add_node(
        self, node: DataPoint | str, properties: dict[str, Any] | None = None
    ) -> None:
        if isinstance(node, str):
            node_id = node
            props = dict(properties) if properties else {}
        elif hasattr(node, "id") and hasattr(node, "model_dump"):
            node_id = str(node.id)
            props = node.model_dump()
        else:
            raise ValueError(f"Invalid node argument: {type(node)}")

        node_type = str(
            props.pop("type", type(node).__name__ if not isinstance(node, str) else "Node")
        )
        name = str(props.pop("name", ""))
        for key in ("id", "updated_at"):
            props.pop(key, None)

        await self._run_query(
            "upsert_node",
            {
                "node_id": node_id,
                "node_type": node_type,
                "name": name,
                "properties_json": self._serialize_properties(props),
                "updated_at": self._now(),
            },
        )

    async def add_nodes(self, nodes: list[Node] | list[DataPoint]) -> None:
        for node in nodes:
            if isinstance(node, tuple) and len(node) == 2:
                node_id, properties = node
                await self.add_node(node_id, properties)
            elif hasattr(node, "id") and hasattr(node, "model_dump"):
                embeddable_values: list[Any] = []
                property_names = DataPoint.get_embeddable_property_names(node)  # type: ignore
                vector_map: dict[str, int] = {}
                for property_name in property_names:
                    property_value = getattr(node, property_name, None)
                    if property_value is not None:
                        vector_map[property_name] = len(embeddable_values)
                        embeddable_values.append(property_value)

                vectorized_values = (
                    await self.embed_data(embeddable_values) if embeddable_values else []
                )

                props = node.model_dump()
                node_id = str(props.pop("id", node.id))
                node_type = str(props.pop("type", type(node).__name__))
                name = str(props.pop("name", ""))
                props.pop("updated_at", None)

                for property_name, idx in vector_map.items():
                    props[f"{property_name}_vector"] = vectorized_values[idx]

                await self._run_query(
                    "upsert_node",
                    {
                        "node_id": node_id,
                        "node_type": node_type,
                        "name": name,
                        "properties_json": self._serialize_properties(props),
                        "updated_at": self._now(),
                    },
                )
            else:
                raise ValueError(
                    f"Invalid node format: {node}. Expected tuple (node_id, properties) "
                    f"or DataPoint object."
                )

    async def delete_node(self, node_id: str) -> None:
        await self._run_query("delete_node", {"node_id": node_id})

    async def delete_nodes(self, node_ids: list[str]) -> None:
        for node_id in node_ids:
            await self.delete_node(node_id)

    async def get_node(self, node_id: str) -> NodeData | None:
        result = await self._run_query("get_node", {"node_id": node_id})
        if not result:
            return None
        if isinstance(result, list):
            return self._node_to_dict(result[0]) if result else None
        return self._node_to_dict(result)

    async def get_nodes(self, node_ids: list[str]) -> list[NodeData]:
        nodes: list[NodeData] = []
        for node_id in node_ids:
            node = await self.get_node(node_id)
            if node:
                nodes.append(node)
        return nodes

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        relationship_name: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        if properties is None:
            properties = {}
        await self._run_query(
            "add_edge",
            {
                "source_node_id": source_id,
                "target_node_id": target_id,
                "relationship_name": relationship_name,
                "properties_json": self._serialize_properties(properties),
                "updated_at": self._now(),
            },
        )

    async def add_edges(
        self,
        edges: list[EdgeData] | list[tuple[str, str, str, dict[str, Any] | None]],
    ) -> None:
        for edge in edges:
            if isinstance(edge, tuple) and len(edge) == 4:
                source_id, target_id, relationship_name, properties = edge
                await self.add_edge(source_id, target_id, relationship_name, properties)
            else:
                raise ValueError(
                    f"Invalid edge format: {edge}. Expected tuple "
                    f"(source_id, target_id, relationship_name, properties)."
                )

    async def has_edge(self, source_id: str, target_id: str, relationship_name: str) -> bool:
        result = await self._run_query("get_edges_out", {"node_id": source_id})
        if not result or not isinstance(result, list):
            return False
        for edge_raw in result:
            if not isinstance(edge_raw, dict):
                continue
            if (
                edge_raw.get("target_node_id") == target_id
                and edge_raw.get("relationship_name") == relationship_name
            ):
                return True
        return False

    async def has_edges(self, edges: list[EdgeData]) -> list[EdgeData]:
        existing: list[EdgeData] = []
        for edge in edges:
            if await self.has_edge(str(edge[0]), str(edge[1]), edge[2]):
                existing.append(edge)
        return existing

    async def get_edges(self, node_id: str) -> list[EdgeData]:
        out_result = await self._run_query("get_edges_out", {"node_id": node_id})
        in_result = await self._run_query("get_edges_in", {"node_id": node_id})
        edges: list[EdgeData] = []
        for raw_list in (out_result, in_result):
            if raw_list and isinstance(raw_list, list):
                for raw in raw_list:
                    edges.append(self._edge_to_tuple(raw))
        return edges

    async def get_neighbors(self, node_id: str) -> list[NodeData]:
        out = await self._run_query("get_outgoing_neighbors", {"node_id": node_id})
        inp = await self._run_query("get_incoming_neighbors", {"node_id": node_id})
        seen: set[str] = set()
        neighbors: list[NodeData] = []
        for raw_list in (out, inp):
            if raw_list and isinstance(raw_list, list):
                for raw in raw_list:
                    node = self._node_to_dict(raw)
                    nid = node.get("id", "")
                    if nid and nid not in seen:
                        seen.add(nid)
                        neighbors.append(node)
        return neighbors

    async def get_connections(
        self, node_id: str | UUID
    ) -> list[tuple[NodeData, dict[str, Any], NodeData]]:
        node_id_str = str(node_id)
        node_data = await self.get_node(node_id_str)
        if not node_data:
            return []

        connections: list[tuple[NodeData, dict[str, Any], NodeData]] = []

        # Predecessors
        in_edges = await self._run_query("get_edges_in", {"node_id": node_id_str})
        if in_edges and isinstance(in_edges, list):
            for raw_edge in in_edges:
                edge_dict = raw_edge if isinstance(raw_edge, dict) else {}
                src_id = edge_dict.get("source_node_id", "")
                src_node = await self.get_node(src_id) if src_id else {}
                connections.append(
                    (
                        src_node or {},
                        {"relationship_name": edge_dict.get("relationship_name", "")},
                        node_data,
                    )
                )

        # Successors
        out_edges = await self._run_query("get_edges_out", {"node_id": node_id_str})
        if out_edges and isinstance(out_edges, list):
            for raw_edge in out_edges:
                edge_dict = raw_edge if isinstance(raw_edge, dict) else {}
                dst_id = edge_dict.get("target_node_id", "")
                dst_node = await self.get_node(dst_id) if dst_id else {}
                connections.append(
                    (
                        node_data,
                        {"relationship_name": edge_dict.get("relationship_name", "")},
                        dst_node or {},
                    )
                )

        return connections

    async def get_graph_data(self) -> tuple[list[Node], list[EdgeData]]:
        raw_nodes = await self._run_query("get_all_nodes")
        raw_edges = await self._run_query("get_all_edges")

        nodes: list[Node] = []
        if raw_nodes and isinstance(raw_nodes, list):
            for raw in raw_nodes:
                d = self._node_to_dict(raw)
                nodes.append((d.get("id", ""), d))

        edges: list[EdgeData] = []
        if raw_edges and isinstance(raw_edges, list):
            for raw in raw_edges:
                edges.append(self._edge_to_tuple(raw))

        return (nodes, edges)

    async def get_neighborhood(
        self,
        node_ids: list[str],
        depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> tuple[list[Node], list[EdgeData]]:
        """Return the k-hop neighborhood subgraph around the seed nodes.

        Collects all nodes and edges within ``depth`` hops of any seed node,
        returning them in the same format as :meth:`get_graph_data`. When
        ``edge_types`` is provided, traversal only follows edges whose
        ``relationship_name`` is in that list.
        """
        if not node_ids:
            return [], []

        seed_ids = {str(nid) for nid in node_ids}
        allowed_types = set(edge_types) if edge_types else None

        # BFS expansion up to `depth` hops, following only allowed edge types.
        visited: set[str] = set(seed_ids)
        frontier: set[str] = set(seed_ids)
        for _ in range(max(depth, 0)):
            if not frontier:
                break
            next_frontier: set[str] = set()
            for nid in frontier:
                # Traverse via edges so we can honor edge_types filtering.
                edges = await self.get_edges(nid)
                for edge in edges:
                    if allowed_types is not None and edge[2] not in allowed_types:
                        continue
                    neighbor_id = edge[1] if edge[0] == nid else edge[0]
                    if neighbor_id and neighbor_id not in visited:
                        visited.add(neighbor_id)
                        next_frontier.add(neighbor_id)
            frontier = next_frontier

        # Fetch the collected nodes in get_graph_data() format.
        nodes: list[Node] = []
        for nid in visited:
            nd = await self.get_node(nid)
            if nd:
                nodes.append((nd.get("id", nid), nd))

        # Fetch all edges whose endpoints are both in the collected set,
        # respecting the edge_types filter.
        seen_edges: set[tuple[str, str, str]] = set()
        edges: list[EdgeData] = []
        for nid in visited:
            for edge in await self.get_edges(nid):
                if allowed_types is not None and edge[2] not in allowed_types:
                    continue
                if edge[0] in visited and edge[1] in visited:
                    key = (edge[0], edge[1], edge[2])
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append(edge)

        return (nodes, edges)

    async def get_graph_metrics(self, include_optional: bool = False) -> dict[str, Any]:
        nodes, edges = await self.get_graph_data()
        num_nodes = len(nodes)
        num_edges = len(edges)

        metrics: dict[str, Any] = {
            "num_nodes": num_nodes,
            "num_edges": num_edges,
            "mean_degree": (2 * num_edges) / num_nodes if num_nodes > 0 else 0,
            "edge_density": (num_edges / (num_nodes * (num_nodes - 1)) if num_nodes > 1 else 0),
            "num_connected_components": 1,
            "sizes_of_connected_components": [num_nodes] if num_nodes > 0 else [],
        }
        metrics.update(
            {
                "num_selfloops": 0 if include_optional else -1,
                "diameter": -1,
                "avg_shortest_path_length": -1,
                "avg_clustering": -1,
            }
        )
        return metrics

    async def get_nodeset_subgraph(
        self,
        node_type: type[Any],
        node_name: list[str],
        node_name_filter_operator: str = "OR",
    ) -> tuple[list[tuple[int, dict]], list[tuple[int, int, str, dict]]]:
        label = node_type.__name__
        all_nodes_raw = await self._run_query("get_all_nodes")
        primary_ids: list[str] = []
        if all_nodes_raw and isinstance(all_nodes_raw, list):
            for raw in all_nodes_raw:
                d = self._node_to_dict(raw)
                if d.get("type") != label:
                    continue
                # A node is a primary (seed) node when its name is one of the
                # requested node_name values. The operator distinguishes the
                # per-node match semantics: "OR" keeps a node matching any name
                # (the natural set membership), while "AND" only keeps nodes
                # whose belongs_to_set contains all requested names.
                if node_name_filter_operator == "AND":
                    if not self._matches_node_name(d.get("belongs_to_set"), node_name, "AND"):
                        continue
                else:
                    if d.get("name") not in node_name:
                        continue
                primary_ids.append(d.get("id", ""))

        if not primary_ids:
            return [], []

        neighbor_ids: set[str] = set()
        for pid in primary_ids:
            neighbors = await self.get_neighbors(pid)
            for n in neighbors:
                nid = n.get("id", "")
                if nid:
                    neighbor_ids.add(nid)

        all_ids = list(set(primary_ids) | neighbor_ids)

        nodes: list[tuple[int, dict]] = []
        for nid in all_ids:
            nd = await self.get_node(nid)
            if nd:
                nodes.append((nid, nd))  # type: ignore[arg-type]

        all_ids_set = set(all_ids)
        seen_edges: set[tuple[str, str, str]] = set()
        edges: list[tuple[int, int, str, dict]] = []
        for nid in all_ids:
            node_edges = await self.get_edges(nid)
            for edge in node_edges:
                key = (edge[0], edge[1], edge[2])
                if edge[0] in all_ids_set and edge[1] in all_ids_set and key not in seen_edges:
                    seen_edges.add(key)
                    edges.append(edge)  # type: ignore[arg-type]

        return nodes, edges

    async def get_filtered_graph_data(
        self, attribute_filters: list[dict[str, list[str | int]]]
    ) -> tuple[list[Node], list[EdgeData]]:
        all_nodes_raw = await self._run_query("get_all_nodes")
        all_edges_raw = await self._run_query("get_all_edges")

        filtered_node_ids: set[str] = set()
        filtered_nodes: list[Node] = []
        if all_nodes_raw and isinstance(all_nodes_raw, list):
            for raw in all_nodes_raw:
                d = self._node_to_dict(raw)
                match = True
                for attr_filter in attribute_filters:
                    for attr_name, attr_values in attr_filter.items():
                        node_val = d.get(attr_name)
                        if node_val is not None and node_val not in attr_values:
                            match = False
                            break
                    if not match:
                        break
                if match:
                    nid = d.get("id", "")
                    filtered_node_ids.add(nid)
                    filtered_nodes.append((nid, d))

        filtered_edges: list[EdgeData] = []
        if all_edges_raw and isinstance(all_edges_raw, list):
            for raw in all_edges_raw:
                e = self._edge_to_tuple(raw)
                if e[0] in filtered_node_ids and e[1] in filtered_node_ids:
                    filtered_edges.append(e)

        return (filtered_nodes, filtered_edges)

    async def delete_graph(self) -> None:
        raw_nodes = await self._run_query("get_all_nodes")
        if raw_nodes and isinstance(raw_nodes, list):
            for raw in raw_nodes:
                nid = raw.get("node_id", "") if isinstance(raw, dict) else ""
                if nid:
                    await self._run_query("delete_node", {"node_id": nid})

    async def prune(self) -> None:
        await self.delete_graph()

    async def is_empty(self) -> bool:
        result = await self._run_query("is_empty_check")
        if not result:
            return True
        if isinstance(result, list):
            return len(result) == 0
        return True

    # ==================================================================
    # VectorDBInterface
    # ==================================================================

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        return await self.embedding_engine.embed_text(data)  # type: ignore[return-value]

    async def create_collection(
        self, collection_name: str, payload_schema: Any | None = None
    ) -> None:
        self._collections.add(collection_name)

    async def has_collection(self, collection_name: str) -> bool:
        return collection_name in self._collections

    async def create_data_points(self, data_points: list[DataPoint]) -> None:
        embeddable_values: list[Any] = []
        vector_map: dict[str, dict[str, int | None]] = {}

        for dp in data_points:
            prop_names = DataPoint.get_embeddable_property_names(dp)
            key = str(dp.id)
            vector_map[key] = {}
            for pname in prop_names:
                pval = getattr(dp, pname, None)
                if pval is not None:
                    vector_map[key][pname] = len(embeddable_values)
                    embeddable_values.append(pval)
                else:
                    vector_map[key][pname] = None

        vectorized = await self.embed_data(embeddable_values) if embeddable_values else []

        for dp in data_points:
            dp_dict = dp.model_dump()
            dp_id = str(dp_dict.pop("id", dp.id))
            dp_type = str(dp_dict.pop("type", type(dp).__name__))
            dp_name = str(dp_dict.pop("name", ""))
            dp_dict.pop("updated_at", None)

            prop_names = DataPoint.get_embeddable_property_names(dp)
            for pname in prop_names:
                vidx = vector_map[str(dp.id)].get(pname)
                if vidx is not None:
                    dp_dict[f"{pname}_vector"] = vectorized[vidx]

            # Upsert the graph node
            await self._run_query(
                "upsert_node",
                {
                    "node_id": dp_id,
                    "node_type": dp_type,
                    "name": dp_name,
                    "properties_json": self._serialize_properties(dp_dict),
                    "updated_at": self._now(),
                },
            )

            # Add vector entries for each embeddable property
            for pname in prop_names:
                vidx = vector_map[str(dp.id)].get(pname)
                if vidx is not None:
                    vector = vectorized[vidx]
                    text = str(getattr(dp, pname, ""))
                    collection_name = f"{dp_type}_{pname}"
                    self._collections.add(collection_name)

                    vec_meta = {
                        "data_point_id": dp_id,
                        "data_point_type": dp_type,
                        "data_point_name": dp_name,
                    }
                    await self._run_query(
                        "add_vector",
                        {
                            "vector": [float(v) for v in vector],
                            "node_id": dp_id,
                            "collection_name": collection_name,
                            "text": text,
                            "properties_json": self._serialize_properties(vec_meta),
                        },
                    )

    async def create_vector_index(self, index_name: str, index_property_name: str) -> None:
        pass  # HelixDB manages vector indexing automatically

    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
    ) -> None:
        pass  # HelixDB manages vector indexing automatically

    @staticmethod
    def _matches_node_name(
        belongs_to_set: Any,
        node_name: list[str],
        node_name_filter_operator: str,
    ) -> bool:
        """Return True if ``belongs_to_set`` satisfies the node_name filter.

        ``node_name_filter_operator`` "OR" matches if the payload's
        ``belongs_to_set`` contains ANY of the names; "AND" requires ALL.
        """
        if isinstance(belongs_to_set, str):
            owned = {belongs_to_set}
        elif isinstance(belongs_to_set, (list, tuple, set)):
            owned = {str(v) for v in belongs_to_set}
        else:
            owned = set()

        if node_name_filter_operator == "AND":
            return all(name in owned for name in node_name)
        return any(name in owned for name in node_name)

    async def search(
        self,
        collection_name: str,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        limit: int | None = None,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
        node_name_filter_operator: str = "OR",
    ) -> list:
        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()

        if query_text and not query_vector:
            query_vector = (await self.embed_data([query_text]))[0]

        # Search with a larger limit to account for filtering by collection
        # (and, when requested, by belongs_to_set / node_name).
        effective_limit = (limit or 10) * 5
        raw_results = await self._run_query(
            "search_vectors",
            {
                "vector": [float(v) for v in query_vector],  # type: ignore[union-attr]
                "limit": effective_limit,
            },
        )

        if not raw_results or not isinstance(raw_results, list):
            return []

        scored_results: list[ScoredResult] = []
        for idx, raw in enumerate(raw_results):
            if not isinstance(raw, dict):
                continue
            vec_collection = raw.get("collection_name", "")
            if vec_collection != collection_name:
                continue

            # HelixQL has no array-contains predicate, so filter results whose
            # payload "belongs_to_set" matches the requested node_name in Python.
            stored_payload = self._deserialize_properties(raw.get("properties_json", "{}"))
            if node_name:
                if not self._matches_node_name(
                    stored_payload.get("belongs_to_set"),
                    node_name,
                    node_name_filter_operator,
                ):
                    continue

            payload: dict[str, Any] = {}
            if include_payload:
                payload = stored_payload
                payload["text"] = raw.get("text", "")
                payload["id"] = raw.get("node_id", "")

            result_payload = payload
            if with_vector and query_vector is not None:
                result_payload = {**payload, "vector": query_vector}

            scored_results.append(
                ScoredResult(
                    id=parse_id(raw.get("node_id", "")),
                    score=raw.get("score", 1.0 - (idx * 0.01)),
                    payload=result_payload,
                )
            )

        if limit is not None:
            scored_results = scored_results[:limit]

        return scored_results

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int | None = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
    ) -> list:
        query_vectors = await self.embedding_engine.embed_text(query_texts)
        results = await asyncio.gather(
            *[
                self.search(
                    collection_name=collection_name,
                    query_vector=qv,
                    limit=limit,
                    with_vector=with_vectors,
                    include_payload=include_payload,
                    node_name=node_name,
                )
                for qv in query_vectors
            ]
        )
        return list(results)

    async def retrieve(self, collection_name: str, data_point_ids: list[UUID]) -> list:
        results: list[Any] = []
        for dp_id in data_point_ids:
            node = await self.get_node(str(dp_id))
            if not node:
                continue
            # Collections are stored as "{node_type}_{property}" (see
            # create_data_points), so scope retrieval to data points whose
            # node_type matches the requested collection_name's prefix.
            node_type = node.get("type")
            if collection_name and node_type:
                if not collection_name.startswith(f"{node_type}_"):
                    continue
            results.append(node)
        return results

    async def delete_data_points(self, collection_name: str, data_point_ids: list[UUID]) -> None:
        # HelixDB does not support deleting vectors independently;
        # delete the containing node instead.
        for dp_id in data_point_ids:
            await self.delete_node(str(dp_id))


if TYPE_CHECKING:
    _a: GraphDBInterface = HelixDBAdapter("", 0, None)  # type: ignore[arg-type]
    _b: VectorDBInterface = HelixDBAdapter("", 0, None)  # type: ignore[arg-type]
