"""Unit tests for the AsyncAPI event-driven specification dlt connector.

Covers:
- asyncapi.yaml parsing and schema traversal
- Channel operation rendering (publish/subscribe, payload properties)
- Full-snapshot sync and forget-on-delete semantics
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cognee_community_connector_asyncapi.asyncapi import (
    ASYNCAPI_SOURCE_NAME,
    _load_spec_file,
    _render_channel_row,
    _render_payload_properties,
    asyncapi_source,
)


@pytest.fixture
def mock_asyncapi_file(tmp_path: Path) -> Path:
    """Creates a temporary mock AsyncAPI specification file."""
    spec_data = {
        "asyncapi": "2.6.0",
        "info": {
            "title": "Account Event Service",
            "version": "1.0.0",
            "description": "Publishes and subscribes to account lifecycle events.",
        },
        "servers": {
            "production-kafka": {
                "url": "kafka.example.com:9092",
                "protocol": "kafka",
            }
        },
        "channels": {
            "accounts/signup": {
                "subscribe": {
                    "summary": "User Signup Event",
                    "description": "Triggered when a new user registers.",
                    "message": {
                        "name": "UserSignedUpMessage",
                        "contentType": "application/json",
                        "payload": {
                            "type": "object",
                            "required": ["userId", "email"],
                            "properties": {
                                "userId": {
                                    "type": "string",
                                    "description": "UUID of the registered user",
                                },
                                "email": {
                                    "type": "string",
                                    "description": "User email address",
                                },
                                "plan": {
                                    "type": "string",
                                    "description": "Selected billing tier",
                                },
                            },
                        },
                    },
                }
            }
        },
    }
    file_path = tmp_path / "asyncapi.yaml"
    file_path.write_text(yaml.dump(spec_data), encoding="utf-8")
    return file_path


def test_load_spec_file(mock_asyncapi_file: Path):
    data = _load_spec_file(mock_asyncapi_file)
    assert data.get("asyncapi") == "2.6.0"
    assert data["info"]["title"] == "Account Event Service"


def test_render_payload_properties():
    schema = {
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string", "description": "Identifier"},
            "count": {"type": "integer"},
        },
    }
    lines = _render_payload_properties(schema)
    assert len(lines) == 2
    assert "- **id** (`string`) *(required)* - Identifier" in lines
    assert "- **count** (`integer`)" in lines


def test_render_channel_row():
    op_data = {
        "summary": "Order Placed",
        "description": "Event emitted when an order succeeds.",
        "message": {
            "name": "OrderPlacedEvent",
            "contentType": "application/json",
            "payload": {
                "type": "object",
                "properties": {"orderId": {"type": "string"}},
            },
        },
    }
    row = _render_channel_row(
        app_title="E-Commerce Orders",
        channel_name="orders/placed",
        operation_type="subscribe",
        operation_data=op_data,
        server_names=["kafka-cluster"],
    )

    assert row["id"] == "asyncapi:orders/placed:subscribe"
    assert row["url"] == "/orders/placed"
    assert "AsyncAPI: SUBSCRIBE orders/placed" in row["title"]
    assert "# E-Commerce Orders" in row["content"]
    assert "**Target Brokers/Servers:** kafka-cluster" in row["content"]
    assert "### Message: OrderPlacedEvent" in row["content"]
    assert "- **orderId** (`string`)" in row["content"]


def test_asyncapi_source_raises_on_invalid_path(tmp_path: Path):
    with pytest.raises(ValueError, match="Valid AsyncAPI file or directory required"):
        asyncapi_source(spec_path=tmp_path / "nonexistent.yaml")


def test_full_snapshot_sync_and_forget_on_delete(mock_asyncapi_file: Path):
    source = asyncapi_source(spec_path=mock_asyncapi_file)
    assert source.name == ASYNCAPI_SOURCE_NAME

    resource = next(iter(source.resources.values()))
    assert resource.write_disposition == "replace"

    rows_1 = list(resource())
    assert len(rows_1) == 1
    assert rows_1[0]["id"] == "asyncapi:accounts/signup:subscribe"

    # Simulate deleting the spec file upstream
    mock_asyncapi_file.unlink()

    # Re-running on directory without specs yields 0 rows (forget-on-delete)
    source_2 = asyncapi_source(spec_path=mock_asyncapi_file.parent)
    resource_2 = next(iter(source_2.resources.values()))
    rows_2 = list(resource_2())
    assert len(rows_2) == 0
