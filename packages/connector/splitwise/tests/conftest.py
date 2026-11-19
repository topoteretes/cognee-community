"""Pytest fixtures and configuration for Splitwise connector tests."""

import pytest


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")
