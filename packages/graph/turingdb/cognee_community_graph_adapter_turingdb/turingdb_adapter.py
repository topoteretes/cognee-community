import json
import re
from typing import Any, Dict, List, Optional, Tuple, Type, Union
from uuid import UUID

from cognee.infrastructure.databases.graph.graph_db_interface import (
    EdgeData,
    GraphDBInterface,
    Node,
    NodeData,
)
from cognee.infrastructure.engine import DataPoint
from cognee.modules.storage.utils import JSONEncoder
from turingdb import TuringDB


class TuringDBAdapter(GraphDBInterface):
    DEFAULT_NODE_LABEL = "Node"
    PROPERTIES_JSON_KEY = "properties_json"

    _WRITE_QUERY_PATTERN = re.compile(
        r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP)\b", re.IGNORECASE
    )
    _CHANGE_WRITE_PATTERN = re.compile(r"\bCHANGE\s+(NEW|SUBMIT|DELETE)\b", re.IGNORECASE)

    def __init__(
        self,
        graph_database_url: str = None,
        database_name: str = None,
        **kwargs,
    ):
        self.graph_database_url = graph_database_url
        self.database_name = database_name if database_name else "default"
        if self.graph_database_url:
            self.driver = TuringDB(host=self.graph_database_url)
        else:
            self.driver = TuringDB()

        existing_graphs = self.driver.list_available_graphs()
        if self.database_name not in existing_graphs:
            self.driver.create_graph(self.database_name)
        self.driver.set_graph(self.database_name)
        change = self.driver.new_change()
        self.driver.checkout(change=change)
        self.driver.query("CREATE (:Node {id: 'seed'})")
        self.driver.query("COMMIT")
        self.driver.query("CHANGE SUBMIT")
        self.driver.checkout()

    def _coerce_json_value(self, value: Any) -> Any:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, DataPoint):
            return {k: self._coerce_json_value(v) for k, v in value.model_dump().items()}
        if isinstance(value, dict):
            return {k: self._coerce_json_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._coerce_json_value(v) for v in value]
        return value

    def _serialize_properties(self, properties: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        serialized_properties = {}

        for property_key, property_value in properties.items():
            if property_value is None:
                continue
            if isinstance(property_value, UUID):
                serialized_properties[property_key] = str(property_value)
                continue

            if isinstance(property_value, dict):
                serialized_properties[property_key] = json.dumps(property_value, cls=JSONEncoder)
                continue
            if isinstance(property_value, str):
                if property_key == "updated_at":
                    from datetime import datetime, timezone

                    dt = datetime.strptime(property_value, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                    serialized_properties[property_key] = int(dt.timestamp() * 1000)
                else:
                    serialized_properties[property_key] = property_value
                continue

            serialized_properties[property_key] = property_value

        return serialized_properties
        # if properties is None:
        #     properties = {}
        # serialized_properties: Dict[str, Any] = {}
        #
        # for property_key, property_value in properties.items():
        #     if property_value is None:
        #         continue
        #     if isinstance(property_value, UUID):
        #         serialized_properties[property_key] = str(property_value)
        #     elif isinstance(property_value, dict) or isinstance(property_value, list):
        #         serialized_properties[property_key] = self._coerce_json_value(property_value)
        #     elif isinstance(property_value, str):
        #         if property_key == "updated_at":
        #             from datetime import datetime, timezone
        #             dt = datetime.strptime(property_value, "%Y-%m-%d %H:%M:%S").replace(
        #                 tzinfo=timezone.utc
        #             )
        #             serialized_properties[property_key] = int(dt.timestamp() * 1000)
        #         else:
        #             serialized_properties[property_key] = property_value
        #     else:
        #         serialized_properties[property_key] = property_value
        #
        # return serialized_properties

    def _build_properties_json(self, properties: Dict[str, Any]) -> str:
        return json.dumps(
            # self._coerce_json_value(properties),
            properties,
            cls=JSONEncoder,
            ensure_ascii=False,
        )

    def _format_value(self, value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (dict, list)):
            value = json.dumps(
                # self._coerce_json_value(value),
                value,
                cls=JSONEncoder,
                ensure_ascii=False,
            )
        if isinstance(value, UUID):
            value = str(value)
        if isinstance(value, str):
            value = self._cypher_escape(value)
        else:
            value = str(value)
        return f"'{value}'"

    def _apply_params(self, query: str, params: Optional[dict]) -> str:
        if not params:
            return query
        for key, value in params.items():
            query = query.replace(f"${key}", self._format_value(value))
        return query

    def _df_to_records(self, result: Any) -> List[Dict[str, Any]]:
        if result is None:
            return []
        if hasattr(result, "to_dict"):
            return result.to_dict(orient="records")
        if isinstance(result, list):
            return result
        return []

    def _is_write_query(self, query: str) -> bool:
        normalized = " ".join(query.strip().split())
        if not normalized:
            return False
        if normalized.upper().startswith("CHANGE LIST"):
            return False
        if normalized.upper().startswith("CALL "):
            return False
        if self._CHANGE_WRITE_PATTERN.search(normalized):
            return True
        return bool(self._WRITE_QUERY_PATTERN.search(normalized))

    def _run_in_change(self, queries: List[str]) -> List[Dict[str, Any]]:
        if not queries:
            return []

        change = self.driver.new_change()
        self.driver.checkout(change=change)
        try:
            results: List[Dict[str, Any]] = []
            for query in queries:
                result = self.driver.query(query=query)
                results = self._df_to_records(result)
            self.driver.query("COMMIT")
            self.driver.query("CHANGE SUBMIT")
            return results
        finally:
            self.driver.checkout()

    async def is_empty(self) -> bool:
        query = """
                MATCH (n)
                RETURN n
                LIMIT 1
                """
        query_result = await self.query(query)
        return len(query_result) == 0

    async def query(self, query: str, params: Optional[dict] = None) -> List[Any]:
        query = self._apply_params(query, params)
        if self._is_write_query(query):
            return self._run_in_change([query])

        result = self.driver.query(query=query)
        return self._df_to_records(result)

    async def add_node(
        self, node: Union[DataPoint, str], properties: Optional[Dict[str, Any]] = None
    ) -> None:
        if isinstance(node, DataPoint):
            node_id = str(node.id)
            node_label = type(node).__name__
            node_properties = node.model_dump()
        else:
            node_id = str(node)
            node_label = self.DEFAULT_NODE_LABEL
            node_properties = properties or {}

        if "id" not in node_properties:
            node_properties["id"] = node_id
        if "type" not in node_properties:
            node_properties["type"] = node_label

        properties_json = self._build_properties_json(node_properties)
        serialized_properties = self._serialize_properties(node_properties)
        serialized_properties[self.PROPERTIES_JSON_KEY] = properties_json

        label = node_label if node_label else self.DEFAULT_NODE_LABEL
        property_fragments = ", ".join(
            f"{key}: {self._format_value(value)}" for key, value in serialized_properties.items()
        )

        exists_query = f"MATCH (n {{id: {self._format_value(node_id)}}}) RETURN n"
        exists_result = await self.query(exists_query)
        if exists_result:
            return

        create_query = f"CREATE (:{label} {{{property_fragments}}})"
        self._run_in_change([create_query])

    async def add_nodes(
        self,
        nodes: Union[List[Node], List[DataPoint]],
        source_ref_key: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
    ) -> None:
        if not nodes:
            return

        queries: List[str] = []
        for node in nodes:
            if hasattr(node, "id") and hasattr(node, "model_dump"):
                node_id = str(node.id)
                node_label = type(node).__name__
                node_properties = node.model_dump()
            else:
                node_id, node_properties = node
                node_label = self.DEFAULT_NODE_LABEL
                node_properties = node_properties or {}

            if "id" not in node_properties:
                node_properties["id"] = str(node_id)
            if "type" not in node_properties:
                node_properties["type"] = node_label

            properties_json = self._build_properties_json(node_properties)
            serialized_properties = self._serialize_properties(node_properties)
            serialized_properties[self.PROPERTIES_JSON_KEY] = properties_json

            label = node_label if node_label else self.DEFAULT_NODE_LABEL
            property_fragments = ", ".join(
                f"{key}: {self._format_value(value)}"
                for key, value in serialized_properties.items()
            )

            exists_query = f"MATCH (n {{id: {self._format_value(str(node_id))}}}) RETURN n"
            exists_result = await self.query(exists_query)
            if exists_result:
                continue

            queries.append(f"CREATE (:{label} {{{property_fragments}}})")

        if queries:
            self._run_in_change(queries)

    async def delete_node(self, node_id: str) -> None:
        query = f"MATCH (n {{id: {self._format_value(node_id)}}}) DETACH DELETE n"
        self._run_in_change([query])

    async def delete_nodes(self, node_ids: List[str]) -> None:
        if not node_ids:
            return
        conditions = " OR ".join(f"n.id = {self._format_value(node_id)}" for node_id in node_ids)
        query = f"MATCH (n) WHERE {conditions} DETACH DELETE n"
        self._run_in_change([query])

    async def get_node(self, node_id: str) -> Optional[NodeData]:
        if await self.is_empty():
            return None
        query = (
            f"MATCH (n {{id: {self._format_value(node_id)}}}) "
            f"RETURN n.{self.PROPERTIES_JSON_KEY} AS properties_json"
        )
        results = await self.query(query)
        if not results:
            return None
        raw_props = results[0].get("properties_json")
        if isinstance(raw_props, dict):
            return raw_props
        if raw_props:
            try:
                return json.loads(raw_props)
            except json.JSONDecodeError:
                return {"id": node_id, self.PROPERTIES_JSON_KEY: raw_props}
        return {"id": node_id}

    async def get_nodes(self, node_ids: List[str]) -> List[NodeData]:
        if not node_ids or await self.is_empty():
            return []
        conditions = " OR ".join(f"n.id = {self._format_value(node_id)}" for node_id in node_ids)
        query = (
            f"MATCH (n) WHERE {conditions} RETURN n.{self.PROPERTIES_JSON_KEY} AS properties_json"
        )
        results = await self.query(query)
        nodes: List[NodeData] = []
        for row in results:
            raw_props = row.get("properties_json")
            if isinstance(raw_props, dict):
                nodes.append(raw_props)
                continue
            if raw_props:
                try:
                    nodes.append(json.loads(raw_props))
                    continue
                except json.JSONDecodeError:
                    nodes.append({self.PROPERTIES_JSON_KEY: raw_props})
            else:
                nodes.append({})
        return nodes

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        relationship_name: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        edge_properties = properties or {}
        edge_properties = {
            **edge_properties,
            "source_node_id": str(source_id),
            "target_node_id": str(target_id),
            "relationship_name": relationship_name,
        }

        properties_json = self._build_properties_json(edge_properties)
        serialized_properties = self._serialize_properties(edge_properties)
        serialized_properties[self.PROPERTIES_JSON_KEY] = properties_json

        property_fragments = ", ".join(
            f"{key}: {self._format_value(value)}" for key, value in serialized_properties.items()
        )

        query = (
            f"MATCH (n {{id: {self._format_value(source_id)}}}), "
            f"(m {{id: {self._format_value(target_id)}}}) "
            f"CREATE (n)-[:{relationship_name} {{{property_fragments}}}]->(m)"
        )
        self._run_in_change([query])

    async def add_edges(
        self,
        edges: Union[
            List[EdgeData],
            List[Tuple[str, str, str, Optional[Dict[str, Any]]]],
        ],
        source_ref_key: Optional[str] = None,
        pipeline_run_id: Optional[str] = None,
    ) -> None:
        if not edges:
            return

        queries: List[str] = []
        for edge in edges:
            source_id, target_id, relationship_name, edge_properties = edge
            edge_properties = edge_properties or {}
            edge_properties = {
                **edge_properties,
                "source_node_id": str(source_id),
                "target_node_id": str(target_id),
                "relationship_name": relationship_name,
            }

            properties_json = self._build_properties_json(edge_properties)
            serialized_properties = self._serialize_properties(edge_properties)
            serialized_properties[self.PROPERTIES_JSON_KEY] = properties_json

            property_fragments = ", ".join(
                f"{key}: {self._format_value(value)}"
                for key, value in serialized_properties.items()
            )

            queries.append(
                f"MATCH (n {{id: {self._format_value(source_id)}}}), "
                f"(m {{id: {self._format_value(target_id)}}}) "
                f"CREATE (n)-[:{relationship_name} {{{property_fragments}}}]->(m)"
            )

        if queries:
            self._run_in_change(queries)

    async def delete_graph(self) -> None:
        if await self.is_empty():
            return
        query = "MATCH (n) DETACH DELETE n"
        self._run_in_change([query])

    async def get_graph_data(self) -> Tuple[List[Node], List[EdgeData]]:
        if await self.is_empty():
            return [], []

        nodes_query = (
            f"MATCH (n) RETURN n.id AS id, n.{self.PROPERTIES_JSON_KEY} AS properties_json"
        )
        nodes_result = await self.query(nodes_query)
        nodes: List[Node] = []
        for row in nodes_result:
            raw_props = row.get("properties_json")
            if isinstance(raw_props, dict):
                props = raw_props
            elif raw_props:
                try:
                    props = json.loads(raw_props)
                except json.JSONDecodeError:
                    props = {self.PROPERTIES_JSON_KEY: raw_props}
            else:
                props = {"id": row.get("id")}
            node_id = props.get("id") or row.get("id")
            nodes.append((node_id, props))

        edges_query = (
            f"MATCH (n)-[r]->(m) "
            f"RETURN n.id AS source_id, m.id AS target_id, "
            f"r.{self.PROPERTIES_JSON_KEY} as properties_json"
        )
        edges_result = await self.query(edges_query)
        edges: List[EdgeData] = []
        for row in edges_result:
            raw_props = row.get("properties_json")
            if isinstance(raw_props, dict):
                props = raw_props
            elif raw_props:
                try:
                    props = json.loads(raw_props)
                except json.JSONDecodeError:
                    props = {self.PROPERTIES_JSON_KEY: raw_props}
            else:
                props = {}
            source_id = props.get("source_node_id") or row.get("source_id")
            target_id = props.get("target_node_id") or row.get("target_id")
            relationship_name = props.get("relationship_name")
            edges.append((source_id, target_id, relationship_name, props))

        return nodes, edges

    async def get_graph_metrics(self, include_optional: bool = False) -> Dict[str, Any]:
        nodes, edges = await self.get_graph_data()
        num_nodes = len(nodes)
        num_edges = len(edges)

        mean_degree = (2 * num_edges) / num_nodes if num_nodes > 0 else 0
        edge_density = num_edges / (num_nodes * (num_nodes - 1)) if num_nodes > 1 else 0

        adjacency: Dict[str, set] = {}
        for node_id, _ in nodes:
            adjacency[str(node_id)] = set()
        for source_id, target_id, _rel, _props in edges:
            if source_id is None or target_id is None:
                continue
            adjacency.setdefault(str(source_id), set()).add(str(target_id))
            adjacency.setdefault(str(target_id), set()).add(str(source_id))

        visited = set()
        component_sizes: List[int] = []

        for node_id in adjacency.keys():
            if node_id in visited:
                continue
            stack = [node_id]
            size = 0
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                size += 1
                stack.extend(adjacency.get(current, []))
            component_sizes.append(size)

        metrics: Dict[str, Any] = {
            "num_nodes": num_nodes,
            "num_edges": num_edges,
            "mean_degree": mean_degree,
            "edge_density": edge_density,
            "num_connected_components": len(component_sizes),
            "sizes_of_connected_components": component_sizes,
        }

        if not include_optional:
            metrics.update(
                {
                    "num_selfloops": -1,
                    "diameter": -1,
                    "avg_shortest_path_length": -1,
                    "avg_clustering": -1,
                }
            )
            return metrics

        num_selfloops = sum(1 for s, t, _r, _p in edges if s == t)

        distances: Dict[str, Dict[str, int]] = {}
        for node_id in adjacency.keys():
            queue = [node_id]
            dist = {node_id: 0}
            while queue:
                current = queue.pop(0)
                for neighbor in adjacency.get(current, []):
                    if neighbor not in dist:
                        dist[neighbor] = dist[current] + 1
                        queue.append(neighbor)
            distances[node_id] = dist

        all_pairs = []
        for src, dist_map in distances.items():
            for dst, d in dist_map.items():
                if src != dst:
                    all_pairs.append(d)

        if all_pairs:
            diameter = max(all_pairs)
            avg_shortest_path_length = sum(all_pairs) / len(all_pairs)
        else:
            diameter = None
            avg_shortest_path_length = None

        clustering_values = []
        for _, neighbors in adjacency.items():
            if len(neighbors) < 2:
                clustering_values.append(0.0)
                continue
            links = 0
            neighbor_list = list(neighbors)
            for i in range(len(neighbor_list)):
                for j in range(i + 1, len(neighbor_list)):
                    if neighbor_list[j] in adjacency.get(neighbor_list[i], set()):
                        links += 1
            possible_links = len(neighbors) * (len(neighbors) - 1) / 2
            clustering_values.append(links / possible_links if possible_links else 0.0)

        avg_clustering = (
            sum(clustering_values) / len(clustering_values) if clustering_values else 0.0
        )

        metrics.update(
            {
                "num_selfloops": num_selfloops,
                "diameter": diameter,
                "avg_shortest_path_length": avg_shortest_path_length,
                "avg_clustering": avg_clustering,
            }
        )
        return metrics

    async def has_edge(self, source_id: str, target_id: str, relationship_name: str) -> bool:
        query = (
            f"MATCH (source {{id: {self._format_value(source_id)}}})"
            f"-[r]->(target {{id: {self._format_value(target_id)}}}) "
            f"WHERE r.relationship_name = {self._format_value(relationship_name)} "
            f"RETURN r"
        )
        results = await self.query(query)
        return len(results) > 0

    async def has_edges(self, edges: List[EdgeData]) -> List[EdgeData]:
        check_query = """MATCH ()-[r]->() RETURN r"""
        results = await self.query(check_query)
        if len(results) == 0:
            return []

        results: List[EdgeData] = []
        for edge in edges:
            if await self.has_edge(edge[0], edge[1], edge[2]):
                results.append(edge)
        return results

    async def get_edges(self, node_id: str) -> List[EdgeData]:
        if await self.is_empty():
            return []
        query = (
            f"MATCH (n {{id: {self._format_value(node_id)}}})"
            f"-[r]-(m) "
            f"RETURN n.id AS source_id, m.id AS target_id, "
            f"r.{self.PROPERTIES_JSON_KEY} AS properties_json"
        )
        results = await self.query(query)
        edges: List[EdgeData] = []
        for row in results:
            raw_props = row.get("properties_json")
            if isinstance(raw_props, dict):
                props = raw_props
            elif raw_props:
                try:
                    props = json.loads(raw_props)
                except json.JSONDecodeError:
                    props = {self.PROPERTIES_JSON_KEY: raw_props}
            else:
                props = {}
            relationship_name = props.get("relationship_name")
            edges.append(
                (
                    props.get("source_node_id") or row.get("source_id"),
                    props.get("target_node_id") or row.get("target_id"),
                    relationship_name,
                    props,
                )
            )
        return edges

    async def get_neighbors(self, node_id: str) -> List[NodeData]:
        if await self.is_empty():
            return []
        query = (
            f"MATCH (n {{id: {self._format_value(node_id)}}})"
            f"-[r]-(m) "
            f"RETURN m.{self.PROPERTIES_JSON_KEY} AS properties_json"
        )
        results = await self.query(query)
        neighbors: List[NodeData] = []
        for row in results:
            raw_props = row.get("properties_json")
            if isinstance(raw_props, dict):
                neighbors.append(raw_props)
                continue
            if raw_props:
                try:
                    neighbors.append(json.loads(raw_props))
                except json.JSONDecodeError:
                    neighbors.append({self.PROPERTIES_JSON_KEY: raw_props})
            else:
                neighbors.append({})
        return neighbors

    async def get_neighborhood(
        self,
        node_ids: List[str],
        depth: int = 1,
        edge_types: Optional[List[str]] = None,
    ) -> Tuple[List[Node], List[EdgeData]]:
        """
        Get the k-hop neighborhood subgraph around a set of seed nodes.

        Returns all nodes and edges within `depth` hops of any seed node,
        in the same format as get_graph_data(). If `edge_types` is given,
        only edges whose relationship_name is in `edge_types` are traversed.
        """
        if not node_ids or await self.is_empty():
            return [], []

        edge_type_set = set(edge_types) if edge_types else None

        # Iterative BFS up to `depth` hops, expanding via incident edges.
        visited_ids = {str(node_id) for node_id in node_ids}
        frontier = {str(node_id) for node_id in node_ids}

        for _ in range(max(depth, 0)):
            if not frontier:
                break
            next_frontier: set = set()
            for current_id in frontier:
                incident_edges = await self.get_edges(current_id)
                for source_id, target_id, relationship_name, _props in incident_edges:
                    if edge_type_set is not None and relationship_name not in edge_type_set:
                        continue
                    for endpoint in (source_id, target_id):
                        if endpoint is None:
                            continue
                        endpoint = str(endpoint)
                        if endpoint not in visited_ids:
                            visited_ids.add(endpoint)
                            next_frontier.add(endpoint)
            frontier = next_frontier

        if not visited_ids:
            return [], []

        all_ids = list(visited_ids)

        # Fetch all nodes within the neighborhood.
        node_props = await self.get_nodes(all_ids)
        nodes: List[Node] = []
        for props in node_props:
            node_id = props.get("id") if isinstance(props, dict) else None
            nodes.append((node_id, props))

        # Fetch all edges between collected nodes, in get_graph_data() format.
        id_set = set(all_ids)
        edges: List[EdgeData] = []
        seen_edges: set = set()
        for current_id in all_ids:
            for source_id, target_id, relationship_name, props in await self.get_edges(current_id):
                if source_id is None or target_id is None:
                    continue
                if str(source_id) not in id_set or str(target_id) not in id_set:
                    continue
                if edge_type_set is not None and relationship_name not in edge_type_set:
                    continue
                edge_key = (str(source_id), str(target_id), relationship_name)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                edges.append((source_id, target_id, relationship_name, props))

        return nodes, edges

    async def get_nodeset_subgraph(
        self,
        node_type: Type[Any],
        node_name: List[str],
        node_name_filter_operator: str = "OR",  # TODO: Add functionality for this parameter
    ) -> Tuple[List[Tuple[int, dict]], List[Tuple[int, int, str, dict]]]:
        if not node_name or await self.is_empty():
            return [], []

        label = node_type.__name__
        name_conditions = " OR ".join(f"n.name = {self._format_value(name)}" for name in node_name)

        # Phase 1: collect seed nodes + neighbors (IDs only)
        phase1_query = (
            f"MATCH (n)-[r]-(m) "
            f"WHERE n.type = {self._format_value(label)} AND ({name_conditions}) "
            f"RETURN n.id AS nid, m.id AS mid"
        )
        phase1_rows = await self.query(phase1_query)
        if not phase1_rows:
            return [], []

        node_ids = set()
        for row in phase1_rows:
            nid = row.get("nid")
            mid = row.get("mid")
            if nid:
                node_ids.add(nid)
            if mid:
                node_ids.add(mid)

        if not node_ids:
            return [], []

        # Phase 2: induced subgraph over collected IDs
        a_conditions = " OR ".join(f"a.id = {self._format_value(node_id)}" for node_id in node_ids)
        b_conditions = " OR ".join(f"b.id = {self._format_value(node_id)}" for node_id in node_ids)
        phase2_query = (
            f"MATCH (a)-[r]-(b) "
            f"WHERE ({a_conditions}) AND ({b_conditions}) "
            f"RETURN a.id AS source_id, b.id AS target_id, "
            f"r.relationship_name AS relationship_name, "
            f"a.{self.PROPERTIES_JSON_KEY} AS a_props, "
            f"b.{self.PROPERTIES_JSON_KEY} AS b_props, "
            f"r.{self.PROPERTIES_JSON_KEY} AS r_props"
        )
        phase2_rows = await self.query(phase2_query)
        if not phase2_rows:
            return [], []

        nodes_map: Dict[str, Tuple[str, dict]] = {}
        edges: List[EdgeData] = []

        for row in phase2_rows:
            a_id = row.get("source_id")
            b_id = row.get("target_id")
            rel = row.get("relationship_name")

            a_raw = row.get("a_props")
            b_raw = row.get("b_props")
            r_raw = row.get("r_props")

            def _coerce_props(raw):
                if isinstance(raw, dict):
                    return raw
                if raw:
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return {self.PROPERTIES_JSON_KEY: raw}
                return {}

            a_props = _coerce_props(a_raw)
            b_props = _coerce_props(b_raw)
            r_props = _coerce_props(r_raw)

            if a_id and a_id not in nodes_map:
                nodes_map[str(a_id)] = (a_id, a_props)
            if b_id and b_id not in nodes_map:
                nodes_map[str(b_id)] = (b_id, b_props)

            edges.append((a_id, b_id, rel, r_props))

        return list(nodes_map.values()), edges

    async def get_connections(
        self, node_id: Union[str, UUID]
    ) -> List[Tuple[NodeData, Dict[str, Any], NodeData]]:
        if await self.is_empty():
            return []
        query = (
            f"MATCH (n {{id: {self._format_value(str(node_id))}}})"
            f"-[r]-(m) "
            f"RETURN n.{self.PROPERTIES_JSON_KEY} AS n_props, "
            f"m.{self.PROPERTIES_JSON_KEY} AS m_props, "
            f"r.{self.PROPERTIES_JSON_KEY} AS r_props"
        )
        results = await self.query(query)
        connections = []
        for row in results:
            n_props_raw = row.get("n_props")
            m_props_raw = row.get("m_props")
            r_props_raw = row.get("r_props")

            try:
                n_props = json.loads(n_props_raw) if n_props_raw else {}
            except json.JSONDecodeError:
                n_props = {self.PROPERTIES_JSON_KEY: n_props_raw}

            try:
                m_props = json.loads(m_props_raw) if m_props_raw else {}
            except json.JSONDecodeError:
                m_props = {self.PROPERTIES_JSON_KEY: m_props_raw}

            try:
                r_props = json.loads(r_props_raw) if r_props_raw else {}
            except json.JSONDecodeError:
                r_props = {self.PROPERTIES_JSON_KEY: r_props_raw}

            connections.append(
                (n_props, {"relationship_name": r_props.get("relationship_name")}, m_props)
            )
        return connections

    async def get_filtered_graph_data(
        self, attribute_filters: List[Dict[str, List[Union[str, int]]]]
    ) -> Tuple[List[Node], List[EdgeData]]:
        if not attribute_filters or await self.is_empty():
            return [], []

        where_clauses = []
        for attribute, values in attribute_filters[0].items():
            value_conditions = " OR ".join(
                f"n.{attribute} = {self._format_value(value)}" for value in values
            )
            where_clauses.append(f"({value_conditions})")

        where_clause = " AND ".join(where_clauses)
        nodes_query = (
            f"MATCH (n) WHERE {where_clause} "
            f"RETURN n.id AS id, n.{self.PROPERTIES_JSON_KEY} AS properties_json"
        )
        nodes_result = await self.query(nodes_query)
        nodes: List[Node] = []
        for row in nodes_result:
            raw_props = row.get("properties_json")
            if isinstance(raw_props, dict):
                props = raw_props
            elif raw_props:
                try:
                    props = json.loads(raw_props)
                except json.JSONDecodeError:
                    props = {self.PROPERTIES_JSON_KEY: raw_props}
            else:
                props = {"id": row.get("id")}
            node_id = props.get("id") or row.get("id")
            nodes.append((node_id, props))

        edges_query = (
            f"MATCH (n)-[r]->(m) "
            f"WHERE {where_clause} AND {where_clause.replace('n.', 'm.')} "
            f"RETURN r,"
            f"n.id AS source_id, m.id AS target_id, r.{self.PROPERTIES_JSON_KEY} AS properties_json"
        )
        edges_result = await self.query(edges_query)
        edges: List[EdgeData] = []
        for row in edges_result:
            raw_props = row.get("properties_json")
            if isinstance(raw_props, dict):
                props = raw_props
            elif raw_props:
                try:
                    props = json.loads(raw_props)
                except json.JSONDecodeError:
                    props = {self.PROPERTIES_JSON_KEY: raw_props}
            else:
                props = {}
            edges.append(
                (
                    props.get("source_node_id") or row.get("source_id"),
                    props.get("target_node_id") or row.get("target_id"),
                    props.get("relationship_name"),
                    props,
                )
            )
        return (nodes, edges)

    def _cypher_escape(self, value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\r", "\\r")
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
