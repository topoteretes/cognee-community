"""Unit tests for the TraefikClient HTTP communication and error handling."""

from unittest.mock import MagicMock

import pytest

from cognee_community_connector_traefik.traefik import (
    TraefikAuthError,
    TraefikClient,
)


def test_client_defaults_and_normalization():
    client = TraefikClient("http://my-traefik:8080/")
    assert client.api_url == "http://my-traefik:8080"
    client.close()


def test_client_bearer_auth_headers():
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [{"name": "router1"}]
    session.request.return_value = resp

    client = TraefikClient("http://my-traefik:8080", token="secret-token-123", session=session)
    routers = client.get_http_routers()

    assert len(routers) == 1
    call_kwargs = session.request.call_args[1]
    assert call_kwargs["headers"]["Authorization"] == "Bearer secret-token-123"


def test_client_basic_auth():
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = []
    session.request.return_value = resp

    client = TraefikClient(
        "http://my-traefik:8080",
        username="admin",
        password="password123",
        session=session,
    )
    client.get_http_services()

    call_kwargs = session.request.call_args[1]
    assert call_kwargs["auth"] == ("admin", "password123")


def test_client_auth_error_on_401():
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 401
    session.request.return_value = resp

    client = TraefikClient("http://my-traefik:8080", session=session)
    with pytest.raises(TraefikAuthError):
        client.get_http_routers()


def test_client_transient_retry(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    session = MagicMock()
    err_resp = MagicMock()
    err_resp.status_code = 503

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = [{"name": "recovered"}]

    session.request.side_effect = [err_resp, ok_resp]

    client = TraefikClient("http://my-traefik:8080", session=session)
    routers = client.get_http_routers()

    assert len(routers) == 1
    assert session.request.call_count == 2
