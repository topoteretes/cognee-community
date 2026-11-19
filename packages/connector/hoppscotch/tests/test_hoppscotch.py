"""Unit tests for the Hoppscotch API collections & environments connector.

Test fixtures are generated programmatically in ``tmp_path`` so tests are self-contained
and do not rely on checked-in external network resources.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_hoppscotch.hoppscotch import (
    DOCUMENT_SOURCE_ATTR,
    HOPPSCOTCH_SOURCE_NAME,
    HOPPSCOTCH_TABLE_NAME,
    hoppscotch_source,
    iter_hoppscotch_requests,
    load_environment_variables,
)

SAMPLE_ENVIRONMENT = {
    "v": 1,
    "id": "env_staging",
    "name": "Staging",
    "variables": [
        {"key": "baseUrl", "value": "https://staging.api.example.com", "secret": False},
        {"key": "apiVersion", "value": "v1", "secret": False},
    ],
}

SAMPLE_COLLECTION = {
    "v": 2,
    "name": "Acme Payments API",
    "folders": [
        {
            "v": 2,
            "name": "Customers",
            "folders": [],
            "requests": [
                {
                    "v": 1,
                    "name": "Get Customer",
                    "method": "GET",
                    "endpoint": "<<baseUrl>>/<<apiVersion>>/customers/{id}",
                    "params": [
                        {"key": "expand", "value": "subscriptions", "active": True, "description": "Expand nested objects"}
                    ],
                    "headers": [
                        {"key": "Accept", "value": "application/json", "active": True}
                    ],
                    "auth": {"authType": "bearer", "authActive": True},
                }
            ],
        },
        {
            "v": 2,
            "name": "Checkout",
            "folders": [],
            "requests": [
                {
                    "v": 1,
                    "name": "Create Session",
                    "method": "POST",
                    "endpoint": "<<baseUrl>>/<<apiVersion>>/checkout/sessions",
                    "headers": [
                        {"key": "Content-Type", "value": "application/json", "active": True}
                    ],
                    "body": {
                        "contentType": "application/json",
                        "body": '{"currency": "usd", "amount": 2000}',
                    },
                    "auth": {"authType": "bearer", "authActive": True},
                },
                {
                    "v": 1,
                    "name": "Expire Session",
                    "method": "POST",
                    "endpoint": "<<baseUrl>>/<<apiVersion>>/checkout/sessions/{id}/expire",
                    "auth": {"authType": "bearer", "authActive": True},
                },
            ],
        },
    ],
    "requests": [
        {
            "v": 1,
            "name": "Health Check",
            "method": "GET",
            "endpoint": "<<baseUrl>>/health",
            "auth": {"authType": "none", "authActive": False},
        }
    ],
}


def test_load_environment_variables(tmp_path):
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps(SAMPLE_ENVIRONMENT))

    vars_dict = load_environment_variables(env_file)
    assert vars_dict["baseUrl"] == "https://staging.api.example.com"
    assert vars_dict["apiVersion"] == "v1"


def test_iter_hoppscotch_requests_with_environment(tmp_path):
    coll_file = tmp_path / "coll.json"
    coll_file.write_text(json.dumps(SAMPLE_COLLECTION))

    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps(SAMPLE_ENVIRONMENT))

    docs = list(iter_hoppscotch_requests(coll_file, environment_path=env_file))

    # 1 top-level request + 1 customer request + 2 checkout requests = 4 total
    assert len(docs) == 4

    # Verify variable substitution
    health_req = next(d for d in docs if d["name"] == "Health Check")
    assert health_req["endpoint"] == "https://staging.api.example.com/health"
    assert "[GET] Health Check" in health_req["title"]
    assert "Authentication: none" in health_req["content"]

    # Verify nested folder request
    create_session = next(d for d in docs if d["name"] == "Create Session")
    assert create_session["method"] == "POST"
    assert create_session["endpoint"] == "https://staging.api.example.com/v1/checkout/sessions"
    assert "Acme Payments API > Checkout" in create_session["content"]
    assert '{"currency": "usd", "amount": 2000}' in create_session["content"]
    assert create_session["auth_type"] == "bearer"


def test_primary_key_is_stable_and_unique(tmp_path):
    coll_file = tmp_path / "coll.json"
    coll_file.write_text(json.dumps(SAMPLE_COLLECTION))

    docs = list(iter_hoppscotch_requests(coll_file))
    ids = [d["id"] for d in docs]

    assert len(ids) == len(set(ids)), "All primary keys must be strictly unique"
    for doc_id in ids:
        assert doc_id.startswith("hoppscotch:acme_payments_api:")


def test_snapshot_deletion_detection(tmp_path):
    """Simulate a second export where 'Expire Session' was deleted upstream."""
    coll_v1 = tmp_path / "coll_v1.json"
    coll_v1.write_text(json.dumps(SAMPLE_COLLECTION))

    # Create v2 without Expire Session
    modified_coll = json.loads(json.dumps(SAMPLE_COLLECTION))
    modified_coll["folders"][1]["requests"] = [
        r for r in modified_coll["folders"][1]["requests"] if r["name"] != "Expire Session"
    ]
    coll_v2 = tmp_path / "coll_v2.json"
    coll_v2.write_text(json.dumps(modified_coll))

    v1_docs = list(iter_hoppscotch_requests(coll_v1))
    v2_docs = list(iter_hoppscotch_requests(coll_v2))

    assert len(v1_docs) == 4
    assert len(v2_docs) == 3

    v1_names = {d["name"] for d in v1_docs}
    v2_names = {d["name"] for d in v2_docs}

    assert "Expire Session" in v1_names
    assert "Expire Session" not in v2_names


def test_missing_and_corrupt_files(tmp_path):
    # Non-existent file raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        list(iter_hoppscotch_requests(tmp_path / "nonexistent.json"))

    # Corrupt JSON file is gracefully skipped
    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_text("{invalid json...}")
    docs = list(iter_hoppscotch_requests(corrupt_file))
    assert docs == []


def test_directory_with_multiple_collections(tmp_path):
    dir_path = tmp_path / "exports"
    dir_path.mkdir()

    (dir_path / "coll_1.json").write_text(json.dumps(SAMPLE_COLLECTION))
    (dir_path / "coll_2.json").write_text(
        json.dumps({
            "name": "Auth API",
            "requests": [
                {"name": "Login", "method": "POST", "endpoint": "/auth/login"}
            ]
        })
    )

    docs = list(iter_hoppscotch_requests(dir_path))
    assert len(docs) == 5  # 4 from coll_1 + 1 from coll_2
    names = {d["name"] for d in docs}
    assert "Login" in names
    assert "Health Check" in names


def test_hoppscotch_source_resource():
    mock_dlt = MagicMock()

    def mock_resource_decorator(*args, **kwargs):
        def decorator(fn):
            wrapper = MagicMock()
            wrapper.__name__ = fn.__name__
            wrapper._kwargs = kwargs
            return wrapper
        return decorator

    mock_dlt.resource = mock_resource_decorator

    with patch.dict("sys.modules", {"dlt": mock_dlt}):
        resource = hoppscotch_source("/dummy/path")

        assert getattr(resource, DOCUMENT_SOURCE_ATTR) == HOPPSCOTCH_SOURCE_NAME
        assert resource._kwargs.get("name") == HOPPSCOTCH_TABLE_NAME
        assert resource._kwargs.get("primary_key") == "id"
        assert resource._kwargs.get("write_disposition") == "replace"
