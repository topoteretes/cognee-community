"""Test fixtures and mock data for Databricks connector offline unit tests."""

import base64
from typing import Any

import pytest


@pytest.fixture
def sample_host() -> str:
    return "https://dbc-12345678.cloud.databricks.com"


@pytest.fixture
def sample_token() -> str:
    return "dapi_test_pat_token_secret_123"


@pytest.fixture
def mock_notebook_content_b64() -> str:
    source_code = "# Databricks notebook source\n# COMMAND ----------\nprint('Ingesting pipeline')"
    return base64.b64encode(source_code.encode("utf-8")).decode("utf-8")


@pytest.fixture
def mock_workspace_items(mock_notebook_content_b64: str) -> dict[str, Any]:
    return {
        "objects": [
            {
                "object_id": 987654321,
                "path": "/Shared/etl_pipeline",
                "object_type": "NOTEBOOK",
                "language": "PYTHON",
                "modified_at": 1700000000000,
            },
            {
                "object_id": 111222333,
                "path": "/Shared/subfolder",
                "object_type": "DIRECTORY",
            },
        ]
    }


@pytest.fixture
def mock_catalogs_data() -> dict[str, Any]:
    return {"catalogs": [{"name": "main"}]}


@pytest.fixture
def mock_schemas_data() -> dict[str, Any]:
    return {"schemas": [{"name": "analytics"}]}


@pytest.fixture
def mock_tables_data() -> dict[str, Any]:
    return {
        "tables": [
            {
                "name": "dim_customers",
                "table_type": "MANAGED",
                "comment": "Customer dimensions and profiles",
                "columns": [
                    {"name": "customer_id", "type_name": "BIGINT", "comment": "Unique ID"},
                    {"name": "email", "type_name": "STRING", "comment": "Customer email"},
                ],
            }
        ]
    }


@pytest.fixture
def mock_describe_history_response() -> dict[str, Any]:
    return {
        "statement_id": "stmt-history-001",
        "status": {"state": "SUCCEEDED"},
        "manifest": {
            "schema": {
                "columns": [
                    {"name": "version", "type_text": "BIGINT"},
                    {"name": "timestamp", "type_text": "TIMESTAMP"},
                    {"name": "operation", "type_text": "STRING"},
                ]
            }
        },
        "result": {
            "data_array": [
                [42, 1700000050000, "MERGE"],
                [41, 1700000040000, "OPTIMIZE"],  # Maintenance op, should be skipped
            ]
        },
    }
