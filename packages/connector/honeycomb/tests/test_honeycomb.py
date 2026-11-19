"""Unit test suite for Honeycomb connector (100% offline with mocks)."""

from unittest.mock import MagicMock, patch

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_honeycomb.config import HoneycombConfig
from cognee_community_connector_honeycomb.honeycomb import (
    HONEYCOMB_SOURCE_NAME,
    HoneycombClient,
    _render_board_markdown,
    _render_dataset_markdown,
    _render_markers_markdown,
    _render_slo_markdown,
    _render_trigger_markdown,
    honeycomb_source,
    redact_dictionary,
    redact_sensitive_value,
)


def test_config_defaults_and_validation():
    config = HoneycombConfig(base_url="https://api.eu1.honeycomb.io/")
    assert config.base_url == "https://api.eu1.honeycomb.io"
    assert config.include_columns is True
    assert config.write_disposition == "replace"

    with pytest.raises(ValueError, match="base_url must start with"):
        HoneycombConfig(base_url="invalid://url")


def test_secret_redaction():
    assert redact_sensitive_value("api_key", "my-secret-key") == "[REDACTED]"
    assert redact_sensitive_value("token", "abc") == "[REDACTED]"
    assert (
        redact_sensitive_value("webhook_url", "https://hooks.slack.com/services/123")
        == "[REDACTED]"
    )
    assert redact_sensitive_value("dataset_name", "web") == "web"

    data = {
        "api_key": "topsecret",
        "recipients": [{"type": "slack", "target": "https://hooks.slack.com/services/T00/B00/X00"}],
        "safe_field": "visible",
    }
    redacted = redact_dictionary(data)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["recipients"][0]["target"] == "[REDACTED]"
    assert redacted["safe_field"] == "visible"


def test_client_headers():
    client = HoneycombClient(api_key="test-api-key-123")
    headers = client._get_headers()
    assert headers["X-Honeycomb-Team"] == "test-api-key-123"
    assert headers["Accept"] == "application/json"


@patch("httpx.Client")
def test_client_retry_and_rate_limiting(mock_client_cls):
    mock_client = MagicMock()
    # Attempt 1: 429 with Retry-After header, Attempt 2: 200 success
    resp_429 = MagicMock(status_code=429, headers={"Retry-After": "0"})
    resp_200 = MagicMock(status_code=200, json=lambda: [{"name": "web", "slug": "web"}])
    resp_200.raise_for_status = MagicMock()

    mock_client.get.side_effect = [resp_429, resp_200]
    mock_client_cls.return_value.__enter__.return_value = mock_client

    client = HoneycombClient(api_key="key", max_retries=3, retry_backoff_factor=0.01)
    datasets = client.list_datasets()

    assert len(datasets) == 1
    assert datasets[0]["slug"] == "web"
    assert mock_client.get.call_count == 2


def test_render_dataset_markdown():
    ds = {
        "name": "Production Web",
        "slug": "prod-web",
        "description": "Main web traffic",
        "last_written_at": "2026-10-10T12:00:00Z",
    }
    cols = [
        {"key_name": "duration_ms", "type": "float", "description": "Request latency"},
        {"key_name": "status_code", "type": "integer", "description": "HTTP status"},
    ]
    md = _render_dataset_markdown(ds, cols)
    assert "# Honeycomb Dataset: Production Web (`prod-web`)" in md
    assert "`duration_ms`" in md
    assert "`status_code`" in md


def test_render_board_markdown():
    board = {
        "name": "Core Service Health",
        "id": "board-123",
        "description": "High level metrics",
        "style": "visual",
        "queries": [{"caption": "P99 Latency", "dataset": "prod-web"}],
    }
    md = _render_board_markdown(board)
    assert "# Honeycomb Board: Core Service Health" in md
    assert "`board-123`" in md
    assert "P99 Latency" in md


def test_render_trigger_markdown():
    trigger = {
        "name": "Elevated Error Rate",
        "id": "trig-456",
        "frequency": 300,
        "disabled": False,
        "threshold": {"op": ">", "value": 5.0},
        "description": "Alerts on 5xx errors",
        "query": {"breakdowns": ["app"], "calculations": [{"op": "COUNT"}]},
    }
    md = _render_trigger_markdown("prod-web", trigger)
    assert "# Honeycomb Trigger: Elevated Error Rate" in md
    assert "`trig-456`" in md
    assert "Every `300s`" in md
    assert "> 5.0" in md


def test_render_slo_markdown():
    slo = {
        "name": "API Availability",
        "id": "slo-789",
        "target_per_million": 999000,
        "time_period_days": 30,
        "sli": "status_code < 500",
        "description": "Three nines availability",
    }
    burn_alerts = [{"id": "ba-1", "exhaustion_minutes": 240, "created_at": "2026-10-10T00:00:00Z"}]
    md = _render_slo_markdown("prod-web", slo, burn_alerts)
    assert "# Honeycomb SLO: API Availability" in md
    assert "`99.9%`" in md
    assert "`4.0h`" in md


def test_render_markers_markdown():
    markers = [
        {
            "message": "Release v2.4.0 deployed",
            "type": "deploy",
            "start_time": "1728561600",
            "url": "https://github.com/org/repo/releases/tag/v2.4.0",
        }
    ]
    md = _render_markers_markdown("prod-web", markers)
    assert "# Honeycomb Markers: `prod-web`" in md
    assert "Release v2.4.0 deployed" in md


def test_honeycomb_source_full_sync():
    mock_client = MagicMock()
    mock_client.base_url = "https://api.honeycomb.io"
    mock_client.list_datasets.return_value = [{"name": "web", "slug": "web"}]
    mock_client.list_columns.return_value = [{"key_name": "duration_ms", "type": "float"}]
    mock_client.list_triggers.return_value = [{"name": "latency_alert", "id": "t1"}]
    mock_client.list_slos.return_value = [
        {"name": "uptime", "id": "s1", "target_per_million": 990000}
    ]
    mock_client.list_burn_alerts.return_value = []
    mock_client.list_markers.return_value = [{"message": "deploy", "type": "deploy"}]
    mock_client.list_boards.return_value = [{"name": "ops", "id": "b1"}]

    config = HoneycombConfig(api_key="dummy-key")
    source = honeycomb_source(config=config, client=mock_client)

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == HONEYCOMB_SOURCE_NAME

    items = list(next(iter(source.resources.values()))())
    assert len(items) == 5

    entity_types = {item["entity_type"] for item in items}
    assert entity_types == {"dataset", "trigger", "slo", "markers", "board"}


def test_honeycomb_source_dataset_filter():
    mock_client = MagicMock()
    mock_client.base_url = "https://api.honeycomb.io"
    mock_client.list_datasets.return_value = [
        {"name": "Keep Me", "slug": "keep-me"},
        {"name": "Skip Me", "slug": "skip-me"},
    ]
    mock_client.list_columns.return_value = []
    mock_client.list_triggers.return_value = []
    mock_client.list_slos.return_value = []
    mock_client.list_burn_alerts.return_value = []
    mock_client.list_markers.return_value = []
    mock_client.list_boards.return_value = []

    config = HoneycombConfig(api_key="key", dataset_slugs=["keep-me"], include_boards=False)
    source = honeycomb_source(config=config, client=mock_client)

    items = list(next(iter(source.resources.values()))())
    assert len(items) == 1
    assert items[0]["dataset_slug"] == "keep-me"
