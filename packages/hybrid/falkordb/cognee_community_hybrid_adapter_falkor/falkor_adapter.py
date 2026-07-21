import asyncio
import json
from collections import defaultdict
from enum import Enum
from textwrap import dedent
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
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

from falkordb.falkordb import FalkorDB
from falkordb.graph import Graph, QueryResult


class IndexSchema(DataPoint):
    """
    Define a schema for indexing that includes text data and associated metadata.

    This class inherits from the DataPoint class. It contains a string attribute 'text' and
    a dictionary 'metadata' that specifies the index fields for this schema.
    """

    text: str

    metadata: dict = {"index_fields": ["text"]}
    belongs_to_set: List[str] = []


class FalkorDBAdapter(VectorDBInterface, GraphDBInterface):
    """
    Manage and interact with a graph database using vector embeddings.

    Public methods include:
    - query
    - embed_data
    - stringify_properties
    - create_data_point_query
    - create_edge_query
    - create_collection
    - has_collection
    - create_data_points
    - create_vector_index
    - has_vector_index
    - index_data_points
    - add_node
    - add_nodes
    - add_edge
    - add_edges
    - has_edges
    - retrieve
    - extract_node
    - extract_nodes
    - get_connections
    - search
    - batch_search
    - get_graph_data
    - delete_data_points
    - delete_node
    - delete_nodes
    - delete_graph
    - prune
    - get_node
    - get_nodes
    - get_neighbors
    - get_graph_metrics
    - get_document_subgraph
    - get_degree_one_nodes
    """

    def __init__(
        self,
        graph_database_url: str | None = None,
        graph_database_port: int | None = 6379,
        graph_database_username: str | None = None,
        graph_database_password: str | None = None,
        embedding_engine: EmbeddingEngine | None = None,
        url: str | None = None,
        api_key: str | None = None,
        database_name: str | None = "cognee_graph",
        **kwargs,
    ):
        # ``create_vector_engine`` builds this adapter on the vector path as
        # ``adapter(url=..., api_key=..., embedding_engine=..., database_name=...)``
        # with no port kwarg, so a non-default FalkorDB port only reaches us
        # embedded in the URL (e.g. ``VECTOR_DB_URL="myhost:6380"``). Parse a
        # trailing ``:port`` out of the host so those deployments are reachable
        # instead of always dialing ``:6379``.
        resolved_host = url if url else graph_database_url
        default_port = graph_database_port if graph_database_port else 6379
        host, port = self._split_host_port(resolved_host, default_port)
        self.driver = FalkorDB(
            host=host,
            port=port,
            username=graph_database_username,
            password=graph_database_password,
        )
        self.embedding_engine = get_embedding_engine() if not embedding_engine else embedding_engine
        self.graph_name = database_name if database_name else "cognee_graph"
        self.api_key = api_key

    @staticmethod
    def _split_host_port(raw_host: Any, default_port: int) -> Tuple[Any, int]:
        """Split a bare ``host:port`` string into ``(host, port)``.

        Only a plain ``host:port`` (single colon, all-digit port) is parsed; a
        scheme-qualified URL (``redis://...``) or an IPv6 literal (multiple
        colons) is left untouched with ``default_port``, so this is safe and
        backward compatible — a bare host keeps the default port.
        """
        if not isinstance(raw_host, str):
            return raw_host, default_port
        if "://" not in raw_host and raw_host.count(":") == 1:
            candidate_host, _, candidate_port = raw_host.partition(":")
            if candidate_port.isdigit():
                return candidate_host, int(candidate_port)
        return raw_host, default_port

    @staticmethod
    def _coerce_stored_value(value: Any) -> Any:
        """Coerce a value that will be STORED as a node/edge property.

        FalkorDB property values must be primitives or arrays of primitives. A
        map, a non-primitive array, or a ``bytes`` value makes FalkorDB reject the
        *entire* query — and since writes bind the property bag as ``$properties``
        / each ``UNWIND``-ed ``$items`` element, one stray value aborts the whole
        pipeline run. The failure modes are:

        - ``bytes`` -> ``ResponseError: Failed to parse query parameter
          'properties' value``; and
        - maps / arrays containing non-primitives -> ``ResponseError: Property
          values can only be of primitive types or arrays of primitive types``.

        JSON-encode maps and non-primitive arrays, decode ``bytes``, and unwrap
        ``Enum`` to its ``.value`` so the value survives the write.
        """
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, (list, tuple)):
            coerced = [FalkorDBAdapter._coerce_stored_value(item) for item in value]
            # FalkorDB arrays must hold primitives (and reject null elements);
            # if any element isn't one, store the whole array as a JSON string.
            if all(
                item is not None and isinstance(item, (bool, int, float, str)) for item in coerced
            ):
                return coerced
            return json.dumps(value, default=str)
        if isinstance(value, dict):
            return json.dumps(value, default=str)
        return str(value)

    @staticmethod
    def _coerce_param_value(value: Any) -> Any:
        """Coerce a bound query-param value, PRESERVING the structure FalkorDB
        needs.

        Maps and arrays-of-maps are valid *bound params* — e.g. an
        ``UNWIND $items AS item ... SET edge += item`` edge batch binds a list of
        maps — so this keeps that shape. A record map keeps its keys, but its
        values are primitivized via :meth:`_coerce_stored_value` (they get stored
        as properties). Flattening a list of maps to JSON strings (as a blanket
        coercion would) makes the ``UNWIND`` fail with ``Type mismatch: expected
        Map ... but was String``. ``bytes`` / ``Enum`` outside a record map are
        still coerced.
        """
        if isinstance(value, dict):
            return {key: FalkorDBAdapter._coerce_stored_value(val) for key, val in value.items()}
        if isinstance(value, (list, tuple)):
            return [FalkorDBAdapter._coerce_param_value(item) for item in value]
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    @staticmethod
    def _sanitize_cypher_params(params: dict) -> dict:
        """Make a bound-params dict safe for FalkorDB.

        Routes every value through :meth:`_coerce_param_value` (structure-
        preserving), which primitivizes the values that get stored as properties
        via :meth:`_coerce_stored_value`. Without this a ``bytes`` value or a
        nested map/array in a model-extracted entity rejects the whole query and
        aborts the pipeline run.
        """
        return {key: FalkorDBAdapter._coerce_param_value(value) for key, value in params.items()}

    # TODO: This should return a list of results, not a single result
    async def query(self, query: str, params: dict = None) -> list:
        """
        Execute a query against the graph database.

        Returns a list of result rows, conforming to the ``GraphDBInterface``
        contract (``List[Any]``).  The returned list also exposes a
        ``result_set`` attribute (pointing to itself) so that existing
        internal code that accesses ``result.result_set`` continues to work
        unchanged.

        Parameters:
        -----------

            - query (str): The query string to be executed against the graph database.
            - params (dict): A dictionary of parameters to be used in the query. (default {})

        Returns:
        --------

            A list of result rows from the query execution.
        """
        if params is None:
            params = {}
        params = self._sanitize_cypher_params(params)
        graph = self.driver.select_graph(self.graph_name)

        try:
            raw = graph.query(query, params)
        except Exception as e:
            print(f"Error executing query: {e}")
            raise e

        # Wrap the result_set list so it satisfies both:
        #   - GraphDBInterface contract: List[Any]
        #   - Internal adapter code: result.result_set
        rows = raw.result_set if raw.result_set else []

        class _ResultList(list):
            """A plain list with a ``result_set`` attribute for backward compat."""

            __slots__ = ()

            @property
            def result_set(self):
                return self

        return _ResultList(rows)

    async def embed_data(self, data: list[str]) -> list[list[float]]:
        """
        Embed a list of text data into vector representations using the embedding engine.

        Empty or blank-only inputs are handled gracefully: an empty list
        returns ``[]``, and blank strings are skipped while preserving
        index alignment in the output.

        Parameters:
        -----------

            - data (list[str]): A list of strings that should be embedded into vectors.

        Returns:
        --------

            - list[list[float]]: A list of lists, where each inner list contains float values
              representing the embedded vectors.
        """
        if not data:
            return []

        # Filter out blank strings that would cause embedding API errors
        non_blank = [s for s in data if s and s.strip()]
        if not non_blank:
            return [[] for _ in data]

        result = await self.embedding_engine.embed_text(non_blank)  # type: ignore

        # Fast path: no blanks were filtered
        if len(non_blank) == len(data):
            return result

        # Re-align: insert empty embeddings at filtered-out positions
        it = iter(result)
        return [next(it) if (s and s.strip()) else [] for s in data]

    async def stringify_properties(self, properties: dict) -> str:
        """
        Convert properties dictionary to a string format suitable for database queries.

        Parameters:
        -----------

            - properties (dict): A dictionary containing properties to be converted to string
              format.

        Returns:
        --------

            - str: A string representation of the properties in the appropriate format.
        """

        # TODO: Check what types we support
        def parse_value(value: Any) -> str:
            """
            Convert a value to its string representation based on type for database queries.

            Parameters:
            -----------

                - value: The value to parse into a string representation.

            Returns:
            --------

                Returns the string representation of the value in the appropriate format.
            """
            if type(value) is UUID:
                return f"'{str(value)}'"
            if type(value) is int or type(value) is float:
                return str(value)
            if (
                type(value) is list
                and len(value) > 0
                and type(value[0]) is float
                and len(value) == self.embedding_engine.get_vector_size()
            ):
                return f"'vecf32({value})'"
            # if type(value) is datetime:
            #     return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
            if type(value) is dict:
                return f"'{json.dumps(value).replace(chr(39), chr(34))}'"
            if type(value) is str:
                # Escape single quotes and handle special characters
                escaped_value = (
                    str(value)
                    .replace("'", "\\'")
                    .replace('"', '\\"')
                    .replace("\n", "\\n")
                    .replace("\r", "\\r")
                    .replace("\t", "\\t")
                )
                return f"'{escaped_value}'"
            return f"'{str(value)}'"

        return ",".join([f"{key}:{parse_value(value)}" for key, value in properties.items()])

    async def create_data_point_query(
        self, data_point: DataPoint, vectorized_values: dict
    ) -> tuple[str, dict]:
        """
        Compose a query to create or update a data point in the database.

        Parameters:
        -----------

            - data_point (DataPoint): An instance of DataPoint containing information about the
              entity.
            - vectorized_values (dict): A dictionary of vectorized values related to the data
              point.

        Returns:
        --------

            A tuple containing the query string and parameters dictionary.
        """
        node_label = type(data_point).__name__
        property_names = DataPoint.get_embeddable_property_names(data_point)

        properties = {
            **data_point.model_dump(),
            **(
                {
                    property_names[index]: (
                        vectorized_values[index]
                        if index < len(vectorized_values)
                        else getattr(data_point, property_name, None)
                    )
                    for index, property_name in enumerate(property_names)
                }
            ),
        }

        # Clean the properties - remove None values and handle special types
        clean_properties = {}
        for key, value in properties.items():
            if value is not None:
                if isinstance(value, UUID):
                    clean_properties[key] = str(value)
                elif isinstance(value, dict):
                    clean_properties[key] = json.dumps(value)
                elif isinstance(value, list) and len(value) > 0 and isinstance(value[0], float):
                    # This is likely a vector - convert to string representation
                    clean_properties[key] = f"vecf32({value})"
                else:
                    clean_properties[key] = value

        query = dedent(
            f"""
            MERGE (node:{node_label} {{id: $node_id}})
            SET node += $properties, node.updated_at = timestamp()
        """
        ).strip()

        params = {"node_id": str(data_point.id), "properties": clean_properties}

        return query, params

    def sanitize_relationship_name(self, relationship_name: str) -> str:
        """
        Sanitize relationship name to be a valid Cypher identifier.

        FalkorDB's Cypher parser only accepts identifiers matching
        ``[A-Za-z_][A-Za-z0-9_]*`` for unquoted relationship types, so
        non-ASCII letters (á, ç, ñ, …) must be folded to ASCII before
        substitution. Without this, names extracted from non-English text
        such as ``responsável_por`` produce queries the parser rejects with
        ``Invalid input ...: expected '|', '*', '{', a parameter or ']'``.

        Parameters:
        -----------
            - relationship_name (str): The original relationship name

        Returns:
        --------
            - str: A sanitized relationship name valid for Cypher
        """
        import re
        import unicodedata

        # Decompose accented chars into base + combining marks, then drop
        # the combining marks: á → a, ç → c, ã → a, ñ → n.
        normalized = unicodedata.normalize("NFKD", relationship_name)
        ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))

        # ``re.ASCII`` forces \w to mean [a-zA-Z0-9_], so any remaining
        # non-ASCII characters (CJK, Cyrillic, …) collapse to underscores.
        sanitized = re.sub(r"[^\w]", "_", ascii_only, flags=re.ASCII)
        # Remove consecutive underscores
        sanitized = re.sub(r"_+", "_", sanitized)
        # Remove leading/trailing underscores
        sanitized = sanitized.strip("_")
        # Ensure it starts with a letter or underscore
        if sanitized and not sanitized[0].isalpha() and sanitized[0] != "_":
            sanitized = "_" + sanitized
        return sanitized or "RELATIONSHIP"

    async def create_edge_query(self, edge: tuple[str, str, str, dict]) -> str:
        """
        Generate a query to create or update an edge between two nodes in the graph.

        Parameters:
        -----------

            - edge (tuple[str, str, str, dict]): A tuple consisting of source and target node
              IDs, edge type, and edge properties.

        Returns:
        --------

            - str: A string containing the query to be executed for creating the edge.
        """
        # Sanitize the relationship name for Cypher compatibility
        sanitized_relationship = self.sanitize_relationship_name(edge[2])

        # Add the original relationship name to properties
        edge_properties = {**edge[3], "relationship_name": edge[2]}
        properties = await self.stringify_properties(edge_properties)
        properties = f"{{{properties}}}"

        return dedent(
            f"""
            MERGE (source {{id:'{edge[0]}'}})
            MERGE (target {{id: '{edge[1]}'}})
            MERGE (source)-[edge:{sanitized_relationship}]->(target)
            ON MATCH SET edge += {properties}, edge.updated_at = timestamp()
            ON CREATE SET edge += {properties}, edge.updated_at = timestamp()
        """
        ).strip()

    # TODO: Check if this is needed, or if collections are created automatically
    async def create_collection(self, collection_name: str, payload_schema: Optional[Any] = None):
        """
        Create a collection in the graph database with the specified name.

        Parameters:
        -----------

            - collection_name (str): The name of the collection to be created.
        """
        pass

    async def has_collection(self, collection_name: str) -> bool:
        """
        Check if a collection with the specified name exists in the graph database.

        Parameters:
        -----------

            - collection_name (str): The name of the collection to check for existence.

        Returns:
        --------

            - bool: Returns true if the collection exists, otherwise false.
        """
        collections = self.driver.list_graphs()

        return collection_name in collections

    async def create_data_points(self, collection_name: str, data_points: list[DataPoint]):
        """
        Add a list of data points to the graph database via batching.

        Can raise exceptions if there are issues during the database operations.

        Parameters:
        -----------

            - data_points (list[DataPoint]): A list of DataPoint instances to be inserted into
              the database.
        """
        embeddable_values: list[Any] = []
        vector_map: dict[str, dict[str, int | None]] = {}

        for data_point in data_points:
            property_names: list[str] = DataPoint.get_embeddable_property_names(data_point)
            key = str(data_point.id)
            vector_map[key] = {}

            for property_name in property_names:
                property_value = getattr(data_point, property_name, None)

                if property_value is not None:
                    vector_map[key][property_name] = len(embeddable_values)
                    embeddable_values.append(property_value)
                else:
                    vector_map[key][property_name] = None

        vectorized_values = await self.embed_data(embeddable_values)

        for data_point in data_points:
            vectorized_data: dict[str, list[float] | None] = {}
            for property_name in DataPoint.get_embeddable_property_names(data_point):
                vector_index = vector_map[str(data_point.id)][property_name]
                if vector_index is not None:
                    vectorized_data[property_name] = vectorized_values[vector_index]
                else:
                    vectorized_data[property_name] = None

            query, params = await self.create_data_point_query(data_point, vectorized_data)
            await self.query(query, params)

    async def create_vector_index(self, index_name: str, index_property_name: str) -> None:
        """
        Create a vector index in the specified graph for a given property if it does not already
        exist.

        Parameters:
        -----------

            - index_name (str): The name of the vector index to be created.
            - index_property_name (str): The name of the property on which the vector index will
              be created.
        """
        graph = self.driver.select_graph(self.graph_name)

        if not self.has_vector_index(graph, index_name, f"{index_property_name}_vector"):
            graph.create_node_vector_index(
                index_name,
                f"{index_property_name}_vector",
                dim=self.embedding_engine.get_vector_size(),
                similarity_function="cosine",
            )

    def has_vector_index(self, graph: Graph, index_name: str, index_property_name: str) -> bool:
        """
        Determine if a vector index exists on the specified property of the given graph.

        Parameters:
        -----------

            - graph: The graph instance to check for the vector index.
            - index_name (str): The name of the index to check for existence.
            - index_property_name (str): The property name associated with the index.

        Returns:
        --------

            - bool: Returns true if the vector index exists, otherwise false.
        """
        indices = graph.list_indices()

        return any(
            [
                (index[0] == index_name and index_property_name in index[1])
                for index in indices.result_set
            ]
        )

    # TODO: Check if this is needed, or if data points are indexed automatically
    async def index_data_points(
        self, index_name: str, index_property_name: str, data_points: list[DataPoint]
    ) -> None:
        """
        Index a list of data points in the specified graph database based on properties.

        To be implemented: does not yet have a defined behavior.

        Parameters:
        -----------

            - index_name (str): The name of the index to be created for the data points.
            - index_property_name (str): The property name on which to index the data points.
            - data_points (list[DataPoint]): A list of DataPoint instances to be indexed.
        """
        pass

    async def add_node(
        self,
        node: Union[DataPoint, str],
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Add a single node with specified properties to the graph.

        Supports both call styles required by ``GraphDBInterface`` in cognee 1.0.x:

        - ``add_node(data_point)`` — when a ``DataPoint`` is passed, the data point is
          embedded and inserted via the same path as ``create_data_points`` so any
          configured vector indexes are populated.
        - ``add_node(node_id, properties)`` — when a string id is provided, ``properties``
          is required and must contain at minimum a ``type`` key. If
          ``metadata.index_fields`` is supplied along with corresponding ``<field>_vector``
          entries the vector columns are written as well.

        Parameters:
        -----------

            - node (Union[DataPoint, str]): A ``DataPoint`` or a string identifier.
            - properties (Optional[Dict[str, Any]]): Required when ``node`` is a string.
        """
        if isinstance(node, DataPoint):
            await self.add_nodes([node])
            return

        if properties is None:
            raise ValueError(
                "FalkorDBAdapter.add_node: 'properties' is required when 'node' is a string id."
            )

        node_id = node

        # Clean the properties - remove None values and handle special types
        clean_properties: Dict[str, Any] = {"id": node_id}
        for key, value in properties.items():
            if value is not None:
                if isinstance(value, UUID):
                    clean_properties[key] = str(value)
                elif isinstance(value, dict):
                    clean_properties[key] = json.dumps(value)
                else:
                    clean_properties[key] = value

        node_label = properties.get("type") or "Node"
        node_label = self.sanitize_relationship_name(str(node_label))

        query = dedent(f"""
            MERGE (node:{node_label} {{id: $node_id}})
            SET node += $properties, node.updated_at = timestamp()
            """).strip()

        node_metadata = properties.get("metadata")
        if isinstance(node_metadata, str):
            try:
                node_metadata = json.loads(node_metadata)
            except (ValueError, TypeError):
                node_metadata = None
        index_fields = (node_metadata or {}).get("index_fields", [])
        for field in index_fields:
            vector_key = f"{field}_vector"
            if vector_key in properties and properties[vector_key] is not None:
                query = query + f", node.{vector_key} = vecf32({properties[vector_key]})"

        params = {"node_id": node_id, "properties": clean_properties}

        await self.query(query, params)

    # Helper methods for DataPoint compatibility
    async def add_data_point_node(self, node: DataPoint) -> None:
        """
        Add a single data point as a node in the graph.

        Parameters:
        -----------

            - node (DataPoint): An instance of DataPoint to be added to the graph.
        """
        await self.create_data_points("", [node])

    async def add_data_point_nodes(self, nodes: list[DataPoint]) -> None:
        """
        Add multiple data points as nodes in the graph.

        Parameters:
        -----------

            - nodes (list[DataPoint]): A list of DataPoint instances to be added to the graph.
        """
        await self.create_data_points("", nodes)

    async def add_nodes(
        self,
        nodes: list[Node] | list[DataPoint],
        source_ref_key: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
    ) -> None:
        """
        Add multiple nodes to the graph in a single operation.

        Collects all embeddable property values across all DataPoint nodes and
        makes a single ``embed_data()`` call instead of one per node.  Individual
        ``add_node()`` MERGE queries are still issued because FalkorDB's
        ``vecf32()`` function requires literal float-list values in the Cypher
        query string (UNWIND parameterisation is not possible).

        Parameters:
        -----------

            - nodes (Union[List[Node], List[DataPoint]]): A list of Node tuples
                                or DataPoint objects to be added to the graph.
            - source_ref_key (Optional[str]): Graph-provenance key passed by
                cognee 1.3.0's ``add_data_points`` write path.
            - pipeline_run_id (Optional[str]): Graph-provenance run id passed by
                the same path.

        The two provenance kwargs are part of the ``GraphDBInterface`` contract
        in cognee 1.3.0; accepting them avoids the ``TypeError`` cognify raised
        against the previous ``add_nodes(self, nodes)`` signature. They are
        intentionally accepted-and-ignored: on FalkorDB the values are always
        ``None`` today (``mark_graph_provenance_if_empty`` is unsupported), so
        ignoring them is lossless. Stamping provenance is left for a follow-up.
        """
        if not nodes:
            return

        # Phase 1 — collect embeddable values across *all* DataPoint nodes.
        all_embeddable: list[str] = []
        datapoint_nodes: list[tuple] = []  # (node, property_names, vector_map)

        for node in nodes:
            if isinstance(node, tuple) and len(node) == 2:
                node_id, properties = node
                await self.add_node(node_id, properties)
                continue
            if not (hasattr(node, "id") and hasattr(node, "model_dump")):
                raise ValueError(
                    f"Invalid node format: {node}. Expected tuple (node_id, properties)"
                    f"or DataPoint object."
                )
            property_names = DataPoint.get_embeddable_property_names(node)  # type: ignore
            vector_map: dict[str, int] = {}
            for property_name in property_names:
                property_value = getattr(node, property_name, None)
                if property_value is not None:
                    vector_map[property_name] = len(all_embeddable)
                    all_embeddable.append(property_value)
            datapoint_nodes.append((node, property_names, vector_map))

        # Phase 2 — single batch embedding call.
        all_vectors: list[list[float]] = []
        if all_embeddable:
            all_vectors = await self.embed_data(all_embeddable)

        # Phase 3 — build properties and persist each node.
        for node, property_names, vector_map in datapoint_nodes:
            properties = {
                **node.model_dump(),
                **{
                    f"{property_name}_vector": (
                        all_vectors[vector_map[property_name]]
                        if property_name in vector_map
                        else []
                    )
                    for property_name in property_names
                },
            }
            await self.add_node(str(node.id), properties)

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        relationship_name: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """
        Create a new edge between two nodes in the graph.

        Parameters:
        -----------

            - source_id (str): The unique identifier of the source node.
            - target_id (str): The unique identifier of the target node.
            - relationship_name (str): The name of the relationship to be established by the
              edge.
            - properties (Optional[Dict[str, Any]]): Optional dictionary of properties
              associated with the edge. (default None)
        """
        if properties is None:
            properties = {}

        edge_tuple = (source_id, target_id, relationship_name, properties)
        query = await self.create_edge_query(edge_tuple)
        await self.query(query)

    async def add_edges(
        self,
        edges: list[EdgeData],
        source_ref_key: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
    ) -> None:
        """
        Add multiple edges to the graph in a single operation.

        Groups edges by sanitised relationship type and issues one UNWIND MERGE
        query per type instead of one query per edge.  FalkorDB requires a
        static relationship-type label in Cypher (no variable type names), so
        grouping is necessary.

        Parameters:
        -----------

            - edges (List[EdgeData]): A list of EdgeData objects representing edges to be added.
            - source_ref_key (Optional[str]): Graph-provenance key passed by
                cognee 1.3.0's ``add_data_points`` write path.
            - pipeline_run_id (Optional[str]): Graph-provenance run id passed by
                the same path.

        As with :meth:`add_nodes`, the provenance kwargs are part of the
        cognee 1.3.0 ``GraphDBInterface`` contract and are accepted-and-ignored
        (always ``None`` on FalkorDB today) to avoid a ``TypeError`` on cognify.
        """
        if not edges:
            return

        def _flatten_value(v: Any) -> Any:
            """Convert values to FalkorDB-compatible scalar types."""
            if isinstance(v, UUID):
                return str(v)
            if isinstance(v, (dict, list)):
                return json.dumps(v)
            if isinstance(v, (str, int, float, bool)) or v is None:
                return v
            return str(v)

        grouped: dict[str, list] = defaultdict(list)
        for edge in edges:
            if not (isinstance(edge, tuple) and len(edge) == 4):
                raise ValueError(
                    f"Invalid edge format: {edge}. Expected tuple (source_id, target_id,"
                    f"relationship_name, properties)."
                )
            rel_type = self.sanitize_relationship_name(edge[2])
            grouped[rel_type].append(edge)

        for rel_type, group in grouped.items():
            params_list = [
                {
                    "source_id": str(e[0]),
                    "target_id": str(e[1]),
                    "props": {
                        k: _flatten_value(v)
                        for k, v in {**e[3], "relationship_name": e[2]}.items()
                        if v is not None
                    },
                }
                for e in group
            ]
            await self.query(
                f"""
                UNWIND $items AS e
                MERGE (source {{id: e.source_id}})
                MERGE (target {{id: e.target_id}})
                MERGE (source)-[r:{rel_type}]->(target)
                SET r += e.props, r.updated_at = timestamp()
                """,
                {"items": params_list},
            )

    async def has_edges(self, edges: list[EdgeData]) -> list[EdgeData]:
        """
        Check if the specified edges exist in the graph based on their attributes.

        Groups edges by sanitised relationship type and issues one UNWIND query
        per type instead of one MATCH per edge.

        Parameters:
        -----------

            - edges: A list of edges to check for existence in the graph.

        Returns:
        --------

            Returns a list of edge tuples that exist in the graph.
        """
        if not edges:
            return []

        grouped: dict[str, list] = defaultdict(list)
        for edge in edges:
            rel_type = self.sanitize_relationship_name(edge[2])
            grouped[rel_type].append(edge)

        existing_edges: list[EdgeData] = []
        for rel_type, group in grouped.items():
            params_list = [
                {
                    "source_id": str(e[0]),
                    "target_id": str(e[1]),
                    "rel_name": e[2],
                }
                for e in group
            ]
            result = await self.query(
                f"""
                UNWIND $items AS e
                OPTIONAL MATCH (source)-[r:{rel_type}]->(target)
                WHERE source.id = e.source_id AND target.id = e.target_id
                AND (r.relationship_name = e.rel_name
                     OR NOT EXISTS(r.relationship_name))
                RETURN e.source_id, e.target_id, e.rel_name,
                       r IS NOT NULL AS edge_exists
                """,
                {"items": params_list},
            )
            for i, row in enumerate(result.result_set):
                if row[3]:  # edge_exists
                    existing_edges.append(group[i])
        return existing_edges

    async def retrieve(self, collection_name: str, data_point_ids: list[str]):
        """
        Retrieve data points from the graph based on their IDs.

        Parameters:
        -----------

            - data_point_ids (list[UUID]): A list of UUIDs representing the data points to
              retrieve.

        Returns:
        --------

            Returns the result set containing the retrieved nodes or an empty list if not found.
        """
        result = await self.query(
            "MATCH (node) WHERE node.id IN $node_ids RETURN node",
            {
                "node_ids": [str(data_point) for data_point in data_point_ids],
            },
        )
        return result.result_set  # type: ignore

    async def extract_node(self, data_point_id: UUID) -> NodeData:
        """
        Extract the properties of a single node identified by its data point ID.

        Parameters:
        -----------

            - data_point_id (UUID): The UUID of the data point to extract.

        Returns:
        --------

            Returns the properties of the node if found, otherwise None.
        """
        result = await self.retrieve([data_point_id])
        if not result[0]:
            return None

        node = result[0][0]
        if not node:
            return None

        return node.properties

    async def extract_nodes(self, data_point_ids: list[UUID]) -> list[NodeData]:
        """
        Extract properties of multiple nodes identified by their data point IDs.

        Parameters:
        -----------

            - data_point_ids (list[UUID]): A list of UUIDs representing the data points to
              extract.

        Returns:
        --------

            Returns the properties of the nodes in a list.
        """
        return await self.retrieve(data_point_ids)

    async def get_connections(self, node_id: UUID) -> list:
        """
        Retrieve connection details (predecessors and successors) for a given node ID.

        Parameters:
        -----------

            - node_id (UUID): The UUID of the node whose connections are to be retrieved.

        Returns:
        --------

            - list: Returns a list of tuples representing the connections of the node.
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

        predecessors = await self.query(predecessors_query, dict(node_id=node_id))
        successors = await self.query(successors_query, dict(node_id=node_id))

        connections = []

        for neighbour in predecessors.result_set:
            connections.append(
                (
                    neighbour[0].properties,
                    {"relationship_name": neighbour[1].properties},
                    neighbour[2].properties,
                )
            )

        for neighbour in successors.result_set:
            connections.append(
                (
                    neighbour[0].properties,
                    {"relationship_name": neighbour[1].properties["relationship_name"]},
                    neighbour[2].properties,
                )
            )

        return connections

    async def search(
        self,
        collection_name: str,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        limit: int | None = None,
        with_vector: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
    ) -> list:
        """
        Search for nodes in a collection based on text or vector query, with optional limitation
        on results.

        Parameters:
        -----------

            - collection_name (str): The name of the collection in which to search.
            - query_text (str): The text to search for (if using text-based query). (default
              None)
            - query_vector (list[float]): The vector representation of the query if using
              vector-based search. (default None)
            - limit (int): Maximum number of results to return from the search. (default 10)
            - with_vector (bool): Flag indicating whether to return vectors with the search
              results. (default False)
            - include_payload (bool): Flag indicating whether to include payload (default False)

        Returns:
        --------

            Returns the search results as a result set from the graph database.
        """
        if query_text is None and query_vector is None:
            raise MissingQueryParameterError()

        if query_text and not query_vector:
            query_vector = (await self.embed_data([query_text]))[0]

        if "_" in collection_name:
            label, _, attribute_name = collection_name.partition("_")
        else:
            # If no dot, treat the whole thing as a property search
            label = ""
            attribute_name = collection_name

        graph = self.driver.select_graph(self.graph_name)
        if not self.has_vector_index(graph, label, f"{attribute_name}_vector"):
            raise CollectionNotFoundError(f"No vector index found for collection {collection_name}")

        if limit is None:
            query = f"MATCH (n:{label}) RETURN COUNT(n)"
            result = await self.query(query)
            limit = result.result_set[0][0]

        if limit == 0:
            return []

        if include_payload:
            result_properties = ["node"]
        else:
            result_properties = ["node.id"]
            if with_vector:
                result_properties.append(f"node.{attribute_name}_vector")

        filters = ""
        if node_name:
            if node_name_filter_operator == "OR":
                filters = "any(x IN $node_names WHERE x IN coalesce(node.belongs_to_set, []))"
            else:
                filters = "all(x IN $node_names WHERE x IN coalesce(node.belongs_to_set, []))"

        where_clause = f"\n            WHERE {filters}" if filters else ""

        query = dedent(f"""
            CALL db.idx.vector.queryNodes(
                "{label}",
                "{attribute_name}_vector",
                {limit},
                vecf32({query_vector})
            )
            YIELD node, score{where_clause}
            RETURN {", ".join(result_properties)}, score
        """).strip()

        params = {"node_names": node_name} if node_name else {}

        search_results = await self.query(query, params)

        # Convert results to ScoredResult objects
        scored_results = []
        for result in search_results.result_set:
            payload_data = result[0].properties if include_payload else {}
            if "name" in payload_data:
                payload_data["text"] = payload_data["name"]

            if not include_payload:
                res_id = result[0]
                vector = result[1] if with_vector else None
                score = result[2] if with_vector else result[1]
            else:
                res_id = payload_data["id"]
                vector = payload_data[f"{attribute_name}_vector"] if with_vector else None
                score = result[1]

            scored_result = ScoredResult(
                id=parse_id(res_id),
                score=score,
                payload=payload_data,
                vector=vector,
            )
            scored_results.append(scored_result)

        return scored_results

    async def batch_search(
        self,
        collection_name: str,
        query_texts: list[str],
        limit: int | None = None,
        with_vectors: bool = False,
        include_payload: bool = False,
        node_name: Optional[List[str]] = None,
        node_name_filter_operator: str = "OR",
    ) -> list:
        """
        Perform batch search across multiple queries based on text inputs and return results
        asynchronously.

        Parameters:
        -----------

            - collection_name (str): The name of the collection in which to perform the
              searches.
            - query_texts (list[str]): A list of text queries to search for.
            - limit (int): Optional limit for the search results for each query. (default None)
            - with_vectors (bool): Flag indicating whether to return vectors with the results.
              (default False)
            - include_payload (bool): Flag indicating whether to include payload for each query.
              (default False)

        Returns:
        --------

            Returns a list of results for each search query executed in parallel.
        """

        query_vectors = await self.embedding_engine.embed_text(query_texts)

        results: list[list] = await asyncio.gather(
            *[
                self.search(
                    collection_name=collection_name,
                    query_vector=query_vector,
                    limit=limit,
                    with_vector=with_vectors,
                    include_payload=include_payload,
                    node_name=node_name,
                    node_name_filter_operator=node_name_filter_operator,
                )
                for query_vector in query_vectors
            ]
        )
        return results

    @staticmethod
    def _deserialize_node_properties(properties: Dict[str, Any]) -> Dict[str, Any]:
        """Restore dict-valued node properties that the write path JSON-encoded.

        FalkorDB stores only primitive property values, so ``add_node`` and
        ``_coerce_stored_value`` ``json.dumps`` any dict-valued property (notably
        ``metadata``) before persisting it. cognee core, however, treats
        ``metadata`` as a dict — e.g. ``DataPoint.get_embeddable_property_names``
        does ``metadata["index_fields"]``. Any code that reads a node back and
        feeds it to core therefore needs ``metadata`` as a dict again; the cognee
        1.2.0 ``namespace_entity_type_node_ids`` graph migration is the first such
        path (it round-trips ``get_graph_data`` output through ``add_nodes``) and
        otherwise raises ``TypeError: string indices must be integers``.

        Scoped to ``metadata`` to avoid mangling legitimate string properties
        (e.g. ``name``/``text``) that may happen to look like JSON. Non-string or
        malformed values are left untouched, so this is safe and idempotent.
        """
        metadata = properties.get("metadata")
        if isinstance(metadata, str):
            try:
                properties["metadata"] = json.loads(metadata)
            except (ValueError, TypeError):
                pass
        return properties

    async def get_graph_data(
        self,
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Tuple[str, str, str, Dict[str, Any]]]]:
        """
        Retrieve all nodes and edges from the graph along with their properties.

        Returns:
        --------

            Returns a tuple containing lists of nodes and edges data retrieved from the graph.
        """
        query = "MATCH (n) RETURN ID(n) AS id, labels(n) AS labels, properties(n) AS properties"

        result = await self.query(query)

        nodes = [
            (
                record[2]["id"],
                self._deserialize_node_properties(record[2]),
            )
            for record in result.result_set
        ]

        query = """
        MATCH (n)-[r]->(m)
        RETURN ID(n) AS source, ID(m) AS target, TYPE(r) AS type, properties(r) AS properties
        """
        result = await self.query(query)
        edges = [
            (
                record[3]["source_node_id"],
                record[3]["target_node_id"],
                record[2],
                record[3],
            )
            for record in result.result_set
        ]

        return (nodes, edges)

    async def delete_data_points(
        self, collection_name: str, data_point_ids: list[UUID]
    ) -> QueryResult:
        """
        Remove specified data points from the graph database based on their IDs.

        Parameters:
        -----------

            - collection_name (str): The name of the collection from which to delete the data
              points.
            - data_point_ids (list[UUID]): A list of UUIDs representing the data points to
              delete.

        Returns:
        --------

            Returns the result of the deletion operation from the database.
        """
        return await self.query(
            "MATCH (node) WHERE node.id IN $node_ids DETACH DELETE node",
            {
                "node_ids": [str(data_point) for data_point in data_point_ids],
            },
        )

    async def remove_belongs_to_set_tags(
        self,
        tags: List[str],
        node_ids: Optional[List[str]] = None,
    ) -> None:
        """
        Strip the given scope tags from every node's ``belongs_to_set`` array and
        delete nodes whose ``belongs_to_set`` becomes empty as a result.

        Implements the ``VectorDBInterface`` contract that cognee's
        ``delete_session_qa_vectors`` relies on. Without this override the adapter
        inherited the base-class no-op, so session QA vectors on FalkorDB were
        never cleaned up and accumulated indefinitely.

        Parameters:
        -----------

            - tags (List[str]): Scope tags to remove from ``belongs_to_set``.
            - node_ids (Optional[List[str]]): When provided, scope the detag to
              rows whose id is in the list (reconciles shared rows that lose one
              tag); otherwise every matching node is considered.
        """
        if not tags:
            return

        id_filter = ""
        params: dict[str, Any] = {"tags": tags}
        if node_ids is not None:
            id_filter = " AND n.id IN $node_ids"
            params["node_ids"] = node_ids

        # Match only nodes actually carrying one of the tags (so pre-existing
        # empty sets are untouched), strip the tags, then delete the ones left
        # empty by this operation — in a single pass so the delete stays scoped
        # to affected nodes.
        await self.query(
            f"""
            MATCH (n)
            WHERE any(t IN $tags WHERE t IN coalesce(n.belongs_to_set, [])){id_filter}
            SET n.belongs_to_set =
                [x IN coalesce(n.belongs_to_set, []) WHERE NOT x IN $tags]
            WITH n
            WHERE size(coalesce(n.belongs_to_set, [])) = 0
            DETACH DELETE n
            """,
            params,
        )

    async def delete_node(self, node_id: str) -> None:
        """
        Delete a specified node from the graph by its ID.

        Parameters:
        -----------

            - node_id (str): Unique identifier for the node to delete.
        """
        query = f"MATCH (node {{id: '{node_id}'}}) DETACH DELETE node"
        await self.query(query)

    async def delete_nodes(self, node_ids: list[str]) -> None:
        """
        Delete multiple nodes from the graph by their identifiers.

        Parameters:
        -----------

            - node_ids (List[str]): A list of unique identifiers for the nodes to delete.
        """
        for node_id in node_ids:
            await self.delete_node(node_id)

    async def delete_graph(self) -> None:
        """
        Delete the entire graph along with all its indices and nodes.
        """
        if self.graph_name not in self.driver.list_graphs():
            return

        try:
            graph = self.driver.select_graph(self.graph_name)

            indices = graph.list_indices()
            for index in indices.result_set:
                for field in index[1]:
                    graph.drop_node_vector_index(index[0], field)

            graph.delete()
        except Exception as e:
            print(f"Error deleting graph: {e}")

    async def get_node(self, node_id: str) -> NodeData | None:
        """
        Retrieve a single node from the graph using its ID.

        Parameters:
        -----------

            - node_id (str): Unique identifier of the node to retrieve.
        """
        result = await self.query(
            "MATCH (node) WHERE node.id = $node_id RETURN node",
            {"node_id": node_id},
        )

        if result.result_set and len(result.result_set) > 0:
            # FalkorDB returns node objects as first element in the result list
            return result.result_set[0][0].properties
        return None

    async def get_nodes(self, node_ids: list[str]) -> list[NodeData]:
        """
        Retrieve multiple nodes from the graph using their IDs.

        Parameters:
        -----------

            - node_ids (List[str]): A list of unique identifiers for the nodes to retrieve.
        """
        result = await self.query(
            "MATCH (node) WHERE node.id IN $node_ids RETURN node",
            {"node_ids": node_ids},
        )

        nodes = []
        if result.result_set:
            for record in result.result_set:
                # FalkorDB returns node objects as first element in each record
                nodes.append(record[0].properties)
        return nodes

    async def get_neighbors(self, node_id: str) -> list[NodeData]:
        """
        Get all neighboring nodes connected to the specified node.

        Parameters:
        -----------

            - node_id (str): Unique identifier of the node for which to retrieve neighbors.
        """
        result = await self.query(
            "MATCH (node)-[]-(neighbor) WHERE node.id = $node_id RETURN DISTINCT neighbor",
            {"node_id": node_id},
        )

        neighbors = []
        if result.result_set:
            for record in result.result_set:
                # FalkorDB returns neighbor objects as first element in each record
                neighbors.append(record[0].properties)
        return neighbors

    async def get_edges(self, node_id: str) -> list[EdgeData]:
        """
        Retrieve all edges that are connected to the specified node.

        Parameters:
        -----------

            - node_id (str): Unique identifier of the node whose edges are to be retrieved.
        """
        result = await self.query(
            """
            MATCH (n)-[r]-(m)
            WHERE n.id = $node_id
            RETURN n.id AS source_id, m.id AS target_id, type(r) AS relationship_name,
            properties(r) AS properties
            """,
            {"node_id": node_id},
        )

        edges = []
        if result.result_set:
            for record in result.result_set:
                # FalkorDB returns values by index: source_id, target_id,
                # relationship_name, properties
                edges.append(
                    (
                        record[0],  # source_id
                        record[1],  # target_id
                        record[2],  # relationship_name
                        record[3],  # properties
                    )
                )
        return edges

    async def has_edge(self, source_id: str, target_id: str, relationship_name: str) -> bool:
        """
        Verify if an edge exists between two specified nodes.

        Parameters:
        -----------

            - source_id (str): Unique identifier of the source node.
            - target_id (str): Unique identifier of the target node.
            - relationship_name (str): Name of the relationship to verify.
        """
        # Check both the sanitized relationship type and the original name in properties
        sanitized_relationship = self.sanitize_relationship_name(relationship_name)

        result = await self.query(
            f"""
            MATCH (source)-[r:{sanitized_relationship}]->(target)
            WHERE source.id = $source_id AND target.id = $target_id
            AND (r.relationship_name = $relationship_name OR NOT EXISTS(r.relationship_name))
            RETURN COUNT(r) > 0 AS edge_exists
            """,
            {
                "source_id": source_id,
                "target_id": target_id,
                "relationship_name": relationship_name,
            },
        )

        if result.result_set and len(result.result_set) > 0:
            # FalkorDB returns scalar results as a list, access by index instead of key
            return result.result_set[0][0]  # type: ignore
        return False

    async def get_graph_metrics(self, include_optional: bool = False) -> dict[str, Any]:
        """
        Fetch metrics and statistics of the graph, possibly including optional details.

        Parameters:
        -----------

            - include_optional (bool): Flag indicating whether to include optional metrics or
              not. (default False)
        """
        # Get basic node and edge counts
        node_result = await self.query("MATCH (n) RETURN count(n) AS node_count")
        edge_result = await self.query("MATCH ()-[r]->() RETURN count(r) AS edge_count")

        # FalkorDB returns scalar results as a list, access by index instead of key
        num_nodes = node_result.result_set[0][0] if node_result.result_set else 0
        num_edges = edge_result.result_set[0][0] if edge_result.result_set else 0

        metrics = {
            "num_nodes": num_nodes,
            "num_edges": num_edges,
            "mean_degree": (2 * num_edges) / num_nodes if num_nodes > 0 else 0,
            "edge_density": num_edges / (num_nodes * (num_nodes - 1)) if num_nodes > 1 else 0,
            "num_connected_components": 1,  # Simplified for now
            "sizes_of_connected_components": [num_nodes] if num_nodes > 0 else [],
        }

        if include_optional:
            # Add optional metrics - simplified implementation
            metrics.update(
                {
                    "num_selfloops": 0,  # Simplified
                    "diameter": -1,  # Not implemented
                    "avg_shortest_path_length": -1,  # Not implemented
                    "avg_clustering": -1,  # Not implemented
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

    async def get_document_subgraph(self, content_hash: str) -> dict:
        """
        Get a subgraph related to a specific document by content hash.

        Parameters:
        -----------

            - content_hash (str): The content hash of the document to find.
        """
        query = """
        MATCH (d) WHERE d.id CONTAINS $content_hash
        OPTIONAL MATCH (d)<-[:CHUNK_OF]-(c)
        OPTIONAL MATCH (c)-[:HAS_ENTITY]->(e)
        OPTIONAL MATCH (e)-[:IS_INSTANCE_OF]->(et)
        RETURN d AS document,
               COLLECT(DISTINCT c) AS chunks,
               COLLECT(DISTINCT e) AS orphan_entities,
               COLLECT(DISTINCT c) AS made_from_nodes,
               COLLECT(DISTINCT et) AS orphan_types
        """

        result = await self.query(query, {"content_hash": f"{content_hash}"})

        if not result.result_set or not result.result_set[0]:
            return {}

        # Convert result to dictionary format
        # FalkorDB returns values by index: document, chunks, orphan_entities,
        # made_from_nodes, orphan_types
        record = result.result_set[0]
        return {
            "document": record[0],
            "chunks": record[1],
            "orphan_entities": record[2],
            "made_from_nodes": record[3],
            "orphan_types": record[4],
        }

    async def get_degree_one_nodes(self, node_type: str) -> list:
        """
        Get all nodes that have only one connection.

        Parameters:
        -----------

            - node_type (str): The type of nodes to filter by, must be 'Entity' or 'EntityType'.
        """
        if not node_type or node_type not in ["Entity", "EntityType"]:
            raise ValueError("node_type must be either 'Entity' or 'EntityType'")

        result = await self.query(
            f"""
            MATCH (n:{node_type})
            WITH n, COUNT {{ MATCH (n)--() }} as degree
            WHERE degree = 1
            RETURN n
            """
        )

        # FalkorDB returns node objects as first element in each record
        return [record[0] for record in result.result_set] if result.result_set else []

    async def get_nodeset_subgraph(
        self, node_type: type[Any], node_name: list[str], node_name_filter_operator: str = "OR"
    ) -> tuple[list[tuple[int, dict]], list[tuple[int, int, str, dict]]]:
        """
        Fetch a subgraph consisting of a specific set of nodes and their relationships.

        Parameters:
        -----------

            - node_type (Type[Any]): The type of nodes to include in the subgraph.
            - node_name (List[str]): A list of names of the nodes to include in the subgraph.
        """
        label = node_type.__name__

        # Find primary nodes of the specified type and names
        primary_query = f"""
        UNWIND $names AS wantedName
        MATCH (n:{label})
        WHERE n.name = wantedName
        RETURN DISTINCT n.id, properties(n) AS properties
        """

        primary_result = await self.query(primary_query, {"names": node_name})
        if not primary_result.result_set:
            return [], []

        # FalkorDB returns values by index: id, properties
        primary_ids = [record[0] for record in primary_result.result_set]

        if node_name_filter_operator == "OR":
            neighbor_query = """
                MATCH (n)-[]-(nbr)
                WHERE n.id IN $ids
                RETURN DISTINCT nbr.id, properties(nbr) AS properties
            """
            params = {"ids": primary_ids}
        else:
            neighbor_query = """
                MATCH (n)-[]-(nbr)
                WHERE n.id IN $ids
                WITH nbr, COUNT(DISTINCT n.id) AS matched_count
                WHERE matched_count = $primary_count
                RETURN nbr.id AS nbr_id, properties(nbr) AS properties
             """
            params = {"ids": primary_ids, "primary_count": len(primary_ids)}

        neighbor_result = await self.query(neighbor_query, params)
        # FalkorDB returns values by index: id, properties
        neighbor_ids = (
            [record[0] for record in neighbor_result.result_set]
            if neighbor_result.result_set
            else []
        )

        all_ids = list(set(primary_ids + neighbor_ids))

        # Get all nodes in the subgraph
        nodes_query = """
        MATCH (n)
        WHERE n.id IN $ids
        RETURN n.id, properties(n) AS properties
        """

        nodes_result = await self.query(nodes_query, {"ids": all_ids})
        nodes = []
        if nodes_result.result_set:
            for record in nodes_result.result_set:
                # FalkorDB returns values by index: id, properties
                nodes.append((record[0], record[1]))

        # Get edges between these nodes
        edges_query = """
        MATCH (a)-[r]->(b)
        WHERE a.id IN $ids AND b.id IN $ids
        RETURN a.id AS source_id, b.id AS target_id, type(r) AS relationship_name,
        properties(r) AS properties
        """

        edges_result = await self.query(edges_query, {"ids": all_ids})
        edges = []
        if edges_result.result_set:
            for record in edges_result.result_set:
                # FalkorDB returns values by index: source_id, target_id,
                # relationship_name, properties
                edges.append(
                    (
                        record[0],  # source_id
                        record[1],  # target_id
                        record[2],  # relationship_name
                        record[3],  # properties
                    )
                )

        return nodes, edges

    async def get_filtered_graph_data(
        self, attribute_filters: list[dict[str, list[str | int]]]
    ) -> tuple[list[tuple[str, dict]], list[tuple[str, str, str, dict]]]:
        """
        Fetch nodes and edges filtered by attribute criteria.

        Returns:
            (
                [(node_id, node_properties), ...],
                [(source_id, target_id, relationship_type, edge_properties), ...]
            )
        """
        # If no filters were provided, return full graph
        if not attribute_filters:
            return await self.get_graph_data()

        where_clauses_n: list[str] = []
        where_clauses_m: list[str] = []
        params: dict[str, list[str | int]] = {}

        # Build parameterized IN predicates (safe and consistent with other adapters)
        for i, filter_dict in enumerate(attribute_filters):
            for attr, values in filter_dict.items():
                if not values:
                    continue

                param_name = f"values_{i}_{attr}"
                normalized_values = [str(v) if isinstance(v, UUID) else v for v in values]

                where_clauses_n.append(f"n.{attr} IN ${param_name}")
                where_clauses_m.append(f"m.{attr} IN ${param_name}")
                params[param_name] = normalized_values

        # If filters resolved to nothing (e.g. only empty lists), return full graph
        if not where_clauses_n:
            return await self.get_graph_data()

        node_where_clause = " AND ".join(where_clauses_n)
        edge_where_clause = f"{' AND '.join(where_clauses_n)} AND {' AND '.join(where_clauses_m)}"

        query_nodes = f"""
        MATCH (n)
        WHERE {node_where_clause}
        RETURN n.id AS id, properties(n) AS properties
        """

        query_edges = f"""
        MATCH (n)-[r]->(m)
        WHERE {edge_where_clause}
        RETURN n.id AS source, m.id AS target, TYPE(r) AS type, properties(r) AS properties
        """

        result_nodes, result_edges = await asyncio.gather(
            self.query(query_nodes, params),
            self.query(query_edges, params),
        )

        nodes: list[tuple[str, dict]] = []
        for record in result_nodes.result_set or []:
            node_id = str(record[0])
            node_properties = record[1] if isinstance(record[1], dict) else {}
            nodes.append((node_id, node_properties))

        edges: list[tuple[str, str, str, dict]] = []
        for record in result_edges.result_set or []:
            source_id = str(record[0])
            target_id = str(record[1])
            rel_type = str(record[2])
            properties = record[3] if isinstance(record[3], dict) else {}

            # Keep compatibility with adapters that prefer explicit source/target in edge props
            source_id = str(properties.get("source_node_id", source_id))
            target_id = str(properties.get("target_node_id", target_id))

            edges.append((source_id, target_id, rel_type, properties))

        return nodes, edges

    async def get_neighborhood(
        self,
        node_ids: List[str],
        depth: int = 1,
        edge_types: Optional[List[str]] = None,
    ) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Tuple[str, str, str, Dict[str, Any]]]]:
        """
        Get the k-hop neighborhood subgraph around a set of seed nodes.

        Returns all nodes and edges within ``depth`` hops of any seed node, in the same
        format as ``get_graph_data()``. When ``edge_types`` is provided traversal is
        restricted to edges whose relationship type matches one of the supplied values.
        """
        if not node_ids:
            return [], []

        try:
            depth = int(depth)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"depth must be an integer, got {depth!r}") from exc
        depth = max(1, depth)

        # FalkorDB Cypher does not allow parameterising relationship types or path
        # lengths, so they are interpolated. ``depth`` is coerced to int and edge type
        # names are run through the existing identifier sanitiser to keep the query safe.
        rel_filter = ""
        if edge_types:
            sanitized = "|".join(self.sanitize_relationship_name(t) for t in edge_types)
            rel_filter = f":{sanitized}"

        path_query = (
            f"MATCH (seed)-[r{rel_filter}*1..{int(depth)}]-(neighbor) "
            "WHERE seed.id IN $node_ids "
            "RETURN DISTINCT neighbor.id AS id"
        )
        neighbor_result = await self.query(path_query, {"node_ids": node_ids})
        neighbor_ids = [record[0] for record in neighbor_result.result_set if record[0]]

        all_ids = list({*node_ids, *neighbor_ids})

        nodes_result = await self.query(
            "MATCH (n) WHERE n.id IN $ids RETURN n",
            {"ids": all_ids},
        )
        nodes: List[Tuple[str, Dict[str, Any]]] = []
        for record in nodes_result.result_set:
            props = record[0].properties or {}
            node_id = str(props.get("id", ""))
            if node_id:
                nodes.append((node_id, props))

        if not nodes:
            return [], []

        edges_result = await self.query(
            """
            MATCH (n)-[r]->(m)
            WHERE n.id IN $ids AND m.id IN $ids
            RETURN n.id AS source, m.id AS target, type(r) AS rel, properties(r) AS props
            """,
            {"ids": all_ids},
        )
        edges: List[Tuple[str, str, str, Dict[str, Any]]] = []
        for record in edges_result.result_set:
            source_id = str(record[0])
            target_id = str(record[1])
            rel_type = str(record[2])
            props = record[3] if isinstance(record[3], dict) else {}
            source_id = str(props.get("source_node_id", source_id))
            target_id = str(props.get("target_node_id", target_id))
            edges.append((source_id, target_id, rel_type, props))

        return nodes, edges

    async def get_triplets_batch(self, offset: int, limit: int) -> List[Dict[str, Any]]:
        """
        Retrieve a batch of (source, relationship, target) triplets from the graph.

        Returns dicts shaped like the Postgres / Kuzu adapters so cognee 1.0 callers
        (e.g. ``cognee.tasks.memify.get_triplet_datapoints``) can consume the result:

        ``{"start_node": {...}, "relationship_properties": {...}, "end_node": {...}}``
        """
        if offset < 0:
            raise ValueError(f"Offset must be non-negative, got {offset}")
        if limit < 0:
            raise ValueError(f"Limit must be non-negative, got {limit}")
        if limit == 0:
            return []

        query = """
        MATCH (n)-[r]->(m)
        RETURN n AS source, type(r) AS rel_type, properties(r) AS edge_props, m AS target
        ORDER BY n.id, m.id, type(r)
        SKIP $offset LIMIT $limit
        """
        result = await self.query(query, {"offset": int(offset), "limit": int(limit)})

        triplets: List[Dict[str, Any]] = []
        for record in result.result_set:
            start_node = dict(record[0].properties) if record[0] is not None else {}
            end_node = dict(record[3].properties) if record[3] is not None else {}
            edge_props = record[2] if isinstance(record[2], dict) else {}

            relationship_name = edge_props.get("relationship_name", str(record[1]))
            relationship_properties: Dict[str, Any] = {
                **edge_props,
                "relationship_name": relationship_name,
                "source_node_id": str(edge_props.get("source_node_id", start_node.get("id", ""))),
                "target_node_id": str(edge_props.get("target_node_id", end_node.get("id", ""))),
            }

            triplets.append(
                {
                    "start_node": start_node,
                    "relationship_properties": relationship_properties,
                    "end_node": end_node,
                }
            )

        return triplets

    async def prune(self) -> None:
        """
        Prune the graph by deleting the entire graph structure.
        """
        await self.delete_graph()

    async def is_empty(self) -> bool:
        query = "MATCH (n) RETURN true LIMIT 1;"
        result = await self.query(query)
        return not result.result_set


if TYPE_CHECKING:
    _a: GraphDBInterface = FalkorDBAdapter("", 0, None)
    _b: VectorDBInterface = FalkorDBAdapter("", 0, None)
