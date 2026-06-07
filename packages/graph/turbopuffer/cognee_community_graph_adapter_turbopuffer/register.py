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
    use_dataset_database_handler(
        "turbopuffer", TurbopufferGraphDatasetDatabaseHandler, "turbopuffer"
    )
