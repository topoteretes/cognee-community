"""Per-dataset isolation handler for the TurboPuffer graph adapter — SKELETON.

Graph analog of the vector adapter's TurbopufferDatasetDatabaseHandler. cognee
persists the returned mapping in its DatasetDatabase table, then constructs the
adapter with ``database_name=<dataset graph db name>`` so each dataset's nodes
and edges live in their own namespace prefix.
"""

from typing import Any, Dict


class TurbopufferGraphDatasetDatabaseHandler:
    async def create_dataset(self, dataset_id: Any) -> Dict[str, str]:
        # Each dataset's graph lives under its own namespace prefix.
        return {"graph_database_name": str(dataset_id)}

    async def delete_dataset(self, dataset_id: Any, **kwargs: Any) -> None:
        # Drop both namespaces for this dataset by delegating to the adapter's
        # delete_graph(), bound to the dataset's namespace prefix.
        from .turbopuffer_graph_adapter import TurbopufferGraphAdapter

        adapter = TurbopufferGraphAdapter(database_name=str(dataset_id), **kwargs)
        await adapter.delete_graph()
