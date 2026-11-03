"""Tests for the Kimai time-tracking connector for cognee."""

import pytest
from pathlib import Path
from cognee_community_connector_kimai.kimai import (
    DOCUMENT_SOURCE_ATTR,
    format_customer_document,
    format_project_document,
    format_activity_document,
    format_timesheet_document,
    parse_kimai_records,
    kimai_source,
)


@pytest.fixture
def mock_kimai_payload():
    return {
        "customers": [
            {
                "id": 1,
                "name": "Acme Corp",
                "number": "CUST-001",
                "country": "US",
                "currency": "USD",
                "comment": "Primary enterprise client for cloud data migration.",
                "visible": True,
            }
        ],
        "projects": [
            {
                "id": 10,
                "name": "Billing Migration V2",
                "number": "PRJ-101",
                "orderNumber": "ORD-2026-9",
                "comment": "Migration of Stripe billing pipelines to Base USDC rails.",
                "visible": True,
                "customer": {"id": 1, "name": "Acme Corp"},
            }
        ],
        "activities": [
            {
                "id": 101,
                "name": "Backend Engineering",
                "comment": "Core Python & Smart Contract architecture",
                "visible": True,
                "project": {"id": 10, "name": "Billing Migration V2"},
            }
        ],
        "timesheets": [
            {
                "id": 501,
                "begin": "2026-10-24T09:00:00",
                "end": "2026-10-24T12:30:00",
                "duration": 12600,  # 3h 30m
                "description": "Shipped hermetic ERC-4337 paymaster validation tests.",
                "billable": True,
                "tags": ["smart-contracts", "python", "p0"],
                "hourlyRate": 150.0,
                "rate": 525.0,
                "user": {"id": 42, "alias": "Rizul Chauhan", "username": "rizul"},
                "project": {"id": 10, "name": "Billing Migration V2", "customer": {"id": 1, "name": "Acme Corp"}},
                "activity": {"id": 101, "name": "Backend Engineering"},
            },
            {
                # Active timer currently running - no end time
                "id": 502,
                "begin": "2026-10-24T13:00:00",
                "end": None,
                "duration": 1800,
                "description": "Investigating live websocket memory usage",
                "billable": True,
                "user": {"id": 42, "alias": "Rizul Chauhan", "username": "rizul"},
                "project": {"id": 10, "name": "Billing Migration V2"},
                "activity": {"id": 101, "name": "Backend Engineering"},
            },
        ],
    }


def test_parse_customers_and_projects(mock_kimai_payload):
    docs = parse_kimai_records(mock_kimai_payload, base_url="https://kimai.internal.agency")
    cust_doc = next(d for d in docs if d["type"] == "customer")
    assert cust_doc["entity_id"] == 1
    assert "Acme Corp" in cust_doc["text"]
    assert "kimai.internal.agency:customer:1" in cust_doc["node_sets"]

    proj_doc = next(d for d in docs if d["type"] == "project")
    assert proj_doc["entity_id"] == 10
    assert "Billing Migration V2" in proj_doc["text"]
    assert "kimai.internal.agency:project:10" in proj_doc["node_sets"]
    assert "kimai.internal.agency:customer:1" in proj_doc["node_sets"]


def test_running_timers_excluded(mock_kimai_payload):
    docs = parse_kimai_records(mock_kimai_payload, base_url="https://kimai.internal.agency")
    timesheet_docs = [d for d in docs if d["type"] == "timesheet"]
    # Only completed timesheet #501 should be present, not running timer #502
    assert len(timesheet_docs) == 1
    assert timesheet_docs[0]["entity_id"] == 501
    assert "Shipped hermetic ERC-4337" in timesheet_docs[0]["text"]


def test_rates_excluded_by_default(mock_kimai_payload):
    # Rates excluded by default
    docs_no_rates = parse_kimai_records(mock_kimai_payload, include_rates=False)
    ts_no_rates = next(d for d in docs_no_rates if d["type"] == "timesheet")
    assert "Hourly Rate" not in ts_no_rates["text"]
    assert "Calculated Fee" not in ts_no_rates["text"]

    # Rates explicitly opt-in
    docs_with_rates = parse_kimai_records(mock_kimai_payload, include_rates=True)
    ts_with_rates = next(d for d in docs_with_rates if d["type"] == "timesheet")
    assert "Hourly Rate" in ts_with_rates["text"]
    assert "150.0" in ts_with_rates["text"]


def test_format_timesheet_document(mock_kimai_payload):
    ts = mock_kimai_payload["timesheets"][0]
    md = format_timesheet_document(ts, base_url="https://kimai.agency.net")
    assert "[Kimai Work Log]" in md
    assert "Contributor:** `Rizul Chauhan`" in md
    assert "Customer:** `Acme Corp`" in md
    assert "Billing Migration V2" in md
    assert "3h 30m (12600s)" in md
    assert "`smart-contracts`" in md
    assert "Billable" in md
    assert "Shipped hermetic ERC-4337" in md


def test_missing_snapshot_file_error():
    with pytest.raises(FileNotFoundError):
        kimai_source(snapshot_data="/non/existent/kimai_snapshot.json")


def test_kimai_source_with_snapshot(mock_kimai_payload):
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt is not installed in the test environment")

    source = kimai_source(snapshot_data=mock_kimai_payload)
    assert source is not None
