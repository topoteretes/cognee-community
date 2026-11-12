"""Unit and integration tests for the OpenLineage / Marquez Cognee connector.

All tests run deterministically offline using an in-memory FakeMarquezClient,
requiring zero external network calls or cloud credentials.
"""

from typing import Any

from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_openlineage.openlineage import (
    OPENLINEAGE_SOURCE_NAME,
    OPENLINEAGE_TABLE_NAME,
    _format_fields_table,
    _format_runs_table,
    _render_job_markdown,
    openlineage_source,
    redact_sensitive_value,
)


class FakeMarquezClient:
    """In-memory fake Marquez client simulating OpenLineage REST endpoints."""

    def __init__(self) -> None:
        self.endpoint_url = "http://fake-marquez:5000"
        self.namespaces = ["analytics", "finance"]
        self.jobs = {
            "analytics": [
                {
                    "name": "daily_user_retention",
                    "namespace": "analytics",
                    "type": "BATCH",
                    "description": "Calculates 30-day cohort retention rates.",
                    "inputs": [
                        {
                            "namespace": "analytics",
                            "name": "raw_user_events",
                            "facets": {
                                "schema": {
                                    "fields": [
                                        {
                                            "name": "event_id",
                                            "type": "STRING",
                                            "description": "Unique event UUID",
                                        },
                                        {
                                            "name": "user_id",
                                            "type": "INT",
                                            "description": "Foreign key to users",
                                        },
                                        {
                                            "name": "timestamp",
                                            "type": "TIMESTAMP",
                                            "description": "Event arrival time",
                                        },
                                    ]
                                }
                            },
                        }
                    ],
                    "outputs": [
                        {
                            "namespace": "analytics",
                            "name": "retention_metrics",
                            "facets": {
                                "schema": {
                                    "fields": [
                                        {
                                            "name": "cohort_date",
                                            "type": "DATE",
                                            "description": "Cohort registration date",
                                        },
                                        {
                                            "name": "retention_rate",
                                            "type": "FLOAT",
                                            "description": "30d retention percentage",
                                        },
                                    ]
                                }
                            },
                        }
                    ],
                }
            ],
            "finance": [
                {
                    "name": "payroll_settlement",
                    "namespace": "finance",
                    "type": "BATCH",
                    "description": "Bi-weekly payroll disbursement job.",
                    "inputs": [{"namespace": "finance", "name": "timecards", "facets": {}}],
                    "outputs": [{"namespace": "finance", "name": "bank_transfers", "facets": {}}],
                }
            ],
        }
        self.runs = {
            ("analytics", "daily_user_retention"): [
                {
                    "id": "run-abc-12345",
                    "state": "COMPLETE",
                    "nominalStartTime": "2026-10-09T04:00:00Z",
                    "endedAt": "2026-10-09T04:05:22Z",
                    "durationMs": 322000,
                    "facets": {},
                },
                {
                    "id": "run-fail-9999",
                    "state": "FAIL",
                    "nominalStartTime": "2026-10-08T04:00:00Z",
                    "endedAt": "2026-10-08T04:01:10Z",
                    "durationMs": 70000,
                    "facets": {"errorMessage": {"message": "OutOfMemoryError: Java heap space"}},
                },
            ]
        }
        self.datasets = {
            ("finance", "timecards"): {
                "name": "timecards",
                "namespace": "finance",
                "facets": {
                    "schema": {
                        "fields": [
                            {"name": "employee_id", "type": "INT", "description": "Employee ID"},
                            {
                                "name": "hours_worked",
                                "type": "DECIMAL",
                                "description": "Hours recorded",
                            },
                        ]
                    }
                },
            }
        }

    def list_namespaces(self) -> list[str]:
        return list(self.namespaces)

    def list_jobs(self, namespace: str) -> list[dict[str, Any]]:
        return self.jobs.get(namespace, [])

    def list_runs(self, namespace: str, job_name: str, limit: int = 5) -> list[dict[str, Any]]:
        runs = self.runs.get((namespace, job_name), [])
        return runs[:limit]

    def get_dataset(self, namespace: str, dataset_name: str) -> dict[str, Any]:
        return self.datasets.get((namespace, dataset_name), {})


def test_format_fields_table():
    """Verify schema fields are formatted into markdown tables with types and descriptions."""
    fields = [
        {"name": "user_id", "type": "INT", "description": "Primary user ID"},
        {"name": "email", "type": "STRING", "description": "Verified user email"},
    ]
    table = _format_fields_table(fields)
    assert "| Field Name | Type | Description |" in table
    assert "| `user_id` | `INT` | Primary user ID |" in table
    assert "| `email` | `STRING` | Verified user email |" in table


