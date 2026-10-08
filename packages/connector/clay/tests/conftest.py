"""Test fixtures and mock helpers for the Clay connector tests."""

from typing import Any

import pytest


@pytest.fixture
def sample_table_id() -> str:
    return "t_0te9i4tZEHwc9hihBXu"


@pytest.fixture
def sample_api_key() -> str:
    return "clay_api_key_test_secret_123"


@pytest.fixture
def mock_single_page_data() -> dict[str, Any]:
    return {
        "data": [
            {
                "record_id": "rec_001",
                "Company Name": {"value": "Acme Corp", "status": "successful"},
                "Domain": {"value": "acme.com", "status": "successful"},
                "ARR": {"value": 150000, "status": "successful"},
            },
            {
                "record_id": "rec_002",
                "Company Name": {"value": "Globex Inc", "status": "successful"},
                "Domain": {"value": "globex.com", "status": "successful"},
                "ARR": {"value": None, "status": "empty"},
            },
        ],
        "cursor": None,
    }


@pytest.fixture
def mock_multi_page_data_p1() -> dict[str, Any]:
    return {
        "data": [
            {
                "Company Name": {"value": "Stark Industries", "status": "successful"},
                "Domain": {"value": "stark.com", "status": "successful"},
            }
        ],
        "cursor": "cursor_token_page_2",
    }


@pytest.fixture
def mock_multi_page_data_p2() -> dict[str, Any]:
    return {
        "data": [
            {
                "Company Name": {"value": "Wayne Enterprises", "status": "successful"},
                "Domain": {"value": "wayne.com", "status": "successful"},
            }
        ],
        "cursor": None,
    }
