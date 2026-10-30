"""Per-dataset database handler: provisioning, isolation, and teardown."""

import uuid

import cognee
import pytest
from cognee.modules.users.models import DatasetDatabase
from support import ADDRESS, Concept, database_exists, server_available

from cognee_community_graph_adapter_typedb import TypeDBAdapter, TypeDBDatasetDatabaseHandler

pytestmark = pytest.mark.skipif(not server_available(), reason=f"no TypeDB server at {ADDRESS}")


def _row(info: dict) -> DatasetDatabase:
    row = DatasetDatabase()
    for key, value in info.items():
        setattr(row, key, value)
    return row


async def test_create_and_delete_dataset_database(typedb_config):
    dataset_id = uuid.uuid4()
    info = await TypeDBDatasetDatabaseHandler.create_dataset(dataset_id, None)

    assert info["graph_database_provider"] == "typedb"
    assert info["graph_dataset_database_handler"] == "typedb"
    assert info["graph_database_name"] == f"cognee_{dataset_id.hex}"
    assert info["graph_database_connection_info"] == {}  # no credentials persisted

    # The database exists with the cognee schema: the adapter can use it immediately.
    adapter = TypeDBAdapter(graph_database_url=ADDRESS, database_name=info["graph_database_name"])
    try:
        assert await adapter.is_empty()
        await adapter.add_nodes([Concept(name="tenant data")])
        assert not await adapter.is_empty()
    finally:
        await adapter.close()

    # Credentials are attached only at resolve time, from the live config.
    resolved = await TypeDBDatasetDatabaseHandler.resolve_dataset_connection_info(_row(info))
    assert resolved.graph_database_connection_info["graph_database_username"] == "admin"
    assert resolved.graph_database_connection_info["graph_database_password"] == "password"

    await TypeDBDatasetDatabaseHandler.delete_dataset(_row(info))
    assert not await database_exists(info["graph_database_name"])


async def test_datasets_are_isolated(typedb_config):
    first = await TypeDBDatasetDatabaseHandler.create_dataset(uuid.uuid4(), None)
    second = await TypeDBDatasetDatabaseHandler.create_dataset(uuid.uuid4(), None)
    a = TypeDBAdapter(graph_database_url=ADDRESS, database_name=first["graph_database_name"])
    b = TypeDBAdapter(graph_database_url=ADDRESS, database_name=second["graph_database_name"])
    try:
        node = Concept(name="only in a")
        await a.add_nodes([node])
        assert await a.has_node(str(node.id))
        assert not await b.has_node(str(node.id))
        assert await b.is_empty()
    finally:
        await a.close()
        await b.close()
        await TypeDBDatasetDatabaseHandler.delete_dataset(_row(first))
        await TypeDBDatasetDatabaseHandler.delete_dataset(_row(second))


async def test_delete_refuses_foreign_databases(typedb_config):
    with pytest.raises(ValueError):
        await TypeDBDatasetDatabaseHandler.delete_dataset(
            _row({"graph_database_name": "cognee", "graph_database_url": ADDRESS})
        )


async def test_create_requires_typedb_provider(typedb_config):
    cognee.config.set_graph_database_provider("networkx")
    try:
        with pytest.raises(ValueError):
            await TypeDBDatasetDatabaseHandler.create_dataset(uuid.uuid4(), None)
    finally:
        cognee.config.set_graph_database_provider("typedb")


async def test_delete_accepts_prune_style_row_mapping(typedb_config):
    """prune_system hands delete_dataset a read-only row mapping, not the ORM object."""
    from types import MappingProxyType

    info = await TypeDBDatasetDatabaseHandler.create_dataset(uuid.uuid4(), None)
    await TypeDBDatasetDatabaseHandler.delete_dataset(MappingProxyType(info))
    assert not await database_exists(info["graph_database_name"])


async def test_reads_never_recreate_a_dropped_dataset_database(typedb_config):
    """A stale engine handle used after prune/delete must see an empty graph,
    not re-provision the dropped database (that was leaking one orphan per prune)."""
    from support import drop_database

    info = await TypeDBDatasetDatabaseHandler.create_dataset(uuid.uuid4(), None)
    name = info["graph_database_name"]
    adapter = TypeDBAdapter(graph_database_url=ADDRESS, database_name=name)
    try:
        await adapter.add_nodes([Concept(name="before the drop")])
        await adapter.close()  # what cognee's cache eviction does to the handle
        await drop_database(name)

        assert await adapter.is_empty()
        assert await adapter.get_graph_data() == ([], [])
        assert await adapter.get_node("anything") is None
        assert await adapter.query("match $n isa node; reduce $c = count;") == []
        assert not await database_exists(name)  # reads did not recreate it

        await adapter.add_nodes([Concept(name="after the drop")])  # writes do provision
        assert await database_exists(name)
    finally:
        await adapter.close()
        await drop_database(name)


async def test_concurrent_provisioning_of_one_database_is_idempotent(typedb_config):
    """Two creators for the same dataset (two workers; cognee's dataset lock is
    per process) must both succeed: the loser of the create race proceeds to
    the idempotent schema define instead of surfacing the server's error."""
    import asyncio

    from support import drop_database

    name = f"cognee_{uuid.uuid4().hex}"
    adapters = [TypeDBAdapter(graph_database_url=ADDRESS, database_name=name) for _ in range(4)]
    try:
        await asyncio.gather(*(adapter._provision_database() for adapter in adapters))
        assert await database_exists(name)
        await adapters[0].add_nodes([Concept(name="after the race")])
        assert not await adapters[-1].is_empty()
    finally:
        for adapter in adapters:
            await adapter.close()
        await drop_database(name)


async def test_provisioning_refuses_a_database_from_the_attribute_provenance_era(typedb_config):
    """A database whose nodes still own provenance-json was written by an
    earlier version; the additive define would succeed and every artifact
    would read as unowned, so provisioning refuses it instead."""
    from typedb.driver import TransactionType

    name = f"cognee_test_{uuid.uuid4().hex[:12]}"
    adapter = TypeDBAdapter(graph_database_url=ADDRESS, database_name=name)
    try:
        await adapter._provision_database()
        driver = adapter._get_driver()
        with driver.transaction(name, TransactionType.SCHEMA) as tx:
            tx.query(
                "define attribute provenance-json value string; entity node owns provenance-json;"
            ).resolve()
            tx.commit()
        await adapter.close()

        stale = TypeDBAdapter(graph_database_url=ADDRESS, database_name=name)
        with pytest.raises(RuntimeError, match="earlier version"):
            await stale.add_nodes([Concept(name="new data")])
        await stale.close()
    finally:
        await adapter.close()
        from support import drop_database

        await drop_database(name)
