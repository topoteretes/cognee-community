from uuid import UUID

from cognee.infrastructure.databases.dataset_database_handler import (
    DatasetDatabaseHandlerInterface,
)
from cognee.infrastructure.databases.vector import get_vectordb_config
from cognee.infrastructure.databases.vector.create_vector_engine import create_vector_engine
from cognee.modules.users.models import DatasetDatabase, User


class S3VectorsDatasetDatabaseHandler(DatasetDatabaseHandlerInterface):
    """Per-dataset S3 Vectors databases: one vector bucket per dataset.

    Used when backend access control is enabled, where each dataset gets its
    own databases. The bucket is created lazily by the adapter on the first
    write, so ``create_dataset`` only records connection info.
    """

    @classmethod
    async def create_dataset(cls, dataset_id: UUID | None, user: User | None) -> dict:
        vector_config = get_vectordb_config()

        if vector_config.vector_db_provider != "s3vectors":
            raise ValueError(
                "S3VectorsDatasetDatabaseHandler can only be used with the "
                "S3Vectors vector database provider."
            )

        # Only URL-shaped values are endpoint overrides; when VECTOR_DB_URL is
        # unset, cognee fills in the default LanceDB path, which must not be
        # carried into the dataset's connection info.
        vector_db_url = vector_config.vector_db_url
        if "://" not in vector_db_url:
            vector_db_url = ""

        return {
            "vector_database_provider": vector_config.vector_db_provider,
            "vector_database_url": vector_db_url,
            "vector_database_name": f"{dataset_id}",
            "vector_dataset_database_handler": "s3vectors",
        }

    @classmethod
    async def resolve_dataset_connection_info(
        cls, dataset_database: DatasetDatabase
    ) -> DatasetDatabase:
        # Credentials are injected at connection time from the live config so
        # they are never stored in the relational database (mirrors the
        # pgvector handler).
        vector_config = get_vectordb_config()
        dataset_database.vector_database_connection_info["username"] = (
            vector_config.vector_db_username
        )
        dataset_database.vector_database_connection_info["password"] = (
            vector_config.vector_db_password
        )
        return dataset_database

    @classmethod
    async def delete_dataset(cls, dataset_database: DatasetDatabase) -> None:
        dataset_database = await cls.resolve_dataset_connection_info(dataset_database)
        connection_info = dataset_database.vector_database_connection_info

        vector_engine = create_vector_engine(
            vector_db_provider=dataset_database.vector_database_provider,
            vector_db_url=dataset_database.vector_database_url,
            vector_db_name=dataset_database.vector_database_name,
            vector_db_username=connection_info.get("username", ""),
            vector_db_password=connection_info.get("password", ""),
        )
        await vector_engine.prune()
