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
                "Company Name": {"value": "Acme Corp", "status": "success"},
                "Domain": {"value": "acme.com", "status": "success"},
                "ARR": {"value": 150000, "status": "success"},
            },
            {
                "record_id": "rec_002",
                "Company Name": {"value": "Globex Inc", "status": "success"},
                "Domain": {"value": "globex.com", "status": "success"},
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
                "record_id": "rec_stark_01",
                "Company Name": {"value": "Stark Industries", "status": "success"},
                "Domain": {"value": "stark.com", "status": "success"},
            }
        ],
        "cursor": "cursor_token_page_2",
    }


@pytest.fixture
def mock_multi_page_data_p2() -> dict[str, Any]:
    return {
        "data": [
            {
                "record_id": "rec_wayne_01",
                "Company Name": {"value": "Wayne Enterprises", "status": "success"},
                "Domain": {"value": "wayne.com", "status": "success"},
            }
        ],
        "cursor": None,
    }


@pytest.fixture
def mock_cell_statuses_data() -> dict[str, Any]:
    """Fixture containing cells across all documented Clay cell statuses:
    'success', 'empty', 'error', 'running'.
    """
    return {
        "data": [
            {
                "record_id": "rec_multi_status_01",
                "Company Name": {"value": "Status Corp", "status": "success"},
                "Domain": {"value": "status.io", "status": "success"},
                "Funding": {"value": None, "status": "empty"},
                "Enrichment": {"status": "error", "error": "Provider rate limit exceeded"},
                "Verified": {"status": "running"},
            }
        ],
        "cursor": None,
    }
