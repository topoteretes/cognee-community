"""Offline cognee-1.4.x conformance tests. No NeuG engine, no database, no secrets.

The adapters import cleanly without the ``neug`` bindings (the native import is
lazy inside the connection manager), so the signature-level contract and the
registration wiring are asserted here on every PR. Anything that needs a real
embedded database lives in ``tests/integration`` and is skipped when ``neug``
is unavailable.
"""

from cognee_community_hybrid_adapter_neug.graph_adapter import NeuGGraphAdapter
from cognee_community_hybrid_adapter_neug.vector_adapter import NeuGVectorAdapter
from contract_suite import assert_graph_contract, assert_vector_contract
from contract_suite.graph_contract import assert_registered as graph_registered
from contract_suite.vector_contract import assert_registered as vector_registered


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(NeuGGraphAdapter)


def test_conforms_to_cognee_vector_contract():
    # instantiate=False: NeuGVectorAdapter.__init__ acquires the shared embedded
    # connection manager, which opens a real NeuG database and needs the native
    # engine. The offline tier asserts the signature-level contract only.
    assert_vector_contract(NeuGVectorAdapter, instantiate=False)


def test_register_adds_neug_providers():
    import cognee_community_hybrid_adapter_neug.register  # noqa: F401

    graph_registered("neug", NeuGGraphAdapter)
    vector_registered("neug", NeuGVectorAdapter)
