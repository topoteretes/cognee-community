"""Pytest fixtures for Traefik connector tests."""

import pytest
from fake_traefik import FakeTraefik


@pytest.fixture
def fake_traefik():
    """Create a populated FakeTraefik cluster."""
    fake = FakeTraefik()

    # Add standard middlewares
    fake.add_http_middleware(
        "rate-limit-100@docker",
        "rateLimit",
        {"average": 100, "burst": 50, "period": "1s"},
    )
    fake.add_http_middleware(
        "auth-forward@docker",
        "forwardAuth",
        {"address": "http://auth-service:9000/verify", "trustForwardHeader": True},
    )
    fake.add_http_middleware(
        "ip-allow-office@docker",
        "ipAllowList",
        {"sourceRange": ["192.168.1.0/24", "10.0.0.0/8"]},
    )

    # Add services
    fake.add_http_service(
        "api-service@docker",
        servers=[
            {"url": "http://10.0.1.10:8000", "weight": 1},
            {"url": "http://10.0.1.11:8000", "weight": 1},
        ],
        health_check={"path": "/healthz", "interval": "10s", "timeout": "2s"},
    )
    fake.add_http_service(
        "dashboard-service@docker",
        servers=[{"url": "http://10.0.1.20:3000", "weight": 1}],
    )

    # Add routers
    fake.add_http_router(
        "api-router@docker",
        rule="Host(`api.example.com`) && PathPrefix(`/v1`)",
        service="api-service@docker",
        entrypoints=["websecure"],
        middlewares=["rate-limit-100@docker", "auth-forward@docker"],
        priority=100,
        tls={"certResolver": "letsencrypt"},
    )
    fake.add_http_router(
        "dashboard-router@docker",
        rule="Host(`admin.example.com`)",
        service="dashboard-service@docker",
        entrypoints=["websecure"],
        middlewares=["ip-allow-office@docker"],
        priority=50,
    )

    return fake
