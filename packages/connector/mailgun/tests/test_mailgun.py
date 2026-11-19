"""Unit tests for the Mailgun email telemetry & template data-source connector.

All tests run completely offline without live server access or network requests,
using synthetic Mailgun API payloads and mocked HTTP transports.
"""

from __future__ import annotations

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_mailgun.mailgun import (
    DEFAULT_EU_BASE_URL,
    DEFAULT_US_BASE_URL,
    MAILGUN_SOURCE_NAME,
    MAILGUN_TABLE_NAME,
    MailgunAPIError,
    MailgunAuthError,
    MailgunClient,
    MailgunConfig,
    MailgunRateLimitError,
    _build_bounces_markdown,
    _build_domains_markdown,
    _build_stats_markdown,
    _build_templates_markdown,
    _redact_token,
    mailgun_connector,
    mailgun_source,
)

# ---------------------------------------------------------------------------
# Synthetic Test Fixtures
# ---------------------------------------------------------------------------

SAMPLE_DOMAINS = {
    "items": [
        {
            "name": "mg.example.com",
            "smtp_login": "postmaster@mg.example.com",
            "state": "active",
            "spam_action": "tag",
            "created_at": "Mon, 15 Jan 2024 10:00:00 GMT",
            "type": "custom",
        },
        {
            "name": "alerts.example.com",
            "smtp_login": "postmaster@alerts.example.com",
            "state": "active",
            "spam_action": "disabled",
            "created_at": "Fri, 01 Mar 2024 14:00:00 GMT",
            "type": "custom",
        },
    ],
    "total_count": 2,
}

SAMPLE_STATS = {
    "stats": [
        {
            "time": "Sun, 01 Oct 2026 00:00:00 GMT",
            "accepted": {"total": 1250, "outgoing": 1250},
            "delivered": {"total": 1230, "smtp": 1230},
            "failed": {"temporary": {"total": 15}, "permanent": {"total": 5}},
            "complained": {"total": 1},
        }
    ]
}

SAMPLE_TEMPLATES = {
    "items": [
        {
            "name": "welcome-user",
            "description": "Onboarding confirmation email with activation link",
            "created_at": "Wed, 10 Jan 2024 12:00:00 GMT",
        }
    ]
}

SAMPLE_TEMPLATE_VERSIONS = {
    "template": {
        "name": "welcome-user",
        "versions": [
            {
                "tag": "v2",
                "active": True,
                "comment": "Updated CTA button branding and copy",
                "created_at": "Thu, 15 Feb 2024 14:00:00 GMT",
            }
        ],
    }
}

SAMPLE_BOUNCES = {
    "items": [
        {
            "address": "invalid-recipient@dest.example.com",
            "code": "550",
            "error": "5.1.1 User mailbox unknown or inactive",
            "created_at": "Thu, 20 Oct 2026 08:30:00 GMT",
        }
    ]
}


class MockTransport(httpx.BaseTransport):
    """Mock HTTP transport returning synthetic Mailgun API responses."""

    def __init__(self, status_code: int = 200, error_text: str | None = None) -> None:
        self.status_code = status_code
        self.error_text = error_text
        self.call_count = 0
        self.last_request = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.last_request = request

        if self.status_code != 200:
            text = self.error_text or "Simulated Mailgun error"
            return httpx.Response(status_code=self.status_code, text=text)

        path = request.url.path
        if "/templates/" in path and "/versions" in path:
            return httpx.Response(200, json=SAMPLE_TEMPLATE_VERSIONS)
        if "/templates" in path:
            return httpx.Response(200, json=SAMPLE_TEMPLATES)
        if "/stats/total" in path:
            return httpx.Response(200, json=SAMPLE_STATS)
        if "/bounces" in path:
            return httpx.Response(200, json=SAMPLE_BOUNCES)
        if path.endswith("/domains"):
            return httpx.Response(200, json=SAMPLE_DOMAINS)

        return httpx.Response(200, json={"items": []})


# ---------------------------------------------------------------------------
# Helper & Redaction Tests
# ---------------------------------------------------------------------------


def test_redact_token() -> None:
    api_key = "key-secret-live-123456789"
    msg = f"Request rejected for api_key {api_key} on api.mailgun.net"
    redacted = _redact_token(msg, api_key)

    assert api_key not in redacted
    assert "[REDACTED_MAILGUN_KEY]" in redacted


