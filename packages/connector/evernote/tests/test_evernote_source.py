"""Unit tests for the Evernote data-source stub."""

import pytest

from cognee_community_connector_evernote import evernote_source


def test_missing_credentials_raises_value_error(monkeypatch):
    """When no token is provided and EVERNOTE_TOKEN is not set, ValueError is raised."""
    monkeypatch.delenv("EVERNOTE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Evernote authentication token required"):
        evernote_source()


def test_token_never_appears_in_repr():
    """Sensitive token string must never appear in the source representation."""
    secret_token = "secret-token-value-12345-xyz"
    source = evernote_source(token=secret_token)
    assert secret_token not in repr(source)
    assert secret_token not in str(source)


def test_token_never_appears_in_error_message(monkeypatch):
    """Sensitive token values must never be echoed in exception messages."""
    monkeypatch.delenv("EVERNOTE_TOKEN", raising=False)
    with pytest.raises(ValueError) as exc_info:
        evernote_source()
    assert "EVERNOTE_TOKEN" in str(exc_info.value)
    assert "secret" not in str(exc_info.value).lower()


def test_explicit_token_initialization():
    """Passing an explicit token succeeds without raising."""
    source = evernote_source(token="valid-token-123")
    assert source.resource_name == "evernote_notes"
    assert source.sandbox is False


def test_client_injection_bypasses_token_check(monkeypatch):
    """Injecting a client object succeeds even if token is absent."""
    monkeypatch.delenv("EVERNOTE_TOKEN", raising=False)
    dummy_client = object()
    source = evernote_source(client=dummy_client)
    assert source.client is dummy_client


def test_environment_variable_token(monkeypatch):
    """Setting EVERNOTE_TOKEN environment variable allows initialization."""
    monkeypatch.setenv("EVERNOTE_TOKEN", "env-token-xyz")
    source = evernote_source()
    assert source.resource_name == "evernote_notes"
