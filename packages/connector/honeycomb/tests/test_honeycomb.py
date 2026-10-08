from typing import Any

from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee_community_connector_honeycomb.honeycomb import (
    HoneycombClient,
    _format_board_to_row,
    _format_dataset_to_row,
    _format_marker_to_row,
    _format_query_to_row,
    _format_slo_to_row,
    _format_trigger_to_row,
    honeycomb_source,
)


class FakeHoneycombClient:
    def __init__(self) -> None:
        self.datasets = [
            {
                "slug": "api-gateway",
                "name": "API Gateway Production",
                "description": "Ingress edge proxy routing and TLS termination telemetry.",
                "created_at": "2026-01-01T00:00:00Z",
                "last_written_at": "2026-10-04T12:00:00Z",
            },
            {
                "slug": "payments-svc",
                "name": "Payments Service",
                "description": "Handles checkout transactions and stripe webhooks.",
                "created_at": "2026-02-01T00:00:00Z",
                "last_written_at": "2026-10-05T08:00:00Z",
            },
        ]
        self.boards = [
            {
                "id": "board_flexible_1",
                "name": "Production Reliability Overview",
                "description": (
                    "High-level overview of error budgets and p99 latency across services."
                ),
                "style": "flexible",
                "updated_at": "2026-10-03T15:00:00Z",
                "panels": [
                    {
                        "type": "query",
                        "name": "P99 Latency by Route",
                        "dataset": "api-gateway",
                    },
                    {
                        "type": "text",
                        "text": "Please escalate checkout error rate breaches directly to on-call.",
                    },
                ],
            }
        ]
        self.queries = {
            "api-gateway": [
                {
                    "id": "query_slow_requests",
                    "name": "Slow Ingress Requests",
                    "description": "Calculates p99 response duration grouped by HTTP route.",
                    "query": {
                        "calculations": [{"op": "P99", "column": "duration_ms"}],
                        "breakdowns": ["route", "http_status"],
                        "filters": [{"column": "duration_ms", "op": ">", "value": 500}],
                    },
                    "updated_at": "2026-10-01T12:00:00Z",
                }
            ]
        }
        self.triggers = {
            "api-gateway": [
                {
                    "id": "trig_001",
                    "name": "High 5xx Rate",
                    "description": "Alert when 5xx HTTP responses exceed 2% of total traffic.",
                    "disabled": False,
                    "frequency": 60,
                    "threshold": {"op": ">", "value": 2.0},
                    "updated_at": "2026-09-20T10:00:00Z",
                }
            ]
        }
        self.slos = {
            "payments-svc": [
                {
                    "id": "slo_checkout",
                    "name": "Checkout Availability SLO",
                    "description": (
                        "99.9% of checkout requests must succeed with HTTP 200 within 1s."
                    ),
                    "target_percentage": 99.9,
                    "time_period_days": 30,
                    "sli": {"alias": "checkout_success_rate"},
                    "updated_at": "2026-09-01T00:00:00Z",
                }
            ]
        }
        self.markers = {
            "__all__": [
                {
                    "id": "mark_deploy_v2",
                    "type": "deploy",
                    "message": "Production Release v2.4.0 deployed across cluster.",
                    "start_time": "2026-10-04T14:30:00Z",
                    "url": "https://github.com/org/repo/releases/v2.4.0",
                }
            ],
            "api-gateway": [
                {
                    "id": "mark_incident_01",
                    "type": "incident",
                    "message": "Gateway route flapping resolved by DNS failover.",
                    "start_time": "2026-10-05T02:15:00Z",
                }
            ],
        }

    def list_datasets(self) -> list[dict[str, Any]]:
        return self.datasets

    def list_boards(self) -> list[dict[str, Any]]:
        return self.boards

    def list_queries(self, dataset: str) -> list[dict[str, Any]]:
        return self.queries.get(dataset, [])

    def list_triggers(self, dataset: str) -> list[dict[str, Any]]:
        return self.triggers.get(dataset, [])

    def list_slos(self, dataset: str) -> list[dict[str, Any]]:
        return self.slos.get(dataset, [])

    def list_markers(self, dataset: str) -> list[dict[str, Any]]:
        return self.markers.get(dataset, [])


def test_honeycomb_client_headers() -> None:
    client = HoneycombClient(api_key="hc_secret_key_123")
    headers = client._headers()
    assert headers["X-Honeycomb-Team"] == "hc_secret_key_123"
    assert headers["Accept"] == "application/json"


def test_format_dataset_to_row() -> None:
    ds = {
        "slug": "billing-engine",
        "name": "Billing Engine",
        "description": "Subscription renewals and invoicing service telemetry.",
        "created_at": "2026-03-01T00:00:00Z",
        "last_written_at": "2026-10-04T10:00:00Z",
    }
    row = _format_dataset_to_row(ds)
    assert row is not None
    assert row["id"] == "honeycomb_dataset_billing-engine"
    assert row["title"] == "Honeycomb Dataset: Billing Engine"
    assert "- **Dataset Slug:** `billing-engine`" in row["text"]
    assert "Subscription renewals and invoicing service telemetry." in row["text"]


