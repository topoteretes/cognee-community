"""Unit tests for secret scrubbing and redaction."""

import pytest
from cognee_community_connector_jupyter.jupyter import redact_sensitive_data


def test_redact_bearer_token():
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test"
    redacted = redact_sensitive_data(text)
    assert "eyJhbGciOiJI" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_api_key():
    text = "client = APIClient(api_key='abc123456789xyz')"
    redacted = redact_sensitive_data(text)
    assert "abc123456789xyz" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_aws_key():
    text = "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    redacted = redact_sensitive_data(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_github_pat():
    text = "git clone https://ghp_0123456789abcdefghijklmnopqrstuvwx@github.com/repo"
    redacted = redact_sensitive_data(text)
    assert "ghp_0123456789abcdefghijklmnopqrstuvwx" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_openai_key():
    text = "openai.api_key = 'sk-1234567890abcdef1234567890abcdef12'"
    redacted = redact_sensitive_data(text)
    assert "sk-1234567890abcdef1234567890abcdef12" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_empty_or_clean_text():
    assert redact_sensitive_data("") == ""
    assert redact_sensitive_data("Clean python code: def add(a, b): return a + b") == "Clean python code: def add(a, b): return a + b"
