from typing import Any

import pytest

from cognee_community_connector_mixpanel.mixpanel import (
    MIXPANEL_SOURCE_NAME,
    MIXPANEL_TABLE_NAME,
    _format_cohort_to_row,
    _format_report_to_row,
    _format_schema_to_row,
    mixpanel_source,
)


class FakeMixpanelClient:
    def __init__(
        self,
        schemas: list[dict[str, Any]] | None = None,
        cohorts: list[dict[str, Any]] | None = None,
        reports: list[dict[str, Any]] | None = None,
    ):
        self._schemas = schemas or []
        self._cohorts = cohorts or []
        self._reports = reports or []

    def list_event_schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    def list_cohorts(self) -> list[dict[str, Any]]:
        return self._cohorts

    def list_reports(self) -> list[dict[str, Any]]:
        return self._reports


def test_format_schema_to_row():
    schema = {
        "name": "Checkout Completed",
        "status": "active",
        "tags": ["revenue", "e-commerce"],
        "description": "Triggered when a customer successfully finishes payment.",
        "last_modified": "2026-10-04T12:00:00Z",
        "properties": [
            {"name": "cart_total", "type": "number", "description": "Total checkout price"},
            {"name": "payment_gateway", "type": "string", "description": "Stripe or PayPal"},
        ],
    }

    row = _format_schema_to_row(schema)
    assert row is not None
    assert row["id"] == "mixpanel_schema_checkout_completed"
    assert "Checkout Completed" in row["title"]
    assert "cart_total" in row["text"]
    assert "Stripe or PayPal" in row["text"]
    assert row["resource_type"] == "event_schema"


def test_format_cohort_to_row():
    cohort = {
        "id": "10023",
        "name": "Power Users (30d Active)",
        "description": "Users who completed more than 50 events in the past 30 days.",
        "count": 4820,
        "is_visible": True,
        "last_modified": "2026-10-05T08:00:00Z",
    }

    row = _format_cohort_to_row(cohort)
    assert row is not None
    assert row["id"] == "mixpanel_cohort_10023"
    assert "Power Users" in row["title"]
    assert "4820" in row["text"]
    assert "completed more than 50 events" in row["text"]
    assert row["resource_type"] == "cohort"


def test_format_report_to_row():
    report = {
        "id": "rep_991",
        "name": "Weekly Retention by Pricing Plan",
        "description": "Funnel analysis measuring week-over-week retention by user plan tier.",
        "type": "retention",
        "creator_name": "Lead Analyst",
        "last_modified": "2026-10-05T10:00:00Z",
    }

    row = _format_report_to_row(report)
    assert row is not None
    assert row["id"] == "mixpanel_report_991"
    assert "Weekly Retention" in row["title"]
    assert "Lead Analyst" in row["text"]
    assert "Funnel analysis measuring" in row["text"]
    assert row["resource_type"] == "report"


def test_mixpanel_source_integration_with_fake_client():
    fake_schemas = [{"name": "Signup", "last_modified": "2026-10-01T00:00:00Z"}]
    fake_cohorts = [{"id": "c1", "name": "Trialists", "last_modified": "2026-10-02T00:00:00Z"}]
    fake_reports = [
        {"id": "r1", "name": "Daily Active Users", "last_modified": "2026-10-03T00:00:00Z"}
    ]

    client = FakeMixpanelClient(schemas=fake_schemas, cohorts=fake_cohorts, reports=fake_reports)
    src = mixpanel_source(client=client)

    assert (
        getattr(src, "cognee_document_source", None) == MIXPANEL_SOURCE_NAME
        or getattr(src, "_cognee_document_source", None) == MIXPANEL_SOURCE_NAME
    )

    rows = list(src.resources[MIXPANEL_TABLE_NAME]())
    assert len(rows) == 3
    types = {r["resource_type"] for r in rows}
    assert types == {"event_schema", "cohort", "report"}


def test_mixpanel_source_resource_flags():
    fake_schemas = [{"name": "Signup"}]
    fake_cohorts = [{"id": "c1", "name": "Trialists"}]
    fake_reports = [{"id": "r1", "name": "Daily Active Users"}]

    client = FakeMixpanelClient(schemas=fake_schemas, cohorts=fake_cohorts, reports=fake_reports)
    src = mixpanel_source(
        include_schemas=True,
        include_cohorts=False,
        include_reports=False,
        client=client,
    )

    rows = list(src.resources[MIXPANEL_TABLE_NAME]())
    assert len(rows) == 1
    assert rows[0]["resource_type"] == "event_schema"


def test_mixpanel_source_incremental_filter():
    fake_schemas = [
        {"name": "Old Event", "last_modified": "2026-09-01T00:00:00Z"},
        {"name": "New Event", "last_modified": "2026-10-04T00:00:00Z"},
    ]
    client = FakeMixpanelClient(schemas=fake_schemas)
    src = mixpanel_source(
        since="2026-10-01T00:00:00Z",
        include_cohorts=False,
        include_reports=False,
        client=client,
    )

    rows = list(src.resources[MIXPANEL_TABLE_NAME]())
    assert len(rows) == 1
    assert rows[0]["id"] == "mixpanel_schema_new_event"


def test_mixpanel_source_missing_credentials_raises():
    with pytest.raises(ValueError, match="Mixpanel credentials required"):
        mixpanel_source(service_account_secret=None, api_secret=None, client=None)
