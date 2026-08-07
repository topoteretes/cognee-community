"""Shared, dependency-free test helpers for cognee-community adapter packages.

This directory is NOT a published package. Adapter test suites import it via a
small sys.path shim in their tests/conftest.py:

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "shared"))

and then:

    from contract_suite import assert_vector_contract, assert_graph_contract
    from contract_suite import FakeEmbeddingEngine
"""

from .fakes import FakeEmbeddingEngine
from .graph_contract import assert_graph_contract
from .vector_contract import assert_vector_contract

__all__ = [
    "FakeEmbeddingEngine",
    "assert_graph_contract",
    "assert_vector_contract",
]
