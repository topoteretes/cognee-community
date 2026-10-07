"""Unit tests for the CircleCI connector. Runnable without a live CircleCI token."""

from cognee_community_connector_circleci import circleci_source


def test_source_is_exported():
    assert callable(circleci_source)
