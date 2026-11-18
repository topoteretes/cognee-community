"""Unit tests for the New Relic observability dlt connector."""

from typing import Any

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_new_relic.new_relic import (
    NEW_RELIC_SOURCE_NAME,
    US_NERDGRAPH_URL,
    NewRelicAPIError,
    _dashboard_to_row,
    _is_transient,
    _issue_to_row,
    _iter_new_relic_rows,
    _policy_to_row,
    new_relic_source,
)


def test_policy_to_row():
    policy = {
        "id": "1001",
        "name": "API Gateway Latency",
        "incidentPreference": "PER_POLICY",
    }
    conditions = [
        {
            "id": "c101",
            "name": "High Gateway Latency",
            "policyId": "1001",
            "enabled": True,
            "description": "Alert when response times exceed threshold.",
            "nrql": {"query": "SELECT average(duration) FROM Transaction"},
            "runbookUrl": "https://wiki/runbooks/gateway-latency",
            "terms": [
                {
                    "priority": "CRITICAL",
                    "operator": "ABOVE",
                    "threshold": 1.2,
                    "thresholdDuration": 300,
                }
            ],
        }
    ]

    row = _policy_to_row(policy, conditions, account_id=987654)
    assert row["id"] == "new_relic_policy_1001"
    assert row["resource_type"] == "policy"
    assert row["account_id"] == 987654

    content = row["content"]
    assert "# Alert Policy: API Gateway Latency" in content
    assert "**Policy ID:** 1001" in content
    assert "**Incident Preference:** PER_POLICY" in content
    assert "High Gateway Latency" in content
    assert "`SELECT average(duration) FROM Transaction`" in content
    assert "[CRITICAL: ABOVE 1.2 for 300s]" in content
    assert "https://wiki/runbooks/gateway-latency" in content


def test_dashboard_to_row():
    dashboard = {
        "guid": "guid-prod-dash",
        "name": "Production Service Overview",
        "description": "Top-level latency and throughput metrics.",
        "accountId": 987654,
        "updatedAt": "2026-10-22T08:00:00Z",
        "tags": [{"key": "Env", "values": ["Prod"]}],
        "pages": [
            {
                "name": "Main KPIs",
                "widgets": [
                    {
                        "title": "Throughput",
                        "rawConfiguration": {
                            "nrqlQueries": [
                                {"query": "SELECT rate(count(*), 1 minute) FROM Transaction"}
                            ]
                        },
                    }
                ],
            }
        ],
    }

    row = _dashboard_to_row(dashboard)
    assert row["id"] == "new_relic_dashboard_guid-prod-dash"
    assert row["resource_type"] == "dashboard"
    assert row["url"] == "https://one.newrelic.com/redirect/entity/guid-prod-dash"

    content = row["content"]
    assert "# Dashboard: Production Service Overview" in content
    assert "**GUID:** guid-prod-dash" in content
    assert "**Tags:** Env: Prod" in content
    assert "#### Page: Main KPIs" in content
    assert "- **Throughput**: `SELECT rate(count(*), 1 minute) FROM Transaction`" in content


def test_issue_to_row():
    issue = {
        "issueId": "issue-999",
        "title": "Database Connection Saturation",
        "priority": "CRITICAL",
        "state": "ACTIVATED",
        "activatedAt": "2026-10-25T14:00:00Z",
        "closedAt": None,
        "sources": ["Alert Condition: Postgres Max Connections"],
        "conditionName": "Postgres Max Connections",
    }

    row = _issue_to_row(issue, account_id=987654)
    assert row["id"] == "new_relic_incident_issue-999"
    assert row["resource_type"] == "incident"
    assert row["state"] == "ACTIVATED"

    content = row["content"]
    assert "# Incident: Database Connection Saturation" in content
    assert "**Issue ID:** issue-999" in content
    assert "**Priority:** CRITICAL" in content
    assert "**State:** ACTIVATED" in content
    assert "**Activated At:** 2026-10-25T14:00:00Z" in content
    assert "**Sources:** Alert Condition: Postgres Max Connections" in content


def test_iter_rows_all_resources(fake_nr, nr_client):
    rows = list(
        _iter_new_relic_rows(
            client=nr_client,
            base_url=US_NERDGRAPH_URL,
            account_id=fake_nr.account_id,
            resources=["policies", "dashboards", "incidents"],
        )
    )

    types = [r["resource_type"] for r in rows if "_deleted" not in r]
    assert "policy" in types
    assert "dashboard" in types
    assert "incident" in types
    assert len(rows) == 4  # 2 policies + 1 dashboard + 1 issue


def test_iter_rows_selective_resources(fake_nr, nr_client):
    rows = list(
        _iter_new_relic_rows(
            client=nr_client,
            base_url=US_NERDGRAPH_URL,
            account_id=fake_nr.account_id,
            resources=["dashboards"],
        )
    )
    assert len(rows) == 1
    assert rows[0]["resource_type"] == "dashboard"
    assert rows[0]["id"] == "new_relic_dashboard_guid-dash-1"


