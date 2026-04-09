"""ArcadeDB Hybrid Adapter for Graph and Vector Database

ArcadeDB is a multi-model database that supports both graph traversal (via the
Neo4j Bolt wire protocol with OpenCypher) and vector search (via its native SQL
engine with HNSW indexes and the vectorNeighbors() function).

Graph operations use the neo4j async Python driver over Bolt.
Vector operations use ArcadeDB's HTTP API for SQL queries, since vector indexing
and search are SQL-native features.
"""

import asyncio
import json
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from enum import Enum
from textwrap import dedent
from typing import Any, Optional
from uuid import UUID

import aiohttp
from cognee.infrastructure.databases.exceptions.exceptions import (
    NodesetFilterNotSupportedError,
)
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
from neo4j import AsyncGraphDatabase, AsyncSession
from neo4j.exceptions import Neo4jError

logger = get_logger("ArcadeDBAdapter", level=ERROR)


class IndexSchema(DataPoint):
    """Schema for indexing that includes text data and associated metadata."""

    text: str
    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: list[str] = []


class ArcadeDBAdapter(VectorDBInterface, GraphDBInterface):
    """
    Hybrid adapter for ArcadeDB supporting both graph and vector operations.

    Graph operations use the Neo4j Bolt wire protocol (ArcadeDB supports Bolt
    natively). Vector operations use ArcadeDB's HTTP API for SQL queries with
    HNSW indexes and the vectorNeighbors() function.
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
        self.graph_database_url = url if url else graph_database_url
        self.graph_database_username = graph_database_username
        self.graph_database_password = graph_database_password
        self.database_name = database_name or "cognee"

        auth = None
        if graph_database_username and graph_database_password:
            auth = (graph_database_username, graph_database_password)

        # Bolt driver for graph operations (Cypher)
        bolt_url = self.graph_database_url
        if not bolt_url.startswith("bolt://"):
            # If an HTTP url was given, derive Bolt url on default port
            bolt_url = f"bolt://{bolt_url.split('://')[-1].split(':')[0]}:7687"

        self.driver = driver or AsyncGraphDatabase.driver(
            bolt_url,
            auth=auth,
            max_connection_lifetime=120,
        )

        # HTTP base URL for SQL queries (vector operations)
        http_host = self.graph_database_url.split("://")[-1].split(":")[0]
        http_port = kwargs.get("http_port", 2480)
        self.http_base_url = f"http://{http_host}:{http_port}"

        self.embedding_engine = (
            get_embedding_engine() if not embedding_engine else embedding_engine
        )

        # Track which vector indexes have been created this session
        self._vector_indexes: set[str] = set()

    # -------------------------------------------------------------------------
    # HTTP API helpers for SQL queries (used by vector operations)
    # -------------------------------------------------------------------------

    async def _http_command(self, sql: str) -> dict:
        """Execute an SQL command via ArcadeDB's HTTP API (POST /command)."""
        url = f"{self.http_base_url}/api/v1/command/{self.database_name}"
        payload = {"language": "sql", "command": sql}
        auth = None
        if self.graph_database_username and self.graph_database_password:
            auth = aiohttp.BasicAuth(
                self.graph_database_username, self.graph_database_password
            )

        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"ArcadeDB HTTP command failed ({resp.status}): {body}"
                    )
                return await resp.json()

    async def _http_query(self, sql: str) -> dict:
        """Execute an SQL query via ArcadeDB's HTTP API (POST /query)."""
        url = f"{self.http_base_url}/api/v1/query/{self.database_name}"
        payload = {"language": "sql", "command": sql}
        auth = None
        if self.graph_database_username and self.graph_database_password:
            auth = aiohttp.BasicAuth(
                self.graph_database_username, self.graph_database_password
            )

        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"ArcadeDB HTTP query failed ({resp.status}): {body}"
                    )
                return await resp.json()

    # -------------------------------------------------------------------------
    # Graph operations (Bolt / Cypher) - inherited from original adapter
    # -------------------------------------------------------------------------

    @asynccontextmanager
    async def get_session(self) -> AsyncSession:
        async with self.driver.session() as session:
            yield session

    async def query(
        self,
        query: str,
        params: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        try:
            async with self.get_session() as session:
                result = await session.run(query, params)
                data = await result.data()
                return data
        except Neo4jError as error:
            logger.error("ArcadeDB query error: %s", error, exc_info=True)
            raise error

    async def has_node(self, node_id: str) -> bool:
        results = await self.query(
            "MATCH (n) WHERE n.id = $node_id RETURN COUNT(n) > 0 AS node_exists",
            {"node_id": node_id},
        )
        return results[0]["node_exists"] if results else False

    async def add_node(self, node: DataPoint):
        serialized_properties = self.serialize_properties(node.model_dump())

        query = """
        MERGE (node {id: $node_id})
        ON CREATE SET node += $properties, node.updated_at = timestamp()
        ON MATCH SET node += $properties, node.updated_at = timestamp()
        RETURN node.id AS nodeId
        """

        params = {
            "node_id": str(node.id),
            "properties": serialized_properties,
        }
        return await self.query(query, params)

    async def add_nodes(self, nodes: list[DataPoint]) -> None:
        query = """
        UNWIND $nodes AS node
        MERGE (n {id: node.node_id})
        ON CREATE SET n += node.properties, n.updated_at = timestamp()
        ON MATCH SET n += node.properties, n.updated_at = timestamp()
        RETURN n.id AS nodeId
        """

        nodes_data = [
            {
                "node_id": str(node.id),
                "properties": self.serialize_properties(node.model_dump()),
            }
            for node in nodes
        ]

        return await self.query(query, {"nodes": nodes_data})

    async def extract_node(self, node_id: str):
        results = await self.extract_nodes([node_id])
        return results[0] if results else None

    async def extract_nodes(self, node_ids: list[str]):
        query = """
        UNWIND $node_ids AS id
        MATCH (node {id: id})
        RETURN node"""

        results = await self.query(query, {"node_ids": node_ids})
        return [result["node"] for result in results]

    async def delete_node(self, node_id: str):
        return await self.query(
            "MATCH (node {id: $node_id}) DETACH DELETE node",
            {"node_id": node_id},
        )

    async def delete_nodes(self, node_ids: list[str]) -> None:
        return await self.query(
            "UNWIND $node_ids AS id MATCH (node {id: id}) DETACH DELETE node",
            {"node_ids": node_ids},
        )

    async def has_edge(self, from_node: UUID, to_node: UUID, edge_label: str) -> bool:
        query = """
            MATCH (from_node)-[relationship]->(to_node)
            WHERE from_node.id = $from_node_id AND to_node.id = $to_node_id
            AND type(relationship) = $edge_label
            RETURN COUNT(relationship) > 0 AS edge_exists
        """

        records = await self.query(
            query,
            {
                "from_node_id": str(from_node),
                "to_node_id": str(to_node),
                "edge_label": edge_label,
            },
        )
        return records[0]["edge_exists"] if records else False

    async def has_edges(self, edges):
        query = """
            UNWIND $edges AS edge
            MATCH (a)-[r]->(b)
            WHERE a.id = edge.from_node AND b.id = edge.to_node
            AND type(r) = edge.relationship_name
            RETURN edge.from_node AS from_node, edge.to_node AS to_node,
            edge.relationship_name AS relationship_name,
            count(r) > 0 AS edge_exists
        """

        try:
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

            results = await self.query(query, params)
            return [result["edge_exists"] for result in results]
        except Neo4jError as error:
            logger.error("ArcadeDB query error: %s", error, exc_info=True)
            raise error

    async def add_edge(
        self,
        from_node: UUID,
        to_node: UUID,
        relationship_name: str,
        edge_properties: Optional[dict[str, Any]] = None,
    ):
        serialized_properties = self.serialize_properties(edge_properties or {})

        query = dedent(
            f"""\
            MATCH (from_node {{id: $from_node}}),
                  (to_node {{id: $to_node}})
            MERGE (from_node)-[r:{relationship_name}]->(to_node)
            ON CREATE SET r += $properties, r.updated_at = timestamp()
            ON MATCH SET r += $properties, r.updated_at = timestamp()
            RETURN r
            """
        )

        params = {
            "from_node": str(from_node),
            "to_node": str(to_node),
            "relationship_name": relationship_name,
            "properties": serialized_properties,
        }

        return await self.query(query, params)

    async def add_edges(
        self, edges: list[tuple[str, str, str, dict[str, Any]]]
    ) -> None:
        grouped: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
        for src, dst, rel_type, properties in edges:
            grouped[rel_type].append((src, dst, properties or {}))

        for rel_type, rel_edges in grouped.items():
            query = dedent(f"""
                UNWIND $edges AS edge
                MATCH (from_node {{id: edge.from_node}}),
                      (to_node   {{id: edge.to_node}})
                MERGE (from_node)-[r:{rel_type}{{
                      source_node_id: edge.from_node,
                      target_node_id: edge.to_node
                  }}]->(to_node)
                ON CREATE SET r += edge.properties,
                              r.updated_at = timestamp()
                ON MATCH  SET r += edge.properties,
                              r.updated_at = timestamp()
                RETURN count(r) AS merged
                """)

            edge_data = [
                {
                    "from_node": str(src),
                    "to_node": str(dst),
                    "properties": {
                        **(properties if properties else {}),
                        "source_node_id": str(src),
                        "target_node_id": str(dst),
                    },
                }
                for src, dst, properties in rel_edges
            ]
            try:
                await self.query(query, {"edges": edge_data})
            except Neo4jError as error:
                logger.error("ArcadeDB query error: %s", error, exc_info=True)
                raise error

    async def get_edges(self, node_id: str):
        results = await self.query(
            "MATCH (n {id: $node_id})-[r]-(m) RETURN n, r, m",
            {"node_id": node_id},
        )

        return [
            (
                result["n"]["id"],
                result["m"]["id"],
                {"relationship_name": result["r"][1]},
            )
            for result in results
        ]

    async def get_disconnected_nodes(self) -> list[str]:
        results = await self.query(
            "MATCH (n) WHERE NOT (n)--() RETURN collect(n.id) AS ids"
        )
        return results[0]["ids"] if results else []

    async def get_predecessors(
        self, node_id: str, edge_label: Optional[str] = None
    ) -> list[str]:
        if edge_label is not None:
            results = await self.query(
                "MATCH (node)<-[r]-(predecessor) WHERE node.id = $node_id AND type(r) = $edge_label RETURN predecessor",
                {"node_id": node_id, "edge_label": edge_label},
            )
        else:
            results = await self.query(
                "MATCH (node)<-[r]-(predecessor) WHERE node.id = $node_id RETURN predecessor",
                {"node_id": node_id},
            )
        return [result["predecessor"] for result in results]

    async def get_successors(
        self, node_id: str, edge_label: Optional[str] = None
    ) -> list[str]:
        if edge_label is not None:
            results = await self.query(
                "MATCH (node)-[r]->(successor) WHERE node.id = $node_id AND type(r) = $edge_label RETURN successor",
                {"node_id": node_id, "edge_label": edge_label},
            )
        else:
            results = await self.query(
                "MATCH (node)-[r]->(successor) WHERE node.id = $node_id RETURN successor",
                {"node_id": node_id},
            )
        return [result["successor"] for result in results]

    async def get_neighbors(self, node_id: str) -> list[dict[str, Any]]:
        predecessors, successors = await asyncio.gather(
            self.get_predecessors(node_id), self.get_successors(node_id)
        )
        return predecessors + successors

    async def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        results = await self.query(
            "MATCH (node {id: $node_id}) RETURN node",
            {"node_id": node_id},
        )
        return results[0]["node"] if results else None

    async def get_nodes(self, node_ids: list[str]) -> list[dict[str, Any]]:
        results = await self.query(
            "UNWIND $node_ids AS id MATCH (node {id: id}) RETURN node",
            {"node_ids": node_ids},
        )
        return [result["node"] for result in results]

    async def get_connections(self, node_id: UUID) -> list:
        predecessors_query = """
        MATCH (node)<-[relation]-(neighbour)
        WHERE node.id = $node_id
        RETURN neighbour, relation, node
        """
        successors_query = """
        MATCH (node)-[relation]->(neighbour)
        WHERE node.id = $node_id
        RETURN node, relation, neighbour
        """

        predecessors, successors = await asyncio.gather(
            self.query(predecessors_query, {"node_id": str(node_id)}),
            self.query(successors_query, {"node_id": str(node_id)}),
        )

        connections = []

        for neighbour in predecessors:
            rel = neighbour["relation"]
            connections.append(
                (rel[0], {"relationship_name": rel[1]}, rel[2])
            )

        for neighbour in successors:
            rel = neighbour["relation"]
            connections.append(
                (rel[0], {"relationship_name": rel[1]}, rel[2])
            )

        return connections

    async def remove_connection_to_predecessors_of(
        self, node_ids: list[str], edge_label: str
    ) -> None:
        return await self.query(
            "UNWIND $node_ids AS nid MATCH (node {id: nid})-[r]->(predecessor) WHERE type(r) = $edge_label DELETE r",
            {"node_ids": node_ids, "edge_label": edge_label},
        )

    async def remove_connection_to_successors_of(
        self, node_ids: list[str], edge_label: str
    ) -> None:
        return await self.query(
            "UNWIND $node_ids AS nid MATCH (node {id: nid})<-[r]-(successor) WHERE type(r) = $edge_label DELETE r",
            {"node_ids": node_ids, "edge_label": edge_label},
        )

    async def delete_graph(self):
        return await self.query("MATCH (node) DETACH DELETE node")

    def serialize_properties(self, properties=None):
        if properties is None:
            properties = {}
        serialized = {}
        for key, value in properties.items():
            if isinstance(value, UUID):
                serialized[key] = str(value)
            elif isinstance(value, Enum):
                serialized[key] = value.value
            elif isinstance(value, dict):
                serialized[key] = json.dumps(value, cls=JSONEncoder)
            elif isinstance(value, list) and value and isinstance(value[0], float):
                # Skip vector properties in Cypher serialization
                continue
            else:
                serialized[key] = value
        return serialized

    async def get_model_independent_graph_data(self):
        nodes = await self.query("MATCH (n) RETURN collect(n) AS nodes")
        edges = await self.query(
            "MATCH (n)-[r]->(m) RETURN collect([n, r, m]) AS elements"
        )
        return (nodes, edges)

    async def get_graph_data(self):
        result = await self.query(
            "MATCH (n) RETURN n.id AS id, labels(n) AS labels, properties(n) AS properties"
        )
        nodes = [(record["id"], record["properties"]) for record in result]

        result = await self.query(
            "MATCH (n)-[r]->(m) RETURN n.id AS source, m.id AS target, TYPE(r) AS type, properties(r) AS properties"
        )
        edges = [
            (record["source"], record["target"], record["type"], record["properties"])
            for record in result
        ]

        return (nodes, edges)

    async def get_nodeset_subgraph(
        self,
        node_type: type[Any],
        node_name: list[str],
        node_name_filter_operator: str = "OR",
    ) -> tuple[list[tuple[int, dict]], list[tuple[int, int, str, dict]]]:
        label = node_type.__name__

        primary_result = await self.query(
            f"UNWIND $names AS wantedName MATCH (n:{label}) WHERE n.name = wantedName RETURN DISTINCT n.id AS id, properties(n) AS properties",
            {"names": node_name},
        )
        if not primary_result:
            return [], []

        primary_ids = [record["id"] for record in primary_result]

        if node_name_filter_operator == "OR":
            neighbor_result = await self.query(
                "MATCH (n)-[]-(nbr) WHERE n.id IN $ids RETURN DISTINCT nbr.id AS id, properties(nbr) AS properties",
                {"ids": primary_ids},
            )
        else:
            neighbor_result = await self.query(
                "MATCH (n)-[]-(nbr) WHERE n.id IN $ids WITH nbr, COUNT(DISTINCT n.id) AS matched_count WHERE matched_count = $primary_count RETURN nbr.id AS id, properties(nbr) AS properties",
                {"ids": primary_ids, "primary_count": len(primary_ids)},
            )

        neighbor_ids = [record["id"] for record in neighbor_result] if neighbor_result else []
        all_ids = list(set(primary_ids + neighbor_ids))

        nodes_result = await self.query(
            "MATCH (n) WHERE n.id IN $ids RETURN n.id AS id, properties(n) AS properties",
            {"ids": all_ids},
        )
        nodes = [(record["id"], record["properties"]) for record in nodes_result]

        edges_result = await self.query(
            "MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids RETURN a.id AS source, b.id AS target, type(r) AS type, properties(r) AS properties",
            {"ids": all_ids},
        )
        edges = [
            (record["source"], record["target"], record["type"], record["properties"])
            for record in edges_result
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
                normalized = [str(v) if isinstance(v, UUID) else v for v in values]
                where_clauses_n.append(f"n.{attr} IN ${param_name}")
                where_clauses_m.append(f"m.{attr} IN ${param_name}")
                params[param_name] = normalized

        if not where_clauses_n:
            return await self.get_graph_data()

        node_where = " AND ".join(where_clauses_n)
        edge_where = f"{' AND '.join(where_clauses_n)} AND {' AND '.join(where_clauses_m)}"

        result_nodes = await self.query(
            f"MATCH (n) WHERE {node_where} RETURN n.id AS id, properties(n) AS properties",
            params,
        )
        nodes = [(record["id"], record["properties"]) for record in result_nodes]

        result_edges = await self.query(
            f"MATCH (n)-[r]->(m) WHERE {edge_where} RETURN n.id AS source, m.id AS target, TYPE(r) AS type, properties(r) AS properties",
            params,
        )
        edges = [
            (record["source"], record["target"], record["type"], record["properties"])
            for record in result_edges
        ]

        return (nodes, edges)

    async def get_graph_metrics(self, include_optional=False):
        try:
            node_count = await self.query("MATCH (n) RETURN count(n) AS cnt")
            edge_count = await self.query("MATCH ()-[r]->() RETURN count(r) AS cnt")
            num_nodes = node_count[0]["cnt"] if node_count else 0
            num_edges = edge_count[0]["cnt"] if edge_count else 0

            metrics = {
                "num_nodes": num_nodes,
                "num_edges": num_edges,
                "mean_degree": (2 * num_edges) / num_nodes if num_nodes > 0 else 0,
                "edge_density": (
                    num_edges / (num_nodes * (num_nodes - 1)) if num_nodes > 1 else 0
                ),
                "num_connected_components": 1 if num_nodes > 0 else 0,
                "sizes_of_connected_components": [num_nodes] if num_nodes > 0 else [],
            }

            if include_optional:
                self_loops = await self.query(
                    "MATCH (n)-[r]->(n) RETURN COUNT(r) AS cnt"
                )
                metrics.update(
                    {
                        "num_selfloops": self_loops[0]["cnt"] if self_loops else 0,
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
        result = await self.query("MATCH (n) RETURN true LIMIT 1")
        return len(result) == 0

    # -------------------------------------------------------------------------
    # Vector operations (HTTP API / SQL)
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
        try:
            # collection_name format: "TypeName_propertyName" or just "TypeName"
            type_name = collection_name.split("_")[0] if "_" in collection_name else collection_name
            result = await self._http_command(
                f"SELECT count(*) AS cnt FROM `{type_name}` LIMIT 0"
            )
            return True
        except RuntimeError:
            return False

    async def create_collection(
        self, collection_name: str, payload_schema: Optional[Any] = None
    ):
        """Create a vertex type in ArcadeDB if it does not exist.

        ArcadeDB auto-creates types on first INSERT when using Cypher/MERGE,
        so this is mainly to ensure the type exists for vector index creation.
        """
        type_name = collection_name.split("_")[0] if "_" in collection_name else collection_name
        try:
            await self._http_command(
                f"CREATE VERTEX TYPE `{type_name}` IF NOT EXISTS"
            )
        except RuntimeError:
            # Type may already exist
            pass

    async def create_vector_index(
        self, index_name: str, index_property_name: str
    ) -> None:
        """Create an HNSW vector index on a vertex type property in ArcadeDB.

        index_name is the vertex type name, index_property_name is the property
        to index (will be suffixed with _vector).
        """
        vector_prop = f"{index_property_name}_vector"
        index_key = f"{index_name}.{vector_prop}"

        if index_key in self._vector_indexes:
            return

        vector_size = self.embedding_engine.get_vector_size()

        try:
            # Create the property as ARRAY of FLOAT if not exists
            await self._http_command(
                f"CREATE PROPERTY `{index_name}`.`{vector_prop}` IF NOT EXISTS ARRAY OF FLOAT"
            )
        except RuntimeError:
            pass

        try:
            await self._http_command(
                f"CREATE INDEX ON `{index_name}` (`{vector_prop}`) NULL_STRATEGY SKIP HNSW {vector_size}"
            )
            self._vector_indexes.add(index_key)
        except RuntimeError as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                self._vector_indexes.add(index_key)
            else:
                raise

    async def create_data_points(
        self, collection_name: str, data_points: list[DataPoint]
    ):
        """Embed and store data points as vertices with vector properties.

        Each data point's embeddable properties are vectorized and stored alongside
        the original data. Vertices are upserted via Cypher MERGE, then vector
        properties are set via SQL UPDATE (since Bolt does not handle float arrays
        natively for HNSW indexing).
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
        vectorized_values = await self.embed_data(embeddable_values) if embeddable_values else []

        for dp in data_points:
            node_label = type(dp).__name__
            prop_names = DataPoint.get_embeddable_property_names(dp)
            dp_key = str(dp.id)

            # Upsert the node via Cypher (graph properties only)
            properties = self.serialize_properties(dp.model_dump())
            await self.query(
                f"MERGE (node:{node_label} {{id: $node_id}}) "
                "ON CREATE SET node += $properties, node.updated_at = timestamp() "
                "ON MATCH SET node += $properties, node.updated_at = timestamp()",
                {"node_id": dp_key, "properties": properties},
            )

            # Set vector properties via SQL UPDATE
            for prop_name in prop_names:
                vec_idx = vector_map[dp_key].get(prop_name)
                if vec_idx is not None and vec_idx < len(vectorized_values):
                    vector = vectorized_values[vec_idx]
                    vector_prop = f"{prop_name}_vector"
                    vector_str = ",".join(str(f) for f in vector)

                    await self._http_command(
                        f"UPDATE `{node_label}` SET `{vector_prop}` = [{vector_str}] "
                        f"WHERE id = '{dp_key}'"
                    )

    async def index_data_points(
        self,
        index_name: str,
        index_property_name: str,
        data_points: list[DataPoint],
    ) -> None:
        """Ensure the HNSW index exists. Data points are indexed automatically
        by ArcadeDB when the vector property is set."""
        await self.create_vector_index(index_name, index_property_name)

    async def retrieve(self, collection_name: str, data_point_ids: list[str]):
        """Retrieve data points by their IDs."""
        results = await self.query(
            "MATCH (node) WHERE node.id IN $node_ids RETURN node",
            {"node_ids": [str(dp_id) for dp_id in data_point_ids]},
        )
        return [result["node"] for result in results]

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
        """
        if query_text is None and query_vector is None:
            raise ValueError("Either query_text or query_vector must be provided")

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

        # Build the vector search SQL query using vectorNeighbors()
        vector_str = ",".join(str(f) for f in query_vector)

        sql = (
            f"SELECT *, vectorNeighbors('{type_name}', '{vector_prop}', [{vector_str}], {limit}) AS score "
            f"FROM `{type_name}` "
            f"WHERE vectorNeighbors('{type_name}', '{vector_prop}', [{vector_str}], {limit}) > 0"
        )

        try:
            result = await self._http_query(sql)
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
            score = record.get("score", 0.0)
            record_id = record.get("id", "")

            payload_data = {}
            if include_payload:
                payload_data = {
                    k: v
                    for k, v in record.items()
                    if k not in ("@rid", "@cat", "@type", "score")
                    and not k.endswith("_vector")
                }
                if "name" in payload_data:
                    payload_data["text"] = payload_data["name"]

            scored_results.append(
                ScoredResult(
                    id=parse_id(record_id),
                    score=score,
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
        return await self.query(
            "MATCH (node) WHERE node.id IN $node_ids DETACH DELETE node",
            {"node_ids": [str(dp_id) for dp_id in data_point_ids]},
        )

    async def prune(self):
        """Remove all data from the database."""
        await self.delete_graph()
