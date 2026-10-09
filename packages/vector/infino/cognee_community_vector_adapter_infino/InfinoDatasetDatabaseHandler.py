import os
from typing import Optional
from uuid import UUID

from cognee.base_config import get_base_config
from cognee.infrastructure.databases.dataset_database_handler import (
    DatasetDatabaseHandlerInterface,
)
from cognee.infrastructure.databases.vector import get_vectordb_config
from cognee.infrastructure.databases.vector.create_vector_engine import (
    vector_engine_cache,
)
from cognee.infrastructure.files.storage.get_file_storage import get_file_storage
from cognee.modules.users.models import DatasetDatabase, User


class InfinoDatasetDatabaseHandler(DatasetDatabaseHandlerInterface):
    """Per-dataset Infino catalogs, mirroring the local LanceDB handler: each
    dataset gets its own directory of Parquet superfiles under the system
    root, so deleting a dataset is deleting its directory."""

    @classmethod
    async def create_dataset(cls, dataset_id: Optional[UUID], user: Optional[User]) -> dict:
        vector_config = get_vectordb_config()
        base_config = get_base_config()

        if vector_config.vector_db_provider != "infino":
            raise ValueError(
                "InfinoDatasetDatabaseHandler can only be used with the Infino "
                "vector database provider."
            )

        databases_directory_path = os.path.join(
            base_config.system_root_directory, "databases", str(user.id)
        )
        await get_file_storage(databases_directory_path).ensure_directory_exists()

        vector_db_name = f"{dataset_id}.infino.db"

        return {
            "vector_database_provider": vector_config.vector_db_provider,
            "vector_database_url": os.path.join(databases_directory_path, vector_db_name),
            "vector_database_key": vector_config.vector_db_key,
            "vector_database_name": vector_db_name,
            "vector_dataset_database_handler": "infino",
        }

    @classmethod
    async def delete_dataset(cls, dataset_database: DatasetDatabase):
        # Evict any cached engine for this database before removing the files,
        # so nothing holds the store open while it disappears (the same order
        # the LanceDB handler documents).
        await vector_engine_cache.aevict_for_database(dataset_database.vector_database_name)

        databases_directory_path = os.path.dirname(dataset_database.vector_database_url)
        file_storage = get_file_storage(databases_directory_path)
        await file_storage.remove_all(dataset_database.vector_database_name)
