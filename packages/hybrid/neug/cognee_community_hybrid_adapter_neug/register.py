from cognee.infrastructure.databases.graph import use_graph_adapter
from cognee.infrastructure.databases.vector import use_vector_adapter

from .graph_adapter import NeuGGraphAdapter
from .vector_adapter import NeuGVectorAdapter

# NeuG is a single embedded database that serves both the graph and the vector
# store through one process-level connection manager, so the same provider name
# is registered for both factories. Setting GRAPH_DATABASE_PROVIDER and
# VECTOR_DB_PROVIDER to "neug" (after importing this module) is what makes the
# provider valid.
use_graph_adapter("neug", NeuGGraphAdapter)
use_vector_adapter("neug", NeuGVectorAdapter)

# NeuG is single-tenant: graph and vector share ONE database file, so there is
# no per-dataset isolation to hand out and no dataset-database handler to
# register. Run cognee with ENABLE_BACKEND_ACCESS_CONTROL=false; with access
# control left on (the default) core raises EnvironmentError because this
# provider ships no handler.
