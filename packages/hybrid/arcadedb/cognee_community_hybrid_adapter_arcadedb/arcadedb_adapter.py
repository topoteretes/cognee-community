"""ArcadeDB Hybrid Adapter for Graph and Vector Database

ArcadeDB is a multi-model database that supports both graph traversal
(OpenCypher) and vector search (SQL with LSM_VECTOR/HNSW indexes and
the vectorNeighbors() function).

All operations use ArcadeDB's HTTP API, with Cypher for graph queries
and SQL for vector operations (index creation, vector property updates,
and KNN search).
"""

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime
from enum import Enum
from textwrap import dedent
from typing import Any, Optional
from uuid import UUID

import aiohttp
from cognee.infrastructure.databases.graph.graph_db_interface import (
    EdgeData,
    GraphDBInterface,
    Node,
    NodeData,
)
from cognee.infrastructure.databases.vector.embeddings import get_embedding_engine
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import (
    EmbeddingEngine,
)
from cognee.infrastructure.databases.vector.exceptions import CollectionNotFoundError
from cognee.infrastructure.databases.vector.models.ScoredResult import ScoredResult
from cognee.infrastructure.databases.vector.vector_db_interface import (
    VectorDBInterface,
)
from cognee.infrastructure.engine import DataPoint
from cognee.infrastructure.engine.utils import parse_id
from cognee.modules.storage.utils import JSONEncoder
from cognee.shared.logging_utils import ERROR, get_logger

logger = get_logger("ArcadeDBAdapter", level=ERROR)


class IndexSchema(DataPoint):
    """Schema for indexing that includes text data and associated metadata."""

    text: str
    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: list[str] = []


