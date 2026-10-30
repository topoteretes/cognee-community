from typing import Optional
from uuid import UUID

from cognee.infrastructure.databases.dataset_database_handler import DatasetDatabaseHandlerInterface
from cognee.infrastructure.databases.vector import get_vectordb_config
from cognee.infrastructure.databases.vector.create_vector_engine import (
    aevict_vector_engines_for_database,
)
from cognee.modules.users.models import DatasetDatabase, User

from .topk_adapter import TopKAdapter


class TopKDatasetDatabaseHandler(DatasetDatabaseHandlerInterface):
    @classmethod
    async def create_dataset(cls, dataset_id: Optional[UUID], user: Optional[User]) -> dict:
        vector_config = get_vectordb_config()

        if vector_config.vector_db_provider != "topk":
            raise ValueError(
                "TopKDatasetDatabaseHandler can only be used with the "
                "TopK vector database provider."
            )

        return {
            "vector_database_provider": vector_config.vector_db_provider,
            "vector_database_url": vector_config.vector_db_url,
            "vector_database_key": vector_config.vector_db_key,
            "vector_database_name": f"{dataset_id}",
            "vector_dataset_database_handler": "topk",
        }

    @classmethod
    async def delete_dataset(cls, dataset_database: DatasetDatabase) -> None:
        await aevict_vector_engines_for_database(dataset_database.vector_database_name)

        adapter = TopKAdapter(
            url=dataset_database.vector_database_url,
            api_key=dataset_database.vector_database_key,
            database_name=dataset_database.vector_database_name,
        )
        await adapter.prune()
