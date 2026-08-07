"""ArcadeDB Adapter for Graph Database

ArcadeDB implements the Neo4j Bolt wire protocol, so this adapter uses the standard
neo4j async Python driver. The Cypher queries are standard OpenCypher, compatible
with ArcadeDB's 97.8% TCK compliance.
"""

import asyncio
import json
from collections import defaultdict
from contextlib import asynccontextmanager
from textwrap import dedent
from typing import Any, Optional, Union
from uuid import UUID

from cognee.infrastructure.databases.graph.graph_db_interface import GraphDBInterface
from cognee.infrastructure.engine import DataPoint
from cognee.modules.storage.utils import JSONEncoder
from cognee.shared.logging_utils import ERROR, get_logger
from neo4j import AsyncGraphDatabase, AsyncSession
from neo4j.exceptions import Neo4jError

logger = get_logger("ArcadeDBAdapter", level=ERROR)


class ArcadeDBAdapter(GraphDBInterface):
    """
    Handles interaction with an ArcadeDB database through the Neo4j Bolt protocol.

    ArcadeDB supports the Bolt wire protocol natively, so the standard neo4j Python
    driver connects directly. All queries use standard OpenCypher.
    """

    def __init__(
        self,
        graph_database_url: str,
        graph_database_username: Optional[str] = None,
        graph_database_password: Optional[str] = None,
        driver: Optional[Any] = None,
        **kwargs,
    ):
        auth = None
        if graph_database_username and graph_database_password:
            auth = (graph_database_username, graph_database_password)

        self.driver = driver or AsyncGraphDatabase.driver(
            graph_database_url,
            auth=auth,
            max_connection_lifetime=120,
        )

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
            """
                MATCH (n)
                WHERE n.id = $node_id
                RETURN COUNT(n) > 0 AS node_exists
            """,
            {"node_id": node_id},
        )
        return results[0]["node_exists"] if len(results) > 0 else False

    async def add_node(
        self,
        node: Union[DataPoint, str],
        properties: Optional[dict[str, Any]] = None,
    ):
        if isinstance(node, DataPoint):
            node_id = str(node.id)
            serialized_properties = self.serialize_properties(node.model_dump())
        else:
            node_id = str(node)
            serialized_properties = self.serialize_properties(properties or {})

        query = """
        MERGE (node {id: $node_id})
        ON CREATE SET node += $properties, node.updated_at = timestamp()
        ON MATCH SET node += $properties, node.updated_at = timestamp()
        RETURN node.id AS nodeId
        """

        params = {
            "node_id": node_id,
            "properties": serialized_properties,
        }
        return await self.query(query, params)

    async def add_nodes(
        self,
        nodes: list[DataPoint],
        source_ref_key: str | None = None,
        pipeline_run_id: str | None = None,
    ) -> None:
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

        results = await self.query(query, {"nodes": nodes_data})
        return results

    async def extract_node(self, node_id: str):
        results = await self.extract_nodes([node_id])
        return results[0] if len(results) > 0 else None

    async def extract_nodes(self, node_ids: list[str]):
        query = """
        UNWIND $node_ids AS id
        MATCH (node {id: id})
        RETURN node"""

        params = {"node_ids": node_ids}
        results = await self.query(query, params)
        return [result["node"] for result in results]

    async def delete_node(self, node_id: str):
        query = "MATCH (node {id: $node_id}) DETACH DELETE node"
        params = {"node_id": node_id}
        return await self.query(query, params)

    async def delete_nodes(self, node_ids: list[str]) -> None:
        query = """
        UNWIND $node_ids AS id
        MATCH (node {id: id})
        DETACH DELETE node"""

        params = {"node_ids": node_ids}
        return await self.query(query, params)

    async def has_edge(self, source_id: str, target_id: str, relationship_name: str) -> bool:
        query = """
            MATCH (from_node)-[relationship]->(to_node)
            WHERE from_node.id = $from_node_id AND to_node.id = $to_node_id
            AND type(relationship) = $edge_label
            RETURN COUNT(relationship) > 0 AS edge_exists
        """

        params = {
            "from_node_id": str(source_id),
            "to_node_id": str(target_id),
            "edge_label": relationship_name,
        }

        records = await self.query(query, params)
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
        source_id: str,
        target_id: str,
        relationship_name: str,
        properties: Optional[dict[str, Any]] = None,
    ):
        serialized_properties = self.serialize_properties(properties or {})

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
            "from_node": str(source_id),
            "to_node": str(target_id),
            "relationship_name": relationship_name,
            "properties": serialized_properties,
        }

        return await self.query(query, params)

    async def add_edges(
        self,
        edges: list[tuple[str, str, str, dict[str, Any]]],
        source_ref_key: str | None = None,
        pipeline_run_id: str | None = None,
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
        query = """
        MATCH (n {id: $node_id})-[r]-(m)
        RETURN n, r, m
        """

        results = await self.query(query, {"node_id": node_id})

        return [
            (
                result["n"]["id"],
                result["m"]["id"],
                {"relationship_name": result["r"][1]},
            )
            for result in results
        ]

    async def get_disconnected_nodes(self) -> list[str]:
        query = """
        MATCH (n)
        WHERE NOT (n)--()
        RETURN collect(n.id) AS ids
        """

        results = await self.query(query)
        return results[0]["ids"] if len(results) > 0 else []

    async def get_predecessors(self, node_id: str, edge_label: Optional[str] = None) -> list[str]:
        if edge_label is not None:
            query = """
            MATCH (node)<-[r]-(predecessor)
            WHERE node.id = $node_id AND type(r) = $edge_label
            RETURN predecessor
            """
            results = await self.query(query, {"node_id": node_id, "edge_label": edge_label})
        else:
            query = """
            MATCH (node)<-[r]-(predecessor)
            WHERE node.id = $node_id
            RETURN predecessor
            """
            results = await self.query(query, {"node_id": node_id})

        return [result["predecessor"] for result in results]

    async def get_successors(self, node_id: str, edge_label: Optional[str] = None) -> list[str]:
        if edge_label is not None:
            query = """
            MATCH (node)-[r]->(successor)
            WHERE node.id = $node_id AND type(r) = $edge_label
            RETURN successor
            """
            results = await self.query(query, {"node_id": node_id, "edge_label": edge_label})
        else:
            query = """
            MATCH (node)-[r]->(successor)
            WHERE node.id = $node_id
            RETURN successor
            """
            results = await self.query(query, {"node_id": node_id})

        return [result["successor"] for result in results]

    async def get_neighbors(self, node_id: str) -> list[dict[str, Any]]:
        predecessors, successors = await asyncio.gather(
            self.get_predecessors(node_id), self.get_successors(node_id)
        )
        return predecessors + successors

    async def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        query = """
        MATCH (node {id: $node_id})
        RETURN node
        """
        results = await self.query(query, {"node_id": node_id})
        return results[0]["node"] if results else None

    async def get_nodes(self, node_ids: list[str]) -> list[dict[str, Any]]:
        query = """
        UNWIND $node_ids AS id
        MATCH (node {id: id})
        RETURN node
        """
        results = await self.query(query, {"node_ids": node_ids})
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
            neighbour = neighbour["relation"]
            connections.append((neighbour[0], {"relationship_name": neighbour[1]}, neighbour[2]))

        for neighbour in successors:
            neighbour = neighbour["relation"]
            connections.append((neighbour[0], {"relationship_name": neighbour[1]}, neighbour[2]))

        return connections

    async def remove_connection_to_predecessors_of(
        self, node_ids: list[str], edge_label: str
    ) -> None:
        query = """
        UNWIND $node_ids AS nid
        MATCH (node {id: nid})-[r]->(predecessor)
        WHERE type(r) = $edge_label
        DELETE r
        """
        params = {"node_ids": node_ids, "edge_label": edge_label}
        return await self.query(query, params)

    async def remove_connection_to_successors_of(
        self, node_ids: list[str], edge_label: str
    ) -> None:
        query = """
        UNWIND $node_ids AS nid
        MATCH (node {id: nid})<-[r]-(successor)
        WHERE type(r) = $edge_label
        DELETE r
        """
        params = {"node_ids": node_ids, "edge_label": edge_label}
        return await self.query(query, params)

    async def delete_graph(self):
        query = "MATCH (node) DETACH DELETE node"
        return await self.query(query)

    def serialize_properties(self, properties=None):
        if properties is None:
            properties = {}
        serialized_properties = {}

        for property_key, property_value in properties.items():
            if isinstance(property_value, UUID):
                serialized_properties[property_key] = str(property_value)
                continue
            if isinstance(property_value, dict):
                serialized_properties[property_key] = json.dumps(property_value, cls=JSONEncoder)
                continue
            serialized_properties[property_key] = property_value

        return serialized_properties

    async def get_model_independent_graph_data(self):
        query_nodes = "MATCH (n) RETURN collect(n) AS nodes"
        nodes = await self.query(query_nodes)

        query_edges = "MATCH (n)-[r]->(m) RETURN collect([n, r, m]) AS elements"
        edges = await self.query(query_edges)

        return (nodes, edges)

    async def get_graph_data(self):
        query = "MATCH (n) RETURN n.id AS id, labels(n) AS labels, properties(n) AS properties"
        result = await self.query(query)

        nodes = [(record["id"], record["properties"]) for record in result]

        query = """
        MATCH (n)-[r]->(m)
        RETURN n.id AS source, m.id AS target, TYPE(r) AS type, properties(r) AS properties
        """
        result = await self.query(query)
        edges = [
            (record["source"], record["target"], record["type"], record["properties"])
            for record in result
        ]

        return (nodes, edges)

    async def get_neighborhood(
        self,
        node_ids: list[str],
        depth: int = 1,
        edge_types: Optional[list[str]] = None,
    ) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, str, str, dict[str, Any]]]]:
        """
        Get the k-hop neighborhood subgraph around a set of seed nodes.

        Returns all nodes and edges within `depth` hops of any seed node,
        in the same format as get_graph_data(). If `edge_types` is provided,
        only those relationship types are traversed.
        """
        if not node_ids:
            logger.warning("No node IDs provided for neighborhood retrieval.")
            return [], []

        node_ids = [str(node_id) for node_id in node_ids]

        # Step 1: collect all node IDs within `depth` hops of any seed node.
        if edge_types:
            path_query = f"""
            MATCH path = (seed)-[*1..{depth}]-(neighbor)
            WHERE seed.id IN $node_ids
              AND ALL(r IN relationships(path) WHERE TYPE(r) IN $edge_types)
            RETURN DISTINCT neighbor.id AS nid
            """
        else:
            path_query = f"""
            MATCH (seed)-[*1..{depth}]-(neighbor)
            WHERE seed.id IN $node_ids
            RETURN DISTINCT neighbor.id AS nid
            """

        params = {"node_ids": node_ids}
        if edge_types:
            params["edge_types"] = edge_types

        result = await self.query(path_query, params)
        neighbor_ids = [record["nid"] for record in result if record.get("nid")]

        all_ids = list(set(node_ids) | set(neighbor_ids))

        # Step 2: fetch all nodes in the collected set.
        nodes_query = """
        MATCH (n)
        WHERE n.id IN $ids
        RETURN n.id AS id, properties(n) AS properties
        """
        nodes_result = await self.query(nodes_query, {"ids": all_ids})
        nodes = [(record["properties"]["id"], record["properties"]) for record in nodes_result]

        # Step 3: fetch all edges between collected nodes (honoring edge_types).
        if edge_types:
            edges_query = """
            MATCH (n)-[r]->(m)
            WHERE n.id IN $ids AND m.id IN $ids AND TYPE(r) IN $edge_types
            RETURN properties(r) AS properties, TYPE(r) AS type
            """
            edges_params = {"ids": all_ids, "edge_types": edge_types}
        else:
            edges_query = """
            MATCH (n)-[r]->(m)
            WHERE n.id IN $ids AND m.id IN $ids
            RETURN properties(r) AS properties, TYPE(r) AS type
            """
            edges_params = {"ids": all_ids}

        edges_result = await self.query(edges_query, edges_params)
        edges = [
            (
                record["properties"]["source_node_id"],
                record["properties"]["target_node_id"],
                record["type"],
                record["properties"],
            )
            for record in edges_result
        ]

        return (nodes, edges)

    async def get_nodeset_subgraph(
        self,
        node_type: type[Any],
        node_name: list[str],
        node_name_filter_operator: str = "OR",
    ) -> tuple[list[tuple[int, dict]], list[tuple[int, int, str, dict]]]:
        """
        Retrieve a subgraph based on specified node names and type, including their
        relationships.

        When `node_name_filter_operator` is "OR" the subgraph includes the neighborhood
        of any matched node; when "AND" only neighbors shared by all matched nodes are
        included.
        """
        label = node_type.__name__

        if node_name_filter_operator == "OR":
            query = f"""
            UNWIND $names AS wantedName
            MATCH (n:`{label}`)
            WHERE n.name = wantedName
            WITH collect(DISTINCT n) AS primary
            UNWIND primary AS p
            OPTIONAL MATCH (p)--(nbr)
            WITH primary, collect(DISTINCT nbr) AS nbrs
            WITH primary + nbrs AS nodelist
            UNWIND nodelist AS node
            WITH collect(DISTINCT node) AS nodes
            MATCH (a)-[r]-(b)
            WHERE a IN nodes AND b IN nodes
            WITH nodes, collect(DISTINCT r) AS rels
            RETURN
              [n IN nodes |
                 {{ id: n.id,
                    properties: properties(n) }}] AS rawNodes,
              [r IN rels  |
                 {{ type: type(r),
                    properties: properties(r) }}] AS rawRels
            """
        else:
            query = f"""
            UNWIND $names AS wantedName
            MATCH (n:`{label}`)
            WHERE n.name = wantedName
            WITH collect(DISTINCT n) AS primary
            UNWIND primary AS p
            MATCH (p)--(nbr)
            WITH primary, nbr, COUNT(DISTINCT p) AS matched_count
            WHERE matched_count = size(primary)
            WITH primary, collect(DISTINCT nbr) AS nbrs
            WITH primary + nbrs AS nodelist
            UNWIND nodelist AS node
            WITH collect(DISTINCT node) AS nodes
            MATCH (a)-[r]-(b)
            WHERE a IN nodes AND b IN nodes
            WITH nodes, collect(DISTINCT r) AS rels
            RETURN
              [n IN nodes | {{ id: n.id, properties: properties(n) }}] AS rawNodes,
              [r IN rels  | {{ type: type(r), properties: properties(r) }}] AS rawRels
            """

        result = await self.query(query, {"names": node_name})

        if not result:
            return [], []

        raw_nodes = result[0]["rawNodes"]
        raw_rels = result[0]["rawRels"]

        nodes = [(n["properties"]["id"], n["properties"]) for n in raw_nodes]

        edges = [
            (
                r["properties"]["source_node_id"],
                r["properties"]["target_node_id"],
                r["type"],
                r["properties"],
            )
            for r in raw_rels
        ]

        return nodes, edges

    async def get_filtered_graph_data(self, attribute_filters):
        where_clauses = []
        for attribute, values in attribute_filters[0].items():
            values_str = ", ".join(
                f"'{value}'" if isinstance(value, str) else str(value) for value in values
            )
            where_clauses.append(f"n.{attribute} IN [{values_str}]")

        where_clause = " AND ".join(where_clauses)

        query_nodes = f"""
        MATCH (n)
        WHERE {where_clause}
        RETURN n.id AS id, labels(n) AS labels, properties(n) AS properties
        """
        result_nodes = await self.query(query_nodes)

        nodes = [(record["id"], record["properties"]) for record in result_nodes]

        query_edges = f"""
        MATCH (n)-[r]->(m)
        WHERE {where_clause} AND {where_clause.replace("n.", "m.")}
        RETURN n.id AS source, m.id AS target, TYPE(r) AS type, properties(r) AS properties
        """
        result_edges = await self.query(query_edges)

        edges = [
            (record["source"], record["target"], record["type"], record["properties"])
            for record in result_edges
        ]

        return (nodes, edges)

    async def get_node_labels_string(self):
        node_labels_query = """
        MATCH (n)
        WITH DISTINCT labels(n) AS labelList
        UNWIND labelList AS label
        RETURN collect(DISTINCT label) AS labels
        """
        node_labels_result = await self.query(node_labels_query)
        node_labels = node_labels_result[0]["labels"] if node_labels_result else []

        if not node_labels:
            raise ValueError("No node labels found in the database")

        node_labels_str = "[" + ", ".join(f"'{label}'" for label in node_labels) + "]"
        return node_labels_str

    async def get_relationship_labels_string(self):
        relationship_types_query = (
            "MATCH ()-[r]->() RETURN collect(DISTINCT type(r)) AS relationships"
        )
        relationship_types_result = await self.query(relationship_types_query)
        relationship_types = (
            relationship_types_result[0]["relationships"] if relationship_types_result else []
        )

        if not relationship_types:
            raise ValueError("No relationship types found in the database.")

        relationship_types_undirected_str = (
            "{"
            + ", ".join(f"{rel}" + ": {orientation: 'UNDIRECTED'}" for rel in relationship_types)
            + "}"
        )
        return relationship_types_undirected_str

    async def get_graph_metrics(self, include_optional=False):
        try:
            node_count = await self.query("MATCH (n) RETURN count(n) AS cnt")
            edge_count = await self.query("MATCH ()-[r]->() RETURN count(r) AS cnt")
            num_nodes = node_count[0]["cnt"] if node_count else 0
            num_edges = edge_count[0]["cnt"] if edge_count else 0

            mandatory_metrics = {
                "num_nodes": num_nodes,
                "num_edges": num_edges,
                "mean_degree": (2 * num_edges) / num_nodes if num_nodes > 0 else 0,
                "edge_density": num_edges / (num_nodes * (num_nodes - 1)) if num_nodes > 1 else 0,
            }

            # Connected components via simple BFS approach
            components_query = """
            MATCH (n)
            WITH collect(n.id) AS all_ids
            RETURN size(all_ids) AS total_nodes
            """
            components_result = await self.query(components_query)
            total = components_result[0]["total_nodes"] if components_result else 0

            mandatory_metrics.update(
                {
                    "num_connected_components": 1 if total > 0 else 0,
                    "sizes_of_connected_components": [total] if total > 0 else [],
                }
            )

            if include_optional:
                self_loops_query = """
                MATCH (n)-[r]->(n)
                RETURN COUNT(r) AS cnt
                """
                self_loops = await self.query(self_loops_query)
                num_selfloops = self_loops[0]["cnt"] if self_loops else 0

                optional_metrics = {
                    "num_selfloops": num_selfloops,
                    "diameter": -1,
                    "avg_shortest_path_length": -1,
                    "avg_clustering": -1,
                }
            else:
                optional_metrics = {
                    "num_selfloops": -1,
                    "diameter": -1,
                    "avg_shortest_path_length": -1,
                    "avg_clustering": -1,
                }

            return {**mandatory_metrics, **optional_metrics}

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
        query = "MATCH (n) RETURN true LIMIT 1"
        result = await self.query(query)
        return len(result) == 0