def test_format_runs_table():
    """Verify execution runs table captures run IDs, states, durations, and errors."""
    runs = [
        {
            "id": "run-test-123456",
            "state": "COMPLETE",
            "durationMs": 45000,
            "facets": {},
        },
        {
            "id": "run-fail-789012",
            "state": "FAIL",
            "durationMs": 12000,
            "facets": {"errorMessage": {"message": "ConnectionRefused"}},
        },
    ]
    table = _format_runs_table(runs)
    assert "| `run-test` | `COMPLETE` |" in table
    assert "45.00s" in table
    assert "| `run-fail` | `FAIL` |" in table
    assert "ConnectionRefused" in table


def test_render_job_markdown_complete():
    """Verify end-to-end rendering of a job markdown document."""
    client = FakeMarquezClient()
    job = client.jobs["analytics"][0]
    runs = client.runs[("analytics", "daily_user_retention")]

    md = _render_job_markdown(job, runs, client)
    assert "# OpenLineage Job: analytics/daily_user_retention" in md
    assert "- **Namespace**: `analytics`" in md
    assert "- **Job Type**: `BATCH`" in md
    assert "### Upstream Input Datasets" in md
    assert "#### Dataset: `analytics.raw_user_events`" in md
    assert "| `event_id` | `STRING` | Unique event UUID |" in md
    assert "### Downstream Output Datasets" in md
    assert "#### Dataset: `analytics.retention_metrics`" in md
    assert "| `retention_rate` | `FLOAT` | 30d retention percentage |" in md
    assert "## Recent Execution Runs" in md
    assert "OutOfMemoryError: Java heap space" in md


def test_openlineage_source_document_mode():
    """Verify source sets document mode attribute for cognify routing."""
    client = FakeMarquezClient()
    source = openlineage_source(client=client)
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == OPENLINEAGE_SOURCE_NAME


def test_openlineage_source_execution_and_replace():
    """Verify dlt resource emits items and uses write_disposition='replace' for forget-on-delete."""
    client = FakeMarquezClient()
    source = openlineage_source(client=client)

    resource = source.resources[OPENLINEAGE_TABLE_NAME]
    assert resource.write_disposition == "replace"

    items = list(resource)
    assert len(items) == 2

    job_ids = [item["id"] for item in items]
    assert "analytics/daily_user_retention" in job_ids
    assert "finance/payroll_settlement" in job_ids

    retention_item = next(item for item in items if item["id"] == "analytics/daily_user_retention")
    assert "OpenLineage Job: analytics/daily_user_retention" in retention_item["content"]
    assert retention_item["namespace"] == "analytics"
    assert retention_item["job_name"] == "daily_user_retention"


def test_namespace_filtering():
    """Verify restriction to specific namespaces."""
    client = FakeMarquezClient()
    source = openlineage_source(client=client, namespaces=["finance"])

    items = list(source.resources[OPENLINEAGE_TABLE_NAME])
    assert len(items) == 1
    assert items[0]["id"] == "finance/payroll_settlement"


def test_job_name_filtering():
    """Verify filtering by specific job names."""
    client = FakeMarquezClient()
    source = openlineage_source(client=client, job_names=["daily_user_retention"])

    items = list(source.resources[OPENLINEAGE_TABLE_NAME])
    assert len(items) == 1
    assert items[0]["id"] == "analytics/daily_user_retention"


def test_secret_redaction():
    """Verify defensive redaction of sensitive credentials and keys."""
    assert redact_sensitive_value("api_key", "secret123") == "[REDACTED]"
    assert redact_sensitive_value("bearer_token", "jwt.abc.xyz") == "[REDACTED]"
    assert redact_sensitive_value("db_password", "hunter2") == "[REDACTED]"
    assert redact_sensitive_value("database_host", "postgres.prod") == "postgres.prod"
    assert redact_sensitive_value("job_name", "etl_pipeline") == "etl_pipeline"


def test_empty_catalog():
    """Verify graceful handling when no namespaces or jobs exist."""
    client = FakeMarquezClient()
    client.namespaces = []
    source = openlineage_source(client=client)

    items = list(source.resources[OPENLINEAGE_TABLE_NAME])
    assert len(items) == 0


def test_full_snapshot_reconciliation_omits_dropped_jobs():
    """Verify forget-on-delete semantics.

    Dropping a job from the upstream catalog omits it from the next sync run.
    """
    client = FakeMarquezClient()
    source1 = openlineage_source(client=client)
    items1 = list(source1.resources[OPENLINEAGE_TABLE_NAME])
    assert len(items1) == 2

    # Drop payroll_settlement from upstream Marquez
    client.jobs["finance"] = []

    source2 = openlineage_source(client=client)
    items2 = list(source2.resources[OPENLINEAGE_TABLE_NAME])
    assert len(items2) == 1
    assert items2[0]["id"] == "analytics/daily_user_retention"
