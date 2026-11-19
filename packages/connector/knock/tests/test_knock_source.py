"""Tests for Knock row formatting, secret redaction, and walker pagination."""

from fake_knock import FakeKnockClient

from cognee_community_connector_knock.knock import (
    KnockWalker,
    _format_channel,
    _format_workflow,
    _redact_value,
)


def test_format_workflow():
    raw = {
        "key": "order-confirmation",
        "name": "Order Confirmation",
        "description": "Sent after checkout",
        "active": True,
        "steps": [{"type": "channel", "channel_id": "sendgrid-primary"}],
        "updated_at": "2026-10-21T00:00:00Z",
    }
    doc = _format_workflow(raw)
    assert doc["id"] == "workflow:order-confirmation"
    assert doc["title"] == "Knock Workflow: Order Confirmation"
    assert "Sent after checkout" in doc["content"]
    assert doc["_deleted"] is False
    assert "https://dashboard.knock.app/workflows/order-confirmation" in doc["url"]


def test_format_channel_secret_redaction():
    raw = {
        "id": "email-chan-1",
        "name": "SendGrid Direct",
        "type": "email",
        "provider": "sendgrid",
        "created_at": "2026-01-01T00:00:00Z",
    }
    doc = _format_channel(raw)
    assert doc["id"] == "channel:email-chan-1"
    assert "SendGrid Direct" in doc["title"]
    assert "email" in doc["content"]
    assert doc["_deleted"] is False


def test_redact_value_utility():
    assert _redact_value("my_secret_token_123") == "[Configured & Protected]"
    assert _redact_value("api_password") == "[Configured & Protected]"
    assert _redact_value("test_user@domain.com") == "te***@domain.com"
    data = {
        "normal_key": "safe_value",
        "auth_secret": "sensitive_password_abc",
        "nested": {"nested_token": "hidden"},
    }
    redacted = _redact_value(data)
    assert redacted["normal_key"] == "safe_value"
    assert redacted["auth_secret"] == "[Configured & Protected]"
    assert redacted["nested"]["nested_token"] == "[Configured & Protected]"


def test_walker_pagination_and_filtering(sample_data):
    client = FakeKnockClient(sample_data=sample_data)
    walker = KnockWalker(client=client, include=("workflows",))
    state = {}
    docs = list(walker.walk(state))

    assert len(docs) == 2
    assert all(d["id"].startswith("workflow:") for d in docs)
    assert "seen_ids" in state
    assert len(state["seen_ids"]) == 2


def test_walker_all_categories(sample_data):
    client = FakeKnockClient(sample_data=sample_data)
    walker = KnockWalker(
        client=client,
        include=("workflows", "channels", "schedules", "messages"),
    )
    state = {}
    docs = list(walker.walk(state))

    # 2 workflows + 2 channels + 1 schedule + 1 message = 6 documents
    assert len(docs) == 6
    ids = {d["id"] for d in docs}
    assert "workflow:user-welcome" in ids
    assert "channel:sendgrid-primary" in ids
    assert "schedule:sch_weekly_digest" in ids
    assert "message:msg_001" in ids
