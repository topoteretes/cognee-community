"""Registration and factory-construction contract.

Mirrors the pggraph community adapter tests: the adapter must register under a
provider name and must accept the keyword shape cognee's ``_create_graph_engine``
factory uses (``database_name=...``, no ``connection_string``). These are pure
unit tests — no network, always run.
"""

import pytest

from cognee_community_graph_adapter_turbopuffer import TurbopufferGraphAdapter, register


def test_register_adds_turbopuffer_provider():
    register()
    from cognee.infrastructure.databases.graph.supported_databases import supported_databases

    assert supported_databases["turbopuffer"] is TurbopufferGraphAdapter


def test_factory_shaped_construction_accepts_database_name():
    """cognee's factory passes the per-dataset name under ``database_name`` and
    the API key under ``graph_database_key``. Construction must not require a
    live client (lazy connect)."""
    inst = TurbopufferGraphAdapter(
        graph_database_url="gcp-us-east4",
        graph_database_username="",
        graph_database_password="",
        graph_database_port="",
        graph_database_key="tpuf-fake-key",
        database_name="dataset_abc",
    )
    # The per-dataset prefix drives namespace naming.
    assert inst.database_name == "dataset_abc"


def test_explicit_graph_database_name_takes_priority_over_database_name():
    inst = TurbopufferGraphAdapter(
        graph_database_name="explicit",
        database_name="from_factory",
        graph_database_key="tpuf-fake-key",
    )
    assert inst.database_name == "explicit"


def test_namespace_names_are_prefixed_per_dataset():
    """Two logical collections (graph_node, graph_edge) per dataset, prefixed by
    database_name so datasets are isolated at the namespace level."""
    inst = TurbopufferGraphAdapter(database_name="ds1", graph_database_key="k")
    node_ns = inst._namespace_name("graph_node")
    edge_ns = inst._namespace_name("graph_edge")
    assert node_ns == "ds1_graph_node"
    assert edge_ns == "ds1_graph_edge"
    assert node_ns != edge_ns
