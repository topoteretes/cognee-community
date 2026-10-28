"""Unit tests for Traefik Markdown document rendering."""

from cognee_community_connector_traefik.traefik import (
    render_http_middleware,
    render_http_router,
    render_http_service,
    render_overview,
    render_tcp_router,
)


def test_render_http_router():
    router = {
        "name": "public-api@docker",
        "rule": "Host(`api.domain.com`) && PathPrefix(`/v1`)",
        "service": "api-svc@docker",
        "entryPoints": ["web", "websecure"],
        "middlewares": ["ratelimit@docker", "auth@docker"],
        "priority": 120,
        "tls": {"certResolver": "letsencrypt"},
        "provider": "docker",
        "status": "enabled",
    }
    doc = render_http_router(router, "http://localhost:8080")

    assert doc["id"] == "traefik:http:router:public-api@docker"
    assert doc["title"] == "Traefik HTTP Router: public-api@docker"
    assert doc["_deleted"] is False
    assert "Host(`api.domain.com`) && PathPrefix(`/v1`)" in doc["content"]
    assert "api-svc@docker" in doc["content"]
    assert "ratelimit@docker, auth@docker" in doc["content"]
    assert "**Priority:** 120" in doc["content"]
    assert "**TLS Enabled:** Yes" in doc["content"]


def test_render_http_router_public_without_middleware():
    router = {
        "name": "health-check@docker",
        "rule": "Path(`/ping`)",
        "service": "ping-svc@docker",
    }
    doc = render_http_router(router, "http://localhost:8080")

    assert "Applied Middlewares:** *(none - public route)*" in doc["content"]
    assert "**TLS Enabled:** No (Plain HTTP)" in doc["content"]


def test_render_tcp_router():
    router = {
        "name": "db-proxy@file",
        "rule": "HostSNI(`db.internal`)",
        "service": "postgres-svc@file",
        "entryPoints": ["postgres-port"],
        "status": "enabled",
    }
    doc = render_tcp_router(router, "http://localhost:8080")

    assert doc["id"] == "traefik:tcp:router:db-proxy@file"
    assert "HostSNI(`db.internal`)" in doc["content"]
    assert "postgres-svc@file" in doc["content"]


def test_render_http_service():
    service = {
        "name": "backend-api@docker",
        "type": "loadbalancer",
        "status": "enabled",
        "provider": "docker",
        "loadBalancer": {
            "servers": [
                {"url": "http://10.0.0.1:8080", "weight": 2},
                {"url": "http://10.0.0.2:8080", "weight": 1},
            ],
            "healthCheck": {
                "path": "/health",
                "interval": "5s",
                "timeout": "1s",
            },
        },
    }
    doc = render_http_service(service, "http://localhost:8080")

    assert doc["id"] == "traefik:http:service:backend-api@docker"
    assert "http://10.0.0.1:8080" in doc["content"]
    assert "weight: 2" in doc["content"]
    assert "Path `/health`" in doc["content"]


def test_render_middlewares_various_types():
    # RateLimit
    rl = {
        "name": "my-ratelimit@docker",
        "type": "rateLimit",
        "rateLimit": {"average": 200, "burst": 100, "period": "1s"},
    }
    doc_rl = render_http_middleware(rl, "http://localhost:8080")
    assert "Average: 200" in doc_rl["content"]

    # BasicAuth
    ba = {
        "name": "admin-auth@docker",
        "type": "basicAuth",
        "basicAuth": {"realm": "AdminArea", "users": ["admin:$apr1$xyz", "guest:$apr1$abc"]},
    }
    doc_ba = render_http_middleware(ba, "http://localhost:8080")
    assert "**Auth Realm:** AdminArea" in doc_ba["content"]
    assert "Authorized Users Count: 2" in doc_ba["content"]

    # ForwardAuth
    fa = {
        "name": "oauth-forward@docker",
        "type": "forwardAuth",
        "forwardAuth": {
            "address": "http://keycloak.local/auth",
            "trustForwardHeader": True,
        },
    }
    doc_fa = render_http_middleware(fa, "http://localhost:8080")
    assert "http://keycloak.local/auth" in doc_fa["content"]
    assert "**Trust Forward Header:** True" in doc_fa["content"]

    # IPAllowList
    ip = {
        "name": "vpc-only@docker",
        "type": "ipAllowList",
        "ipAllowList": {"sourceRange": ["10.0.0.0/16", "172.16.0.0/12"]},
    }
    doc_ip = render_http_middleware(ip, "http://localhost:8080")
    assert "10.0.0.0/16" in doc_ip["content"]


def test_render_overview():
    overview = {
        "http": {
            "routers": {"total": 12, "errors": 0, "warnings": 1},
            "services": {"total": 6, "errors": 0, "warnings": 0},
            "middlewares": {"total": 8, "errors": 0, "warnings": 0},
        }
    }
    entrypoints = [
        {"name": "web", "address": ":80"},
        {"name": "websecure", "address": ":443"},
    ]
    doc = render_overview(overview, entrypoints, "http://localhost:8080")

    assert doc["id"] == "traefik:cluster:overview"
    assert "12 total (Errors: 0, Warnings: 1)" in doc["content"]
    assert "Listening on `:443`" in doc["content"]
