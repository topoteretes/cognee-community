"""Tests for Inngest document rendering and walker row generation."""

import pytest
from cognee_community_connector_inngest.inngest import (
    render_function,
    render_run,
    render_event,
    inngest_source,
    _iter_rows,
)
from fake_inngest import FakeInngest


def test_render_function():
    fn = {
        "id": "sync-user-stripe",
        "name": "Sync User Stripe",
        "app_id": "billing-app",
        "triggers": [{"event": "stripe/customer.created"}],
        "concurrency": 10,
    }
    doc = render_function(fn)
    assert doc["id"] == "function:sync-user-stripe"
    assert "Inngest Function: Sync User Stripe" in doc["title"]
    assert "Sync User Stripe" in doc["content"]
    assert "Event: stripe/customer.created" in doc["content"]
    assert "billing-app" in doc["content"]
    assert doc["_deleted"] is False


def test_render_run_with_error():
    run = {
        "run_id": "run_01H123456",
        "function_id": "sync-user-stripe",
        "status": "FAILED",
        "started_at": "2026-10-01T12:00:00Z",
        "ended_at": "2026-10-01T12:00:02Z",
        "error": {
            "message": "Payment gateway timeout",
            "stack": "Traceback:\n  File 'app.py', line 45 in sync_stripe\n    raise TimeoutError()",
        },
    }
    doc = render_run(run)
    assert doc["id"] == "run:run_01H123456"
    assert "Function Run: sync-user-stripe - FAILED" in doc["title"]
    assert "Payment gateway timeout" in doc["content"]
    assert "TimeoutError" in doc["content"]
    assert doc["_deleted"] is False


def test_render_event():
    evt = {
        "id": "evt_signup_001",
        "name": "user/signup.completed",
        "timestamp": "2026-10-01T11:59:00Z",
    }
    doc = render_event(evt)
    assert doc["id"] == "event:evt_signup_001"
    assert "Inngest Event: user/signup.completed" in doc["title"]
    assert "user/signup.completed" in doc["content"]
    assert doc["_deleted"] is False


def test_iter_rows_walker():
    fake = FakeInngest()
    fake.add_function("send-email", "Send Email")
    fake.add_event("user/signup.completed")
    fake.add_run("run_1", "send-email", "COMPLETED")

    state = {}
    stats = {}
    rows = list(_iter_rows(fake, state, stats, include=("functions", "events", "runs")))

    assert len(rows) == 3
    ids = {r["id"] for r in rows}
    assert "function:send-email" in ids
    assert "event:evt_user/signup.completed_1" in ids
    assert "run:run_1" in ids
    assert stats["scanned"] == 3
    assert stats["skipped"] == 0


def test_source_validation():
    with pytest.raises(ValueError, match="non-empty resource_name"):
        inngest_source(resource_name="", api_key="signkey-prod-12345678")

    with pytest.raises(ValueError, match="requires an api_key"):
        inngest_source(resource_name="test")

    with pytest.raises(ValueError, match="non-empty subset"):
        inngest_source(resource_name="test", api_key="signkey-prod-12345678", include=("invalid_kind",))
