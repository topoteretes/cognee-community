"""Offline cognee-1.4.1 conformance tests. No TurboPuffer account, no secrets.

Complements tests/unit/test_registration.py (registration + handler specifics)
with the shared 1.4.1 call-shape contract.
"""

from cognee_community_graph_adapter_turbopuffer import TurbopufferGraphAdapter, register
from contract_suite import assert_graph_contract
from contract_suite.graph_contract import assert_registered


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(TurbopufferGraphAdapter)


def test_register_adds_turbopuffer_provider():
    register()
    assert_registered("turbopuffer", TurbopufferGraphAdapter)
