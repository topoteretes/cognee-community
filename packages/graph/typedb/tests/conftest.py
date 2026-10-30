"""Shared pytest setup: contract_suite + tests/support.py on sys.path, shared fixtures."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "shared"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # tests/support.py

import pytest


@pytest.fixture
def typedb_config():
    """Point cognee at the test TypeDB server (provider, credentials, dataset handler)."""
    import cognee
    from support import graph_db_config

    from cognee_community_graph_adapter_typedb import register

    register()
    cognee.config.set_graph_database_provider("typedb")
    cognee.config.set_graph_db_config(graph_db_config())