def test_redact_token_short_or_none() -> None:
    assert _redact_token("error msg", None) == "error msg"
    assert _redact_token("error msg", "12") == "error msg"


# ---------------------------------------------------------------------------
# MailgunClient Transport & Auth Tests
# ---------------------------------------------------------------------------


def test_client_endpoints_us_and_eu() -> None:
    client_us = MailgunClient(api_key="key-test", is_eu_region=False)
    assert client_us.base_url == DEFAULT_US_BASE_URL

    client_eu = MailgunClient(api_key="key-test", is_eu_region=True)
    assert client_eu.base_url == DEFAULT_EU_BASE_URL


def test_client_fetches_domains() -> None:
    transport = MockTransport()
    client = MailgunClient(api_key="key-test", client=httpx.Client(transport=transport))

    domains = client.get_domains()
    assert len(domains) == 2
    assert domains[0]["name"] == "mg.example.com"
    assert domains[1]["name"] == "alerts.example.com"


def test_client_fetches_stats_and_templates() -> None:
    transport = MockTransport()
    client = MailgunClient(api_key="key-test", client=httpx.Client(transport=transport))

    stats = client.get_stats("mg.example.com")
    assert "stats" in stats
    assert stats["stats"][0]["accepted"]["total"] == 1250

    templates = client.get_templates("mg.example.com")
    assert len(templates) == 1
    assert templates[0]["name"] == "welcome-user"

    versions = client.get_template_versions("mg.example.com", "welcome-user")
    assert len(versions) == 1
    assert versions[0]["tag"] == "v2"


def test_client_fetches_bounces() -> None:
    transport = MockTransport()
    client = MailgunClient(api_key="key-test", client=httpx.Client(transport=transport))

    bounces = client.get_bounces("mg.example.com")
    assert len(bounces) == 1
    assert bounces[0]["address"] == "invalid-recipient@dest.example.com"
    assert bounces[0]["code"] == "550"


def test_client_handles_auth_error_401() -> None:
    api_key = "invalid-key-secret-123"
    transport = MockTransport(
        status_code=401,
        error_text=f"Unauthorized access for api_key: {api_key}",
    )
    client = MailgunClient(api_key=api_key, client=httpx.Client(transport=transport))

    with pytest.raises(MailgunAuthError) as exc_info:
        client.get_domains()

    assert api_key not in str(exc_info.value)
    assert "[REDACTED_MAILGUN_KEY]" in str(exc_info.value)


def test_client_handles_rate_limit_429() -> None:
    transport = MockTransport(status_code=429)
    client = MailgunClient(api_key="key-test", client=httpx.Client(transport=transport))

    with pytest.raises(MailgunRateLimitError):
        client.get_domains()


def test_client_handles_server_error_500() -> None:
    transport = MockTransport(status_code=500)
    client = MailgunClient(api_key="key-test", client=httpx.Client(transport=transport))

    with pytest.raises(MailgunAPIError):
        client.get_domains()


# ---------------------------------------------------------------------------
# Markdown Formatting Tests
# ---------------------------------------------------------------------------


def test_build_domains_markdown() -> None:
    domains = SAMPLE_DOMAINS["items"]
    md = _build_domains_markdown(domains, "US (api.mailgun.net)")

    assert "# Mailgun Sending Domains & DNS Reputation" in md
    assert "mg.example.com" in md
    assert "alerts.example.com" in md
    assert "Sending Status**: ACTIVE" in md


def test_build_stats_markdown() -> None:
    stats = SAMPLE_STATS
    md = _build_stats_markdown("mg.example.com", stats)

    assert "# Mailgun Delivery & Engagement Analytics: mg.example.com" in md
    assert "Accepted Messages**: 1250" in md
    assert "Successfully Delivered**: 1230" in md
    assert "Spam Complaints**: 1" in md


def test_build_templates_markdown() -> None:
    templates = SAMPLE_TEMPLATES["items"]
    versions_map = {"welcome-user": SAMPLE_TEMPLATE_VERSIONS["template"]["versions"]}
    md = _build_templates_markdown("mg.example.com", templates, versions_map)

    assert "# Mailgun Email Templates Catalog: mg.example.com" in md
    assert "welcome-user" in md
    assert "Onboarding confirmation email" in md
    assert "Tag `v2` [ACTIVE]" in md
    assert "Updated CTA button branding and copy" in md


