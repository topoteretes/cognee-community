"""Tests for Axiom document rendering and walker row generation."""

import pytest
from cognee_community_connector_axiom.axiom import (
    render_dataset,
    render_monitor,
    render_notifier,
    axiom_source,
    _iter_rows,
)
from fake_axiom import FakeAxiom


def test_render_dataset():
    ds = {
        "id": "http-logs",
        "name": "http-logs",
        "description": "Production edge HTTP access logs",
        "retentionDays": 60,
        "created": "2026-10-01T10:00:00Z",
    }
    doc = render_dataset(ds)
    assert doc["id"] == "dataset:http-logs"
    assert "Axiom Dataset: http-logs" in doc["title"]
    assert "Production edge HTTP access logs" in doc["content"]
    assert "Retention Days: 60" in doc["content"]
    assert doc["_deleted"] is False


def test_render_monitor():
    mon = {
        "id": "mon_01H123456",
        "name": "High 5xx Rate",
        "description": "Alerts when HTTP 5xx responses exceed 10 in 5m",
        "aplQuery": "['http-logs'] | where status >= 500 | count()",
        "interval": "5m",
        "operator": "above",
        "threshold": 10,
        "disabledUntil": None,
    }
    doc = render_monitor(mon)
    assert doc["id"] == "monitor:mon_01H123456"
    assert "Axiom Monitor: High 5xx Rate [Active]" in doc["title"]
    assert "['http-logs'] | where status >= 500" in doc["content"]
    assert "Threshold: 10" in doc["content"]
    assert doc["_deleted"] is False


def test_render_notifier():
    notif = {
        "id": "notif_slack_ops",
        "name": "Slack On-Call",
        "type": "slack",
        "description": "Post alerts to #eng-ops",
    }
    doc = render_notifier(notif)
    assert doc["id"] == "notifier:notif_slack_ops"
    assert "Axiom Notifier: Slack On-Call (slack)" in doc["title"]
    assert "Post alerts to #eng-ops" in doc["content"]
    assert doc["_deleted"] is False


def test_iter_rows_walker():
    fake = FakeAxiom()
    fake.add_dataset("web-traffic")
    fake.add_monitor("mon_1", "Error Spike", "['web-traffic'] | where status == 500")
    fake.add_notifier("notif_1", "DevOps Pager", "pagerduty")

    state = {}
    stats = {}
    rows = list(_iter_rows(fake, state, stats, include=("datasets", "monitors", "notifiers")))

    assert len(rows) == 3
    ids = {r["id"] for r in rows}
    assert "dataset:web-traffic" in ids
    assert "monitor:mon_1" in ids
    assert "notifier:notif_1" in ids
    assert stats["scanned"] == 3
    assert stats["skipped"] == 0


def test_source_validation():
    with pytest.raises(ValueError, match="non-empty resource_name"):
        axiom_source(resource_name="", api_key="xapt-12345678")

    with pytest.raises(ValueError, match="requires an api_key"):
        axiom_source(resource_name="test")

    with pytest.raises(ValueError, match="non-empty subset"):
        axiom_source(resource_name="test", api_key="xapt-12345678", include=("invalid_kind",))
