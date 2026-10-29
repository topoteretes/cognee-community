"""Unit tests for Evernote 3-legged OAuth 1.0a authentication helper.

All tests run offline using fake/mock HTTP clients.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cognee_community_connector_evernote import (
    EvernoteAccessToken,
    EvernoteAuthError,
    EvernoteOAuth,
)


class FakeHttpClient:
    """Mock HTTP client with httpx-style .post(url, headers=..., content=...) method."""

    def __init__(self, response=None, handler=None):
        self.response = response
        self.handler = handler
        self.calls = []

    def post(self, url, headers=None, content=None):
        self.calls.append({"url": url, "headers": headers, "content": content})
        if self.handler:
            return self.handler(url, headers=headers, content=content)
        return self.response


def test_missing_consumer_credentials_raises_value_error(monkeypatch):
    """When consumer key or secret is missing and not in env, ValueError is raised."""
    monkeypatch.delenv("EVERNOTE_CONSUMER_KEY", raising=False)
    monkeypatch.delenv("EVERNOTE_CONSUMER_SECRET", raising=False)

    with pytest.raises(ValueError, match="Evernote consumer_key and consumer_secret are required"):
        EvernoteOAuth()

    with pytest.raises(ValueError, match="Evernote consumer_key and consumer_secret are required"):
        EvernoteOAuth(consumer_key="my-key", consumer_secret=None)

    with pytest.raises(ValueError, match="Evernote consumer_key and consumer_secret are required"):
        EvernoteOAuth(consumer_key=None, consumer_secret="my-secret")


def test_consumer_credentials_from_environment(monkeypatch):
    """Consumer credentials fall back to environment variables."""
    monkeypatch.setenv("EVERNOTE_CONSUMER_KEY", "env-key")
    monkeypatch.setenv("EVERNOTE_CONSUMER_SECRET", "env-secret")

    oauth = EvernoteOAuth()
    assert oauth._consumer_key == "env-key"
    assert oauth._consumer_secret == "env-secret"
    assert oauth.sandbox is False


def test_authorize_url_production_and_sandbox():
    """Authorize URL is correctly built for both production and sandbox environments."""
    oauth_prod = EvernoteOAuth(
        consumer_key="key",
        consumer_secret="secret",
        sandbox=False,
    )
    url_prod = oauth_prod.authorize_url("req-token-123")
    assert url_prod == "https://www.evernote.com/OAuth.action?oauth_token=req-token-123"

    oauth_sandbox = EvernoteOAuth(
        consumer_key="key",
        consumer_secret="secret",
        sandbox=True,
    )
    url_sandbox = oauth_sandbox.authorize_url("req-token-xyz+abc")
    assert (
        url_sandbox == "https://sandbox.evernote.com/OAuth.action?oauth_token=req-token-xyz%2Babc"
    )

    with pytest.raises(ValueError, match="oauth_token is required"):
        oauth_prod.authorize_url("")


def test_request_token_signing_parameters_present():
    """Request token request signs with OAuth 1.0a HMAC-SHA1 headers and sends callback."""
    fake_http = FakeHttpClient(
        response=SimpleNamespace(
            status_code=200,
            text=(
                "oauth_token=test-req-token&"
                "oauth_token_secret=test-req-secret&"
                "oauth_callback_confirmed=true"
            ),
        )
    )

    oauth = EvernoteOAuth(
        consumer_key="consumer-key-1",
        consumer_secret="consumer-secret-1",
        sandbox=True,
        http=fake_http,
    )

    req_token, req_secret = oauth.get_request_token(callback_url="https://app.example.com/oauth/cb")

    assert req_token == "test-req-token"
    assert req_secret == "test-req-secret"
    assert len(fake_http.calls) == 1
    call = fake_http.calls[0]
    assert call["url"] == "https://sandbox.evernote.com/oauth"

    auth_header = call["headers"]["Authorization"]
    assert auth_header.startswith("OAuth ")
    assert 'oauth_consumer_key="consumer-key-1"' in auth_header
    assert 'oauth_signature_method="HMAC-SHA1"' in auth_header
    assert "oauth_signature=" in auth_header
    assert "oauth_nonce=" in auth_header
    assert "oauth_timestamp=" in auth_header
    assert "oauth_callback=" in auth_header


def test_get_access_token_parsing():
    """Access token exchange parses token, user_id, expires, and note_store_url."""
    fake_http = FakeHttpClient(
        response=SimpleNamespace(
            status_code=200,
            text=(
                "oauth_token=final-access-token-999&"
                "edam_noteStoreUrl=https%3A%2F%2Fsandbox.evernote.com%2Fedam%2Fnotestore%2Fs1&"
                "edam_userId=424242&"
                "edam_expires=1700000000000"
            ),
        )
    )

    oauth = EvernoteOAuth(
        consumer_key="ckey",
        consumer_secret="csec",
        sandbox=True,
        http=fake_http,
    )

    access_token = oauth.get_access_token(
        oauth_token="req-tok",
        oauth_token_secret="req-sec",
        verifier="verifier-123",
    )

    assert isinstance(access_token, EvernoteAccessToken)
    assert access_token.token == "final-access-token-999"
    assert access_token.note_store_url == "https://sandbox.evernote.com/edam/notestore/s1"
    assert access_token.user_id == 424242
    assert access_token.expires == 1700000000000

    call = fake_http.calls[0]
    auth_header = call["headers"]["Authorization"]
    assert 'oauth_token="req-tok"' in auth_header
    assert 'oauth_verifier="verifier-123"' in auth_header


def test_get_access_token_missing_user_id():
    """When edam_userId is missing from the response, user_id is set to None."""
    fake_http = FakeHttpClient(
        response=SimpleNamespace(
            status_code=200,
            text=(
                "oauth_token=tok-without-uid&"
                "edam_noteStoreUrl=https://notestore.example.com&"
                "edam_expires=1700000000000"
            ),
        )
    )

    oauth = EvernoteOAuth(consumer_key="k", consumer_secret="s", http=fake_http)
    access_token = oauth.get_access_token("rtok", "rsec", "ver")
    assert access_token.token == "tok-without-uid"
    assert access_token.user_id is None
    assert access_token.expires == 1700000000000


def test_get_access_token_non_integer_user_id():
    """When edam_userId is not an integer, user_id is safely set to None."""
    fake_http = FakeHttpClient(
        response=SimpleNamespace(
            status_code=200,
            text=(
                "oauth_token=tok-invalid-uid&"
                "edam_noteStoreUrl=https://notestore.example.com&"
                "edam_userId=not-a-number"
            ),
        )
    )

    oauth = EvernoteOAuth(consumer_key="k", consumer_secret="s", http=fake_http)
    access_token = oauth.get_access_token("rtok", "rsec", "ver")
    assert access_token.token == "tok-invalid-uid"
    assert access_token.user_id is None


def test_get_access_token_with_client_object():
    """Injectable http client implementing .post(...) method works."""
    mock_client = MagicMock()
    mock_client.post.return_value = SimpleNamespace(
        status_code=200,
        text="oauth_token=tok-abc&edam_noteStoreUrl=https://notestore.example.com&edam_userId=10",
    )

    oauth = EvernoteOAuth(
        consumer_key="k",
        consumer_secret="s",
        http=mock_client,
    )

    access_token = oauth.get_access_token("rtok", "rsec", "ver")
    assert access_token.token == "tok-abc"
    assert access_token.user_id == 10
    assert access_token.expires is None
    assert access_token.note_store_url == "https://notestore.example.com"
    mock_client.post.assert_called_once()


def test_error_mapping_http_errors():
    """HTTP 4xx/5xx responses raise EvernoteAuthError."""
    fake_http = FakeHttpClient(
        response=SimpleNamespace(status_code=401, text="Unauthorized: invalid signature")
    )

    oauth = EvernoteOAuth(
        consumer_key="key",
        consumer_secret="secret",
        http=fake_http,
    )

    with pytest.raises(EvernoteAuthError, match=r"HTTP status 401"):
        oauth.get_request_token("https://example.com/cb")


def test_error_mapping_network_errors():
    """Underlying network / client errors raise EvernoteAuthError."""

    def raise_err(url, headers=None, content=None):
        raise ConnectionResetError("Connection lost to server")

    fake_http = FakeHttpClient(handler=raise_err)

    oauth = EvernoteOAuth(
        consumer_key="key",
        consumer_secret="secret",
        http=fake_http,
    )

    with pytest.raises(EvernoteAuthError, match=r"ConnectionResetError"):
        oauth.get_request_token("https://example.com/cb")


def test_error_mapping_malformed_response():
    """Responses missing expected tokens raise EvernoteAuthError."""
    fake_http = FakeHttpClient(
        response=SimpleNamespace(status_code=200, text="not_oauth=hello&some_key=world")
    )

    oauth = EvernoteOAuth(
        consumer_key="key",
        consumer_secret="secret",
        http=fake_http,
    )

    with pytest.raises(EvernoteAuthError, match=r"missing oauth_token"):
        oauth.get_request_token("https://example.com/cb")

    with pytest.raises(EvernoteAuthError, match=r"missing oauth_token"):
        oauth.get_access_token("tok", "sec", "ver")


def test_no_secret_or_token_leaks_in_repr_or_exceptions():
    """Secret consumer keys, request tokens, and access tokens never leak into repr or errors."""
    secret_consumer_secret = "super-confidential-secret-999"
    secret_token = "ultra-secret-user-access-token-000"

    access_token = EvernoteAccessToken(
        token=secret_token,
        note_store_url="https://notestore.example.com",
        user_id=1,
    )

    # Token must not appear in repr or str
    assert secret_token not in repr(access_token)
    assert secret_token not in str(access_token)
    assert "***" in repr(access_token)

    oauth = EvernoteOAuth(
        consumer_key="consumer-key",
        consumer_secret=secret_consumer_secret,
    )

    # Secret must not appear in oauth repr or str
    assert secret_consumer_secret not in repr(oauth)
    assert secret_consumer_secret not in str(oauth)

    # Network failure error message must not contain secret
    failing_http = MagicMock()
    failing_http.post.side_effect = RuntimeError(f"Failed with secret {secret_consumer_secret}")

    oauth._http = failing_http
    with pytest.raises(EvernoteAuthError) as exc_info:
        oauth.get_request_token("https://example.com/cb")

    assert secret_consumer_secret not in str(exc_info.value)
    assert secret_consumer_secret not in repr(exc_info.value)
