"""Per-dataset isolation handler for the TypeDB graph adapter.

Cognee's multi-tenant / backend-access-control mode maps every dataset to its
own graph database through a ``DatasetDatabaseHandlerInterface``. For TypeDB
that is one server-side database per dataset (``cognee_<dataset uuid hex>``),
created with the cognee schema on ``create_dataset`` and dropped on
``delete_dataset``. Credentials are never persisted: ``create_dataset`` stores
only the server address and database name (``graph_database_key`` is not a
TypeDB concept and is stored empty), and ``resolve_dataset_connection_info``
re-derives username/password from the live graph config right before a
connection is opened.

Select it with ``GRAPH_DATASET_DATABASE_HANDLER=typedb`` (cognee's built-in
provider→handler derivation only knows in-tree providers).

Only writes provision a database. Reads on a missing database see an empty
graph, so an engine handle that outlives ``prune_system`` / dataset deletion
does not recreate the dropped database (cognee's shared e2e suite calls
``is_empty()`` on such a handle after its final prune).

Isolation is per database, not per TypeDB user: every dataset database is
opened with the one service account in the graph config. Half-configured
credentials (username without password or vice versa) are rejected up front
with cognee's ``DatabaseCredentialsError``; when neither is set the adapter's
development defaults (``admin`` / ``password``) apply.
"""

import re
from collections.abc import Mapping
from uuid import UUID

from cognee.infrastructure.databases.dataset_database_handler import (
    DatasetDatabaseHandlerInterface,
)
from cognee.infrastructure.databases.exceptions import DatabaseCredentialsError
from cognee.infrastructure.databases.graph.config import get_graph_config
from cognee.infrastructure.databases.graph.get_graph_engine import graph_engine_cache
from cognee.modules.users.models import DatasetDatabase, User

from .typedb_adapter import TypeDBAdapter

TYPEDB_DATASET_DATABASE_HANDLER = "typedb"
TYPEDB_DATASET_DATABASE_PREFIX = "cognee_"
# The only shape this handler creates (prefix + 32-hex dataset uuid); anything
# else is refused on delete, however it got there.
TYPEDB_DATABASE_NAME_PATTERN = re.compile(rf"^{TYPEDB_DATASET_DATABASE_PREFIX}[0-9a-f]{{32}}$")


