from uuid import UUID

from cognee.infrastructure.databases.dataset_database_handler import DatasetDatabaseHandlerInterface
from cognee.infrastructure.databases.vector import get_vectordb_config
from cognee.infrastructure.databases.vector.create_vector_engine import create_vector_engine
from cognee.modules.users.models import DatasetDatabase, User


class ElasticsearchDatasetDatabaseHandler(DatasetDatabaseHandlerInterface):
    """Map every cognee dataset to its own ``database_name`` on a shared cluster.

    Datasets share the cluster and its indices; the adapter scopes rows by
    ``database_name``, so handing each dataset its id as the name is enough to
    keep them apart. No index is created here: the adapter creates indices on
    first write, and ``delete_dataset`` removes the dataset's rows and any
    indices left empty.
    """

    @classmethod
    async def create_dataset(cls, dataset_id: UUID | None, user: User | None) -> dict:
        vector_config = get_vectordb_config()

        if vector_config.vector_db_provider != "elasticsearch":
            raise ValueError(
                "ElasticsearchDatasetDatabaseHandler can only be used with the "
                "Elasticsearch vector database provider."
            )

        return {
            "vector_database_provider": vector_config.vector_db_provider,
            "vector_database_url": vector_config.vector_db_url,
            "vector_database_key": vector_config.vector_db_key,
            "vector_database_name": f"{dataset_id}",
            "vector_dataset_database_handler": "elasticsearch",
        }

    @classmethod
    async def delete_dataset(cls, dataset_database: DatasetDatabase) -> None:
        vector_engine = create_vector_engine(
            vector_db_provider=dataset_database.vector_database_provider,
            vector_db_url=dataset_database.vector_database_url,
            vector_db_key=dataset_database.vector_database_key,
            vector_db_name=dataset_database.vector_database_name,
        )
        await vector_engine.prune()
