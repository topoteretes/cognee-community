"""Tests for Resend row formatting, secret redaction, and walker traversal."""

from fake_resend import FakeResendClient

from cognee_community_connector_resend.resend import (
    ResendWalker,
    _format_api_key,
    _format_domain,
    _format_email,
    _redact_value,
)


def test_format_domain():
    raw = {
        "id": "dom_abc_123",
        "name": "mail.example.com",
        "status": "verified",
        "region": "us-east-1",
        "created_at": "2026-01-01T00:00:00Z",
    }
    doc = _format_domain(raw)
    assert doc["id"] == "domain:dom_abc_123"
    assert doc["title"] == "Resend Domain: mail.example.com (verified)"
    assert "mail.example.com" in doc["content"]
    assert doc["_deleted"] is False
    assert "https://resend.com/domains/dom_abc_123" in doc["url"]


def test_format_email_redaction():
    raw = {
        "id": "em_xyz_789",
        "subject": "Monthly Invoice",
        "from": "billing@company.com",
        "to": ["finance_lead@customer.com"],
        "last_event": "delivered",
        "created_at": "2026-10-20T00:00:00Z",
    }
    doc = _format_email(raw)
    assert doc["id"] == "email:em_xyz_789"
    assert "Monthly Invoice" in doc["title"]
    assert "delivered" in doc["content"]
    assert doc["_deleted"] is False


def test_format_api_key_redaction():
    raw = {"id": "key_worker_1", "name": "Production Key", "created_at": "2026-01-01T00:00:00Z"}
    doc = _format_api_key(raw)
    assert doc["id"] == "api_key:key_worker_1"
    assert "[Configured & Protected]" in doc["content"]
    assert doc["_deleted"] is False


def test_redact_value_utility():
    assert _redact_value("re_live_secret_token_123") == "[Configured & Protected]"
    assert _redact_value("test_user@domain.com") == "te***@domain.com"
    data = {
        "name": "safe_domain_name",
        "secret_token": "re_secret_abc",
        "nested": {"api_key": "hidden_key"},
    }
    redacted = _redact_value(data)
    assert redacted["name"] == "safe_domain_name"
    assert redacted["secret_token"] == "[Configured & Protected]"
    assert redacted["nested"]["api_key"] == "[Configured & Protected]"


def test_walker_filtering(sample_data):
    client = FakeResendClient(sample_data=sample_data)
    walker = ResendWalker(client=client, include=("domains",))
    state = {}
    docs = list(walker.walk(state))

    assert len(docs) == 2
    assert all(d["id"].startswith("domain:") for d in docs)
    assert "seen_ids" in state
    assert len(state["seen_ids"]) == 2


def test_walker_all_categories(sample_data):
    client = FakeResendClient(sample_data=sample_data)
    walker = ResendWalker(
        client=client,
        include=("domains", "emails", "audiences", "api_keys"),
    )
    state = {}
    docs = list(walker.walk(state))

    # 2 domains + 1 email + 1 audience + 1 api_key = 5 documents
    assert len(docs) == 5
    ids = {d["id"] for d in docs}
    assert "domain:dom_prod_001" in ids
    assert "email:em_sent_101" in ids
    assert "audience:aud_saas_users" in ids
    assert "api_key:key_service_worker" in ids
