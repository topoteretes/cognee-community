"""Helpers shared by the integration and e2e tiers (put on sys.path by tests/conftest.py)."""

import os
import socket

from cognee.infrastructure.engine import DataPoint

from cognee_community_graph_adapter_typedb import TypeDBAdapter

ADDRESS = os.environ.get("GRAPH_DATABASE_URL", "127.0.0.1:1729")
# The e2e tier's shared database (access-control-off runs); never the default "cognee".
E2E_DATABASE = "cognee_e2e"
USERNAME = os.environ.get("GRAPH_DATABASE_USERNAME", "admin")
PASSWORD = os.environ.get("GRAPH_DATABASE_PASSWORD", "password")


def server_available() -> bool:
    host, _, port = ADDRESS.rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=2):
            return True
    except OSError:
        return False


def graph_db_config(**overrides) -> dict:
    """The set_graph_db_config() dict for the test server, incl. the dataset handler."""
    return {
        "graph_database_url": ADDRESS,
        "graph_database_username": USERNAME,
        "graph_database_password": PASSWORD,
        "graph_dataset_database_handler": "typedb",
        **overrides,
    }


async def database_exists(name: str) -> bool:
    """Whether a database exists on the test server (driver call off the event loop)."""
    adapter = TypeDBAdapter(
        graph_database_url=ADDRESS,
        graph_database_username=USERNAME,
        graph_database_password=PASSWORD,
    )
    try:
        return await adapter._run_sync(lambda: adapter._get_driver().databases.contains(name))
    finally:
        await adapter.close()


async def list_databases() -> set[str]:
    adapter = TypeDBAdapter(
        graph_database_url=ADDRESS,
        graph_database_username=USERNAME,
        graph_database_password=PASSWORD,
    )
    try:
        return await adapter._run_sync(
            lambda: {db.name for db in adapter._get_driver().databases.all()}
        )
    finally:
        await adapter.close()


async def drop_database(name: str) -> None:
    adapter = TypeDBAdapter(
        graph_database_url=ADDRESS,
        graph_database_username=USERNAME,
        graph_database_password=PASSWORD,
    )

    def drop():
        driver = adapter._get_driver()
        if driver.databases.contains(name):
            driver.databases.get(name).delete()

    try:
        await adapter._run_sync(drop)
    finally:
        await adapter.close()


class Concept(DataPoint):
    name: str
    description: str | None = None
    metadata: dict = {"index_fields": ["name"]}
