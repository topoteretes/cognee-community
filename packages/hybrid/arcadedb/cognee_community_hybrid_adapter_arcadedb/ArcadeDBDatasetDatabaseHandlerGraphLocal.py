from typing import Optional
from uuid import UUID

from cognee.infrastructure.databases.dataset_database_handler import DatasetDatabaseHandlerInterface
from cognee.infrastructure.databases.graph import get_graph_config
from cognee.infrastructure.databases.graph.get_graph_engine import create_graph_engine
from cognee.modules.users.models import DatasetDatabase, User


class ArcadeDBDatasetDatabaseHandlerGraphLocal(DatasetDatabaseHandlerInterface):
    @classmethod
    async def create_dataset(cls, dataset_id: Optional[UUID], user: Optional[User]) -> dict:
        graph_config = get_graph_config()

        if graph_config.graph_database_provider != "arcadedb":
            raise ValueError(
                "ArcadeDBDatasetDatabaseHandlerGraph can only be used with the "
                "ArcadeDB graph database provider."
            )

        return {
            "graph_database_name": f"{dataset_id}",
            "graph_database_url": graph_config.graph_database_url,
            "graph_database_provider": graph_config.graph_database_provider,
            "graph_database_key": graph_config.graph_database_key,
            "graph_dataset_database_handler": "arcadedb_graph_local",
            "graph_database_connection_info": {
                "graph_database_username": graph_config.graph_database_username,
                "graph_database_password": graph_config.graph_database_password,
            },
        }

    @classmethod
    async def delete_dataset(cls, dataset_database: DatasetDatabase) -> None:
        graph_engine = create_graph_engine(
            graph_database_provider=dataset_database.graph_database_provider,
            graph_database_url=dataset_database.graph_database_url,
            graph_database_name=dataset_database.graph_database_name,
            graph_database_key=dataset_database.graph_database_key,
            graph_database_username=dataset_database.graph_database_connection_info.get(
                "graph_database_username", ""
            ),
            graph_database_password=dataset_database.graph_database_connection_info.get(
                "graph_database_password", ""
            ),
            graph_dataset_database_handler="",
            graph_file_path="",
        )

        await graph_engine.delete_graph()