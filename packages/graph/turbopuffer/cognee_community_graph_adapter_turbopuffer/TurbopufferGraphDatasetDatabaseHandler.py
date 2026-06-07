"""Per-dataset isolation handler for the TurboPuffer graph adapter.

Graph analog of the vector adapter's TurbopufferDatasetDatabaseHandler, matching
cognee's DatasetDatabaseHandlerInterface. With backend access control enabled,
cognee calls create_dataset() to mint per-dataset connection info (persisted in
the DatasetDatabase table) and delete_dataset() on teardown. Each dataset maps to
its own namespace prefix (graph_database_name), so datasets are isolated at the
TurboPuffer namespace level.
"""

from typing import Optional
from uuid import UUID

from cognee.infrastructure.databases.dataset_database_handler import (
    DatasetDatabaseHandlerInterface,
)
from cognee.infrastructure.databases.graph.config import get_graph_config
from cognee.infrastructure.databases.graph.get_graph_engine import create_graph_engine
from cognee.modules.users.models import DatasetDatabase, User


class TurbopufferGraphDatasetDatabaseHandler(DatasetDatabaseHandlerInterface):
    """Handler for per-dataset TurboPuffer graph namespaces."""

    @classmethod
    async def create_dataset(cls, dataset_id: Optional[UUID], user: Optional[User]) -> dict:
        graph_config = get_graph_config()

        if graph_config.graph_database_provider != "turbopuffer":
            raise ValueError(
                "TurbopufferGraphDatasetDatabaseHandler can only be used "
                "with the turbopuffer graph database provider."
            )

        # The dataset id becomes the namespace prefix; namespaces are created
        # lazily on first write, so there is nothing to provision here.
        graph_db_name = f"{dataset_id}"

        return {
            "graph_database_provider": "turbopuffer",
            "graph_database_url": graph_config.graph_database_url,
            "graph_database_name": graph_db_name,
            "graph_database_key": graph_config.graph_database_key,
            "graph_dataset_database_handler": "turbopuffer",
        }

    @classmethod
    async def delete_dataset(cls, dataset_database: DatasetDatabase) -> None:
        engine = create_graph_engine(
            graph_database_provider="turbopuffer",
            graph_file_path="",
            graph_database_name=dataset_database.graph_database_name,
            graph_database_url=dataset_database.graph_database_url,
            graph_database_key=dataset_database.graph_database_key,
        )
        await engine.delete_graph()
