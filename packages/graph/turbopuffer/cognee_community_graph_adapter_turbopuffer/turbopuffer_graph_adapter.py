"""TurboPuffer graph adapter — v1.

Implements the GraphDBInterface contract on top of TurboPuffer's namespace API.
TurboPuffer is a vector/document store with no graph traversal or query language,
so this adapter models the graph as two document namespaces per dataset and
performs all traversal client-side with single-hop edge queries.

Storage model (per dataset, namespace-prefixed by ``database_name``):
  - ``{database_name}_graph_node``: one doc per node. Attributes: id, name, type,
    belongs_to_set (string[]), degree (int), properties (json string).
  - ``{database_name}_graph_edge``: one doc per edge. id = f"{src}__{rel}__{tgt}";
    attributes: source_id, target_id, relationship_name, plus DENORMALIZED
    endpoint identity source_name/source_type/target_name/target_type, and
    properties (json string). The endpoint denormalization makes single-hop reads
    (get_neighbors / get_connections / get_edges) one query with zero hydration.

Resolved SDK unknowns (TurboPuffer SDK 2.3.0):
  - Vectorless writes ARE allowed: RowParam.vector is optional, so node/edge docs
    are written WITHOUT any vector at all.
  - Row-level delete signature is ``ns.write(deletes=[id1, id2, ...])``.

All TurboPuffer SDK calls are synchronous; they are wrapped in
``asyncio.to_thread`` exactly like the vector adapter does.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple, Type, Union

import turbopuffer
from cognee.infrastructure.databases.graph.graph_db_interface import GraphDBInterface
from cognee.infrastructure.engine import DataPoint
from cognee.modules.storage.utils import JSONEncoder
from cognee.shared.logging_utils import get_logger

from .serialization import (
    _build_row_schema,
    _truncate_large_values,
)

logger = get_logger("TurbopufferGraphAdapter")

# TurboPuffer caps a single query at 10,000 returned rows; we page/chunk below it.
_MAX_QUERY_ROWS = 10000

# Core node attributes stored as first-class TurboPuffer columns. Everything else
# is folded into the JSON ``properties`` attribute.
_NODE_CORE_KEYS = {"id", "name", "type"}


class TurbopufferGraphAdapter(GraphDBInterface):
    def __init__(
        self,
        graph_database_url: str = "",
        graph_database_username: str = "",
        graph_database_password: str = "",
        graph_database_port: str = "",
        graph_database_key: str = "",
        database_name: Optional[str] = None,
        graph_database_name: Optional[str] = None,
        graph_database_host: str = "",
        **kwargs: Any,
    ) -> None:
        # cognee's factory passes the per-dataset name as ``database_name``; an
        # explicit ``graph_database_name`` wins if both are given.
        self.database_name = graph_database_name or database_name or "cognee_graph"
        self.api_key = graph_database_key
        self.region = graph_database_url  # TurboPuffer region travels in the url slot
        self._client = None  # lazy

    # --- helpers -----------------------------------------------------------

    def _get_client(self) -> turbopuffer.Turbopuffer:
        if self._client is None:
            kwargs: Dict[str, Any] = {}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.region:
                kwargs["region"] = self.region
            self._client = turbopuffer.Turbopuffer(**kwargs)
        return self._client

    def _namespace_name(self, collection_name: str) -> str:
        return f"{self.database_name}_{collection_name}"

    def _node_namespace(self):
        return self._get_client().namespace(self._namespace_name("graph_node"))

    def _edge_namespace(self):
        return self._get_client().namespace(self._namespace_name("graph_edge"))

    @staticmethod
    def _edge_id(source_id: str, relationship_name: str, target_id: str) -> str:
        return f"{source_id}__{relationship_name}__{target_id}"

    @staticmethod
    def _serialize_properties(props: Optional[Dict[str, Any]]) -> str:
        return json.dumps(props or {}, cls=JSONEncoder)

    @staticmethod
    def _parse_properties(raw: Any) -> Dict[str, Any]:
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}

    @classmethod
    def _node_dict_from_row(cls, row) -> Dict[str, Any]:
        """Build a merged node dict ({id, name, type, **properties}) from a row."""
        extra = row.model_extra or {}
        node = {
            "id": str(row.id),
            "name": extra.get("name", ""),
            "type": extra.get("type", ""),
        }
        node.update(cls._parse_properties(extra.get("properties")))
        return node

    @staticmethod
    def _node_dict_from_endpoint(node_id: Any, name: Any, type_: Any) -> Dict[str, Any]:
        """Build a node dict from denormalized endpoint identity on an edge row."""
        return {"id": str(node_id), "name": name or "", "type": type_ or ""}

    async def _query_all(self, namespace, filters=None) -> List[Any]:
        """Cursor-scan a namespace deterministically (rank_by id asc), returning rows."""
        rows: List[Any] = []
        cursor: Optional[str] = None
        while True:
            page_filter = ("id", "Gt", cursor) if cursor is not None else None
            if filters is not None and page_filter is not None:
                effective = ("And", (filters, page_filter))
            elif filters is not None:
                effective = filters
            else:
                effective = page_filter

            kwargs: Dict[str, Any] = {
                "rank_by": ("id", "asc"),
                "top_k": _MAX_QUERY_ROWS,
                "include_attributes": True,
            }
            if effective is not None:
                kwargs["filters"] = effective

            response = await asyncio.to_thread(namespace.query, **kwargs)
            page = response.rows or []
            rows.extend(page)
            if len(page) < _MAX_QUERY_ROWS:
                break
            cursor = str(page[-1].id)
        return rows

    async def _query_by_ids(self, namespace, ids: List[str]) -> List[Any]:
        """Fetch rows for the given ids, chunked under the per-query row ceiling."""
        rows: List[Any] = []
        unique_ids = list(dict.fromkeys(str(i) for i in ids))
        for start in range(0, len(unique_ids), _MAX_QUERY_ROWS):
            chunk = unique_ids[start : start + _MAX_QUERY_ROWS]
            if not chunk:
                continue
            response = await asyncio.to_thread(
                namespace.query,
                filters=("id", "In", chunk),
                top_k=len(chunk),
                include_attributes=True,
            )
            rows.extend(response.rows or [])
        return rows

    async def _edges_touching(self, node_ids: List[str]) -> List[Any]:
        """All edge rows where source_id or target_id is in node_ids.

        Chunks the id set so the ``In`` filter never exceeds the per-query
        ceiling (e.g. deleting >10k nodes at once)."""
        ids = list(dict.fromkeys(str(i) for i in node_ids))
        if not ids:
            return []
        rows: List[Any] = []
        seen_edge_ids: set = set()
        for start in range(0, len(ids), _MAX_QUERY_ROWS):
            chunk = ids[start : start + _MAX_QUERY_ROWS]
            filters = ("Or", (("source_id", "In", chunk), ("target_id", "In", chunk)))
            for row in await self._query_all(self._edge_namespace(), filters=filters):
                # An edge between two in-set nodes can surface in two chunks; dedup.
                if str(row.id) not in seen_edge_ids:
                    seen_edge_ids.add(str(row.id))
                    rows.append(row)
        return rows

    # --- lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        # No-op: TurboPuffer namespaces are created on first write.
        return None

    async def is_empty(self) -> bool:
        ns = self._node_namespace()
        try:
            response = await asyncio.to_thread(
                ns.query,
                rank_by=("id", "asc"),
                top_k=1,
            )
        except Exception:
            # Namespace does not exist yet -> empty graph.
            return True
        return not (response.rows or [])

    async def delete_graph(self) -> None:
        for ns in (self._node_namespace(), self._edge_namespace()):
            try:
                await asyncio.to_thread(ns.delete_all)
            except Exception as error:
                # delete_all on a non-existent namespace is a no-op for us.
                logger.debug("delete_all on %s ignored: %s", ns.id, error)

    # --- writes ------------------------------------------------------------

    async def add_node(
        self, node: Union[DataPoint, str], properties: Optional[Dict[str, Any]] = None
    ) -> None:
        if isinstance(node, str):
            props = dict(properties or {})
            props.setdefault("id", node)
            await self.add_nodes([(node, props)])
        else:
            await self.add_nodes([node])

    def _node_row(self, node: Union[Tuple[str, Dict], DataPoint]) -> Dict[str, Any]:
        if isinstance(node, tuple):
            props = {**(node[1] or {}), "id": node[0]}
        elif hasattr(node, "model_dump"):
            props = node.model_dump()
        else:
            props = vars(node)

        extra = {k: v for k, v in props.items() if k not in _NODE_CORE_KEYS}
        belongs_to_set = extra.pop("belongs_to_set", None) or []
        if not isinstance(belongs_to_set, list):
            belongs_to_set = [belongs_to_set]
        belongs_to_set = [str(v) for v in belongs_to_set]

        row = {
            "id": str(props.get("id", "")),
            "name": str(props.get("name", "") or ""),
            "type": str(props.get("type", "") or ""),
            "belongs_to_set": belongs_to_set,
            "properties": self._serialize_properties(extra),
        }
        return _truncate_large_values(row)

    async def add_nodes(self, nodes: Union[List[Tuple[str, Dict]], List[DataPoint]]) -> None:
        if not nodes:
            return

        rows = [self._node_row(node) for node in nodes]
        # Deduplicate by id (last wins) so a single write batch never conflicts.
        rows = list({r["id"]: r for r in rows}.values())

        schema = _build_row_schema(rows)
        await asyncio.to_thread(
            self._node_namespace().write,
            upsert_rows=rows,
            schema=schema,
        )

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        relationship_name: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self.add_edges(
            [(str(source_id), str(target_id), relationship_name, properties or {})]
        )

    async def add_edges(
        self, edges: Union[List, List[Tuple[str, str, str, Optional[Dict[str, Any]]]]]
    ) -> None:
        if not edges:
            return

        # Collect endpoint ids so we can denormalize name/type onto each edge doc.
        endpoint_ids = set()
        for edge in edges:
            endpoint_ids.add(str(edge[0]))
            endpoint_ids.add(str(edge[1]))

        node_rows = await self._query_by_ids(self._node_namespace(), list(endpoint_ids))
        identity: Dict[str, Tuple[str, str]] = {}
        for row in node_rows:
            extra = row.model_extra or {}
            identity[str(row.id)] = (extra.get("name", ""), extra.get("type", ""))

        rows = []
        for edge in edges:
            source_id = str(edge[0])
            target_id = str(edge[1])
            relationship_name = str(edge[2])
            raw_props = edge[3] if len(edge) > 3 and edge[3] else {}

            src_name, src_type = identity.get(source_id, ("", ""))
            tgt_name, tgt_type = identity.get(target_id, ("", ""))

            row = {
                "id": self._edge_id(source_id, relationship_name, target_id),
                "source_id": source_id,
                "target_id": target_id,
                "relationship_name": relationship_name,
                "source_name": src_name or "",
                "source_type": src_type or "",
                "target_name": tgt_name or "",
                "target_type": tgt_type or "",
                "properties": self._serialize_properties(raw_props),
            }
            rows.append(_truncate_large_values(row))

        # Deduplicate by edge id (last wins) within the batch.
        rows = list({r["id"]: r for r in rows}.values())

        schema = _build_row_schema(rows)
        await asyncio.to_thread(
            self._edge_namespace().write,
            upsert_rows=rows,
            schema=schema,
        )

    # --- deletes -----------------------------------------------------------

    async def delete_node(self, node_id: str) -> None:
        await self.delete_nodes([node_id])

    async def delete_nodes(self, node_ids: List[str]) -> None:
        if not node_ids:
            return
        ids = [str(i) for i in node_ids]

        # Delete the node docs (chunked under the per-request ceiling).
        await self._delete_ids(self._node_namespace(), ids)

        # Manual cascade: delete every edge touching any deleted node.
        touching = await self._edges_touching(ids)
        edge_ids = [str(row.id) for row in touching]
        await self._delete_ids(self._edge_namespace(), edge_ids)

    async def _delete_ids(self, namespace, ids: List[str]) -> None:
        for start in range(0, len(ids), _MAX_QUERY_ROWS):
            chunk = ids[start : start + _MAX_QUERY_ROWS]
            if chunk:
                await asyncio.to_thread(namespace.write, deletes=chunk)

    # --- point reads -------------------------------------------------------

    async def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        results = await self.get_nodes([node_id])
        return results[0] if results else None

    async def get_nodes(self, node_ids: List[str]) -> List[Dict[str, Any]]:
        if not node_ids:
            return []
        rows = await self._query_by_ids(self._node_namespace(), node_ids)
        return [self._node_dict_from_row(row) for row in rows]

    async def has_edge(self, source_id: str, target_id: str, relationship_name: str) -> bool:
        result = await self.has_edges([(str(source_id), str(target_id), relationship_name)])
        return len(result) > 0

    async def has_edges(self, edges: List) -> List:
        if not edges:
            return []
        wanted = [(str(s), str(t), str(r)) for (s, t, r) in edges]
        edge_ids = [self._edge_id(s, r, t) for (s, t, r) in wanted]
        rows = await self._query_by_ids(self._edge_namespace(), edge_ids)
        present = {str(row.id) for row in rows}
        return [
            (s, t, r) for (s, t, r), eid in zip(wanted, edge_ids, strict=False) if eid in present
        ]

    # --- single-hop reads --------------------------------------------------

    async def get_edges(self, node_id: str) -> List:
        # Returns EdgeData tuples: (source_id, target_id, relationship_name, properties).
        rows = await self._edges_touching([str(node_id)])
        edges = []
        for row in rows:
            extra = row.model_extra or {}
            edges.append(
                (
                    str(extra.get("source_id")),
                    str(extra.get("target_id")),
                    extra.get("relationship_name"),
                    self._parse_properties(extra.get("properties")),
                )
            )
        return edges

    async def get_neighbors(self, node_id: str) -> List[Dict[str, Any]]:
        nid = str(node_id)
        rows = await self._edges_touching([nid])
        neighbors: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            extra = row.model_extra or {}
            source_id = str(extra.get("source_id"))
            target_id = str(extra.get("target_id"))
            if source_id == nid:
                neighbors[target_id] = self._node_dict_from_endpoint(
                    target_id, extra.get("target_name"), extra.get("target_type")
                )
            else:
                neighbors[source_id] = self._node_dict_from_endpoint(
                    source_id, extra.get("source_name"), extra.get("source_type")
                )
        return list(neighbors.values())

    async def get_connections(
        self, node_id: Union[str, Any]
    ) -> List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]]:
        rows = await self._edges_touching([str(node_id)])
        connections = []
        for row in rows:
            extra = row.model_extra or {}
            src = self._node_dict_from_endpoint(
                extra.get("source_id"), extra.get("source_name"), extra.get("source_type")
            )
            tgt = self._node_dict_from_endpoint(
                extra.get("target_id"), extra.get("target_name"), extra.get("target_type")
            )
            edge = {"relationship_name": extra.get("relationship_name")}
            edge.update(self._parse_properties(extra.get("properties")))
            connections.append((src, edge, tgt))
        return connections

    async def get_neighborhood(
        self,
        node_ids: List[str],
        depth: int = 1,
        edge_types: Optional[List[str]] = None,
    ) -> Tuple[List, List]:
        if depth != 1:
            raise NotImplementedError(
                "TurboPuffer graph adapter v1 supports single-hop neighborhood only "
                "(depth=1); multi-hop BFS is not implemented."
            )
        if not node_ids:
            return [], []

        seeds = [str(i) for i in node_ids]
        edge_rows = await self._edges_touching(seeds)

        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Tuple[str, str, str, Dict[str, Any]]] = []
        for row in edge_rows:
            extra = row.model_extra or {}
            relationship_name = extra.get("relationship_name")
            if edge_types and relationship_name not in edge_types:
                continue
            source_id = str(extra.get("source_id"))
            target_id = str(extra.get("target_id"))
            nodes.setdefault(
                source_id,
                {"name": extra.get("source_name", ""), "type": extra.get("source_type", "")},
            )
            nodes.setdefault(
                target_id,
                {"name": extra.get("target_name", ""), "type": extra.get("target_type", "")},
            )
            edges.append(
                (
                    source_id,
                    target_id,
                    relationship_name,
                    self._parse_properties(extra.get("properties")),
                )
            )

        # Seeds are always part of the neighborhood even if they have no edges.
        missing_seeds = [s for s in seeds if s not in nodes]
        if missing_seeds:
            seed_rows = await self._query_by_ids(self._node_namespace(), missing_seeds)
            for row in seed_rows:
                node = self._node_dict_from_row(row)
                node.pop("id", None)
                nodes[str(row.id)] = node

        return [(nid, data) for nid, data in nodes.items()], edges

    # --- whole-graph reads -------------------------------------------------

    async def get_graph_data(self) -> Tuple[List, List]:
        node_rows = await self._query_all(self._node_namespace())
        nodes = []
        for row in node_rows:
            extra = row.model_extra or {}
            data = {"name": extra.get("name", ""), "type": extra.get("type", "")}
            data.update(self._parse_properties(extra.get("properties")))
            nodes.append((str(row.id), data))

        if not nodes:
            return [], []

        edge_rows = await self._query_all(self._edge_namespace())
        edges = []
        for row in edge_rows:
            extra = row.model_extra or {}
            edges.append(
                (
                    str(extra.get("source_id")),
                    str(extra.get("target_id")),
                    extra.get("relationship_name"),
                    self._parse_properties(extra.get("properties")),
                )
            )
        return nodes, edges

    async def get_filtered_graph_data(
        self, attribute_filters: List[Dict[str, List[Union[str, int]]]]
    ) -> Tuple[List, List]:
        if not attribute_filters:
            return await self.get_graph_data()

        allowed = {"id", "name", "type"}
        and_filters = []
        for filter_dict in attribute_filters:
            for attr, values in filter_dict.items():
                if attr not in allowed:
                    raise ValueError(f"Invalid filter attribute: {attr!r}")
                and_filters.append((attr, "In", [str(v) for v in values]))

        node_filter = and_filters[0] if len(and_filters) == 1 else ("And", tuple(and_filters))
        node_rows = await self._query_all(self._node_namespace(), filters=node_filter)

        nodes = []
        node_ids = set()
        for row in node_rows:
            extra = row.model_extra or {}
            data = {"name": extra.get("name", ""), "type": extra.get("type", "")}
            data.update(self._parse_properties(extra.get("properties")))
            nodes.append((str(row.id), data))
            node_ids.add(str(row.id))

        if not node_ids:
            return [], []

        edge_rows = await self._query_all(self._edge_namespace())
        edges = []
        for row in edge_rows:
            extra = row.model_extra or {}
            source_id = str(extra.get("source_id"))
            target_id = str(extra.get("target_id"))
            if source_id in node_ids and target_id in node_ids:
                edges.append(
                    (
                        source_id,
                        target_id,
                        extra.get("relationship_name"),
                        self._parse_properties(extra.get("properties")),
                    )
                )
        return nodes, edges

    async def get_nodeset_subgraph(
        self, node_type: Type[Any], node_name: List[str], node_name_filter_operator: str = "OR"
    ) -> Tuple[List, List]:
        label = node_type.__name__

        primary_filter = (
            "And",
            (("type", "Eq", label), ("name", "In", [str(n) for n in node_name])),
        )
        primary_rows = await self._query_all(self._node_namespace(), filters=primary_filter)
        primary_ids = {str(row.id) for row in primary_rows}

        if not primary_ids:
            return [], []

        # Single-hop expansion around the primary nodes.
        edge_rows = await self._edges_touching(list(primary_ids))

        # Map each neighbor to the set of primaries it connects to.
        neighbor_to_primaries: Dict[str, set] = {}
        for row in edge_rows:
            extra = row.model_extra or {}
            source_id = str(extra.get("source_id"))
            target_id = str(extra.get("target_id"))
            if source_id in primary_ids:
                primary, neighbor = source_id, target_id
            else:
                primary, neighbor = target_id, source_id
            if neighbor in primary_ids:
                continue
            neighbor_to_primaries.setdefault(neighbor, set()).add(primary)

        if node_name_filter_operator == "AND":
            neighbor_ids = {
                n
                for n, primaries in neighbor_to_primaries.items()
                if len(primaries) == len(primary_ids)
            }
        else:
            neighbor_ids = set(neighbor_to_primaries.keys())

        all_ids = primary_ids | neighbor_ids

        node_rows = await self._query_by_ids(self._node_namespace(), list(all_ids))
        nodes = []
        for row in node_rows:
            extra = row.model_extra or {}
            data = {"name": extra.get("name", ""), "type": extra.get("type", "")}
            data.update(self._parse_properties(extra.get("properties")))
            nodes.append((str(row.id), data))

        edges = []
        all_edge_rows = await self._edges_touching(list(all_ids))
        for row in all_edge_rows:
            extra = row.model_extra or {}
            source_id = str(extra.get("source_id"))
            target_id = str(extra.get("target_id"))
            if source_id in all_ids and target_id in all_ids:
                edges.append(
                    (
                        source_id,
                        target_id,
                        extra.get("relationship_name"),
                        self._parse_properties(extra.get("properties")),
                    )
                )
        return nodes, edges

    async def get_graph_metrics(self, include_optional: bool = False) -> Dict[str, Any]:
        node_rows = await self._query_all(self._node_namespace())
        num_nodes = len(node_rows)

        edge_rows = await self._query_all(self._edge_namespace()) if num_nodes else []
        num_edges = len(edge_rows)

        mean_degree = (2 * num_edges) / num_nodes if num_nodes else 0.0
        edge_density = num_edges / (num_nodes * (num_nodes - 1)) if num_nodes > 1 else 0

        num_selfloops = 0
        for row in edge_rows:
            extra = row.model_extra or {}
            if str(extra.get("source_id")) == str(extra.get("target_id")):
                num_selfloops += 1

        metrics: Dict[str, Any] = {
            "num_nodes": num_nodes,
            "num_edges": num_edges,
            "mean_degree": mean_degree,
            "edge_density": edge_density,
            # Connectivity-dependent metrics require traversal not done in v1.
            "num_connected_components": -1,
            "sizes_of_connected_components": [],
            "num_selfloops": num_selfloops if include_optional else -1,
            "diameter": -1,
            "avg_shortest_path_length": -1,
            "avg_clustering": -1,
        }
        return metrics

    # --- unsupported in v1 -------------------------------------------------

    async def query(self, query: str, params: Optional[dict] = None) -> List[Any]:
        raise NotImplementedError("TurboPuffer has no query language; Cypher is unsupported.")

    async def get_triplets_batch(self, offset: int, limit: int) -> List[Dict[str, Any]]:
        if offset != 0:
            raise NotImplementedError(
                "TurboPuffer is cursor-paginated, not offset-paginated; nonzero offset "
                "is unsupported in v1."
            )

        edge_rows = await self._query_all(self._edge_namespace())
        edge_rows = edge_rows[: max(limit, 0)]
        if not edge_rows:
            return []

        # Hydrate endpoints so triplet nodes carry their full property payload.
        endpoint_ids = set()
        for row in edge_rows:
            extra = row.model_extra or {}
            endpoint_ids.add(str(extra.get("source_id")))
            endpoint_ids.add(str(extra.get("target_id")))

        node_rows = await self._query_by_ids(self._node_namespace(), list(endpoint_ids))
        node_lookup = {str(row.id): self._node_dict_from_row(row) for row in node_rows}

        triplets = []
        for row in edge_rows:
            extra = row.model_extra or {}
            source_id = str(extra.get("source_id"))
            target_id = str(extra.get("target_id"))

            start_node = node_lookup.get(
                source_id,
                self._node_dict_from_endpoint(
                    source_id, extra.get("source_name"), extra.get("source_type")
                ),
            )
            end_node = node_lookup.get(
                target_id,
                self._node_dict_from_endpoint(
                    target_id, extra.get("target_name"), extra.get("target_type")
                ),
            )

            rel = {"relationship_name": extra.get("relationship_name")}
            rel.update(self._parse_properties(extra.get("properties")))

            triplets.append(
                {
                    "start_node": start_node,
                    "relationship_properties": rel,
                    "end_node": end_node,
                }
            )
        return triplets