class TypeDBDatasetDatabaseHandler(DatasetDatabaseHandlerInterface):
    """One TypeDB database per cognee dataset."""

    @classmethod
    async def create_dataset(cls, dataset_id: UUID | None, user: User | None) -> dict:
        graph_config = get_graph_config()
        if graph_config.graph_database_provider != "typedb":
            raise ValueError(
                "TypeDBDatasetDatabaseHandler can only be used with the typedb "
                "graph database provider."
            )

        database_name = cls._database_name_for_dataset(dataset_id)
        url, username, password = cls._connection_settings(graph_config)

        # Provision the database and define the cognee schema now, so the first
        # pipeline write finds it ready (and so a bad address or bad
        # credentials fail here, not mid-cognify).
        adapter = TypeDBAdapter(
            graph_database_url=url,
            graph_database_username=username,
            graph_database_password=password,
            database_name=database_name,
        )
        try:
            await adapter._provision_database()
        finally:
            await adapter.close()

        return {
            "graph_database_provider": "typedb",
            "graph_database_url": url,
            "graph_database_name": database_name,
            "graph_database_key": "",  # not a TypeDB concept; never persist another provider's key
            "graph_dataset_database_handler": TYPEDB_DATASET_DATABASE_HANDLER,
            "graph_database_connection_info": {},
        }

    @classmethod
    async def resolve_dataset_connection_info(
        cls, dataset_database: DatasetDatabase
    ) -> DatasetDatabase:
        """Attach credentials from the live config; nothing is written back.

        Cognee hands over a detached row and discards it after connecting.
        The credentials go into the existing JSON dict in place (as the Neo4j
        handler does): the column is a plain ``JSON`` without change tracking,
        so even a caller that resolved inside a committing session would not
        flush them. Re-assigning the attribute would.
        """
        url, username, password = cls._connection_settings(get_graph_config())
        if dataset_database.graph_database_connection_info is None:
            dataset_database.graph_database_connection_info = {}
        info = dataset_database.graph_database_connection_info
        info.setdefault("graph_database_username", username)
        info.setdefault("graph_database_password", password)
        if not dataset_database.graph_database_url:
            dataset_database.graph_database_url = url
        return dataset_database

    @classmethod
    async def delete_dataset(cls, dataset_database: DatasetDatabase | Mapping) -> None:
        """Drop the dataset's database.

        Accepts the ``DatasetDatabase`` ORM object (dataset deletion) or the
        read-only row mapping ``prune_system`` iterates over.
        """
        database_name = cls._row_field(dataset_database, "graph_database_name")
        # Never drop a database this handler did not create.
        cls._validate_database_name(database_name)

        config_url, config_username, config_password = cls._connection_settings(get_graph_config())
        url = cls._row_field(dataset_database, "graph_database_url") or config_url
        info = dict(cls._row_field(dataset_database, "graph_database_connection_info") or {})
        username = info.get("graph_database_username") or config_username
        password = info.get("graph_database_password") or config_password

        # Evict by database name: cognee's cache key also carries the user-scoped
        # graph_file_path and subprocess flag, which a handler cannot rebuild.
        await graph_engine_cache.aevict_for_database(database_name)

        adapter = TypeDBAdapter(
            graph_database_url=url,
            graph_database_username=username,
            graph_database_password=password,
            database_name=database_name,
        )
        try:
            await adapter._run_sync(cls._drop_database_sync, adapter)
        finally:
            await adapter.close()

    # ------------------------------------------------------------------

    @staticmethod
    def _row_field(row, name: str):
        """Read a column from an ORM object or a dict-like row mapping."""
        if isinstance(row, Mapping):
            return row.get(name)
        return getattr(row, name, None)

    @staticmethod
    def _drop_database_sync(adapter: TypeDBAdapter) -> None:
        driver = adapter._get_driver()
        if driver.databases.contains(adapter.database_name):
            driver.databases.get(adapter.database_name).delete()

    @staticmethod
    def _connection_settings(graph_config) -> tuple[str | None, str | None, str | None]:
        """(url, username, password) for the service account, validated.

        TypeDB has no anonymous access: a username without a password (or the
        reverse) can only be a misconfiguration, so it is rejected before any
        database is created. Neither set means the adapter's development
        defaults; an empty URL means its default local address.
        """
        username = graph_config.graph_database_username or None
        password = graph_config.graph_database_password or None
        if bool(username) != bool(password):
            provided = "username" if username else "password"
            missing = "password" if username else "username"
            raise DatabaseCredentialsError(
                message=(
                    f"TypeDB credentials are incomplete: '{provided}' was provided but "
                    f"'{missing}' is missing. Set both GRAPH_DATABASE_USERNAME and "
                    "GRAPH_DATABASE_PASSWORD, or neither for the development defaults."
                )
            )
        return graph_config.graph_database_url or None, username, password

    @classmethod
    def _database_name_for_dataset(cls, dataset_id: UUID | None) -> str:
        if dataset_id is None:
            raise ValueError("dataset_id is required to create a TypeDB dataset database.")
        database_name = f"{TYPEDB_DATASET_DATABASE_PREFIX}{UUID(str(dataset_id)).hex}"
        cls._validate_database_name(database_name)
        return database_name

    @classmethod
    def _validate_database_name(cls, database_name: str) -> None:
        if not database_name or not database_name.startswith(TYPEDB_DATASET_DATABASE_PREFIX):
            raise ValueError(
                "Refusing to manage a TypeDB database that was not created by the "
                f"typedb dataset handler: {database_name!r}"
            )
        if not TYPEDB_DATABASE_NAME_PATTERN.fullmatch(database_name):
            raise ValueError(f"Invalid TypeDB dataset database name: {database_name!r}")
