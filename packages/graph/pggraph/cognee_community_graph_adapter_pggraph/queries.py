"""SQL helpers for the pgGraph extension (graph schema)."""

NODE_TABLE = "graph_node"
EDGE_TABLE = "graph_edge"

EXTENSION_AVAILABLE = """
SELECT EXISTS (
  SELECT 1 FROM pg_available_extensions WHERE name = 'graph'
)
"""

CREATE_EXTENSION = "CREATE EXTENSION IF NOT EXISTS graph"

REGISTER_NODE_TABLE = f"""
SELECT graph.add_table('{NODE_TABLE}'::regclass, 'id')
"""

REGISTER_EDGE_TABLE = f"""
SELECT graph.add_edge(
  '{EDGE_TABLE}'::regclass,
  'source_id',
  '{EDGE_TABLE}'::regclass,
  'target_id',
  'cognee_edge',
  true,
  NULL,
  'relationship_name'
)
"""

BUILD_GRAPH = "SELECT * FROM graph.build()"

RESET_GRAPH = "SELECT graph.reset()"

PGGRAPH_ENABLED = "SELECT graph.test_enabled()"

REGISTERED_TABLES = "SELECT table_name FROM graph.registered_tables()"

REGISTERED_EDGES = "SELECT label FROM graph.registered_edges()"

NEIGHBORS_TRAVERSE = f"""
SELECT DISTINCT t.node_id, t.node
FROM graph.traverse(
  '{NODE_TABLE}'::regclass,
  :seed_id,
  1,
  :edge_types,
  'any',
  NULL,
  NULL,
  NULL,
  'bfs',
  'node_global',
  false,
  true,
  10000,
  0
) AS t
WHERE t.depth = 1
"""

MULTI_START_TRAVERSE = f"""
SELECT DISTINCT t.node_id, t.depth
FROM graph.traverse(
  CAST(:start_tables AS regclass[]),
  CAST(:start_ids AS text[]),
  :max_depth,
  :edge_types,
  'any',
  NULL,
  NULL,
  NULL,
  'bfs',
  'node_global',
  true,
  false,
  100000,
  0
) AS t
WHERE t.node_table = '{NODE_TABLE}'::regclass::oid
"""

SUBGRAPH_EDGES = """
SELECT ge.source_id, ge.target_id, ge.relationship_name, ge.properties
FROM graph_edge ge
WHERE ge.source_id = ANY(:ids)
  AND ge.target_id = ANY(:ids)
"""
