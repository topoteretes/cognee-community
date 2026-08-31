"""
Smoke tests for the Turso adapter package scaffold.
"""

from cognee_community_hybrid_adapter_turso import register


def test_register_is_exposed():
    assert callable(register)
