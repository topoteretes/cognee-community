"""ArcadeDB Hybrid Adapter for Graph and Vector Database

ArcadeDB is a multi-model database that supports both graph traversal
(OpenCypher) and vector search (SQL with LSM_VECTOR/HNSW indexes and
the vectorNeighbors() function).

Graph operations use either:
- Neo4j Bolt protocol (if the BoltProtocolPlugin is enabled, port 7687)
- HTTP API with Cypher (fallback, port 2480)

Vector operations always use ArcadeDB's HTTP API for SQL queries
(index creation, vector property updates, and KNN search).
"""

import asyncio
import json
import time
from datetime import datetime
from enum import Enum
from textwrap import dedent
from typing import Any
from uuid import UUID

import aiohttp
from cognee.infrastructure.databases.graph.config import get_graph_context_config
from cognee.infrastructure.databases.graph.graph_db_interface import (
    GraphDBInterface,
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

    Graph operations use the Neo4j Bolt protocol when available (requires
    the BoltProtocolPlugin on port 7687), falling back to HTTP API Cypher.
    Vector operations always use the HTTP API for SQL queries.
    """

    def __init__(
        self,
        graph_database_url: str,
        graph_database_username: str | None = None,
        graph_database_password: str | None = None,
        embedding_engine: EmbeddingEngine | None = None,
        driver: Any | None = None,
        url: str | None = None,
        api_key: str | None = None,
        database_name: str | None = "cognee",
        **kwargs,
    ):
        raw_url = url if url else graph_database_url
        self.graph_database_username = graph_database_username
        self.graph_database_password = graph_database_password
        self.database_name = database_name or "cognee"

        # Derive host from URL
        host = raw_url.split("://")[-1].split(":")[0].split("/")[0]

        # HTTP base URL (always needed for SQL/vector operations)
        http_port = kwargs.get("http_port", 2480)
        self.http_base_url = f"http://{host}:{http_port}"

        # Try to set up Bolt driver for graph operations
        self._bolt_driver = None
        bolt_port = kwargs.get("bolt_port", 7687)

        if driver is not None:
            # Pre-built driver provided
            self._bolt_driver = driver
            logger.info("Using provided Bolt driver")
        else:
            try:
                from neo4j import AsyncGraphDatabase

                bolt_url = f"bolt://{host}:{bolt_port}"
                auth = None
                if graph_database_username and graph_database_password:
                    auth = (graph_database_username, graph_database_password)

                self._bolt_driver = AsyncGraphDatabase.driver(
                    bolt_url,
                    auth=auth,
                    max_connection_lifetime=120,
                )
                logger.info("Bolt driver initialized at %s", bolt_url)
            except ImportError:
                logger.info("neo4j package not installed, using HTTP for Cypher")
            except Exception as e:
                logger.info("Could not initialize Bolt driver: %s, using HTTP", e)

        self.embedding_engine = get_embedding_engine() if not embedding_engine else embedding_engine

        # Track which vector indexes have been created this session
        self._vector_indexes: set[str] = set()
        # Track whether Bolt is confirmed working
        self._bolt_verified = False
        # Track whether the target database has been ensured
        self._database_ensured = False

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _now() -> int:
        """Return current time in milliseconds for updated_at fields."""
        return int(time.time() * 1000)

    @staticmethod
    def _split_collection_name(collection_name: str) -> tuple[str, str]:
        """Split Cognee collection names into logical type and indexed field."""
        if "_" in collection_name:
            logical_type, _, attr_name = collection_name.partition("_")
        else:
            logical_type = collection_name
            attr_name = "text"

        return logical_type, attr_name

    _vector_storage_type_cache: str | None = None

    async def _vector_storage_type(self) -> str:
        """
        Auto-detect the physical ArcadeDB type used for vector storage.

        ArcadeDB's Cypher MERGE creates vertex types whose case depends on the
        server version: 26.3.x → ``vertex`` (lowercase), 26.4+ → ``Vertex``
        (PascalCase).  Rather than hard-coding one or the other, we probe the
        schema on first call and cache the result.
        """
        if self._vector_storage_type_cache is not None:
            return self._vector_storage_type_cache

        try:
            result = await self._sql("SELECT types FROM schema")
            for record in result.get("result", []):
                types = record.get("types", [])
                for t in types:
                    if t.lower() == "vertex":
                        self._vector_storage_type_cache = t
                        return t
        except Exception:
            pass

        # Default for modern ArcadeDB (26.4+)
        self._vector_storage_type_cache = "Vertex"
        return "Vertex"

    @staticmethod
    def _escape_sql_literal(value: str) -> str:
        """Escape a string for safe embedding in simple SQL literals."""
        return value.replace("'", "''")

    async def _upsert_cypher_node(
        self, node_id: str, properties: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Upsert a graph node through the generic Vertex-backed Cypher path."""
        return await self._cypher(
            "MERGE (node {id: $node_id}) "
            "ON CREATE SET node += $properties, node.updated_at = $now "
            "ON MATCH SET node += $properties, node.updated_at = $now "
            "RETURN node.id AS nodeId",
            {"node_id": node_id, "properties": properties, "now": self._now()},
        )

    async def _store_vertex_vectors(
        self,
        node_id: str,
        node_type: str,
        embeddable_properties: dict[str, Any],
    ) -> None:
        """
        Persist embeddable property vectors on the shared Vertex storage.

        Cognee's graph pipeline writes nodes through the generic `MERGE (node
        {id: ...})` path, so vector properties must be updated on `Vertex`
        records keyed by `id` and the logical `type`.
        """
        values_to_embed = [
            (prop_name, str(prop_value))
            for prop_name, prop_value in embeddable_properties.items()
            if prop_value is not None and str(prop_value).strip()
        ]
        if not values_to_embed:
            return

        vectors = await self.embed_data([prop_value for _, prop_value in values_to_embed])
        escaped_node_id = self._escape_sql_literal(node_id)
        escaped_node_type = self._escape_sql_literal(node_type)

        for (prop_name, _), vector in zip(values_to_embed, vectors, strict=False):
            if not vector:
                continue

            await self.create_vector_index(node_type, prop_name)

            vector_prop = f"{prop_name}_vector"
            vec_str = ",".join(str(f) for f in vector)
            await self._sql(
                f"UPDATE `{await self._vector_storage_type()}` "
                f"SET `{vector_prop}` = [{vec_str}] "
                f"WHERE id = '{escaped_node_id}' "
                f"AND type = '{escaped_node_type}'"
            )

    def _get_auth(self) -> aiohttp.BasicAuth | None:
        if self.graph_database_username and self.graph_database_password:
            return aiohttp.BasicAuth(self.graph_database_username, self.graph_database_password)
        return None

    async def _ensure_database(self) -> None:
        """Make sure the target database exists before the first real call.

        ArcadeDB does not auto-create databases on connect, so the very
        first HTTP/Bolt call against a fresh server would otherwise fail
        with a 500 ``Database '<name>' is not available``.

        We first probe ``GET /api/v1/exists/<db>``, which any authenticated
        database user can call. Only if the database is missing do we
        attempt ``POST /api/v1/server create database <db>``, which requires
        server-level (root) credentials. This keeps the adapter usable
        for non-root users connecting to a pre-created database.
        """
        if self._database_ensured:
            return

        async with aiohttp.ClientSession(auth=self._get_auth()) as session:
            # Step 1: check if the database already exists
            exists_url = f"{self.http_base_url}/api/v1/exists/{self.database_name}"
            async with session.get(exists_url) as resp:
                exists_body = await resp.text()
                if resp.status == 200:
                    try:
                        if json.loads(exists_body).get("result") is True:
                            self._database_ensured = True
                            return
                    except (ValueError, AttributeError):
                        pass
                elif resp.status in (401, 403):
                    raise RuntimeError(
                        f"ArcadeDB authentication failed on "
                        f"/api/v1/exists ({resp.status}). Check the "
                        f"username and password supplied to the adapter."
                    )

            # Step 2: database missing — try to create it (needs root creds)
            create_url = f"{self.http_base_url}/api/v1/server"
            payload = {"command": f"create database {self.database_name}"}
            async with session.post(create_url, json=payload) as resp:
                body = await resp.text()
                if resp.status == 200:
                    logger.info("Created ArcadeDB database '%s'", self.database_name)
                    self._database_ensured = True
                elif "already exists" in body.lower():
                    self._database_ensured = True
                elif resp.status in (401, 403):
                    raise RuntimeError(
                        f"ArcadeDB database '{self.database_name}' does "
                        f"not exist and the supplied credentials lack the "
                        f"server-level privileges required to create it "
                        f"(HTTP {resp.status}). Either create the database "
                        f"manually or supply root credentials."
                    )
                else:
                    raise RuntimeError(f"ArcadeDB create database failed ({resp.status}): {body}")

    async def _http_request(
        self,
        endpoint: str,
        payload: dict,
    ) -> dict:
        """Execute a request via ArcadeDB's HTTP API.

        Retries up to 5 times on HTTP 503 (ConcurrentModificationException)
        because ArcadeDB uses optimistic concurrency control and concurrent
        writes to the same page can transiently conflict.  The database itself
        advises "Please retry the operation" in the error body.
        """
        import asyncio as _asyncio

        if not self._database_ensured:
            await self._ensure_database()
        url = f"{self.http_base_url}/api/v1/{endpoint}/{self.database_name}"

        max_retries = 5
        for attempt in range(max_retries):
            async with aiohttp.ClientSession(auth=self._get_auth()) as session:
                async with session.post(url, json=payload) as resp:
                    body = await resp.text()
                    if resp.status == 503 and attempt < max_retries - 1:
                        # ArcadeDB ConcurrentModificationException — back off and retry
                        await _asyncio.sleep(0.3 * (attempt + 1))
                        continue
                    if resp.status != 200:
                        raise RuntimeError(f"ArcadeDB HTTP error ({resp.status}): {body}")
                    return json.loads(body)
        # Should not reach here, but just in case
        raise RuntimeError(f"ArcadeDB HTTP error (503): {body}")

    async def _sql(self, sql: str) -> dict:
        """Execute an SQL command via HTTP."""
        return await self._http_request("command", {"language": "sql", "command": sql})

    async def _sql_query(self, sql: str) -> dict:
        """Execute an SQL read query via HTTP."""
        return await self._http_request("query", {"language": "sql", "command": sql})

    async def _cypher_via_http(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute Cypher via HTTP API. Returns flat dicts."""
        payload = {"language": "cypher", "command": cypher}
        if params:
            payload["params"] = params
        result = await self._http_request("command", payload)
        return result.get("result", [])

    async def _cypher_via_bolt(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute Cypher via Bolt protocol. Normalizes response to flat dicts."""
        from neo4j.exceptions import ServiceUnavailable

        try:
            async with self._bolt_driver.session(database=self.database_name) as session:
                result = await session.run(cypher, params)
                records = await result.data()

            # Normalize: Bolt returns {"alias": <Node/value>} per record.
            # Convert Node objects to plain dicts for consistency with HTTP.
            normalized = []
            for record in records:
                flat = {}
                for key, value in record.items():
                    if hasattr(value, "items"):
                        # Already a dict (properties)
                        flat.update(value)
                    elif hasattr(value, "_properties"):
                        # neo4j Node object
                        flat.update(dict(value._properties))
                    else:
                        flat[key] = value
                normalized.append(flat)
            return normalized

        except (ServiceUnavailable, OSError) as e:
            # Bolt not available, disable and fall back to HTTP
            logger.info("Bolt connection failed: %s, falling back to HTTP", e)
            self._bolt_driver = None
            self._bolt_verified = False
            return await self._cypher_via_http(cypher, params)

    async def _cypher(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute Cypher query using Bolt if available, otherwise HTTP.

        On the first call, verifies that Bolt is actually reachable.
        If Bolt fails, permanently falls back to HTTP for the session.
        """
        if self._bolt_driver is not None:
            if not self._bolt_verified:
                # First call: verify Bolt connectivity
                try:
                    result = await self._cypher_via_bolt("RETURN true AS ok")
                    if result and result[0].get("ok"):
                        self._bolt_verified = True
                        logger.info("Bolt protocol verified, using Bolt for Cypher")
                    else:
                        raise RuntimeError("Bolt verification returned no data")
                except Exception as e:
                    logger.info("Bolt verification failed: %s, using HTTP", e)
                    self._bolt_driver = None

            if self._bolt_driver is not None:
                return await self._cypher_via_bolt(cypher, params)

        return await self._cypher_via_http(cypher, params)

    async def _cypher_projection(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Execute graph projection queries via HTTP so aliased nested maps stay
        under their original keys.

        Bolt normalization intentionally flattens Node objects and dict-valued
        aliases for most adapter reads. That behavior is convenient for
        `RETURN node`, but it breaks projection queries like
        `properties(n) AS properties`, where Cognee expects a nested
        `record["properties"]` mapping.
        """

        return await self._cypher_via_http(cypher, params)

    # -------------------------------------------------------------------------
    # GraphDBInterface - Cypher operations
    # -------------------------------------------------------------------------

    async def query(
        self,
        query: str,
        params: dict[str, Any] | None = None,
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
        node_id = str(node.id)
        result = await self._upsert_cypher_node(node_id, serialized)

        await self._store_vertex_vectors(
            node_id=node_id,
            node_type=str(serialized.get("type") or type(node).__name__),
            embeddable_properties={
                prop_name: getattr(node, prop_name, None)
                for prop_name in DataPoint.get_embeddable_property_names(node)
            },
        )

        return result

    async def add_nodes(
        self,
        nodes: list[DataPoint],
        source_ref_key: str | None = None,
        pipeline_run_id: str | None = None,
    ) -> None:
        if not nodes:
            return []

        # ArcadeDB's HTTP Cypher parser rejects the batch form
        # `SET n += node.properties` used with `UNWIND $nodes AS node`.
        # Keep the write path compatible with both Bolt and HTTP fallback by
        # reusing the proven single-node upsert query.
        results = []
        for node in nodes:
            results.append(await self.add_node(node))

        return results

    async def extract_node(self, node_id: str):
        results = await self.extract_nodes([node_id])
        return results[0] if results else None

    async def extract_nodes(self, node_ids: list[str]):
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
            "RETURN DISTINCT edge.from_node AS from_node, "
            "edge.to_node AS to_node, "
            "edge.relationship_name AS relationship_name",
            params,
        )
        return [
            (
                r["from_node"],
                r["to_node"],
                r["relationship_name"],
            )
            for r in results
        ]

    async def add_edge(
        self,
        from_node: UUID,
        to_node: UUID,
        relationship_name: str,
        edge_properties: dict[str, Any] | None = None,
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
        self,
        edges: list[tuple[str, str, str, dict[str, Any]]],
        source_ref_key: str | None = None,
        pipeline_run_id: str | None = None,
    ) -> None:
        if not edges:
            return []

        results = []
        for src, dst, rel_type, properties in edges:
            edge_properties = {
                **(properties or {}),
                "source_node_id": str(src),
                "target_node_id": str(dst),
            }
            results.append(await self.add_edge(src, dst, rel_type, edge_properties))

        return results

    async def get_edges(self, node_id: str):
        results = await self._cypher(
            "MATCH (n {id: $node_id})-[r]-(m) "
            "RETURN n.id AS source_id, m.id AS target_id, type(r) AS rel_type",
            {"node_id": node_id},
        )
        return [
            (r["source_id"], r["target_id"], {"relationship_name": r["rel_type"]}) for r in results
        ]

    async def get_disconnected_nodes(self) -> list[str]:
        results = await self._cypher("MATCH (n) WHERE NOT (n)--() RETURN collect(n.id) AS ids")
        return results[0]["ids"] if results else []

    async def get_predecessors(self, node_id: str, edge_label: str | None = None) -> list[str]:
        if edge_label is not None:
            results = await self._cypher(
                "MATCH (node)<-[r]-(predecessor) "
                "WHERE node.id = $node_id AND type(r) = $edge_label "
                "RETURN predecessor",
                {"node_id": node_id, "edge_label": edge_label},
            )
        else:
            results = await self._cypher(
                "MATCH (node)<-[r]-(predecessor) WHERE node.id = $node_id RETURN predecessor",
                {"node_id": node_id},
            )
        return results

    async def get_successors(self, node_id: str, edge_label: str | None = None) -> list[str]:
        if edge_label is not None:
            results = await self._cypher(
                "MATCH (node)-[r]->(successor) "
                "WHERE node.id = $node_id AND type(r) = $edge_label "
                "RETURN successor",
                {"node_id": node_id, "edge_label": edge_label},
            )
        else:
            results = await self._cypher(
                "MATCH (node)-[r]->(successor) WHERE node.id = $node_id RETURN successor",
                {"node_id": node_id},
            )
        return results

    async def get_neighbors(self, node_id: str) -> list[dict[str, Any]]:
        predecessors, successors = await asyncio.gather(
            self.get_predecessors(node_id), self.get_successors(node_id)
        )
        return predecessors + successors

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        results = await self._cypher(
            "MATCH (node {id: $node_id}) RETURN node",
            {"node_id": node_id},
        )
        return results[0] if results else None

    async def get_nodes(self, node_ids: list[str]) -> list[dict[str, Any]]:
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
            connections.append((r["src"], {"relationship_name": r["rel"]}, r["dst"]))
        for r in successors:
            connections.append((r["src"], {"relationship_name": r["rel"]}, r["dst"]))
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
        result = await self._cypher_projection(
            "MATCH (n) RETURN n.id AS id, labels(n) AS labels, properties(n) AS properties"
        )
        nodes = [(r["id"], r["properties"]) for r in result]

        result = await self._cypher_projection(
            "MATCH (n)-[r]->(m) RETURN n.id AS source, m.id AS target, "
            "TYPE(r) AS type, properties(r) AS properties"
        )
        edges = [(r["source"], r["target"], r["type"], r["properties"]) for r in result]
        return (nodes, edges)

    async def get_triplets_batch(self, offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """
        Return graph triplets in the structure expected by cognee memify.

        We intentionally use the HTTP Cypher path here because the Bolt result
        normalizer flattens nested maps, while memify needs distinct nested
        dictionaries for start_node, relationship_properties and end_node.
        """

        safe_offset = max(int(offset), 0)
        safe_limit = max(int(limit), 0)

        results = await self._cypher_via_http(
            dedent(
                f"""\
                MATCH (start_node)-[relationship]->(end_node)
                RETURN properties(start_node) AS start_node,
                       properties(relationship) AS relationship_properties,
                       type(relationship) AS relationship_name,
                       properties(end_node) AS end_node
                SKIP {safe_offset}
                LIMIT {safe_limit}
                """
            )
        )

        triplets: list[dict[str, Any]] = []
        for row in results:
            start_node = row.get("start_node") or {}
            end_node = row.get("end_node") or {}
            relationship_properties = row.get("relationship_properties") or {}

            if not isinstance(start_node, dict):
                start_node = dict(start_node)
            if not isinstance(end_node, dict):
                end_node = dict(end_node)
            if not isinstance(relationship_properties, dict):
                relationship_properties = dict(relationship_properties)

            relationship_name = row.get("relationship_name")
            if relationship_name:
                relationship_properties = {
                    **relationship_properties,
                    "relationship_name": relationship_name,
                }

            triplets.append(
                {
                    "start_node": start_node,
                    "relationship_properties": relationship_properties,
                    "end_node": end_node,
                }
            )

        return triplets

    async def get_nodeset_subgraph(
        self,
        node_type: type[Any],
        node_name: list[str],
        node_name_filter_operator: str = "OR",
    ) -> tuple[list[tuple[int, dict]], list[tuple[int, int, str, dict]]]:
        label = node_type.__name__

        primary_result = await self._cypher_projection(
            f"UNWIND $names AS wantedName MATCH (n:{label}) "
            "WHERE n.name = wantedName "
            "RETURN DISTINCT n.id AS id, properties(n) AS properties",
            {"names": node_name},
        )
        if not primary_result:
            return [], []

        primary_ids = [r["id"] for r in primary_result]

        if node_name_filter_operator == "OR":
            neighbor_result = await self._cypher_projection(
                "MATCH (n)-[]-(nbr) WHERE n.id IN $ids "
                "RETURN DISTINCT nbr.id AS id, properties(nbr) AS properties",
                {"ids": primary_ids},
            )
        else:
            neighbor_result = await self._cypher_projection(
                "MATCH (n)-[]-(nbr) WHERE n.id IN $ids "
                "WITH nbr, COUNT(DISTINCT n.id) AS matched_count "
                "WHERE matched_count = $primary_count "
                "RETURN nbr.id AS id, properties(nbr) AS properties",
                {"ids": primary_ids, "primary_count": len(primary_ids)},
            )

        neighbor_ids = [r["id"] for r in neighbor_result] if neighbor_result else []
        all_ids = list(set(primary_ids + neighbor_ids))

        nodes_result = await self._cypher_projection(
            "MATCH (n) WHERE n.id IN $ids RETURN n.id AS id, properties(n) AS properties",
            {"ids": all_ids},
        )
        nodes = [(r["id"], r["properties"]) for r in nodes_result]

        edges_result = await self._cypher_projection(
            "MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids "
            "RETURN a.id AS source, b.id AS target, "
            "type(r) AS type, properties(r) AS properties",
            {"ids": all_ids},
        )
        edges = [(r["source"], r["target"], r["type"], r["properties"]) for r in edges_result]
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

        result_nodes = await self._cypher_projection(
            f"MATCH (n) WHERE {node_where} RETURN n.id AS id, properties(n) AS properties",
            params,
        )
        nodes = [(r["id"], r["properties"]) for r in result_nodes]

        result_edges = await self._cypher_projection(
            f"MATCH (n)-[r]->(m) WHERE {edge_where} "
            "RETURN n.id AS source, m.id AS target, "
            "TYPE(r) AS type, properties(r) AS properties",
            params,
        )
        edges = [(r["source"], r["target"], r["type"], r["properties"]) for r in result_edges]
        return (nodes, edges)

    async def get_graph_metrics(self, include_optional=False):
        try:
            node_count = await self._cypher("MATCH (n) RETURN count(n) AS cnt")
            edge_count = await self._cypher("MATCH ()-[r]->() RETURN count(r) AS cnt")
            num_nodes = node_count[0]["cnt"] if node_count else 0
            num_edges = edge_count[0]["cnt"] if edge_count else 0

            metrics = {
                "num_nodes": num_nodes,
                "num_edges": num_edges,
                "mean_degree": ((2 * num_edges) / num_nodes if num_nodes > 0 else 0),
                "edge_density": (num_edges / (num_nodes * (num_nodes - 1)) if num_nodes > 1 else 0),
                "num_connected_components": 1 if num_nodes > 0 else 0,
                "sizes_of_connected_components": ([num_nodes] if num_nodes > 0 else []),
            }

            if include_optional:
                self_loops = await self._cypher("MATCH (n)-[r]->(n) RETURN COUNT(r) AS cnt")
                metrics.update(
                    {
                        "num_selfloops": (self_loops[0]["cnt"] if self_loops else 0),
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

    async def get_neighborhood(
        self,
        node_ids: list[str],
        depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, str, str, dict[str, Any]]]]:
        """
        Get the k-hop neighborhood subgraph around a set of seed nodes.

        Returns all nodes and edges within `depth` hops of any seed node,
        in the same format as get_graph_data().
        Optional edge_type filtering to constrain traversal paths.

        Uses _cypher_projection (HTTP path) to preserve nested property maps.
        """
        if not node_ids:
            return [], []

        # Step 1: Collect all node IDs within depth hops
        if edge_types:
            path_query = dedent(f"""\
                MATCH path = (seed)-[*1..{depth}]-(neighbor)
                WHERE seed.id IN $node_ids
                  AND ALL(r IN relationships(path) WHERE TYPE(r) IN $edge_types)
                RETURN DISTINCT neighbor.id AS nid
            """)
        else:
            path_query = dedent(f"""\
                MATCH (seed)-[*1..{depth}]-(neighbor)
                WHERE seed.id IN $node_ids
                RETURN DISTINCT neighbor.id AS nid
            """)

        params: dict[str, Any] = {"node_ids": node_ids}
        if edge_types:
            params["edge_types"] = edge_types

        result = await self._cypher_projection(path_query, params)
        neighbor_ids = [record["nid"] for record in result if record.get("nid")]

        all_ids = list(set(node_ids) | set(neighbor_ids))

        # Step 2: Fetch all nodes
        nodes_result = await self._cypher_projection(
            "MATCH (n) WHERE n.id IN $ids RETURN n.id AS id, properties(n) AS properties",
            {"ids": all_ids},
        )
        nodes = []
        for record in nodes_result:
            props = record.get("properties", {})
            node_id = record.get("id", "")
            if isinstance(props, dict):
                nodes.append((node_id, props))
            else:
                nodes.append((node_id, dict(props)))

        # Step 3: Fetch all edges between collected nodes
        edges_result = await self._cypher_projection(
            "MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids "
            "RETURN a.id AS source, b.id AS target, "
            "TYPE(r) AS type, properties(r) AS properties",
            {"ids": all_ids},
        )
        edges = []
        for record in edges_result:
            props = record.get("properties", {})
            if not isinstance(props, dict):
                props = dict(props)
            edges.append(
                (
                    record["source"],
                    record["target"],
                    record["type"],
                    props,
                )
            )

        return (nodes, edges)

    # -------------------------------------------------------------------------
    # VectorDBInterface - SQL operations (always HTTP)
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
        type_name = collection_name.split("_")[0] if "_" in collection_name else collection_name
        try:
            await self._sql(f"SELECT count(*) AS cnt FROM `{type_name}` LIMIT 0")
            return True
        except RuntimeError:
            return False

    async def create_collection(self, collection_name: str, payload_schema: Any | None = None):
        """Create a vertex type in ArcadeDB if it does not exist."""
        type_name = collection_name.split("_")[0] if "_" in collection_name else collection_name
        try:
            await self._sql(f"CREATE VERTEX TYPE `{type_name}` IF NOT EXISTS")
        except RuntimeError:
            pass

    async def create_vector_index(self, index_name: str, index_property_name: str) -> None:
        """Create an LSM_VECTOR (HNSW) index on the shared Vertex storage."""
        vector_prop = f"{index_property_name}_vector"
        storage_type = await self._vector_storage_type()
        index_key = f"{storage_type}.{vector_prop}"

        if index_key in self._vector_indexes:
            return

        # Keep the logical collection for compatibility with collection checks,
        # but store vector properties on the physical Vertex type because
        # Cypher MERGE writes nodes there and tracks the model class in `type`.
        await self.create_collection(index_name)

        vector_size = self.embedding_engine.get_vector_size()

        try:
            await self._sql(
                f"CREATE PROPERTY `{storage_type}`.`{vector_prop}` IF NOT EXISTS ARRAY_OF_FLOATS"
            )
        except RuntimeError:
            pass

        try:
            await self._sql(
                f"CREATE INDEX ON `{storage_type}` (`{vector_prop}`) "
                f"LSM_VECTOR METADATA {{ dimensions: {vector_size}, "
                f"similarity: 'COSINE' }}"
            )
            self._vector_indexes.add(index_key)
        except RuntimeError as e:
            err_msg = str(e).lower()
            if (
                "already exists" in err_msg
                or "duplicate" in err_msg
                or "existent index" in err_msg
                or "defined on the properties" in err_msg
            ):
                self._vector_indexes.add(index_key)
            else:
                raise

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]):
        """Embed and store data points as vertices with vector properties."""
        if not data_points:
            return

        # Collect all embeddable values for batch embedding
        embeddable_values: list[str] = []
        vector_map: dict[str, dict[str, int | None]] = {}
        created_types: set[str] = set()

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
            dp_key = str(dp.id)

            if node_label not in created_types:
                await self.create_collection(node_label)
                created_types.add(node_label)

            properties = self.serialize_properties(dp.model_dump())
            await self._upsert_cypher_node(dp_key, properties)

            embeddable_properties = {}
            for prop_name in DataPoint.get_embeddable_property_names(dp):
                vec_idx = vector_map[dp_key].get(prop_name)
                if vec_idx is not None and vec_idx < len(vectorized_values):
                    embeddable_properties[prop_name] = vectorized_values[vec_idx]

            if embeddable_properties:
                escaped_node_label = self._escape_sql_literal(node_label)
                escaped_dp_key = self._escape_sql_literal(dp_key)
                for prop_name, vector in embeddable_properties.items():
                    await self.create_vector_index(node_label, prop_name)
                    vector_prop = f"{prop_name}_vector"
                    vec_str = ",".join(str(f) for f in vector)
                    await self._sql(
                        f"UPDATE `{await self._vector_storage_type()}` "
                        f"SET `{vector_prop}` = [{vec_str}] "
                        f"WHERE id = '{escaped_dp_key}' "
                        f"AND type = '{escaped_node_label}'"
                    )

    async def index_data_points(
        self,
        index_name: str,
        index_property_name: str,
        data_points: list[DataPoint],
    ) -> None:
        """Embed data points and store vectors, delegating to create_data_points.

        Follows the same pattern as LanceDB, ChromaDB, and PGVector adapters:
        create the index structure AND embed + persist vectors in one call.
        """
        await self.create_data_points(
            f"{index_name}_{index_property_name}",
            data_points,
        )

    async def retrieve(self, collection_name: str, data_point_ids: list[str]):
        """Retrieve data points by their IDs."""
        return await self._cypher(
            "MATCH (node) WHERE node.id IN $node_ids RETURN node",
            {"node_ids": [str(dp_id) for dp_id in data_point_ids]},
        )

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
        """Perform vector similarity search using ArcadeDB's vectorNeighbors()."""
        if query_text is None and query_vector is None:
            raise ValueError("Either query_text or query_vector must be provided")

        if query_text and not query_vector:
            query_vector = (await self.embed_data([query_text]))[0]

        logical_type, attr_name = self._split_collection_name(collection_name)

        vector_prop = f"{attr_name}_vector"
        storage_type = await self._vector_storage_type()

        if limit is None:
            limit = 10
        if limit == 0:
            return []

        vec_str = ",".join(str(f) for f in query_vector)
        candidate_limit = max(limit * 10, limit)
        escaped_logical_type = self._escape_sql_literal(logical_type)

        try:
            result = await self._sql_query(
                f"SELECT *, distance FROM ("
                f"  SELECT expand(vectorNeighbors("
                f"    '{storage_type}[{vector_prop}]', "
                f"[{vec_str}], {candidate_limit}"
                f"  ))"
                f") WHERE type = '{escaped_logical_type}' "
                f"LIMIT {limit}"
            )
        except RuntimeError as e:
            if "not found" in str(e).lower() or "index" in str(e).lower():
                raise CollectionNotFoundError(
                    f"No vector index found for collection {collection_name}"
                ) from e
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
            distance = record.get("distance", 0.0)
            record_id = record.get("id", "")

            payload_data = {}
            if include_payload:
                payload_data = {
                    k: v
                    for k, v in record.items()
                    if k not in ("@rid", "@cat", "@type", "distance") and not k.endswith("_vector")
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
        limit: int | None = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: list[str] | None = None,
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

    async def delete_data_points(self, collection_name: str, data_point_ids: list[UUID]):
        """Delete data points by their IDs."""
        return await self._cypher(
            "MATCH (node) WHERE node.id IN $node_ids DETACH DELETE node",
            {"node_ids": [str(dp_id) for dp_id in data_point_ids]},
        )

    async def prune(self):
        """Remove all data from the database."""
        await self.delete_graph()


class ArcadeDBVectorAdapter(ArcadeDBAdapter):
    """
    Vector-provider shim for Cognee.

    Cognee's vector factory instantiates custom providers with the vector-style
    signature (`url`, `api_key`, `embedding_engine`, `database_name`) and does
    not pass graph connection fields. For ArcadeDB we still need the graph
    connection details because the hybrid adapter shares HTTP auth and database
    naming between graph and vector operations.

    Compatible with Cognee >= 1.0.0 (includes get_neighborhood support).
    """

    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        embedding_engine: EmbeddingEngine | None = None,
        database_name: str | None = "cognee",
        **kwargs,
    ):
        graph_config = get_graph_context_config()

        if isinstance(graph_config, dict):
            graph_database_url = graph_config.get("graph_database_url") or url
            graph_database_username = graph_config.get("graph_database_username")
            graph_database_password = graph_config.get("graph_database_password")
            graph_database_name = graph_config.get("graph_database_name")
        else:
            graph_database_url = getattr(graph_config, "graph_database_url", None) or url
            graph_database_username = getattr(graph_config, "graph_database_username", None)
            graph_database_password = getattr(graph_config, "graph_database_password", None)
            graph_database_name = getattr(graph_config, "graph_database_name", None)

        super().__init__(
            graph_database_url=graph_database_url,
            graph_database_username=graph_database_username,
            graph_database_password=graph_database_password,
            embedding_engine=embedding_engine,
            url=url,
            api_key=api_key,
            database_name=database_name or graph_database_name or "cognee",
            **kwargs,
        )