def test_incremental_incident_sync(fake_nr, nr_client):
    state: dict[str, Any] = {
        "new_relic_seen_inventory": [],
        "new_relic_incident_cursor": "2026-10-25T10:00:00Z",
    }

    # Incident activatedAt is 2026-10-25T10:00:00Z; cursor is the same timestamp so skipped
    rows = list(
        _iter_new_relic_rows(
            client=nr_client,
            base_url=US_NERDGRAPH_URL,
            account_id=fake_nr.account_id,
            resources=["incidents"],
            incremental=True,
            resource_state=state,
        )
    )
    assert len(rows) == 0

    # Add a newer incident
    fake_nr.issues.append(
        {
            "issueId": "iss-502",
            "title": "New Spike in 500 Responses",
            "priority": "HIGH",
            "state": "ACTIVATED",
            "activatedAt": "2026-10-26T12:00:00Z",
            "closedAt": None,
            "sources": ["HTTP 500 Monitor"],
            "conditionName": "500 Monitor",
        }
    )

    new_rows = list(
        _iter_new_relic_rows(
            client=nr_client,
            base_url=US_NERDGRAPH_URL,
            account_id=fake_nr.account_id,
            resources=["incidents"],
            incremental=True,
            resource_state=state,
        )
    )
    assert len(new_rows) == 1
    assert new_rows[0]["id"] == "new_relic_incident_iss-502"
    assert state["new_relic_incident_cursor"] == "2026-10-26T12:00:00Z"


def test_authoritative_forget_on_delete(fake_nr, nr_client):
    state: dict[str, Any] = {
        "new_relic_seen_inventory": [
            "new_relic_policy_pol-101",
            "new_relic_policy_pol-102",
            "new_relic_dashboard_guid-dash-1",
            "new_relic_dashboard_guid-dash-deleted",
        ],
        "new_relic_incident_cursor": "",
    }

    # Dashboards upstream currently only have guid-dash-1
    rows = list(
        _iter_new_relic_rows(
            client=nr_client,
            base_url=US_NERDGRAPH_URL,
            account_id=fake_nr.account_id,
            resources=["policies", "dashboards"],
            incremental=False,
            resource_state=state,
        )
    )

    tombstones = [r for r in rows if r.get("_deleted") is True]
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "new_relic_dashboard_guid-dash-deleted"
    assert tombstones[0]["_deleted"] is True


def test_region_endpoint_resolution(nr_client):
    res_us = new_relic_source(api_key="k", account_id=123, region="US", client=nr_client)
    assert res_us is not None

    res_eu = new_relic_source(api_key="k", account_id=123, region="EU", client=nr_client)
    assert res_eu is not None


def test_parameter_validation_missing_key():
    with pytest.raises(ValueError, match="New Relic API key is required"):
        new_relic_source(api_key="", account_id=123, client=None)


def test_parameter_validation_missing_account():
    with pytest.raises(ValueError, match="New Relic Account ID is required"):
        new_relic_source(api_key="k", account_id=None, client=None)


def test_parameter_validation_invalid_resource():
    with pytest.raises(ValueError, match="Invalid resource"):
        new_relic_source(api_key="k", account_id=123, resources=["unknown_resource"], client=None)


def test_nerdgraph_partial_error_recovery(fake_nr, nr_client):
    fake_nr.return_partial_errors = True
    rows = list(
        _iter_new_relic_rows(
            client=nr_client,
            base_url=US_NERDGRAPH_URL,
            account_id=fake_nr.account_id,
            resources=["dashboards"],
        )
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "new_relic_dashboard_guid-dash-1"


def test_nerdgraph_fatal_error_raises(fake_nr, nr_client):
    fake_nr.return_fatal_errors = True
    with pytest.raises(NewRelicAPIError, match="NerdGraph GraphQL errors"):
        list(
            _iter_new_relic_rows(
                client=nr_client,
                base_url=US_NERDGRAPH_URL,
                account_id=fake_nr.account_id,
                resources=["dashboards"],
            )
        )


def test_transient_error_retry_and_recovery(fake_nr, nr_client):
    fake_nr.rate_limit_hits = 1
    fake_nr.server_error_hits = 1

    rows = list(
        _iter_new_relic_rows(
            client=nr_client,
            base_url=US_NERDGRAPH_URL,
            account_id=fake_nr.account_id,
            resources=["dashboards"],
        )
    )
    assert len(rows) == 1
    assert fake_nr.rate_limit_hits == 0
    assert fake_nr.server_error_hits == 0


def test_error_classification():
    assert _is_transient(httpx.ReadTimeout("Timeout")) is True
    assert _is_transient(httpx.ConnectError("Network blip")) is True
    assert _is_transient(ValueError("Fatal argument")) is False


def test_new_relic_source_document_mode_attributes(nr_client):
    res = new_relic_source(api_key="test_key", account_id=12345, client=nr_client)
    assert getattr(res, DOCUMENT_SOURCE_ATTR) == NEW_RELIC_SOURCE_NAME
    assert res.cognee_pipeline_scope == NEW_RELIC_SOURCE_NAME
