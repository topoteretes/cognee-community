"""Pytest fixtures for Knock connector tests."""

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONNECTOR_DIR = TESTS_DIR.parent

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(CONNECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(CONNECTOR_DIR))

import pytest  # noqa: E402
from fake_knock import FakeKnockClient, make_sample_knock_data  # noqa: E402


@pytest.fixture
def sample_data():
    return make_sample_knock_data()


@pytest.fixture
def fake_client(sample_data):
    return FakeKnockClient(sample_data=sample_data)
