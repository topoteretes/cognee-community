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

    Credentials are never persisted: the stored row keeps an empty API key and
    no username/password, and ``resolve_dataset_connection_info`` fills all
    three from the live vector config right before cognee connects (as core's
    pgvector handler does for its username/password). The adapter connects by
    URL only, so no host/port is stored either.
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
            "vector_database_key": "",  # resolved at connect time, never stored
            "vector_database_name": f"{dataset_id}",
            "vector_database_connection_info": {},
            "vector_dataset_database_handler": "elasticsearch",
        }

    @classmethod
    async def resolve_dataset_connection_info(
        cls, dataset_database: DatasetDatabase
    ) -> DatasetDatabase:
        """Fill the API key and basic-auth credentials from the live vector config.

        cognee calls this on a row detached from its session before building the
        dataset's engine, so nothing set here is written back to the database.
        """
        vector_config = get_vectordb_config()
        dataset_database.vector_database_key = vector_config.vector_db_key
        if dataset_database.vector_database_connection_info is None:
            dataset_database.vector_database_connection_info = {}
        info = dataset_database.vector_database_connection_info
        info["username"] = vector_config.vector_db_username
        info["password"] = vector_config.vector_db_password
        return dataset_database

    @classmethod
    async def delete_dataset(cls, dataset_database: DatasetDatabase) -> None:
        # Credentials come straight from the config rather than through
        # resolve_dataset_connection_info: cognee's delete_dataset passes a row
        # still attached to its session and commits afterwards, so assigning the
        # key onto the row here would persist it.
        vector_config = get_vectordb_config()
        vector_engine = create_vector_engine(
            vector_db_provider=dataset_database.vector_database_provider,
            vector_db_url=dataset_database.vector_database_url,
            vector_db_key=vector_config.vector_db_key,
            vector_db_name=dataset_database.vector_database_name,
            vector_db_username=vector_config.vector_db_username,
            vector_db_password=vector_config.vector_db_password,
        )
        await vector_engine.prune()
