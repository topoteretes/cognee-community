"""Pytest fixtures for Resend connector tests."""

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONNECTOR_DIR = TESTS_DIR.parent

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(CONNECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(CONNECTOR_DIR))

import pytest  # noqa: E402
from fake_resend import FakeResendClient, make_sample_resend_data  # noqa: E402


@pytest.fixture
def sample_data():
    return make_sample_resend_data()


@pytest.fixture
def fake_client(sample_data):
    return FakeResendClient(sample_data=sample_data)
