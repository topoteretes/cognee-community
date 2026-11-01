"""DigitalOcean data-source connector for Cognee.

Syncs DigitalOcean cloud infrastructure (Droplets, App Platform applications,
managed databases, Kubernetes clusters, projects, and account actions) into Cognee
AI graph and vector memory as structured, searchable knowledge documents.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

try:
    import dlt
except ImportError:
    dlt = None

DOCUMENT_SOURCE_ATTR = "document_source"

logger = logging.getLogger("cognee_connector_digitalocean")

DO_API_BASE = "https://api.digitalocean.com/v2"

_MAX_RETRIES = 3
_PAGE_SIZE = 100


class DigitalOceanConnectorError(RuntimeError):
    """Raised when an unrecoverable DigitalOcean API failure occurs."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DigitalOceanRenderOptions:
    """Configurable rendering and security redaction options for DigitalOcean."""

    include_droplets: bool = True
    include_apps: bool = True
    include_databases: bool = True
    include_kubernetes: bool = True
    include_projects: bool = True
    include_actions: bool = True
    redact_secrets: bool = True


class DigitalOceanClient:
    """HTTP client for interacting with the DigitalOcean REST API v2."""

    def __init__(
        self,
        token: str,
        client: httpx.Client | None = None,
        base_url: str | None = None,
    ):
        if not token or not token.strip():
            raise ValueError(
                "DigitalOcean Personal Access Token is required. Set DIGITALOCEAN_TOKEN."
            )

        self.token = token.strip()
        self.base_url = (base_url or DO_API_BASE).rstrip("/")
        self._client = client or httpx.Client()
        self._owns_client = client is None

    def close(self):
        if self._owns_client:
            self._client.close()

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-digitalocean/0.1.0",
        }

    def request(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute GET request with automated retry and backoff on HTTP 429."""
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}/{endpoint.lstrip('/')}"
        params = params or {}

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._client.get(
                    url,
                    headers=self._get_headers(),
                    params=params,
                    timeout=30.0,
                )

                if resp.status_code == 429:
                    reset_ts = resp.headers.get("Ratelimit-Reset")
                    delay = 2
                    if reset_ts and reset_ts.isdigit():
                        delay = max(1, min(int(reset_ts) - int(time.time()), 60))
                    logger.warning("DigitalOcean rate limit hit, backing off for %ds", delay)
                    time.sleep(delay)
                    continue

                if resp.status_code == 401:
                    raise DigitalOceanConnectorError(
                        "Unauthorized: Invalid or expired DigitalOcean token", 401
                    )
                if resp.status_code == 403:
                    raise DigitalOceanConnectorError(
                        "Forbidden: Token lacks required read scopes", 403
                    )
                if resp.status_code == 404:
                    raise DigitalOceanConnectorError(f"Resource not found: {endpoint}", 404)
                if resp.status_code >= 400:
                    raise DigitalOceanConnectorError(
                        f"DigitalOcean API error ({resp.status_code}): {resp.text}",
                        resp.status_code,
                    )

                return resp.json()

            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                if attempt == _MAX_RETRIES:
                    raise DigitalOceanConnectorError(
                        f"Connection failed after {attempt} attempts: {exc}"
                    ) from exc
                time.sleep(1.0 * attempt)

        raise DigitalOceanConnectorError("Max retries exceeded for DigitalOcean request")

    def paginate(
        self, endpoint: str, key: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Paginate across DigitalOcean collection endpoints using meta/links."""
        url: str | None = endpoint
        page = 1
        query_params = dict(params or {})
        query_params["per_page"] = _PAGE_SIZE

        while url:
            data = self.request(url, params=query_params if page == 1 else None)
            items = data.get(key, [])
            yield from items

            # Follow next link if provided in links.pages
            links = data.get("links", {})
            pages = links.get("pages", {})
            next_url = pages.get("next")

            if next_url:
                url = next_url
                page += 1
            else:
                break


# ─── Document Renderers (Infrastructure to Searchable AI Knowledge) ───────────


def render_droplet(
    droplet: dict[str, Any],
    options: DigitalOceanRenderOptions | None = None,
) -> dict[str, Any]:
    """Convert a DigitalOcean Droplet into a structured knowledge document."""
    d_id = str(droplet.get("id", ""))
    name = droplet.get("name", f"droplet-{d_id}")
    status = droplet.get("status", "unknown")
    region = droplet.get("region", {}).get("slug", "unknown")
    size_slug = droplet.get("size_slug", "")
    vcpus = droplet.get("vcpus", 1)
    memory_mb = droplet.get("memory", 0)
    disk_gb = droplet.get("disk", 0)
    created_at = droplet.get("created_at", "")
    tags = droplet.get("tags", [])

    networks = droplet.get("networks", {}).get("v4", [])
    ip_addrs = [net.get("ip_address") for net in networks if net.get("ip_address")]
    public_ip = ip_addrs[0] if ip_addrs else "N/A"

    image_name = droplet.get("image", {}).get("distribution", "")
    image_desc = droplet.get("image", {}).get("name", "")

    content = f"""# DigitalOcean Droplet: {name}
- Droplet ID: {d_id}
- Name: {name}
- Status: {status}
- Region: {region}
- Size: {size_slug} ({vcpus} vCPU, {memory_mb} MB RAM, {disk_gb} GB SSD)
- Public IP: {public_ip}
- Operating System / Image: {image_name} {image_desc}
- Tags: {", ".join(tags) if tags else "None"}
- Created At: {created_at}
"""
    return {
        "id": f"do:droplet:{d_id}",
        "title": f"Droplet: {name} ({status})",
        "content": content.strip(),
        "url": f"https://cloud.digitalocean.com/droplets/{d_id}",
        "_deleted": False,
        "type": "droplet",
        "resource_id": d_id,
        "status": status,
        "region": region,
    }


def render_app(
    app: dict[str, Any],
    deployments: list[dict[str, Any]] | None = None,
    options: DigitalOceanRenderOptions | None = None,
) -> dict[str, Any]:
    """Convert a DigitalOcean App Platform application and deployments into a document."""
    app_id = str(app.get("id", ""))
    spec = app.get("spec", {})
    name = spec.get("name") or app.get("default_ingress", f"app-{app_id}")
    region = spec.get("region", "unknown")
    live_url = app.get("live_url", "")
    active_deploy = app.get("active_deployment", {})
    deploy_phase = active_deploy.get("phase", "UNKNOWN")
    updated_at = app.get("updated_at", "")

    lines = [
        f"# DigitalOcean App: {name}",
        f"- App ID: {app_id}",
        f"- Name: {name}",
        f"- Region: {region}",
        f"- Live URL: {live_url or 'N/A'}",
        f"- Active Deployment Phase: {deploy_phase}",
        f"- Last Updated: {updated_at}",
    ]

    if deployments:
        lines.append(f"\n## Recent Deployments ({len(deployments)} recorded):")
        for dep in deployments[:5]:
            dep_id = dep.get("id", "")
            phase = dep.get("phase", "UNKNOWN")
            cause = dep.get("cause", "Commit")
            created = dep.get("created_at", "")
            progress = dep.get("progress", {})
            summary = progress.get("summary", "")
            lines.append(
                f"- Deployment {dep_id[:8]} [{phase}]: Cause: {cause} | Time: {created} | {summary}"
            )

    return {
        "id": f"do:app:{app_id}",
        "title": f"App Platform: {name} ({deploy_phase})",
        "content": "\n".join(lines).strip(),
        "url": f"https://cloud.digitalocean.com/apps/{app_id}",
        "_deleted": False,
        "type": "app",
        "resource_id": app_id,
        "region": region,
    }


def render_database(
    database: dict[str, Any],
    options: DigitalOceanRenderOptions | None = None,
) -> dict[str, Any]:
    """Convert a DigitalOcean Managed Database cluster into a document.

    Ensures credentials, passwords, and private URI strings are strictly excluded.
    """
    db_id = str(database.get("id", ""))
    name = database.get("name", f"db-{db_id}")
    engine = database.get("engine", "unknown")
    version = database.get("version", "")
    num_nodes = database.get("num_nodes", 1)
    size = database.get("size", "")
    region = database.get("region", "unknown")
    status = database.get("status", "unknown")
    created_at = database.get("created_at", "")

    content = f"""# DigitalOcean Managed Database: {name}
- Database ID: {db_id}
- Cluster Name: {name}
- Engine: {engine} v{version}
- Nodes: {num_nodes} node(s) ({size})
- Region: {region}
- Status: {status}
- Created At: {created_at}
- Security Note: Connection passwords and credentials safely redacted.
"""
    return {
        "id": f"do:database:{db_id}",
        "title": f"Database: {name} ({engine} v{version})",
        "content": content.strip(),
        "url": f"https://cloud.digitalocean.com/databases/{db_id}",
        "_deleted": False,
        "type": "database",
        "resource_id": db_id,
        "engine": engine,
        "status": status,
    }


def render_kubernetes_cluster(
    cluster: dict[str, Any],
    options: DigitalOceanRenderOptions | None = None,
) -> dict[str, Any]:
    """Convert a DigitalOcean DOKS Kubernetes cluster into a knowledge document."""
    cluster_id = str(cluster.get("id", ""))
    name = cluster.get("name", f"k8s-{cluster_id}")
    region = cluster.get("region", "unknown")
    version = cluster.get("version", "unknown")
    status = cluster.get("status", {}).get("state", "unknown")
    node_pools = cluster.get("node_pools", [])
    total_nodes = sum(p.get("count", 0) for p in node_pools)

    content = f"""# DigitalOcean Kubernetes Cluster: {name}
- Cluster ID: {cluster_id}
- Name: {name}
- Kubernetes Version: {version}
- Status: {status}
- Region: {region}
- Node Pools: {len(node_pools)} pool(s) ({total_nodes} total nodes)
"""
    return {
        "id": f"do:k8s:{cluster_id}",
        "title": f"Kubernetes: {name} ({version})",
        "content": content.strip(),
        "url": f"https://cloud.digitalocean.com/kubernetes/clusters/{cluster_id}",
        "_deleted": False,
        "type": "kubernetes",
        "resource_id": cluster_id,
        "status": status,
    }


def render_project(
    project: dict[str, Any],
    resources: list[dict[str, Any]] | None = None,
    options: DigitalOceanRenderOptions | None = None,
) -> dict[str, Any]:
    """Convert a DigitalOcean Project and its linked resource URNs into a document."""
    proj_id = str(project.get("id", ""))
    name = project.get("name", "Default Project")
    description = project.get("description", "No description provided.")
    purpose = project.get("purpose", "General")
    is_default = project.get("is_default", False)

    lines = [
        f"# DigitalOcean Project: {name}",
        f"- Project ID: {proj_id}",
        f"- Name: {name}",
        f"- Purpose: {purpose}",
        f"- Default Project: {'Yes' if is_default else 'No'}",
        f"- Description: {description}",
    ]

    if resources:
        lines.append(f"\n## Assigned Resources ({len(resources)} items):")
        for res in resources:
            urn = res.get("urn", "")
            lines.append(f"- Resource: {urn}")

    return {
        "id": f"do:project:{proj_id}",
        "title": f"Project: {name}",
        "content": "\n".join(lines).strip(),
        "url": f"https://cloud.digitalocean.com/projects/{proj_id}",
        "_deleted": False,
        "type": "project",
        "resource_id": proj_id,
    }


# ─── Synchronization & Generator ──────────────────────────────────────────────


def sync_digitalocean_records(
    client: DigitalOceanClient,
    project_ids: list[str] | None = None,
    options: DigitalOceanRenderOptions | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield all rendered documents for DigitalOcean cloud resources."""
    options = options or DigitalOceanRenderOptions()

    # 1. Projects
    if options.include_projects:
        try:
            for proj in client.paginate("projects", "projects"):
                p_id = proj.get("id")
                if project_ids and p_id not in project_ids:
                    continue
                # Fetch project resources
                resources = []
                try:
                    res_data = client.request(f"projects/{p_id}/resources")
                    resources = res_data.get("resources", [])
                except Exception:
                    pass
                yield render_project(proj, resources, options=options)
        except Exception as exc:
            logger.error("Failed fetching DigitalOcean projects: %s", exc)

    # 2. Droplets
    if options.include_droplets:
        try:
            for droplet in client.paginate("droplets", "droplets"):
                yield render_droplet(droplet, options=options)
        except Exception as exc:
            logger.error("Failed fetching DigitalOcean droplets: %s", exc)

    # 3. App Platform Apps & Deployments
    if options.include_apps:
        try:
            for app in client.paginate("apps", "apps"):
                app_id = app.get("id")
                deployments = []
                if app_id:
                    try:
                        dep_data = client.request(f"apps/{app_id}/deployments")
                        deployments = dep_data.get("deployments", [])
                    except Exception:
                        pass
                yield render_app(app, deployments, options=options)
        except Exception as exc:
            logger.error("Failed fetching DigitalOcean apps: %s", exc)

    # 4. Managed Databases
    if options.include_databases:
        try:
            for db in client.paginate("databases", "databases"):
                yield render_database(db, options=options)
        except Exception as exc:
            logger.error("Failed fetching DigitalOcean databases: %s", exc)

    # 5. Kubernetes Clusters
    if options.include_kubernetes:
        try:
            for cluster in client.paginate("kubernetes/clusters", "kubernetes_clusters"):
                yield render_kubernetes_cluster(cluster, options=options)
        except Exception as exc:
            logger.error("Failed fetching DigitalOcean Kubernetes clusters: %s", exc)


if dlt is not None:

    @dlt.source(name="digitalocean")
    def digitalocean_source(
        token: str,
        project_ids: list[str] | None = None,
        options: DigitalOceanRenderOptions | None = None,
    ):
        """DLT source to sync DigitalOcean resources into Cognee AI knowledge graph."""
        options = options or DigitalOceanRenderOptions()
        client = DigitalOceanClient(token=token)

        @dlt.resource(name="digitalocean_resources", primary_key="id", write_disposition="merge")
        def digitalocean_resources():
            yield from sync_digitalocean_records(client, project_ids=project_ids, options=options)

        setattr(digitalocean_resources, DOCUMENT_SOURCE_ATTR, "digitalocean")
        return [digitalocean_resources]

else:

    def digitalocean_source(
        token: str,
        project_ids: list[str] | None = None,
        options: DigitalOceanRenderOptions | None = None,
    ):
        """Fallback generator when dlt is not loaded."""
        options = options or DigitalOceanRenderOptions()
        client = DigitalOceanClient(token=token)
        return list(sync_digitalocean_records(client, project_ids=project_ids, options=options))
