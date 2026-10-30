"""Shared e2e fixtures: cognee roots isolated per test, TypeDB e2e database config."""

import os
import re
import warnings

import cognee
import pytest
from support import E2E_DATABASE, drop_database, list_databases, server_available

DATASET_DATABASE = re.compile(r"cognee_[0-9a-f]{32}")


@pytest.fixture
def isolated_roots(tmp_path):
    """Point cognee's data/system roots at a temp dir (as run_graph_db_test does)
    so pruning never touches the installed package's default storage."""
    from cognee.base_config import get_base_config

    base_config = get_base_config()
    prev_data_root = base_config.data_root_directory
    prev_system_root = base_config.system_root_directory
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    yield
    cognee.config.data_root_directory(prev_data_root)
    cognee.config.system_root_directory(prev_system_root)


@pytest.fixture(autouse=True)
async def sweep_dataset_databases():
    """Report per-dataset databases a test leaves behind; drop them when opted in.

    The per-test temp roots take cognee's relational registry with them, so a
    dataset database not dropped by the test itself would be orphaned on the
    server. Cleaning up is the test's job; this fixture makes a leak visible.
    It cannot tell a leaked database from one another process created on the
    same server during the test, so it only drops with ``TYPEDB_E2E_SWEEP=1``
    (set in CI, where the server is the job's own container).
    """
    if not server_available():
        yield
        return
    before = await list_databases()
    yield
    leaked = sorted(
        name for name in await list_databases() - before if DATASET_DATABASE.fullmatch(name)
    )
    if not leaked:
        return
    if os.environ.get("TYPEDB_E2E_SWEEP") == "1":
        for name in leaked:
            await drop_database(name)
        warnings.warn(f"test left dataset databases behind (dropped): {leaked}", stacklevel=1)
    else:
        warnings.warn(
            f"test left dataset databases behind (kept; TYPEDB_E2E_SWEEP=1 drops them): {leaked}",
            stacklevel=1,
        )


@pytest.fixture(scope="session", autouse=True)
def drop_shared_e2e_database():
    """Drop the tier's shared ``cognee_e2e`` database once the session ends.

    Access-control-off runs write to it, and prune_system only empties a
    shared database, so it would otherwise outlive the test run. Sync on
    purpose: a session-scoped async fixture would need its own loop scope.
    """
    yield
    if not server_available():
        return
    from support import ADDRESS, PASSWORD, USERNAME

    from cognee_community_graph_adapter_typedb import TypeDBAdapter

    adapter = TypeDBAdapter(
        graph_database_url=ADDRESS,
        graph_database_username=USERNAME,
        graph_database_password=PASSWORD,
    )
    try:
        driver = adapter._get_driver()
        if driver.databases.contains(E2E_DATABASE):
            driver.databases.get(E2E_DATABASE).delete()
    finally:
        adapter._close_sync()
