"""Memgraph Adapter for Graph Database"""

import asyncio
import json
from contextlib import asynccontextmanager
from textwrap import dedent
from typing import Any, Optional, Union
from uuid import UUID

from cognee.infrastructure.databases.graph.graph_db_interface import (
    GraphDBInterface,
    NodeData,
)
from cognee.infrastructure.engine import DataPoint
from cognee.modules.storage.utils import JSONEncoder
from cognee.shared.logging_utils import ERROR, get_logger
from neo4j import AsyncGraphDatabase, AsyncSession
from neo4j.exceptions import Neo4jError

logger = get_logger("MemgraphAdapter", level=ERROR)


class MemgraphAdapter(GraphDBInterface):
    """
    Handles interaction with a Memgraph database through various graph operations.

    Public methods include:
    - get_session
    - query
    - has_node
    - add_node
    - add_nodes
    - extract_node
    - extract_nodes
    - delete_node
    - delete_nodes
    - has_edge
    - has_edges
    - add_edge
    - add_edges
    - get_edges
    - get_disconnected_nodes
    - get_predecessors
    - get_successors
    - get_neighbours
    - get_connections
    - remove_connection_to_predecessors_of
    - remove_connection_to_successors_of
    - delete_graph
    - serialize_properties
    - get_model_independent_graph_data
    - get_graph_data
    - get_nodeset_subgraph
    - get_filtered_graph_data
    - get_node_labels_string
    - get_relationship_labels_string
    - get_graph_metrics
    """

    def __init__(
        self,
        graph_database_url: str,
        graph_database_username: Optional[str] = None,
        graph_database_password: Optional[str] = None,
        driver: Optional[Any] = None,
        **kwargs,
    ):
        # Only use auth if both username and password are provided
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
        """
        Manage a session with the database, yielding the session for use in operations.
        """
        async with self.driver.session() as session:
            yield session

    async def query(
        self,
        query: str,
        params: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """
        Execute a provided query on the Memgraph database and return the results.

        Parameters:
        -----------

            - query (str): The Cypher query to be executed against the database.
            - params (Optional[Dict[str, Any]]): Optional parameters to be used in the query.
              (default None)

        Returns:
        --------

            - List[Dict[str, Any]]: A list of dictionaries representing the result set of the
              query.
        """
        try:
            async with self.get_session() as session:
                result = await session.run(query, params)
                data = await result.data()
                return data
        except Neo4jError as error:
            logger.error("Memgraph query error: %s", error, exc_info=True)
            raise error

    async def has_node(self, node_id: str) -> bool:
        """
        Determine if a node with the given ID exists in the database.

        Parameters:
        -----------

            - node_id (str): The ID of the node to check for existence.

        Returns:
        --------

            - bool: True if the node exists; otherwise, False.
        """
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
        self, node: Union[DataPoint, str], properties: Optional[dict[str, Any]] = None
    ):
        """
        Add a new node to the database with specified properties.

        Parameters:
        -----------

            - node (Union[DataPoint, str]): Either a DataPoint object or a string identifier
              for the node being added.
            - properties (Optional[Dict[str, Any]]): A dictionary of properties associated
              with the node. Required when node is a string, ignored when node is a DataPoint.
              (default None)

        Returns:
        --------

            The result of the node addition, including its internal ID and node ID.
        """
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
        RETURN ID(node) AS internal_id, node.id AS nodeId
        """

        params = {
            "node_id": node_id,
            "properties": serialized_properties,
        }
        return await self.query(query, params)

    async def add_nodes(
        self,
        nodes: list[DataPoint],
        source_ref_key: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
    ) -> None:
        """
        Add multiple nodes to the database in a single operation.

        Parameters:
        -----------

            - nodes (list[DataPoint]): A list of DataPoint objects representing the nodes to
              add.
            - source_ref_key (Optional[str]): Graph-provenance source ref (cognee 1.3.0). Accepted
              for interface compatibility and ignored — Memgraph does not fold provenance today.
              (default None)
            - pipeline_run_id (Optional[str]): Run id paired with the provenance stamp. Accepted
              and ignored by this backend. (default None)

        Returns:
        --------

            - None: None.
        """
        query = """
        UNWIND $nodes AS node
        MERGE (n {id: node.node_id})
        ON CREATE SET n += node.properties, n.updated_at = timestamp()
        ON MATCH SET n += node.properties, n.updated_at = timestamp()
        RETURN ID(n) AS internal_id, n.id AS nodeId
        """

        nodes = [
            {
                "node_id": str(node.id),
                "properties": self.serialize_properties(node.model_dump()),
            }
            for node in nodes
        ]

        results = await self.query(query, {"nodes": nodes})
        return results

    async def extract_node(self, node_id: str):
        """
        Retrieve a single node based on its ID.

        Parameters:
        -----------

            - node_id (str): The ID of the node to retrieve.

        Returns:
        --------

            The node corresponding to the provided ID, or None if not found.
        """
        results = await self.extract_nodes([node_id])

        return results[0] if len(results) > 0 else None

    async def extract_nodes(self, node_ids: list[str]):
        """
        Retrieve multiple nodes based on their IDs.

        Parameters:
        -----------

            - node_ids (List[str]): A list of IDs for the nodes to retrieve.

        Returns:
        --------

            A list of nodes corresponding to the provided IDs.
        """
        query = """
        UNWIND $node_ids AS id
        MATCH (node {id: id})
        RETURN node"""

        params = {"node_ids": node_ids}

        results = await self.query(query, params)

        return [result["node"] for result in results]

    async def delete_node(self, node_id: str):
        """
        Delete a node from the database based on its ID.

        Parameters:
        -----------

            - node_id (str): The ID of the node to delete.

        Returns:
        --------

            None.
        """
        sanitized_id = node_id.replace(":", "_")

        query = "MATCH (node: {{id: $node_id}}) DETACH DELETE node"
        params = {"node_id": sanitized_id}

        return await self.query(query, params)

    async def delete_nodes(self, node_ids: list[str]) -> None:
        """
        Delete multiple nodes from the database based on their IDs.

        Parameters:
        -----------

            - node_ids (list[str]): A list of IDs for the nodes to delete.

        Returns:
        --------

            - None: None.
        """
        query = """
        UNWIND $node_ids AS id
        MATCH (node {id: id})
        DETACH DELETE node"""

        params = {"node_ids": node_ids}

        return await self.query(query, params)

    async def has_edge(
        self, source_id: Union[str, UUID], target_id: Union[str, UUID], relationship_name: str
    ) -> bool:
        """
        Check if a directed edge exists between two nodes identified by their IDs.

        Parameters:
        -----------

            - source_id (Union[str, UUID]): The ID of the source node.
            - target_id (Union[str, UUID]): The ID of the target node.
            - relationship_name (str): The name of the relationship to check.

        Returns:
        --------

            - bool: True if the edge exists; otherwise, False.
        """
        query = """
            MATCH (from_node)-[relationship]->(to_node)
            WHERE from_node.id = $from_node_id AND to_node.id = $to_node_id
            AND type(relationship) = $relationship_name
            RETURN COUNT(relationship) > 0 AS edge_exists
        """

        params = {
            "from_node_id": str(source_id),
            "to_node_id": str(target_id),
            "relationship_name": relationship_name,
        }

        records = await self.query(query, params)
        return records[0]["edge_exists"] if records else False

    async def has_edges(self, edges):
        """
        Check for the existence of multiple edges based on provided criteria.

        Parameters:
        -----------

            - edges: A list of edges to verify existence for.

        Returns:
        --------

            A list of boolean values indicating the existence of each edge.
        """
        query = """
            UNWIND $edges AS edge
            MATCH (a)-[r]->(b)
            WHERE id(a) = edge.from_node AND id(b) = edge.to_node
            AND type(r) = edge.relationship_name
            RETURN edge.from_node AS from_node, edge.to_node AS to_node, edge.relationship_name
            AS relationship_name, count(r) > 0 AS edge_exists
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
            logger.error("Memgraph query error: %s", error, exc_info=True)
            raise error

    async def add_edge(
        self,
        source_id: Union[str, UUID],
        target_id: Union[str, UUID],
        relationship_name: str,
        properties: Optional[dict[str, Any]] = None,
    ):
        """
        Add a directed edge between two nodes with optional properties.

        Parameters:
        -----------

            - source_id (Union[str, UUID]): The ID of the source node.
            - target_id (Union[str, UUID]): The ID of the target node.
            - relationship_name (str): The type/label of the relationship to create.
            - properties (Optional[Dict[str, Any]]): Optional properties associated with
              the edge. (default None)

        Returns:
        --------

            The result of the edge addition operation, including relationship details.
        """
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
        source_ref_key: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
    ) -> None:
        """
        Batch add multiple edges between nodes, enforcing specified relationships.

        Parameters:
        -----------

            - edges (list[tuple[str, str, str, dict[str, Any]]): A list of tuples containing
              specifications for each edge to add.
            - source_ref_key (Optional[str]): Graph-provenance source ref (cognee 1.3.0). Accepted
              for interface compatibility and ignored — Memgraph does not fold provenance today.
              (default None)
            - pipeline_run_id (Optional[str]): Run id paired with the provenance stamp. Accepted
              and ignored by this backend. (default None)

        Returns:
        --------

            - None: None.
        """
        from collections import defaultdict

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

            edges = [
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
                await self.query(query, {"edges": edges})
            except Neo4jError as error:
                logger.error("Memgraph query error: %s", error, exc_info=True)
                raise error

    async def get_edges(self, node_id: str):
        """
        Retrieve all edges connected to a specific node identified by its ID.

        Parameters:
        -----------

            - node_id (str): The ID of the node for which to retrieve connected edges.

        Returns:
        --------

            A list of tuples representing the edges connected to the node.
        """
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
        """
        Identify nodes in the graph that do not belong to the largest connected component.

        Returns:
        --------

            - list[str]: A list of IDs representing the disconnected nodes.
        """
        query = """
        // Step 1: Collect all nodes
        MATCH (n)
        WITH COLLECT(n) AS nodes

        // Step 2: Find all connected components
        WITH nodes
        CALL {
          WITH nodes
          UNWIND nodes AS startNode
          MATCH path = (startNode)-[*]-(connectedNode)
          WITH startNode, COLLECT(DISTINCT connectedNode) AS component
          RETURN component
        }

        // Step 3: Aggregate components
        WITH COLLECT(component) AS components

        // Step 4: Identify the largest connected component
        UNWIND components AS component
        WITH component
        ORDER BY SIZE(component) DESC
        LIMIT 1
        WITH component AS largestComponent

        // Step 5: Find nodes not in the largest connected component
        MATCH (n)
        WHERE NOT n IN largestComponent
        RETURN COLLECT(ID(n)) AS ids
        """

        results = await self.query(query)
        return results[0]["ids"] if len(results) > 0 else []

    async def get_predecessors(self, node_id: str, edge_label: Optional[str] = None) -> list[str]:
        """
        Retrieve all predecessors of a node based on its ID and optional edge label.

        Parameters:
        -----------

            - node_id (str): The ID of the node to find predecessors for.
            - edge_label (str): Optional edge label to filter predecessors. (default None)

        Returns:
        --------

            - list[str]: A list of predecessor node IDs.
        """
        if edge_label is not None:
            query = """
            MATCH (node)<-[r]-(predecessor)
            WHERE node.id = $node_id AND type(r) = $edge_label
            RETURN predecessor
            """

            results = await self.query(
                query,
                {
                    "node_id": node_id,
                    "edge_label": edge_label,
                },
            )

            return [result["predecessor"] for result in results]
        else:
            query = """
            MATCH (node)<-[r]-(predecessor)
            WHERE node.id = $node_id
            RETURN predecessor
            """

            results = await self.query(
                query,
                {
                    "node_id": node_id,
                },
            )

            return [result["predecessor"] for result in results]

    async def get_successors(self, node_id: str, edge_label: Optional[str] = None) -> list[str]:
        """
        Retrieve all successors of a node based on its ID and optional edge label.

        Parameters:
        -----------

            - node_id (str): The ID of the node to find successors for.
            - edge_label (str): Optional edge label to filter successors. (default None)

        Returns:
        --------

            - list[str]: A list of successor node IDs.
        """
        if edge_label is not None:
            query = """
            MATCH (node)-[r]->(successor)
            WHERE node.id = $node_id AND type(r) = $edge_label
            RETURN successor
            """

            results = await self.query(
                query,
                {
                    "node_id": node_id,
                    "edge_label": edge_label,
                },
            )

            return [result["successor"] for result in results]
        else:
            query = """
            MATCH (node)-[r]->(successor)
            WHERE node.id = $node_id
            RETURN successor
            """

            results = await self.query(
                query,
                {
                    "node_id": node_id,
                },
            )

            return [result["successor"] for result in results]

    async def get_neighbors(self, node_id: str) -> list[dict[str, Any]]:
        """
        Get both predecessors and successors of a node.

        Parameters:
        -----------

            - node_id (str): The ID of the node to find neighbors for.

        Returns:
        --------

            - List[Dict[str, Any]]: A combined list of neighbor node IDs.
        """
        predecessors, successors = await asyncio.gather(
            self.get_predecessors(node_id), self.get_successors(node_id)
        )

        return predecessors + successors

    async def get_neighborhood(
        self,
        node_ids: list[str],
        depth: int = 1,
        edge_types: Optional[list[str]] = None,
    ) -> tuple[list[tuple[str, dict]], list[tuple[str, str, str, dict]]]:
        """
        Get the k-hop neighborhood subgraph around a set of seed nodes.

        Returns all nodes and edges within ``depth`` hops of any seed node, in the same
        format as ``get_nodeset_subgraph()`` / the neo4j adapter: nodes are keyed by the
        cognee ``id`` property (a UUID), and edges use the source/target ``id`` properties.
        When ``edge_types`` is provided, only edges of those relationship types are
        traversed and returned.

        Parameters:
        -----------

            - node_ids (List[str]): Seed node identifiers (the ``id`` property) to start from.
            - depth (int): Number of hops to traverse from each seed node. (default 1)
            - edge_types (Optional[List[str]]): If given, only traverse/return edges of these
              relationship types. (default None)
        """
        if not node_ids:
            return ([], [])

        # Memgraph requires a literal upper bound in a variable-length pattern, so depth is
        # validated to an int and interpolated; user-supplied ids stay parameterised.
        depth = max(0, int(depth))
        seed_ids = [str(node_id) for node_id in node_ids]

        params: dict[str, Any] = {"node_ids": seed_ids}
        path_edge_clause = ""
        if edge_types:
            path_edge_clause = " WHERE ALL(r IN relationships(path) WHERE TYPE(r) IN $edge_types)"
            params["edge_types"] = list(edge_types)

        nodes_query = (
            "MATCH (seed) WHERE seed.id IN $node_ids "
            f"MATCH path = (seed)-[*0..{depth}]-(n)"
            f"{path_edge_clause} "
            "RETURN DISTINCT n.id AS id, properties(n) AS properties"
        )
        node_records = await self.query(nodes_query, params)
        nodes = [(record["id"], record["properties"]) for record in node_records]

        node_uuids = [record["id"] for record in node_records]
        if not node_uuids:
            return (nodes, [])

        edge_params: dict[str, Any] = {"ids": node_uuids}
        edge_type_filter = ""
        if edge_types:
            edge_type_filter = " AND TYPE(r) IN $edge_types"
            edge_params["edge_types"] = list(edge_types)

        edges_query = (
            "MATCH (n)-[r]->(m) "
            f"WHERE n.id IN $ids AND m.id IN $ids{edge_type_filter} "
            "RETURN n.id AS source, m.id AS target, TYPE(r) AS type, "
            "properties(r) AS properties"
        )
        edge_records = await self.query(edges_query, edge_params)
        edges = [
            (record["source"], record["target"], record["type"], record["properties"])
            for record in edge_records
        ]

        return (nodes, edges)

    async def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        """Get a single node by ID."""
        query = """
        MATCH (node {id: $node_id})
        RETURN node
        """
        results = await self.query(query, {"node_id": node_id})
        return results[0]["node"] if results else None

    async def get_nodes(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Get multiple nodes by their IDs."""
        query = """
        UNWIND $node_ids AS id
        MATCH (node {id: id})
        RETURN node
        """
        results = await self.query(query, {"node_ids": node_ids})
        return [result["node"] for result in results]

    async def get_connections(
        self, node_id: Union[str, UUID]
    ) -> list[tuple[NodeData, dict[str, Any], NodeData]]:
        """
        Retrieve connections for a given node, including both predecessors and successors.

        Parameters:
        -----------

            - node_id (Union[str, UUID]): The ID of the node for which to retrieve connections.

        Returns:
        --------

            - List[Tuple[NodeData, Dict[str, Any], NodeData]]: A list of connections associated
              with the node.
        """
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
        """
        Remove specified connections to the predecessors of the given node IDs.

        Parameters:
        -----------

            - node_ids (list[str]): A list of node IDs from which to remove predecessor
              connections.
            - edge_label (str): The label of the edges to remove.

        Returns:
        --------

            - None: None.
        """
        query = f"""
        UNWIND $node_ids AS nid
        MATCH (node {id: nid})-[r]->(predecessor)
        WHERE type(r) = $edge_label
        DELETE r;
        """

        params = {"node_ids": node_ids, "edge_label": edge_label}

        return await self.query(query, params)

    async def remove_connection_to_successors_of(
        self, node_ids: list[str], edge_label: str
    ) -> None:
        """
        Remove specified connections to the successors of the given node IDs.

        Parameters:
        -----------

            - node_ids (list[str]): A list of node IDs from which to remove successor
              connections.
            - edge_label (str): The label of the edges to remove.

        Returns:
        --------

            - None: None.
        """
        query = f"""
        UNWIND $node_ids AS id
        MATCH (node:`{id}`)<-[r:{edge_label}]-(successor)
        DELETE r;
        """

        params = {"node_ids": node_ids}

        return await self.query(query, params)

    async def delete_graph(self):
        """
        Completely delete the graph from the database, removing all nodes and edges.

        Returns:
        --------

            None.
        """
        query = """MATCH (node)
                DETACH DELETE node;"""

        return await self.query(query)

    def serialize_properties(self, properties=None):
        """
        Convert property values to a suitable representation for storage.

        Parameters:
        -----------

            - properties: A dictionary of properties to serialize. (default dict())

        Returns:
        --------

            A dictionary of serialized properties.
        """
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
        """
        Fetch nodes and relationships without any specific model filtering.

        Returns:
        --------

            A tuple containing nodes and edges as collections.
        """
        query_nodes = "MATCH (n) RETURN collect(n) AS nodes"
        nodes = await self.query(query_nodes)

        query_edges = "MATCH (n)-[r]->(m) RETURN collect([n, r, m]) AS elements"
        edges = await self.query(query_edges)

        return (nodes, edges)

    async def get_graph_data(self):
        """
        Retrieve all nodes and edges from the graph, including their properties.

        Returns:
        --------

            A tuple containing lists of nodes and edges.
        """
        query = "MATCH (n) RETURN ID(n) AS id, labels(n) AS labels, properties(n) AS properties"

        result = await self.query(query)

        nodes = [
            (
                record["id"],
                record["properties"],
            )
            for record in result
        ]

        query = """
        MATCH (n)-[r]->(m)
        RETURN ID(n) AS source, ID(m) AS target, TYPE(r) AS type, properties(r) AS properties
        """
        result = await self.query(query)
        edges = [
            (
                record["source"],
                record["target"],
                record["type"],
                record["properties"],
            )
            for record in result
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

        The seed nodes are those of label ``node_type.__name__`` whose ``name`` matches one of
        the provided ``node_name`` values. With operator "OR" any neighbour of a seed node is
        included; with operator "AND" only neighbours connected to every seed node are included.

        Parameters:
        -----------

            - node_type (Type[Any]): The type of nodes to filter.
            - node_name (List[str]): A list of node names to filter.
            - node_name_filter_operator (str): How to combine the node names, either "OR"
              (default) or "AND".

        Returns:
        --------

            - Tuple[List[Tuple[int, dict]], List[Tuple[int, int, str, dict]]]: A tuple
              containing nodes and edges in the requested subgraph.
        """
        label = node_type.__name__

        if node_name_filter_operator == "OR":
            query = """
            UNWIND $names AS wantedName
            MATCH (n)
            WHERE n.type = $label AND n.name = wantedName
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
                 { id: n.id,
                    properties: properties(n) }] AS rawNodes,
              [r IN rels  |
                 { type: type(r),
                    properties: properties(r) }] AS rawRels
            """
        else:
            query = """
            UNWIND $names AS wantedName
            MATCH (n)
            WHERE n.type = $label AND n.name = wantedName
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
              [n IN nodes | { id: n.id, properties: properties(n) }] AS rawNodes,
              [r IN rels  | { type: type(r), properties: properties(r) }] AS rawRels
            """

        result = await self.query(query, {"names": node_name, "label": label})

        if not result:
            return [], []

        raw_nodes = result[0]["rawNodes"]
        raw_rels = result[0]["rawRels"]

        nodes = []
        for n in raw_nodes:
            nodes.append((n["properties"]["id"], n["properties"]))

        edges = []
        for r in raw_rels:
            edges.append(
                (
                    r["properties"]["source_node_id"],
                    r["properties"]["target_node_id"],
                    r["type"],
                    r["properties"],
                )
            )

        return nodes, edges

    async def get_filtered_graph_data(self, attribute_filters):
        """
        Fetch nodes and relationships based on specified attribute filters.

        Parameters:
        -----------

            - attribute_filters: A list of criteria to filter nodes and relationships.

        Returns:
        --------

            A tuple containing filtered nodes and edges.
        """
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
        RETURN ID(n) AS id, labels(n) AS labels, properties(n) AS properties
        """
        result_nodes = await self.query(query_nodes)

        nodes = [
            (
                record["id"],
                record["properties"],
            )
            for record in result_nodes
        ]

        query_edges = f"""
        MATCH (n)-[r]->(m)
        WHERE {where_clause} AND {where_clause.replace("n.", "m.")}
        RETURN ID(n) AS source, ID(m) AS target, TYPE(r) AS type, properties(r) AS properties
        """
        result_edges = await self.query(query_edges)

        edges = [
            (
                record["source"],
                record["target"],
                record["type"],
                record["properties"],
            )
            for record in result_edges
        ]

        return (nodes, edges)

    async def get_node_labels_string(self):
        """
        Retrieve a string representation of all unique node labels in the graph.

        Returns:
        --------

            A string containing unique node labels.
        """
        node_labels_query = """
        MATCH (n)
        WITH DISTINCT labels(n) AS labelList
        UNWIND labelList AS label
        RETURN collect(DISTINCT label) AS labels;
        """
        node_labels_result = await self.query(node_labels_query)
        node_labels = node_labels_result[0]["labels"] if node_labels_result else []

        if not node_labels:
            raise ValueError("No node labels found in the database")

        node_labels_str = "[" + ", ".join(f"'{label}'" for label in node_labels) + "]"
        return node_labels_str

    async def get_relationship_labels_string(self):
        """
        Retrieve a string representation of all unique relationship types in the graph.

        Returns:
        --------

            A string containing unique relationship types.
        """
        relationship_types_query = (
            "MATCH ()-[r]->() RETURN collect(DISTINCT type(r)) AS relationships;"
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

    async def _connected_component_sizes(self) -> list[int]:
        """
        Return the sizes of the graph's weakly connected components.

        Uses MAGE's weakly_connected_components when available, falling back to a
        client-side union-find for deployments running Memgraph without MAGE.

        Returns:
        --------

            - list[int]: One entry per component, holding that component's node count.
        """
        try:
            rows = await self.query(
                """
                CALL weakly_connected_components.get()
                YIELD node, component_id
                RETURN component_id, count(node) AS size
                """
            )
            return [int(row["size"]) for row in rows]
        except Neo4jError:
            logger.debug("MAGE unavailable; computing connected components client-side.")
            return await self._connected_component_sizes_without_mage()

    async def _connected_component_sizes_without_mage(self) -> list[int]:
        """
        Compute weakly connected component sizes client-side via union-find.

        Transfers the node ids and edge endpoints, so cost grows with graph size; it
        exists so metrics still work without the MAGE query modules installed.

        Returns:
        --------

            - list[int]: One entry per component, holding that component's node count.
        """
        nodes = await self.query("MATCH (n) RETURN n.id AS id")
        edges = await self.query("MATCH (a)-[]->(b) RETURN a.id AS source, b.id AS target")

        parent = {row["id"]: row["id"] for row in nodes}

        def find(node_id):
            while parent[node_id] != node_id:
                parent[node_id] = parent[parent[node_id]]
                node_id = parent[node_id]
            return node_id

        for edge in edges:
            source, target = edge["source"], edge["target"]
            if source in parent and target in parent:
                source_root, target_root = find(source), find(target)
                if source_root != target_root:
                    parent[target_root] = source_root

        sizes: dict[Any, int] = {}
        for node_id in parent:
            root = find(node_id)
            sizes[root] = sizes.get(root, 0) + 1
        return list(sizes.values())

    async def get_graph_metrics(self, include_optional=False):
        """
        Calculate and return various metrics of the graph, including mandatory and optional
        metrics.

        Parameters:
        -----------

            - include_optional: Specify whether to include optional metrics in the results.
              (default False)

        Returns:
        --------

            A dictionary containing calculated graph metrics.
        """

        try:
            # Basic metrics. query() returns a list of dicts keyed by the RETURN
            # names, so every result is aliased and read by key.
            node_count = await self.query("MATCH (n) RETURN count(n) AS cnt")
            edge_count = await self.query("MATCH ()-[r]->() RETURN count(r) AS cnt")
            num_nodes = node_count[0]["cnt"] if node_count else 0
            num_edges = edge_count[0]["cnt"] if edge_count else 0

            # Calculate mandatory metrics
            mandatory_metrics = {
                "num_nodes": num_nodes,
                "num_edges": num_edges,
                "mean_degree": (2 * num_edges) / num_nodes if num_nodes > 0 else 0,
                "edge_density": (num_edges) / (num_nodes * (num_nodes - 1)) if num_nodes > 1 else 0,
            }

            component_sizes = await self._connected_component_sizes()

            mandatory_metrics.update(
                {
                    "num_connected_components": len(component_sizes),
                    "sizes_of_connected_components": component_sizes,
                }
            )

            if include_optional:
                self_loops = await self.query("MATCH (n)-[r]->(n) RETURN COUNT(r) AS cnt")
                num_selfloops = self_loops[0]["cnt"] if self_loops else 0

                # The all-pairs metrics below need every shortest path in the graph,
                # which is prohibitive on a general graph. They are reported as
                # unsupported (-1), consistent with the other community adapters.
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
        query = "MATCH (n) RETURN true LIMIT 1;"
        result = await self.query(query)
        return len(result) == 0
