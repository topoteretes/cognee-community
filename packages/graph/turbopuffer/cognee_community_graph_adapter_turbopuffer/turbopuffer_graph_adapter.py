"""TurboPuffer graph adapter — v1 SKELETON (test-driven).

This is the red-phase skeleton: it satisfies the GraphDBInterface contract well
enough to be instantiated and import-checked, and it pins the v1 boundaries that
the tests assert (no multi-hop BFS, no Cypher, no random-offset pagination).

Every data method raises NotImplementedError until implemented. The design that
each method will follow is documented inline so implementation is a fill-in-the-
blanks exercise against the test suite.

Storage model (per dataset, namespace-prefixed by ``database_name``):
  - ``{database_name}_graph_node``: one doc per node. Attributes: id, name, type,
    belongs_to_set (string[]), degree (int), properties (json string).
  - ``{database_name}_graph_edge``: one doc per edge. id = f"{src}__{rel}__{tgt}";
    attributes: source_id, target_id, relationship_name, plus DENORMALIZED
    endpoint identity source_name/source_type/target_name/target_type, and
    properties (json string). The endpoint denormalization makes single-hop reads
    (get_neighbors / get_connections / get_edges) one query with zero hydration.
"""

from typing import Any, Dict, List, Optional, Tuple, Type, Union

from cognee.infrastructure.databases.graph.graph_db_interface import GraphDBInterface
from cognee.infrastructure.engine import DataPoint


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

    def _namespace_name(self, collection_name: str) -> str:
        return f"{self.database_name}_{collection_name}"

    def _node_namespace(self):
        raise NotImplementedError

    def _edge_namespace(self):
        raise NotImplementedError

    @staticmethod
    def _edge_id(source_id: str, relationship_name: str, target_id: str) -> str:
        return f"{source_id}__{relationship_name}__{target_id}"

    # --- lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        # No-op: TurboPuffer namespaces are created on first write.
        return None

    async def is_empty(self) -> bool:
        # query(node_ns, rank_by=("id","asc"), top_k=1) -> rows empty?
        raise NotImplementedError

    async def delete_graph(self) -> None:
        # node_ns.delete_all(); edge_ns.delete_all()
        raise NotImplementedError

    # --- writes ------------------------------------------------------------

    async def add_node(
        self, node: Union[DataPoint, str], properties: Optional[Dict[str, Any]] = None
    ) -> None:
        raise NotImplementedError

    async def add_nodes(self, nodes: Union[List[Tuple[str, Dict]], List[DataPoint]]) -> None:
        # node_ns.write(upsert_rows=[serialize(n) for n in nodes], schema=...)
        raise NotImplementedError

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        relationship_name: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        raise NotImplementedError

    async def add_edges(
        self, edges: Union[List, List[Tuple[str, str, str, Optional[Dict[str, Any]]]]]
    ) -> None:
        # Denormalize endpoint name/type onto each edge doc (look up from the
        # node batch, or via get_nodes when not co-present), then
        # edge_ns.write(upsert_rows=...).
        raise NotImplementedError

    # --- deletes -----------------------------------------------------------

    async def delete_node(self, node_id: str) -> None:
        raise NotImplementedError

    async def delete_nodes(self, node_ids: List[str]) -> None:
        # node_ns.write(deletes=node_ids); then query touching edges
        # ("Or", source_id In ids, target_id In ids) and edge_ns.write(deletes=...).
        raise NotImplementedError

    # --- point reads -------------------------------------------------------

    async def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    async def get_nodes(self, node_ids: List[str]) -> List[Dict[str, Any]]:
        # node_ns.query(filters=("id","In",node_ids), include_attributes=True),
        # chunked to <=10000 ids per query.
        raise NotImplementedError

    async def has_edge(self, source_id: str, target_id: str, relationship_name: str) -> bool:
        raise NotImplementedError

    async def has_edges(self, edges: List) -> List:
        # edge_ns.query(filters=("id","In",[edge_id(...) ...])) -> intersect.
        raise NotImplementedError

    # --- single-hop reads --------------------------------------------------

    async def get_edges(self, node_id: str) -> List:
        # edge_ns.query(("Or", source_id Eq, target_id Eq), include_attributes=True)
        raise NotImplementedError

    async def get_neighbors(self, node_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def get_connections(
        self, node_id: Union[str, Any]
    ) -> List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]]:
        # One edge query; assemble (source, rel, target) from denormalized attrs.
        raise NotImplementedError

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
        # depth==1: one edge query around seeds + node hydration.
        raise NotImplementedError

    # --- whole-graph reads -------------------------------------------------

    async def get_graph_data(self) -> Tuple[List, List]:
        # Cursor scan: query(rank_by=("id","asc"), filters=("id","Gt",cursor), top_k=10000)
        raise NotImplementedError

    async def get_filtered_graph_data(
        self, attribute_filters: List[Dict[str, List[Union[str, int]]]]
    ) -> Tuple[List, List]:
        raise NotImplementedError

    async def get_nodeset_subgraph(
        self, node_type: Type[Any], node_name: List[str], node_name_filter_operator: str = "OR"
    ) -> Tuple[List, List]:
        # OR: type Eq + name In; AND: client-side intersection of 1-hop expansions.
        raise NotImplementedError

    async def get_graph_metrics(self, include_optional: bool = False) -> Dict[str, Any]:
        # num_nodes/num_edges/mean_degree from scans; connectivity-dependent
        # fields return the -1 sentinel (no traversal in v1).
        raise NotImplementedError

    # --- unsupported in v1 -------------------------------------------------

    async def query(self, query: str, params: Optional[dict] = None) -> List[Any]:
        raise NotImplementedError("TurboPuffer has no query language; Cypher is unsupported.")

    async def get_triplets_batch(self, offset: int, limit: int) -> List[Dict[str, Any]]:
        if offset != 0:
            raise NotImplementedError(
                "TurboPuffer is cursor-paginated, not offset-paginated; nonzero offset "
                "is unsupported in v1."
            )
        raise NotImplementedError
