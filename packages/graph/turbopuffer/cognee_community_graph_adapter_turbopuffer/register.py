"""Register the TurboPuffer graph adapter with cognee under the "turbopuffer"
graph provider name. Call ``register()`` before
``cognee.config.set_graph_database_provider("turbopuffer")``.
"""

from cognee.infrastructure.databases.graph.use_graph_adapter import use_graph_adapter

from .turbopuffer_graph_adapter import TurbopufferGraphAdapter


def register() -> None:
    use_graph_adapter("turbopuffer", TurbopufferGraphAdapter)
