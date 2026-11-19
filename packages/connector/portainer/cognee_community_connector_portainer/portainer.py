"""Portainer connector for Cognee.

Ingests environments, stacks, docker-compose definitions, and container state
from Portainer instances into structured Markdown documents for Cognee memory.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from typing import Any, Iterable

import dlt
import httpx
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PORTAINER_SOURCE_NAME = "portainer"
PORTAINER_TABLE_NAME = "portainer_infrastructure"

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0


class PortainerAPIError(Exception):
    """Base exception for Portainer connector errors."""


class PortainerAuthError(PortainerAPIError):
    """Raised when Portainer rejects credentials."""


class PortainerRateLimitError(PortainerAPIError):
    """Raised when Portainer throttles requests with 429."""


def _redact_token(text: str, token: str | None) -> str:
    """Mask credential strings in error messages to avoid accidental log exposure."""
    if not token or len(token) < 4:
        return text
    return text.replace(token, "[REDACTED_PORTAINER_CREDENTIAL]")


class PortainerConfig(BaseModel):
    """Configuration for Portainer data-source connector."""

    api_url: str = Field(
        ...,
        description="Base URL of Portainer instance (e.g. https://portainer.local:9443)",
    )
    api_key: str | None = Field(
        default=None,
        description="Portainer API Key passed via X-API-Key header",
    )
    jwt_token: str | None = Field(
        default=None,
        description="Pre-authenticated JWT token passed via Authorization Bearer",
    )
    username: str | None = Field(
        default=None,
        description="Username for automatic authentication via /api/auth",
    )
    password: str | None = Field(
        default=None,
        description="Password for automatic authentication via /api/auth",
    )
    endpoint_ids: list[int] | None = Field(
        default=None,
        description="Specific environment/endpoint IDs to ingest (None for all)",
    )
    include_containers: bool = Field(
        default=True,
        description="Whether to ingest container runtime states",
    )
    include_stacks: bool = Field(
        default=True,
        description="Whether to ingest stack topologies and compose files",
    )
    request_timeout: float = Field(
        default=30.0,
        description="HTTP request timeout in seconds",
    )


class PortainerClient:
    """Synchronous HTTP client for interacting with Portainer REST APIs."""

    def __init__(
        self,
        api_url: str,
        api_key: str | None = None,
        jwt_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.jwt_token = jwt_token
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    def _get_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        elif self.jwt_token:
            headers["Authorization"] = f"Bearer {self.jwt_token}"
        return headers

    def authenticate(self) -> None:
        """Authenticate using username and password if no token is preset."""
        if self.api_key or self.jwt_token or not (self.username and self.password):
            return

        url = f"{self.api_url}/api/auth"
        payload = {"Username": self.username, "Password": self.password}
        try:
            response = self._client.post(url, json=payload)
            if response.status_code in (401, 403, 422):
                msg = f"Portainer authentication failed: {response.text}"
                raise PortainerAuthError(_redact_token(msg, self.password))
            response.raise_for_status()
            data = response.json()
            self.jwt_token = data.get("jwt")
        except httpx.HTTPError as exc:
            msg = f"Failed to authenticate against Portainer: {exc}"
            raise PortainerAPIError(_redact_token(msg, self.password)) from exc

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.authenticate()
        url = f"{self.api_url}{path}"
        headers = self._get_headers()
        retries = 0

        while True:
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                )

                if response.status_code in (401, 403):
                    msg = (
                        f"Portainer authentication rejected "
                        f"({response.status_code}): {response.text}"
                    )
                    secret = self.api_key or self.jwt_token or self.password
                    raise PortainerAuthError(_redact_token(msg, secret))

                if response.status_code == 429:
                    retries += 1
                    if retries > _MAX_RETRIES:
                        raise PortainerRateLimitError("Portainer rate limit exceeded after retries")
                    backoff = _BASE_BACKOFF * (2 ** (retries - 1)) + random.uniform(0.1, 0.5)
                    logger.warning("Portainer rate-limited (429). Retrying in %.2fs...", backoff)
                    time.sleep(backoff)
                    continue

                if response.status_code >= 500:
                    retries += 1
                    if retries > _MAX_RETRIES:
                        response.raise_for_status()
                    backoff = _BASE_BACKOFF * (2 ** (retries - 1)) + random.uniform(0.1, 0.5)
                    logger.warning(
                        "Portainer server error (%s). Retrying in %.2fs...",
                        response.status_code,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue

                response.raise_for_status()
                return response.json()

            except httpx.HTTPError as exc:
                if retries >= _MAX_RETRIES:
                    secret = self.api_key or self.jwt_token or self.password
                    raise PortainerAPIError(_redact_token(str(exc), secret)) from exc
                retries += 1
                time.sleep(_BASE_BACKOFF * retries)

    def get_endpoints(self) -> list[dict[str, Any]]:
        """Fetch list of managed Portainer environments/endpoints."""
        result = self._request("GET", "/api/endpoints")
        if isinstance(result, list):
            return result
        return []

    def get_stacks(self) -> list[dict[str, Any]]:
        """Fetch all deployment stacks registered in Portainer."""
        result = self._request("GET", "/api/stacks")
        if isinstance(result, list):
            return result
        return []

    def get_stack_file(self, stack_id: int) -> str:
        """Fetch the raw docker-compose or manifest file for a given stack."""
        try:
            result = self._request("GET", f"/api/stacks/{stack_id}/file")
            return result.get("StackFileContent", "")
        except PortainerAPIError:
            return ""

    def get_containers(self, endpoint_id: int) -> list[dict[str, Any]]:
        """Fetch all Docker containers for a specific endpoint."""
        result = self._request(
            "GET",
            f"/api/endpoints/{endpoint_id}/docker/containers/json",
            params={"all": 1},
        )
        if isinstance(result, list):
            return result
        return []


def _endpoint_type_name(ep_type: int) -> str:
    types = {
        1: "Docker Standalone",
        2: "Docker Swarm Agent",
        3: "Docker Kubernetes",
        4: "Edge Agent Docker",
        5: "Edge Agent Kubernetes",
    }
    return types.get(ep_type, f"Type {ep_type}")


def _build_environments_markdown(endpoints: list[dict[str, Any]]) -> str:
    """Format environments overview into structured Markdown."""
    lines = [
        "# Portainer Managed Environments & Cluster Inventory",
        f"\n**Total Managed Environments**: {len(endpoints)}\n",
        "## Registered Environments",
    ]

    for ep in endpoints:
        ep_id = ep.get("Id", "unknown")
        name = ep.get("Name", f"Endpoint {ep_id}")
        ep_type = _endpoint_type_name(ep.get("Type", 1))
        url = ep.get("URL", "n/a")
        status = "Up / Operational" if ep.get("Status") == 1 else "Down / Unreachable"
        snapshots = ep.get("Snapshots") or [{}]
        docker_ver = snapshots[0].get("DockerVersion", "n/a")

        lines.append(f"### Environment: {name} (ID: {ep_id})")
        lines.append(f"- **Cluster Type**: {ep_type}")
        lines.append(f"- **Status**: {status}")
        lines.append(f"- **URL / Host**: `{url}`")
        lines.append(f"- **Engine Version**: {docker_ver}\n")

    return "\n".join(lines) + "\n"


def _build_stacks_markdown(
    stacks: list[dict[str, Any]],
    stack_files: dict[int, str] | None = None,
) -> str:
    """Format stacks and compose definitions into structured Markdown."""
    stack_files = stack_files or {}
    lines = [
        "# Portainer Stacks & Compose Topology",
        f"\n**Total Active Stacks**: {len(stacks)}\n",
    ]

    for st in stacks:
        st_id = st.get("Id", 0)
        name = st.get("Name", f"Stack {st_id}")
        ep_id = st.get("EndpointId", "unknown")
        st_type = "Compose v2" if st.get("Type") == 2 else "Swarm / K8s"
        status = "Running" if st.get("Status") == 1 else "Stopped"
        created_by = st.get("CreatedBy", "admin")

        lines.append(f"## Stack: {name} (ID: {st_id})")
        lines.append(f"- **Associated Environment ID**: {ep_id}")
        lines.append(f"- **Stack Type**: {st_type}")
        lines.append(f"- **Deployment Status**: {status}")
        lines.append(f"- **Created By**: {created_by}")

        file_content = stack_files.get(st_id)
        if file_content:
            lines.append("\n**Compose / Manifest Definition**:")
            lines.append("```yaml")
            lines.append(file_content.strip())
            lines.append("```\n")
        else:
            lines.append("")

    return "\n".join(lines) + "\n"


def _build_containers_markdown(
    endpoint_name: str,
    endpoint_id: int,
    containers: list[dict[str, Any]],
) -> str:
    """Format container inventory for a specific endpoint into structured Markdown."""
    lines = [
        f"# Container Runtime State: {endpoint_name} (ID: {endpoint_id})",
        f"\n**Total Containers**: {len(containers)}\n",
        "## Container Catalog",
    ]

    for c in containers:
        c_id = c.get("Id", "")[:12]
        names = ", ".join(c.get("Names", [f"/{c_id}"]))
        image = c.get("Image", "unknown")
        state = c.get("State", "unknown")
        status_str = c.get("Status", "unknown")

        ports: list[str] = []
        for p in c.get("Ports", []):
            pub = p.get("PublicPort")
            priv = p.get("PrivatePort")
            p_type = p.get("Type", "tcp")
            if pub:
                ports.append(f"{pub}->{priv}/{p_type}")
            elif priv:
                ports.append(f"{priv}/{p_type}")
        ports_str = ", ".join(ports) if ports else "none"

        mounts: list[str] = []
        for m in c.get("Mounts", []):
            src = m.get("Source", "")
            dst = m.get("Destination", "")
            if src and dst:
                mounts.append(f"`{src}` -> `{dst}`")
        mounts_str = "; ".join(mounts) if mounts else "none"

        lines.append(f"### Container: {names}")
        lines.append(f"- **Container ID**: `{c_id}`")
        lines.append(f"- **Image**: `{image}`")
        lines.append(f"- **State / Status**: `{state}` ({status_str})")
        lines.append(f"- **Port Forwarding**: {ports_str}")
        lines.append(f"- **Volume Mounts**: {mounts_str}\n")

    return "\n".join(lines) + "\n"


def portainer_source(
    config: PortainerConfig | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    jwt_token: str | None = None,
    username: str | None = None,
    password: str | None = None,
    endpoint_ids: list[int] | None = None,
    include_containers: bool = True,
    include_stacks: bool = True,
    client: PortainerClient | None = None,
) -> Iterable[Any]:
    """DLT Source for Portainer infrastructure and container telemetry."""
    if config:
        cfg = config
    else:
        if not api_url and not client:
            raise ValueError("Either 'config', 'api_url', or 'client' must be provided.")
        cfg = PortainerConfig(
            api_url=api_url or (client.api_url if client else ""),
            api_key=api_key,
            jwt_token=jwt_token,
            username=username,
            password=password,
            endpoint_ids=endpoint_ids,
            include_containers=include_containers,
            include_stacks=include_stacks,
        )

    k_client = client or PortainerClient(
        api_url=cfg.api_url,
        api_key=cfg.api_key,
        jwt_token=cfg.jwt_token,
        username=cfg.username,
        password=cfg.password,
        timeout=cfg.request_timeout,
    )

    @dlt.resource(name=PORTAINER_TABLE_NAME, write_disposition="replace")
    def portainer_resource() -> Iterable[dict[str, Any]]:
        endpoints = k_client.get_endpoints()

        # Filter endpoints if specific IDs requested
        if cfg.endpoint_ids:
            endpoints = [ep for ep in endpoints if ep.get("Id") in cfg.endpoint_ids]

        # 1. Yield Environment Inventory Report
        if endpoints:
            env_md = _build_environments_markdown(endpoints)
            env_doc_id = str(
                uuid.uuid5(uuid.NAMESPACE_OID, f"portainer:cluster_inventory:{cfg.api_url}")
            )
            yield {
                "id": env_doc_id,
                "text": env_md,
                "metadata": {
                    "source": PORTAINER_SOURCE_NAME,
                    "report_type": "environments",
                    "total_endpoints": len(endpoints),
                },
            }

        # 2. Yield Stacks Report
        if cfg.include_stacks:
            stacks = k_client.get_stacks()
            if cfg.endpoint_ids:
                stacks = [st for st in stacks if st.get("EndpointId") in cfg.endpoint_ids]

            stack_files: dict[int, str] = {}
            for st in stacks:
                st_id = st.get("Id")
                if st_id:
                    file_content = k_client.get_stack_file(st_id)
                    if file_content:
                        stack_files[st_id] = file_content

            if stacks:
                stacks_md = _build_stacks_markdown(stacks, stack_files)
                stacks_doc_id = str(
                    uuid.uuid5(uuid.NAMESPACE_OID, f"portainer:stacks_topology:{cfg.api_url}")
                )
                yield {
                    "id": stacks_doc_id,
                    "text": stacks_md,
                    "metadata": {
                        "source": PORTAINER_SOURCE_NAME,
                        "report_type": "stacks",
                        "total_stacks": len(stacks),
                    },
                }

        # 3. Yield Containers Report per endpoint
        if cfg.include_containers:
            for ep in endpoints:
                ep_id = ep.get("Id")
                ep_name = ep.get("Name", f"Endpoint-{ep_id}")
                if ep_id is not None:
                    containers = k_client.get_containers(ep_id)
                    if containers:
                        c_md = _build_containers_markdown(ep_name, ep_id, containers)
                        c_doc_id = str(
                            uuid.uuid5(
                                uuid.NAMESPACE_OID,
                                f"portainer:{ep_id}:containers_catalog",
                            )
                        )
                        yield {
                            "id": c_doc_id,
                            "text": c_md,
                            "metadata": {
                                "source": PORTAINER_SOURCE_NAME,
                                "report_type": "containers",
                                "endpoint_id": ep_id,
                                "endpoint_name": ep_name,
                                "total_containers": len(containers),
                            },
                        }

    @dlt.source(name=PORTAINER_SOURCE_NAME)
    def _source() -> Any:
        return portainer_resource

    source_obj = _source()
    # Cognee invariant: Must set DOCUMENT_SOURCE_ATTR to enable cognify()
    setattr(source_obj, DOCUMENT_SOURCE_ATTR, PORTAINER_SOURCE_NAME)
    return source_obj


async def portainer_connector(
    config: PortainerConfig,
    client: PortainerClient | None = None,
) -> list[dict[str, Any]]:
    """Convenience async wrapper returning ingested documents directly for cognee.add()."""
    source = portainer_source(config=config, client=client)
    documents: list[dict[str, Any]] = []

    for resource_func in source.selected_resources.values():
        for item in resource_func():
            documents.append(item)

    return documents
