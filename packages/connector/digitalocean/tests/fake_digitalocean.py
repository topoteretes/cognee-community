"""In-memory mock DigitalOcean API server for hermetic unit testing."""

from __future__ import annotations

import httpx

from cognee_community_connector_digitalocean.digitalocean import DigitalOceanClient

FAKE_TOKEN = "dop_v1_fake_token_1234567890abcdef"


class FakeDigitalOceanTransport(httpx.BaseTransport):
    """Hermetic mock transport simulating DigitalOcean v2 endpoints."""

    def __init__(self, valid_token: str = FAKE_TOKEN):
        self.valid_token = valid_token

        self.droplets = [
            {
                "id": 3164444,
                "name": "web-prod-01",
                "status": "active",
                "region": {"slug": "nyc3", "name": "New York 3"},
                "size_slug": "s-2vcpu-4gb",
                "vcpus": 2,
                "memory": 4096,
                "disk": 80,
                "created_at": "2026-10-01T10:00:00Z",
                "tags": ["production", "web"],
                "networks": {"v4": [{"ip_address": "159.65.12.34", "type": "public"}]},
                "image": {"distribution": "Ubuntu", "name": "24.04 (LTS) x64"},
            },
            {
                "id": 3164445,
                "name": "worker-queue-01",
                "status": "off",
                "region": {"slug": "fra1", "name": "Frankfurt 1"},
                "size_slug": "s-1vcpu-2gb",
                "vcpus": 1,
                "memory": 2048,
                "disk": 50,
                "created_at": "2026-10-15T14:30:00Z",
                "tags": ["worker"],
                "networks": {"v4": [{"ip_address": "164.90.111.22", "type": "public"}]},
                "image": {"distribution": "Debian", "name": "12 x64"},
            },
        ]

        self.apps = [
            {
                "id": "app_99112233-4455-6677",
                "default_ingress": "api-service",
                "live_url": "https://api-service-prod.ondigitalocean.app",
                "updated_at": "2026-10-25T11:00:00Z",
                "spec": {
                    "name": "api-service",
                    "region": "nyc",
                },
                "active_deployment": {
                    "id": "dep_12345",
                    "phase": "ACTIVE",
                },
            }
        ]

        self.deployments = [
            {
                "id": "dep_12345",
                "phase": "ACTIVE",
                "cause": "Manual trigger from branch main",
                "created_at": "2026-10-25T10:55:00Z",
                "progress": {"summary": "Deployment succeeded and healthcheck passed"},
            }
        ]

        self.databases = [
            {
                "id": "db_55667788",
                "name": "primary-postgres",
                "engine": "pg",
                "version": "16",
                "num_nodes": 2,
                "size": "db-s-2vcpu-4gb",
                "region": "nyc3",
                "status": "online",
                "created_at": "2026-09-01T08:00:00Z",
                # Sensitive fields that must be excluded:
                "connection": {
                    "uri": "postgres://doadmin:SECRET_PASSWORD@db.ondigitalocean.com:25060/defaultdb",
                    "password": "SUPER_SECRET_PASSWORD_123",
                },
            }
        ]

        self.kubernetes_clusters = [
            {
                "id": "k8s_cluster_alpha",
                "name": "doks-prod-cluster",
                "region": "nyc3",
                "version": "1.30.2-do.0",
                "status": {"state": "running"},
                "node_pools": [
                    {
                        "id": "pool_worker",
                        "name": "worker-pool",
                        "count": 3,
                        "size": "s-2vcpu-4gb",
                    }
                ],
            }
        ]

        self.projects = [
            {
                "id": "proj_default_01",
                "name": "Core Production",
                "description": "Primary customer-facing cloud infrastructure",
                "purpose": "Production Application",
                "is_default": True,
            }
        ]

        self.project_resources = [
            {"urn": "do:droplet:3164444"},
            {"urn": "do:dbaas:db_55667788"},
        ]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return httpx.Response(status_code=401, json={"id": "unauthorized", "message": "Auth"})

        token = auth_header.replace("Bearer ", "").strip()
        if token != self.valid_token:
            return httpx.Response(
                status_code=401, json={"id": "unauthorized", "message": "Invalid"}
            )

        path = request.url.path

        if path == "/v2/droplets":
            return httpx.Response(
                status_code=200,
                json={
                    "droplets": self.droplets,
                    "links": {},
                    "meta": {"total": len(self.droplets)},
                },
            )

        if path == "/v2/apps":
            return httpx.Response(
                status_code=200,
                json={"apps": self.apps, "links": {}, "meta": {"total": len(self.apps)}},
            )

        if path.startswith("/v2/apps/") and path.endswith("/deployments"):
            return httpx.Response(status_code=200, json={"deployments": self.deployments})

        if path == "/v2/databases":
            return httpx.Response(
                status_code=200,
                json={"databases": self.databases, "links": {}, "meta": {"total": 1}},
            )

        if path == "/v2/kubernetes/clusters":
            return httpx.Response(
                status_code=200,
                json={"kubernetes_clusters": self.kubernetes_clusters, "links": {}},
            )

        if path == "/v2/projects":
            return httpx.Response(
                status_code=200,
                json={"projects": self.projects, "links": {}},
            )

        if path.startswith("/v2/projects/") and path.endswith("/resources"):
            return httpx.Response(status_code=200, json={"resources": self.project_resources})

        return httpx.Response(status_code=404, json={"id": "not_found", "message": "Not found"})


def create_fake_client(token: str = FAKE_TOKEN) -> DigitalOceanClient:
    """Create a DigitalOceanClient backed by an in-memory FakeDigitalOceanTransport."""
    transport = FakeDigitalOceanTransport(valid_token=FAKE_TOKEN)
    http_client = httpx.Client(transport=transport)
    return DigitalOceanClient(token=token, client=http_client)
