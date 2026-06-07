"""Register the TurboPuffer graph adapter with cognee under the "turbopuffer"
graph provider name, plus its per-dataset database handler (for backend access
control / multi-tenant mode). Call ``register()`` before
``cognee.config.set_graph_database_provider("turbopuffer")``.
"""

from cognee.infrastructure.databases.dataset_database_handler import (
    use_dataset_database_handler,
)
from cognee.infrastructure.databases.graph.use_graph_adapter import use_graph_adapter

from .turbopuffer_graph_adapter import TurbopufferGraphAdapter
from .TurbopufferGraphDatasetDatabaseHandler import TurbopufferGraphDatasetDatabaseHandler


def register() -> None:
    use_graph_adapter("turbopuffer", TurbopufferGraphAdapter)
    # Register under a graph-specific handler key so it does not collide with the
    # TurboPuffer *vector* adapter, which registers its own handler under
    # "turbopuffer". The third argument is the graph_database_provider this
    # handler serves. Select it with GRAPH_DATASET_DATABASE_HANDLER=turbopuffer_graph.
    use_dataset_database_handler(
        "turbopuffer_graph", TurbopufferGraphDatasetDatabaseHandler, "turbopuffer"
    )
