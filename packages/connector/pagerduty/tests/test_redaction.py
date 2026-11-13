"""Tests for credential and secret scrubbing in PagerDuty connector."""

from cognee_community_connector_pagerduty.redaction import redact_secrets


def test_redact_bearer_token():
    text = "Authorization header: Bearer abcdef1234567890abcdef1234567890 in log"
    redacted = redact_secrets(text)
    assert "abcdef1234567890" not in redacted
    assert "Bearer [REDACTED]" in redacted


def test_redact_api_key():
    text = "Failed with api_key: 12345678901234567890 and secret=abcdef1234567890"
    redacted = redact_secrets(text)
    assert "12345678901234567890" not in redacted
    assert "[REDACTED_KEY]" in redacted


def test_redact_aws_key():
    text = "Found AWS access key AKIAIOSFODNN7EXAMPLE in traceback"
    redacted = redact_secrets(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "[REDACTED_AWS_KEY]" in redacted


def test_redact_github_pat():
    text = "Token: ghp_123456789012345678901234567890123456"
    redacted = redact_secrets(text)
    assert "ghp_1234567890" not in redacted
    assert "[REDACTED_GITHUB_PAT]" in redacted


def test_redact_clean_text():
    text = "Normal database connection pool exhausted on prod-db-01."
    assert redact_secrets(text) == text