class ArcadeDBAdapter(VectorDBInterface, GraphDBInterface):
    """
    Hybrid adapter for ArcadeDB supporting both graph and vector operations.

    All communication uses ArcadeDB's HTTP API:
    - Graph operations use Cypher (language="cypher") with parameterized queries
    - Vector operations use SQL (language="sql") for LSM_VECTOR indexes and
      vectorNeighbors() KNN search
    """

    def __init__(
        self,
        graph_database_url: str,
        graph_database_username: Optional[str] = None,
        graph_database_password: Optional[str] = None,
        embedding_engine: Optional[EmbeddingEngine] = None,
        driver: Optional[Any] = None,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        database_name: Optional[str] = "cognee",
        **kwargs,
    ):
        raw_url = url if url else graph_database_url
        self.graph_database_username = graph_database_username
        self.graph_database_password = graph_database_password
        self.database_name = database_name or "cognee"

        # Derive HTTP base URL
        host = raw_url.split("://")[-1].split(":")[0].split("/")[0]
        http_port = kwargs.get("http_port", 2480)
        self.http_base_url = f"http://{host}:{http_port}"

        self.embedding_engine = (
            get_embedding_engine() if not embedding_engine else embedding_engine
        )

        # Track which vector indexes have been created this session
        self._vector_indexes: set[str] = set()

    # -------------------------------------------------------------------------
    # HTTP API helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _now() -> int:
        """Return current time in milliseconds for updated_at fields."""
        return int(time.time() * 1000)

    def _get_auth(self) -> Optional[aiohttp.BasicAuth]:
        if self.graph_database_username and self.graph_database_password:
            return aiohttp.BasicAuth(
                self.graph_database_username, self.graph_database_password
            )
        return None

    async def _http_request(
        self,
        endpoint: str,
        payload: dict,
    ) -> dict:
        """Execute a request via ArcadeDB's HTTP API."""
        url = f"{self.http_base_url}/api/v1/{endpoint}/{self.database_name}"
        async with aiohttp.ClientSession(auth=self._get_auth()) as session:
            async with session.post(url, json=payload) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(
                        f"ArcadeDB HTTP error ({resp.status}): {body}"
                    )
                return json.loads(body)

    async def _sql(self, sql: str) -> dict:
        """Execute an SQL command."""
        return await self._http_request(
            "command", {"language": "sql", "command": sql}
        )

    async def _sql_query(self, sql: str) -> dict:
        """Execute an SQL read query."""
        return await self._http_request(
            "query", {"language": "sql", "command": sql}
        )

    async def _cypher(
        self,
        cypher: str,
        params: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """Execute a Cypher command with optional parameters."""
        payload = {"language": "cypher", "command": cypher}
        if params:
            payload["params"] = params
        result = await self._http_request("command", payload)
        return result.get("result", [])

    # -------------------------------------------------------------------------
    # GraphDBInterface - Cypher operations
    # -------------------------------------------------------------------------

    async def query(
        self,
        query: str,
        params: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        return await self._cypher(query, params)

    async def has_node(self, node_id: str) -> bool:
        results = await self._cypher(
            "MATCH (n) WHERE n.id = $node_id RETURN COUNT(n) > 0 AS node_exists",
            {"node_id": node_id},
        )
        return results[0]["node_exists"] if results else False

    async def add_node(self, node: DataPoint):
        serialized = self.serialize_properties(node.model_dump())
        return await self._cypher(
            "MERGE (node {id: $node_id}) "
            "ON CREATE SET node += $properties, node.updated_at = $now "
            "ON MATCH SET node += $properties, node.updated_at = $now "
            "RETURN node.id AS nodeId",
            {"node_id": str(node.id), "properties": serialized, "now": self._now()},
        )

    async def add_nodes(self, nodes: list[DataPoint]) -> None:
        nodes_data = [
            {
                "node_id": str(node.id),
                "properties": self.serialize_properties(node.model_dump()),
            }
            for node in nodes
        ]
        return await self._cypher(
            "UNWIND $nodes AS node "
            "MERGE (n {id: node.node_id}) "
            "ON CREATE SET n += node.properties, n.updated_at = $now "
            "ON MATCH SET n += node.properties, n.updated_at = $now "
            "RETURN n.id AS nodeId",
            {"nodes": nodes_data, "now": self._now()},
        )

    async def extract_node(self, node_id: str):
        results = await self.extract_nodes([node_id])
        return results[0] if results else None

    async def extract_nodes(self, node_ids: list[str]):
        # ArcadeDB HTTP Cypher returns vertices as flat dicts
        return await self._cypher(
            "UNWIND $node_ids AS id MATCH (node {id: id}) RETURN node",
            {"node_ids": node_ids},
        )

    async def delete_node(self, node_id: str):
        return await self._cypher(
            "MATCH (node {id: $node_id}) DETACH DELETE node",
            {"node_id": node_id},
        )

    async def delete_nodes(self, node_ids: list[str]) -> None:
        return await self._cypher(
            "UNWIND $node_ids AS id MATCH (node {id: id}) DETACH DELETE node",
            {"node_ids": node_ids},
        )

    async def has_edge(self, from_node: UUID, to_node: UUID, edge_label: str) -> bool:
        records = await self._cypher(
            "MATCH (from_node)-[relationship]->(to_node) "
            "WHERE from_node.id = $from_node_id AND to_node.id = $to_node_id "
            "AND type(relationship) = $edge_label "
            "RETURN COUNT(relationship) > 0 AS edge_exists",
            {
                "from_node_id": str(from_node),
                "to_node_id": str(to_node),
                "edge_label": edge_label,
            },
        )
        return records[0]["edge_exists"] if records else False

    async def has_edges(self, edges):
        params = {
            "edges": [
                {
                    "from_node": str(edge[0]),
                    "to_node": str(edge[1]),
                    "relationship_name": edge[2],
                }
                for edge in edges
            ],
        }
        results = await self._cypher(
            "UNWIND $edges AS edge "
            "MATCH (a)-[r]->(b) "
            "WHERE a.id = edge.from_node AND b.id = edge.to_node "
            "AND type(r) = edge.relationship_name "
            "RETURN edge.from_node AS from_node, edge.to_node AS to_node, "
            "edge.relationship_name AS relationship_name, "
            "count(r) > 0 AS edge_exists",
            params,
        )
        return [r["edge_exists"] for r in results]

    async def add_edge(
        self,
        from_node: UUID,
        to_node: UUID,
        relationship_name: str,
        edge_properties: Optional[dict[str, Any]] = None,
    ):
        serialized = self.serialize_properties(edge_properties or {})
        return await self._cypher(
            dedent(f"""\
                MATCH (from_node {{id: $from_node}}),
                      (to_node {{id: $to_node}})
                MERGE (from_node)-[r:{relationship_name}]->(to_node)
                ON CREATE SET r += $properties, r.updated_at = $now
                ON MATCH SET r += $properties, r.updated_at = $now
                RETURN r"""),
            {
                "from_node": str(from_node),
                "to_node": str(to_node),
                "properties": serialized,
                "now": self._now(),
            },
        )

    async def add_edges(
        self, edges: list[tuple[str, str, str, dict[str, Any]]]
    ) -> None:
        grouped: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
        for src, dst, rel_type, properties in edges:
            grouped[rel_type].append((src, dst, properties or {}))

        for rel_type, rel_edges in grouped.items():
            edge_data = [
                {
                    "from_node": str(src),
                    "to_node": str(dst),
                    "properties": {
                        **(props if props else {}),
                        "source_node_id": str(src),
                        "target_node_id": str(dst),
                    },
                }
                for src, dst, props in rel_edges
            ]
            await self._cypher(
                dedent(f"""\
                    UNWIND $edges AS edge
                    MATCH (from_node {{id: edge.from_node}}),
                          (to_node   {{id: edge.to_node}})
                    MERGE (from_node)-[r:{rel_type}{{
                          source_node_id: edge.from_node,
                          target_node_id: edge.to_node
                      }}]->(to_node)
                    ON CREATE SET r += edge.properties, r.updated_at = $now
                    ON MATCH  SET r += edge.properties, r.updated_at = $now
                    RETURN count(r) AS merged"""),
                {"edges": edge_data, "now": self._now()},
            )

    async def get_edges(self, node_id: str):
        results = await self._cypher(
            "MATCH (n {id: $node_id})-[r]-(m) "
            "RETURN n.id AS source_id, m.id AS target_id, type(r) AS rel_type",
            {"node_id": node_id},
        )
        return [
            (r["source_id"], r["target_id"], {"relationship_name": r["rel_type"]})
            for r in results
        ]

    async def get_disconnected_nodes(self) -> list[str]:
        results = await self._cypher(
            "MATCH (n) WHERE NOT (n)--() RETURN collect(n.id) AS ids"
        )
        return results[0]["ids"] if results else []

    async def get_predecessors(
        self, node_id: str, edge_label: Optional[str] = None
    ) -> list[str]:
        if edge_label is not None:
            results = await self._cypher(
                "MATCH (node)<-[r]-(predecessor) "
                "WHERE node.id = $node_id AND type(r) = $edge_label "
                "RETURN predecessor",
                {"node_id": node_id, "edge_label": edge_label},
            )
        else:
            results = await self._cypher(
                "MATCH (node)<-[r]-(predecessor) "
                "WHERE node.id = $node_id RETURN predecessor",
                {"node_id": node_id},
            )
        return results

    async def get_successors(
        self, node_id: str, edge_label: Optional[str] = None
    ) -> list[str]:
        if edge_label is not None:
            results = await self._cypher(
                "MATCH (node)-[r]->(successor) "
                "WHERE node.id = $node_id AND type(r) = $edge_label "
                "RETURN successor",
                {"node_id": node_id, "edge_label": edge_label},
            )
        else:
            results = await self._cypher(
                "MATCH (node)-[r]->(successor) "
                "WHERE node.id = $node_id RETURN successor",
                {"node_id": node_id},
            )
        return results

    async def get_neighbors(self, node_id: str) -> list[dict[str, Any]]:
        predecessors, successors = await asyncio.gather(
            self.get_predecessors(node_id), self.get_successors(node_id)
        )
        return predecessors + successors

    async def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        results = await self._cypher(
            "MATCH (node {id: $node_id}) RETURN node",
            {"node_id": node_id},
        )
        return results[0] if results else None

    async def get_nodes(self, node_ids: list[str]) -> list[dict[str, Any]]:
        # ArcadeDB HTTP Cypher returns vertices as flat dicts
        return await self._cypher(
            "UNWIND $node_ids AS id MATCH (node {id: id}) RETURN node",
            {"node_ids": node_ids},
        )

    async def get_connections(self, node_id: UUID) -> list:
        predecessors, successors = await asyncio.gather(
            self._cypher(
                "MATCH (node)<-[r]-(neighbour) "
                "WHERE node.id = $node_id "
                "RETURN neighbour.id AS src, type(r) AS rel, node.id AS dst",
                {"node_id": str(node_id)},
            ),
            self._cypher(
                "MATCH (node)-[r]->(neighbour) "
                "WHERE node.id = $node_id "
                "RETURN node.id AS src, type(r) AS rel, neighbour.id AS dst",
                {"node_id": str(node_id)},
            ),
        )

        connections = []
        for r in predecessors:
            connections.append(
                (r["src"], {"relationship_name": r["rel"]}, r["dst"])
            )
        for r in successors:
            connections.append(
                (r["src"], {"relationship_name": r["rel"]}, r["dst"])
            )
        return connections

    async def remove_connection_to_predecessors_of(
        self, node_ids: list[str], edge_label: str
    ) -> None:
        await self._cypher(
            "UNWIND $node_ids AS nid "
            "MATCH (node {id: nid})-[r]->(predecessor) "
            "WHERE type(r) = $edge_label DELETE r",
            {"node_ids": node_ids, "edge_label": edge_label},
        )

    async def remove_connection_to_successors_of(
        self, node_ids: list[str], edge_label: str
    ) -> None:
        await self._cypher(
            "UNWIND $node_ids AS nid "
            "MATCH (node {id: nid})<-[r]-(successor) "
            "WHERE type(r) = $edge_label DELETE r",
            {"node_ids": node_ids, "edge_label": edge_label},
        )

    async def delete_graph(self):
        return await self._cypher("MATCH (node) DETACH DELETE node")

    def serialize_properties(self, properties=None):
        if properties is None:
            properties = {}
        serialized = {}
        for key, value in properties.items():
            if value is None:
                continue
            elif isinstance(value, UUID):
                serialized[key] = str(value)
            elif isinstance(value, Enum):
                serialized[key] = value.value
            elif isinstance(value, datetime):
                serialized[key] = int(value.timestamp() * 1000)
            elif isinstance(value, dict):
                serialized[key] = json.dumps(value, cls=JSONEncoder)
            elif isinstance(value, list):
                if value and isinstance(value[0], float):
                    continue  # Skip vector properties in Cypher serialization
                serialized[key] = json.dumps(value, cls=JSONEncoder)
            else:
                serialized[key] = value
        return serialized

    async def get_graph_data(self):
        result = await self._cypher(
            "MATCH (n) RETURN n.id AS id, labels(n) AS labels, "
            "properties(n) AS properties"
        )
        nodes = [(r["id"], r["properties"]) for r in result]

        result = await self._cypher(
            "MATCH (n)-[r]->(m) RETURN n.id AS source, m.id AS target, "
            "TYPE(r) AS type, properties(r) AS properties"
        )
        edges = [
            (r["source"], r["target"], r["type"], r["properties"])
            for r in result
        ]
        return (nodes, edges)

    async def get_nodeset_subgraph(
        self,
        node_type: type[Any],
        node_name: list[str],
        node_name_filter_operator: str = "OR",
    ) -> tuple[list[tuple[int, dict]], list[tuple[int, int, str, dict]]]:
        label = node_type.__name__

        primary_result = await self._cypher(
            f"UNWIND $names AS wantedName MATCH (n:{label}) "
            "WHERE n.name = wantedName "
            "RETURN DISTINCT n.id AS id, properties(n) AS properties",
            {"names": node_name},
        )
        if not primary_result:
            return [], []

        primary_ids = [r["id"] for r in primary_result]

        if node_name_filter_operator == "OR":
            neighbor_result = await self._cypher(
                "MATCH (n)-[]-(nbr) WHERE n.id IN $ids "
                "RETURN DISTINCT nbr.id AS id, properties(nbr) AS properties",
                {"ids": primary_ids},
            )
        else:
            neighbor_result = await self._cypher(
                "MATCH (n)-[]-(nbr) WHERE n.id IN $ids "
                "WITH nbr, COUNT(DISTINCT n.id) AS matched_count "
                "WHERE matched_count = $primary_count "
                "RETURN nbr.id AS id, properties(nbr) AS properties",
                {"ids": primary_ids, "primary_count": len(primary_ids)},
            )

        neighbor_ids = [r["id"] for r in neighbor_result] if neighbor_result else []
        all_ids = list(set(primary_ids + neighbor_ids))

        nodes_result = await self._cypher(
            "MATCH (n) WHERE n.id IN $ids "
            "RETURN n.id AS id, properties(n) AS properties",
            {"ids": all_ids},
        )
        nodes = [(r["id"], r["properties"]) for r in nodes_result]

        edges_result = await self._cypher(
            "MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids "
            "RETURN a.id AS source, b.id AS target, "
            "type(r) AS type, properties(r) AS properties",
            {"ids": all_ids},
        )
        edges = [
            (r["source"], r["target"], r["type"], r["properties"])
            for r in edges_result
        ]
        return nodes, edges

    async def get_filtered_graph_data(self, attribute_filters):
        if not attribute_filters:
            return await self.get_graph_data()

        where_clauses_n = []
        where_clauses_m = []
        params = {}

        for i, filter_dict in enumerate(attribute_filters):
            for attr, values in filter_dict.items():
                if not values:
                    continue
                param_name = f"values_{i}_{attr}"
                normalized = [
                    str(v) if isinstance(v, UUID) else v for v in values
                ]
                where_clauses_n.append(f"n.{attr} IN ${param_name}")
                where_clauses_m.append(f"m.{attr} IN ${param_name}")
                params[param_name] = normalized

        if not where_clauses_n:
            return await self.get_graph_data()

        node_where = " AND ".join(where_clauses_n)
        edge_where = (
            f"{' AND '.join(where_clauses_n)} AND "
            f"{' AND '.join(where_clauses_m)}"
        )

        result_nodes = await self._cypher(
            f"MATCH (n) WHERE {node_where} "
            "RETURN n.id AS id, properties(n) AS properties",
            params,
        )
        nodes = [(r["id"], r["properties"]) for r in result_nodes]

        result_edges = await self._cypher(
            f"MATCH (n)-[r]->(m) WHERE {edge_where} "
            "RETURN n.id AS source, m.id AS target, "
            "TYPE(r) AS type, properties(r) AS properties",
            params,
        )
        edges = [
            (r["source"], r["target"], r["type"], r["properties"])
            for r in result_edges
        ]
        return (nodes, edges)

    async def get_graph_metrics(self, include_optional=False):
        try:
            node_count = await self._cypher(
                "MATCH (n) RETURN count(n) AS cnt"
            )
            edge_count = await self._cypher(
                "MATCH ()-[r]->() RETURN count(r) AS cnt"
            )
            num_nodes = node_count[0]["cnt"] if node_count else 0
            num_edges = edge_count[0]["cnt"] if edge_count else 0

            metrics = {
                "num_nodes": num_nodes,
                "num_edges": num_edges,
                "mean_degree": (
                    (2 * num_edges) / num_nodes if num_nodes > 0 else 0
                ),
                "edge_density": (
                    num_edges / (num_nodes * (num_nodes - 1))
                    if num_nodes > 1
                    else 0
                ),
                "num_connected_components": 1 if num_nodes > 0 else 0,
                "sizes_of_connected_components": (
                    [num_nodes] if num_nodes > 0 else []
                ),
            }

            if include_optional:
                self_loops = await self._cypher(
                    "MATCH (n)-[r]->(n) RETURN COUNT(r) AS cnt"
                )
                metrics.update(
                    {
                        "num_selfloops": (
                            self_loops[0]["cnt"] if self_loops else 0
                        ),
                        "diameter": -1,
                        "avg_shortest_path_length": -1,
                        "avg_clustering": -1,
                    }
                )
            else:
                metrics.update(
                    {
                        "num_selfloops": -1,
                        "diameter": -1,
                        "avg_shortest_path_length": -1,
                        "avg_clustering": -1,
                    }
                )
            return metrics

        except Exception as e:
            logger.error(f"Failed to get graph metrics: {e}")
            return {
                "num_nodes": 0,
                "num_edges": 0,
                "mean_degree": 0,
                "edge_density": 0,
                "num_connected_components": 0,
                "sizes_of_connected_components": [],
                "num_selfloops": -1,
                "diameter": -1,
                "avg_shortest_path_length": -1,
                "avg_clustering": -1,
            }

    async def is_empty(self) -> bool:
        result = await self._cypher("MATCH (n) RETURN true LIMIT 1")
        return len(result) == 0

    # -------------------------------------------------------------------------
    # VectorDBInterface - SQL operations
    # -------------------------------------------------------------------------

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        """Embed text data into vectors using the configured embedding engine."""
        if not data:
            return []

        non_blank = [s for s in data if s and s.strip()]
        if not non_blank:
            return [[] for _ in data]

        result = await self.embedding_engine.embed_text(non_blank)

        if len(non_blank) == len(data):
            return result

        it = iter(result)
        return [next(it) if (s and s.strip()) else [] for s in data]

    async def has_collection(self, collection_name: str) -> bool:
        """Check if a vertex type (collection) exists in ArcadeDB."""
        type_name = (
            collection_name.split("_")[0]
            if "_" in collection_name
            else collection_name
        )
        try:
            await self._sql(
                f"SELECT count(*) AS cnt FROM `{type_name}` LIMIT 0"
            )
            return True
        except RuntimeError:
            return False

    async def create_collection(
        self, collection_name: str, payload_schema: Optional[Any] = None
    ):
        """Create a vertex type in ArcadeDB if it does not exist."""
        type_name = (
            collection_name.split("_")[0]
            if "_" in collection_name
            else collection_name
        )
        try:
            await self._sql(
                f"CREATE VERTEX TYPE `{type_name}` IF NOT EXISTS"
            )
        except RuntimeError:
            pass

    async def create_vector_index(
        self, index_name: str, index_property_name: str
    ) -> None:
        """Create an LSM_VECTOR (HNSW) index on a vertex type property.

        index_name is the vertex type name, index_property_name is the property
        to index (will be suffixed with _vector).
        """
        vector_prop = f"{index_property_name}_vector"
        index_key = f"{index_name}.{vector_prop}"

        if index_key in self._vector_indexes:
            return

        vector_size = self.embedding_engine.get_vector_size()

        try:
            await self._sql(
                f"CREATE PROPERTY `{index_name}`.`{vector_prop}` "
                "IF NOT EXISTS ARRAY_OF_FLOATS"
            )
        except RuntimeError:
            pass

        try:
            await self._sql(
                f"CREATE INDEX ON `{index_name}` (`{vector_prop}`) "
                f"LSM_VECTOR METADATA {{ dimensions: {vector_size}, "
                f"similarity: 'COSINE' }}"
            )
            self._vector_indexes.add(index_key)
        except RuntimeError as e:
            err_msg = str(e).lower()
            if "already exists" in err_msg or "duplicate" in err_msg:
                self._vector_indexes.add(index_key)
            else:
                raise

    async def create_data_points(
        self, collection_name: str, data_points: list[DataPoint]
    ):
        """Embed and store data points as vertices with vector properties.

        Nodes are upserted via Cypher MERGE, then vector properties are set
        via SQL UPDATE (vectors use ARRAY_OF_FLOATS properties indexed with
        LSM_VECTOR).
        """
        if not data_points:
            return

        # Collect all embeddable values for batch embedding
        embeddable_values: list[str] = []
        vector_map: dict[str, dict[str, Optional[int]]] = {}

        for dp in data_points:
            prop_names = DataPoint.get_embeddable_property_names(dp)
            key = str(dp.id)
            vector_map[key] = {}

            for prop_name in prop_names:
                prop_value = getattr(dp, prop_name, None)
                if prop_value is not None:
                    vector_map[key][prop_name] = len(embeddable_values)
                    embeddable_values.append(str(prop_value))
                else:
                    vector_map[key][prop_name] = None

        # Batch embed all values at once
        vectorized_values = (
            await self.embed_data(embeddable_values)
            if embeddable_values
            else []
        )

        for dp in data_points:
            node_label = type(dp).__name__
            prop_names = DataPoint.get_embeddable_property_names(dp)
            dp_key = str(dp.id)

            # Upsert node via Cypher (graph properties only)
            properties = self.serialize_properties(dp.model_dump())
            await self._cypher(
                f"MERGE (node:{node_label} {{id: $node_id}}) "
                "ON CREATE SET node += $properties, "
                "node.updated_at = $now "
                "ON MATCH SET node += $properties, "
                "node.updated_at = $now",
                {"node_id": dp_key, "properties": properties, "now": self._now()},
            )

            # Set vector properties via SQL UPDATE
            for prop_name in prop_names:
                vec_idx = vector_map[dp_key].get(prop_name)
                if vec_idx is not None and vec_idx < len(vectorized_values):
                    vector = vectorized_values[vec_idx]
                    vector_prop = f"{prop_name}_vector"
                    vec_str = ",".join(str(f) for f in vector)

                    await self._sql(
                        f"UPDATE `{node_label}` "
                        f"SET `{vector_prop}` = [{vec_str}] "
                        f"WHERE id = '{dp_key}'"
                    )

    async def index_data_points(
        self,
        index_name: str,
        index_property_name: str,
        data_points: list[DataPoint],
    ) -> None:
        """Ensure the vector index exists. Data points are indexed
        automatically by ArcadeDB when the vector property is set."""
        await self.create_vector_index(index_name, index_property_name)

    async def retrieve(self, collection_name: str, data_point_ids: list[str]):
        """Retrieve data points by their IDs."""
        results = await self._cypher(
            "MATCH (node) WHERE node.id IN $node_ids RETURN node",
            {"node_ids": [str(dp_id) for dp_id in data_point_ids]},
        )
        return results

    async def search(
        self,
        collection_name: str,
        query_text: Optional[str] = None,
        query_vector: Optional[list[float]] = None,
        limit: Optional[int] = None,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: Optional[list[str]] = None,
        node_name_filter_operator: str = "OR",
    ) -> list:
        """Perform vector similarity search using ArcadeDB's vectorNeighbors().

        collection_name format: "TypeName_propertyName" (e.g. "IndexSchema_text")

        Uses: SELECT expand(vectorNeighbors('Type[prop_vector]', [vec], k))
        Returns ScoredResult objects sorted by distance (lower = closer).
        """
        if query_text is None and query_vector is None:
            raise ValueError(
                "Either query_text or query_vector must be provided"
            )

        if query_text and not query_vector:
            query_vector = (await self.embed_data([query_text]))[0]

        # Parse collection_name to extract type and property
        if "_" in collection_name:
            type_name, _, attr_name = collection_name.partition("_")
        else:
            type_name = collection_name
            attr_name = "text"

        vector_prop = f"{attr_name}_vector"

        if limit is None:
            limit = 10
        if limit == 0:
            return []

        vec_str = ",".join(str(f) for f in query_vector)

        try:
            result = await self._sql_query(
                f"SELECT *, distance FROM ("
                f"  SELECT expand(vectorNeighbors("
                f"    '{type_name}[{vector_prop}]', [{vec_str}], {limit}"
                f"  ))"
                f")"
            )
        except RuntimeError as e:
            if "not found" in str(e).lower() or "index" in str(e).lower():
                raise CollectionNotFoundError(
                    f"No vector index found for collection {collection_name}"
                )
            raise

        records = result.get("result", [])

        # Apply node_name filtering if specified
        if node_name:
            filtered = []
            for record in records:
                belongs_to = record.get("belongs_to_set", [])
                if isinstance(belongs_to, str):
                    try:
                        belongs_to = json.loads(belongs_to)
                    except (json.JSONDecodeError, TypeError):
                        belongs_to = [belongs_to]

                if node_name_filter_operator == "OR":
                    if any(n in belongs_to for n in node_name):
                        filtered.append(record)
                else:
                    if all(n in belongs_to for n in node_name):
                        filtered.append(record)
            records = filtered

        scored_results = []
        for record in records:
            # vectorNeighbors() returns a 'distance' field (lower = closer)
            distance = record.get("distance", 0.0)
            record_id = record.get("id", "")

            payload_data = {}
            if include_payload:
                payload_data = {
                    k: v
                    for k, v in record.items()
                    if k
                    not in ("@rid", "@cat", "@type", "distance")
                    and not k.endswith("_vector")
                }
                if "name" in payload_data:
                    payload_data["text"] = payload_data["name"]

            scored_results.append(
                ScoredResult(
                    id=parse_id(record_id),
                    score=distance,
                    payload=payload_data if include_payload else None,
                )
            )

        return scored_results

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: Optional[int] = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: Optional[list[str]] = None,
        node_name_filter_operator: str = "OR",
    ) -> list:
        """Perform batch vector search across multiple queries."""
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
                    node_name_filter_operator=node_name_filter_operator,
                )
                for qv in query_vectors
            ]
        )
        return results

    async def delete_data_points(
        self, collection_name: str, data_point_ids: list[UUID]
    ):
        """Delete data points by their IDs."""
        return await self._cypher(
            "MATCH (node) WHERE node.id IN $node_ids DETACH DELETE node",
            {"node_ids": [str(dp_id) for dp_id in data_point_ids]},
        )

    async def prune(self):
        """Remove all data from the database."""
        await self.delete_graph()
