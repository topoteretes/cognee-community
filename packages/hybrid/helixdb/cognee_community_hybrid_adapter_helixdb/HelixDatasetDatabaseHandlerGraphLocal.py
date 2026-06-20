from typing import Optional
from uuid import UUID

from cognee.infrastructure.databases.dataset_database_handler import DatasetDatabaseHandlerInterface
from cognee.infrastructure.databases.graph import get_graph_config
from cognee.infrastructure.databases.graph.get_graph_engine import create_graph_engine
from cognee.modules.users.models import DatasetDatabase, User


class HelixDatasetDatabaseHandlerGraphLocal(DatasetDatabaseHandlerInterface):
    @classmethod
    async def create_dataset(cls, dataset_id: Optional[UUID], user: Optional[User]) -> dict:
        graph_config = get_graph_config()

        if graph_config.graph_database_provider != "helixdb":
            raise ValueError(
                "HelixDatasetDatabaseHandlerGraph can only be used with the "
                "HelixDB graph database provider."
            )

        graph_db_name = f"{dataset_id}"
        graph_db_url = graph_config.graph_database_url
        graph_db_key = graph_config.graph_database_key
        graph_db_username = graph_config.graph_database_username
        graph_db_password = graph_config.graph_database_password
        graph_db_port = graph_config.graph_database_port

        return {
            "graph_database_name": graph_db_name,
            "graph_database_url": graph_db_url,
            "graph_database_provider": graph_config.graph_database_provider,
            "graph_database_key": graph_db_key,
            "graph_dataset_database_handler": "helixdb_graph_local",
            "graph_database_connection_info": {
                "graph_database_username": graph_db_username,
                "graph_database_password": graph_db_password,
                "graph_database_port": graph_db_port,
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
            graph_database_port=dataset_database.graph_database_connection_info.get(
                "graph_database_port", ""
            ),
            graph_dataset_database_handler="",
            graph_file_path="",
        )

        await graph_engine.delete_graph()
