"""Tests for Trigger.dev documents rendering, source creation and rows generation."""

import pytest
from cognee_community_connector_triggerdev.triggerdev import (
    render_task,
    render_run,
    render_schedule,
    triggerdev_source,
    _iter_rows,
)
from fake_triggerdev import FakeTriggerDev


def test_render_task():
    task = {
        "id": "task_1",
        "slug": "stripe-webhook-handler",
        "filePath": "src/trigger/stripe.ts",
        "exportName": "stripeHandler",
        "triggerSource": "webhook",
        "createdAt": "2026-10-01T10:00:00Z",
    }
    doc = render_task(task)
    assert doc["id"] == "task:task_1"
    assert "stripe-webhook-handler" in doc["title"]
    assert "src/trigger/stripe.ts" in doc["content"]
    assert doc["_deleted"] is False


def test_render_run_with_error():
    run = {
        "id": "run_999",
        "taskIdentifier": "send-welcome-email",
        "number": 42,
        "status": "FAILED",
        "durationMs": 1250,
        "error": {
            "name": "SMTPConnectionTimeout",
            "message": "Connection to mail.acme.com:587 timed out",
            "stack": "Error: Connection timed out\n  at Socket.connect (net.js:123)",
        },
    }
    doc = render_run(run)
    assert doc["id"] == "run:run_999"
    assert "send-welcome-email (#42) - FAILED" in doc["title"]
    assert "SMTPConnectionTimeout" in doc["content"]
    assert "Connection to mail.acme.com:587 timed out" in doc["content"]
    assert doc["_deleted"] is False


def test_render_schedule():
    sched = {
        "id": "sched_daily",
        "taskIdentifier": "daily-summary-report",
        "cron": "0 8 * * *",
        "timezone": "America/New_York",
        "active": True,
    }
    doc = render_schedule(sched)
    assert doc["id"] == "schedule:sched_daily"
    assert "Schedule: daily-summary-report (0 8 * * *)" in doc["title"]
    assert "America/New_York" in doc["content"]
    assert "Status: Active" in doc["content"]
    assert doc["_deleted"] is False


def test_iter_rows_walker():
    fake = FakeTriggerDev()
    fake.add_task("task-a")
    fake.add_schedule("sched-1", "task-a")
    fake.add_run("run-1", "task-a", status="COMPLETED")
    fake.add_run("run-2", "task-a", status="FAILED", error={"name": "Err", "message": "Failed"})

    state = {}
    stats = {}
    rows = list(_iter_rows(fake, state, stats, include=("tasks", "runs", "schedules")))

    assert len(rows) == 4
    ids = {r["id"] for r in rows}
    assert "task:task_task-a" in ids
    assert "schedule:sched-1" in ids
    assert "run:run-1" in ids
    assert "run:run-2" in ids
    assert stats["scanned"] == 4
    assert stats["skipped"] == 0


def test_source_validation():
    with pytest.raises(ValueError, match="non-empty resource_name"):
        triggerdev_source(resource_name="", api_key="tr_prod_123")

    with pytest.raises(ValueError, match="requires an api_key"):
        triggerdev_source(resource_name="test")

    with pytest.raises(ValueError, match="non-empty subset"):
        triggerdev_source(resource_name="test", api_key="tr_prod_123", include=("invalid_kind",))