def test_format_flexible_board_to_row() -> None:
    board = {
        "id": "board_flex_xyz",
        "name": "Database Health",
        "description": "Connection pool and lock wait metrics.",
        "style": "flexible",
        "updated_at": "2026-10-01T00:00:00Z",
        "panels": [
            {"type": "query", "name": "Lock Wait Duration", "dataset": "postgres-prod"},
            {"type": "text", "text": "Escalate lock waits > 10s to DBA."},
        ],
    }
    row = _format_board_to_row(board)
    assert row is not None
    assert row["id"] == "honeycomb_board_board_flex_xyz"
    assert "Database Health" in row["text"]
    assert "**Query Panel:** Lock Wait Duration (Dataset: `postgres-prod`)" in row["text"]
    assert "**Text Panel:** Escalate lock waits > 10s to DBA." in row["text"]


def test_format_query_to_row() -> None:
    query = {
        "id": "q_p99_dur",
        "name": "P99 Endpoint Latency",
        "description": "Tracks p99 endpoint latency by controller.",
        "query": {
            "calculations": [{"op": "P99", "column": "duration_ms"}],
            "breakdowns": ["controller_action"],
        },
        "updated_at": "2026-10-01T00:00:00Z",
    }
    row = _format_query_to_row(query, dataset_slug="web-api")
    assert row is not None
    assert row["id"] == "honeycomb_query_q_p99_dur"
    assert "P99 Endpoint Latency" in row["text"]
    assert "Breakdowns / Group By:** controller_action" in row["text"]


def test_format_marker_to_row() -> None:
    marker = {
        "id": "m_rel_1",
        "type": "deploy",
        "message": "Deployed canary build to prod cluster.",
        "start_time": "2026-10-05T09:00:00Z",
        "url": "https://ci.example.com/build/123",
    }
    row = _format_marker_to_row(marker, dataset_slug="__all__")
    assert row is not None
    assert row["id"] == "honeycomb_marker_m_rel_1"
    assert "Honeycomb Timeline Marker: Deployed canary build" in row["text"]
    assert "- **Marker Type:** `deploy`" in row["text"]
    assert "- **External URL:** https://ci.example.com/build/123" in row["text"]


def test_format_trigger_to_row() -> None:
    trigger = {
        "id": "trig_99",
        "name": "High Memory Usage",
        "description": "Triggers when RSS exceeds 90%.",
        "disabled": False,
        "frequency": 60,
        "threshold": {"op": ">", "value": 90},
        "updated_at": "2026-10-01T00:00:00Z",
    }
    row = _format_trigger_to_row(trigger, dataset_slug="k8s-nodes")
    assert row is not None
    assert row["id"] == "honeycomb_trigger_trig_99"
    assert "- **Dataset:** `k8s-nodes`" in row["text"]
    assert "- **Threshold Rule:** Metric > 90" in row["text"]


def test_format_slo_to_row() -> None:
    slo = {
        "id": "slo_api",
        "name": "API Success SLO",
        "description": "99.95% API reliability objective.",
        "target_percentage": 99.95,
        "time_period_days": 30,
        "sli": {"alias": "http_2xx_ratio"},
        "updated_at": "2026-10-01T00:00:00Z",
    }
    row = _format_slo_to_row(slo, dataset_slug="core-api")
    assert row is not None
    assert row["id"] == "honeycomb_slo_slo_api"
    assert "- **Target Reliability:** 99.95% over 30 days" in row["text"]


def test_honeycomb_source_iteration() -> None:
    fake_client = FakeHoneycombClient()
    source = honeycomb_source(api_key="test_key", client=fake_client)

    records = list(source)
    # 2 datasets + 1 board + 1 query + 1 trigger + 1 slo + 2 markers = 8 records
    assert len(records) == 8

    ids = [r["id"] for r in records]
    assert "honeycomb_dataset_api-gateway" in ids
    assert "honeycomb_dataset_payments-svc" in ids
    assert "honeycomb_board_board_flexible_1" in ids
    assert "honeycomb_query_query_slow_requests" in ids
    assert "honeycomb_trigger_trig_001" in ids
    assert "honeycomb_slo_slo_checkout" in ids
    assert "honeycomb_marker_mark_deploy_v2" in ids
    assert "honeycomb_marker_mark_incident_01" in ids


def test_honeycomb_canonical_document_source_attribute() -> None:
    fake_client = FakeHoneycombClient()
    source = honeycomb_source(api_key="test_key", client=fake_client)
    assert hasattr(source, DOCUMENT_SOURCE_ATTR)
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "honeycomb"
    assert DOCUMENT_SOURCE_ATTR == "cognee_document_source"
