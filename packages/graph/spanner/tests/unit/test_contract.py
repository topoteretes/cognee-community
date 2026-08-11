"""Offline cognee-1.4.1 conformance tests. No Spanner instance, no secrets."""

from contract_suite import assert_graph_contract
from contract_suite.graph_contract import assert_registered

from cognee_community_graph_adapter_spanner import SpannerGraphAdapter, register


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(SpannerGraphAdapter)


def test_register_adds_spanner_provider():
    register()
    assert_registered("spanner", SpannerGraphAdapter)
