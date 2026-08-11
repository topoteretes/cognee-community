"""Offline cognee-1.4.1 conformance tests. No TuringDB server, no secrets."""

from cognee_community_graph_adapter_turingdb.turingdb_adapter import TuringDBAdapter
from contract_suite import assert_graph_contract
from contract_suite.graph_contract import assert_registered


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(TuringDBAdapter)


def test_register_adds_turingdb_provider():
    import cognee_community_graph_adapter_turingdb.register  # noqa: F401

    assert_registered("turingdb", TuringDBAdapter)
