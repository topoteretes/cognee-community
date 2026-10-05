from typing import Any

from cognee_community_connector_honeycomb.honeycomb import (
    DOCUMENT_SOURCE_ATTR,
    HoneycombClient,
    _format_board_to_row,
    _format_dataset_to_row,
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
                "id": "board_123",
                "name": "Production Reliability Overview",
                "description": (
                    "High-level overview of error budgets and p99 latency across services."
                ),
                "type": "board",
                "updated_at": "2026-10-03T15:00:00Z",
                "queries": [
                    {"caption": "P99 Latency by Route", "dataset": "api-gateway"},
                    {"caption": "Failed Checkout Spans", "dataset": "payments-svc"},
                ],
            }
        ]
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
            ],
            "payments-svc": [
                {
                    "id": "trig_002",
                    "name": "Stripe Webhook Timeout",
                    "description": "Triggers when webhook processing latency exceeds 5000ms.",
                    "disabled": False,
                    "frequency": 120,
                    "threshold": {"op": ">", "value": 5000},
                    "updated_at": "2026-10-02T11:00:00Z",
                }
            ],
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

    def list_datasets(self) -> list[dict[str, Any]]:
        return self.datasets

    def list_boards(self) -> list[dict[str, Any]]:
        return self.boards

    def list_triggers(self, dataset: str) -> list[dict[str, Any]]:
        return self.triggers.get(dataset, [])

    def list_slos(self, dataset: str) -> list[dict[str, Any]]:
        return self.slos.get(dataset, [])


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


def test_format_board_to_row() -> None:
    board = {
        "id": "board_xyz",
        "name": "Database Health",
        "description": "Connection pool and lock wait metrics.",
        "type": "board",
        "updated_at": "2026-10-01T00:00:00Z",
        "queries": [{"caption": "Lock Wait Duration", "dataset": "postgres-prod"}],
    }
    row = _format_board_to_row(board)
    assert row is not None
    assert row["id"] == "honeycomb_board_board_xyz"
    assert "Database Health" in row["text"]
    assert "**Lock Wait Duration** (Dataset: `postgres-prod`)" in row["text"]


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
    # 2 datasets + 1 board + 2 triggers + 1 slo = 6 records
    assert len(records) == 6

    ids = [r["id"] for r in records]
    assert "honeycomb_dataset_api-gateway" in ids
    assert "honeycomb_dataset_payments-svc" in ids
    assert "honeycomb_board_board_123" in ids
    assert "honeycomb_trigger_trig_001" in ids
    assert "honeycomb_trigger_trig_002" in ids
    assert "honeycomb_slo_slo_checkout" in ids


def test_honeycomb_document_source_attribute() -> None:
    fake_client = FakeHoneycombClient()
    source = honeycomb_source(api_key="test_key", client=fake_client)
    assert hasattr(source, DOCUMENT_SOURCE_ATTR)
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "honeycomb"


def test_honeycomb_source_since_filter() -> None:
    fake_client = FakeHoneycombClient()
    source = honeycomb_source(api_key="test_key", since="2026-10-05T00:00:00Z", client=fake_client)

    records = list(source)
    # Only payments-svc dataset was written at 2026-10-05T08:00:00Z
    assert len(records) == 1
    assert records[0]["id"] == "honeycomb_dataset_payments-svc"