def test_build_bounces_markdown() -> None:
    bounces = SAMPLE_BOUNCES["items"]
    md = _build_bounces_markdown("mg.example.com", bounces)

    assert "# Mailgun Bounce & Failure Diagnostics: mg.example.com" in md
    assert "invalid-recipient@dest.example.com" in md
    assert "SMTP Error Code**: `550`" in md
    assert "User mailbox unknown" in md


# ---------------------------------------------------------------------------
# DLT Source Invariant & Integration Tests
# ---------------------------------------------------------------------------


def test_mailgun_source_document_mode_invariant() -> None:
    transport = MockTransport()
    fake_client = MailgunClient("key-test", client=httpx.Client(transport=transport))

    config = MailgunConfig(api_key="key-test")
    source = mailgun_source(config=config, client=fake_client)

    # Invariant: Must declare DOCUMENT_SOURCE_ATTR = "mailgun"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == MAILGUN_SOURCE_NAME


def test_mailgun_source_yields_expected_reports() -> None:
    transport = MockTransport()
    fake_client = MailgunClient("key-test", client=httpx.Client(transport=transport))

    config = MailgunConfig(
        api_key="key-test",
        include_stats=True,
        include_templates=True,
        include_bounces=True,
    )
    source = mailgun_source(config=config, client=fake_client)

    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == MAILGUN_TABLE_NAME
    assert resource.write_disposition == "replace"

    rows = list(resource())
    # 1 overview + 2 domains * (1 stats + 1 template + 1 bounce) = 1 + 6 = 7 documents
    assert len(rows) == 7

    report_types = [r["metadata"]["report_type"] for r in rows]
    assert "domains_overview" in report_types
    assert "delivery_analytics" in report_types
    assert "templates_catalog" in report_types
    assert "bounces_and_failures" in report_types

    for r in rows:
        assert r["metadata"]["source"] == MAILGUN_SOURCE_NAME
        assert len(r["id"]) > 0
        assert r["text"].startswith("# ")


def test_mailgun_source_deterministic_id_stability() -> None:
    transport = MockTransport()
    fake_client = MailgunClient("key-test", client=httpx.Client(transport=transport))

    config = MailgunConfig(api_key="key-test")

    source_1 = mailgun_source(config=config, client=fake_client)
    rows_1 = list(next(iter(source_1.selected_resources.values()))())

    source_2 = mailgun_source(config=config, client=fake_client)
    rows_2 = list(next(iter(source_2.selected_resources.values()))())

    # Deterministic IDs ensure repeat runs update existing nodes without duplicates
    assert [r["id"] for r in rows_1] == [r["id"] for r in rows_2]


def test_mailgun_source_filters_by_domain() -> None:
    transport = MockTransport()
    fake_client = MailgunClient("key-test", client=httpx.Client(transport=transport))

    config = MailgunConfig(
        api_key="key-test",
        domains=["mg.example.com"],
        include_stats=True,
        include_templates=False,
        include_bounces=False,
    )
    source = mailgun_source(config=config, client=fake_client)

    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())

    # 1 overview (filtered) + 1 stats = 2
    assert len(rows) == 2
    assert rows[0]["metadata"]["report_type"] == "domains_overview"
    assert rows[1]["metadata"]["report_type"] == "delivery_analytics"
    assert rows[1]["metadata"]["domain"] == "mg.example.com"


@pytest.mark.asyncio
async def test_mailgun_connector_async_wrapper() -> None:
    transport = MockTransport()
    fake_client = MailgunClient("key-test", client=httpx.Client(transport=transport))

    config = MailgunConfig(
        api_key="key-test",
        domains=["mg.example.com"],
        include_stats=True,
        include_templates=True,
        include_bounces=True,
    )

    documents = await mailgun_connector(config, client=fake_client)
    # 1 overview + 1 stats + 1 template + 1 bounce = 4
    assert len(documents) == 4
    for doc in documents:
        assert "id" in doc
        assert "text" in doc
        assert doc["metadata"]["source"] == "mailgun"
