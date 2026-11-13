"""Traefik connector for cognee: a dlt resource that syncs reverse proxy routing into memory.

It ingests Traefik HTTP/TCP routers, middlewares, backend services, and cluster
topology into structured markdown documents, enabling Cognee AI agents to reason about
live traffic routing, security middlewares, and infrastructure health.

Usage with ``cognee.remember``::

    await cognee.remember(
        traefik_source(api_url="http://localhost:8080"),
        dataset_name="traefik",
        primary_key="id",
        write_disposition="merge",
    )
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urljoin

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("traefik_connector")

TRAEFIK_TABLE_NAME = "traefik_documents"
TRAEFIK_SOURCE_NAME = "traefik"
DEFAULT_API_URL = "http://localhost:8080"
DEFAULT_TIMEOUT = (10, 30)
DEFAULT_RETRIES = 3
_TRANSIENT_STATUSES = (500, 502, 503, 504)
KINDS = ("routers", "services", "middlewares", "overview")


class TraefikSourceError(RuntimeError):
    """Base class for errors raised by the Traefik connector."""


class TraefikAPIError(TraefikSourceError):
    """Traefik API returned an error response or could not be reached."""


class TraefikAuthError(TraefikSourceError):
    """Traefik rejected authentication (HTTP 401 or 403)."""


class TraefikNotFoundError(TraefikAPIError):
    """The requested Traefik resource was not found (HTTP 404)."""


def _hash(value: Any) -> str:
    """Compute a deterministic short SHA-256 hash for fingerprinting."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _text(value: Any) -> str:
    """Return non-empty stripped string or empty string."""
    if value is None:
        return ""
    return str(value).strip()


class TraefikClient:
    """HTTP client communicating with Traefik's API and Dashboard endpoints."""

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        session: requests.Session | Any | None = None,
        timeout: tuple[int, int] = DEFAULT_TIMEOUT,
        check_active: Callable[[], None] | None = None,
    ):
        self.api_url = (api_url or DEFAULT_API_URL).rstrip("/")
        self.token = token or os.environ.get("TRAEFIK_API_TOKEN")
        self.username = username or os.environ.get("TRAEFIK_USERNAME")
        self.password = password or os.environ.get("TRAEFIK_PASSWORD")
        self.timeout = timeout
        self.check_active = check_active
        self._external_session = session is not None
        self.session = session if session is not None else requests.Session()

    def close(self) -> None:
        """Close session if owned."""
        if not self._external_session and hasattr(self.session, "close"):
            self.session.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.check_active is not None:
            self.check_active()

        url = urljoin(self.api_url + "/", path.lstrip("/"))
        headers = kwargs.pop("headers", {})

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        auth = kwargs.pop("auth", None)
        if not self.token and self.username and self.password:
            auth = (self.username, self.password)

        last_exc: Exception | None = None
        for attempt in range(DEFAULT_RETRIES):
            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    auth=auth,
                    timeout=self.timeout,
                    **kwargs,
                )
                if resp.status_code in (401, 403):
                    raise TraefikAuthError(
                        f"Authentication failed for Traefik API ({resp.status_code})"
                    )
                if resp.status_code == 404:
                    raise TraefikNotFoundError(f"Endpoint not found: {path}")
                if resp.status_code in _TRANSIENT_STATUSES and attempt < DEFAULT_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (TraefikAuthError, TraefikNotFoundError):
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < DEFAULT_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue

        raise TraefikAPIError(f"Failed to query Traefik endpoint {path}: {last_exc}") from last_exc

    def get_http_routers(self) -> list[dict[str, Any]]:
        """Fetch all HTTP routers."""
        data = self._request("GET", "/api/http/routers")
        return data if isinstance(data, list) else []

    def get_tcp_routers(self) -> list[dict[str, Any]]:
        """Fetch all TCP routers."""
        try:
            data = self._request("GET", "/api/tcp/routers")
            return data if isinstance(data, list) else []
        except TraefikNotFoundError:
            return []

    def get_udp_routers(self) -> list[dict[str, Any]]:
        """Fetch all UDP routers."""
        try:
            data = self._request("GET", "/api/udp/routers")
            return data if isinstance(data, list) else []
        except TraefikNotFoundError:
            return []

    def get_http_services(self) -> list[dict[str, Any]]:
        """Fetch all HTTP services."""
        data = self._request("GET", "/api/http/services")
        return data if isinstance(data, list) else []

    def get_http_middlewares(self) -> list[dict[str, Any]]:
        """Fetch all HTTP middlewares."""
        data = self._request("GET", "/api/http/middlewares")
        return data if isinstance(data, list) else []

    def get_entrypoints(self) -> list[dict[str, Any]]:
        """Fetch cluster entrypoints."""
        try:
            data = self._request("GET", "/api/entrypoints")
            return data if isinstance(data, list) else []
        except TraefikNotFoundError:
            return []

    def get_overview(self) -> dict[str, Any]:
        """Fetch cluster overview statistics."""
        try:
            data = self._request("GET", "/api/overview")
            return data if isinstance(data, dict) else {}
        except TraefikNotFoundError:
            return {}


# ---------------------------------------------------------------------------
# Document Renderers (Markdown documents for LLM and Cognee Graph reasoning)
# ---------------------------------------------------------------------------


def render_http_router(router: dict[str, Any], api_url: str) -> dict[str, Any]:
    """Render an HTTP router as a structured Markdown document."""
    name = _text(router.get("name")) or "unnamed-router"
    status = _text(router.get("status")) or "enabled"
    rule = _text(router.get("rule"))
    service = _text(router.get("service"))
    entrypoints = router.get("entryPoints") or []
    middlewares = router.get("middlewares") or []
    priority = router.get("priority")
    tls = router.get("tls")
    provider = _text(router.get("provider"))

    lines = [
        f"# Traefik HTTP Router: {name}",
        f"**Status:** {status}",
        f"**Routing Rule:** `{rule}`" if rule else "**Routing Rule:** *(none)*",
        f"**Attached Service:** {service}" if service else "**Attached Service:** *(none)*",
        f"**EntryPoints:** {', '.join(entrypoints)}"
        if entrypoints
        else "**EntryPoints:** *(default)*",
    ]

    if middlewares:
        lines.append(f"**Applied Middlewares:** {', '.join(middlewares)}")
    else:
        lines.append("**Applied Middlewares:** *(none - public route)*")

    if priority is not None:
        lines.append(f"**Priority:** {priority}")

    if tls:
        cert_resolver = tls.get("certResolver") if isinstance(tls, dict) else None
        lines.append(f"**TLS Enabled:** Yes (Resolver: {cert_resolver or 'custom/default'})")
    else:
        lines.append("**TLS Enabled:** No (Plain HTTP)")

    if provider:
        lines.append(f"**Configuration Provider:** {provider}")

    return {
        "id": f"traefik:http:router:{name}",
        "title": f"Traefik HTTP Router: {name}",
        "content": "\n".join(lines),
        "url": f"{api_url}/dashboard/#/http/routers/{name}",
        "_deleted": False,
    }


def render_tcp_router(router: dict[str, Any], api_url: str) -> dict[str, Any]:
    """Render a TCP router as a structured Markdown document."""
    name = _text(router.get("name")) or "unnamed-tcp-router"
    status = _text(router.get("status")) or "enabled"
    rule = _text(router.get("rule"))
    service = _text(router.get("service"))
    entrypoints = router.get("entryPoints") or []

    lines = [
        f"# Traefik TCP Router: {name}",
        f"**Status:** {status}",
        f"**SNI / Routing Rule:** `{rule}`" if rule else "**Routing Rule:** *(catch-all)*",
        f"**Backend Service:** {service}" if service else "**Backend Service:** *(none)*",
        f"**EntryPoints:** {', '.join(entrypoints)}"
        if entrypoints
        else "**EntryPoints:** *(default)*",
    ]

    return {
        "id": f"traefik:tcp:router:{name}",
        "title": f"Traefik TCP Router: {name}",
        "content": "\n".join(lines),
        "url": f"{api_url}/dashboard/#/tcp/routers/{name}",
        "_deleted": False,
    }


def render_http_service(service: dict[str, Any], api_url: str) -> dict[str, Any]:
    """Render an HTTP service as a structured Markdown document."""
    name = _text(service.get("name")) or "unnamed-service"
    service_type = _text(service.get("type")) or "loadbalancer"
    status = _text(service.get("status")) or "enabled"
    provider = _text(service.get("provider"))

    lines = [
        f"# Traefik HTTP Service: {name}",
        f"**Status:** {status}",
        f"**Service Type:** {service_type}",
    ]

    if provider:
        lines.append(f"**Provider:** {provider}")

    # Inspect load balancer servers
    lb = service.get("loadBalancer") or {}
    servers = lb.get("servers") or []
    if servers:
        lines.append("\n### Backend Server Targets:")
        for idx, s in enumerate(servers, start=1):
            s_url = s.get("url") or "unknown"
            s_weight = s.get("weight")
            weight_str = f" (weight: {s_weight})" if s_weight is not None else ""
            lines.append(f"- Server {idx}: `{s_url}`{weight_str}")
    else:
        lines.append("\n**Backend Servers:** *(none configured)*")

    # Health check info
    health = lb.get("healthCheck")
    if health and isinstance(health, dict):
        lines.append(
            f"**Health Check:** Path `{health.get('path', '/')}`, "
            f"Interval `{health.get('interval', 'unknown')}`, "
            f"Timeout `{health.get('timeout', 'unknown')}`"
        )

    return {
        "id": f"traefik:http:service:{name}",
        "title": f"Traefik HTTP Service: {name}",
        "content": "\n".join(lines),
        "url": f"{api_url}/dashboard/#/http/services/{name}",
        "_deleted": False,
    }


def render_middleware(middleware: dict[str, Any], api_url: str) -> dict[str, Any]:
    """Render a Traefik middleware with its type and configuration parameters."""
    name = _text(middleware.get("name")) or "unnamed-middleware"
    m_type = _text(middleware.get("type")) or "custom"
    status = _text(middleware.get("status")) or "enabled"
    provider = _text(middleware.get("provider"))

    lines = [
        f"# Traefik Middleware: {name}",
        f"**Type:** {m_type}",
        f"**Status:** {status}",
    ]

    if provider:
        lines.append(f"**Provider:** {provider}")

    # Specific middleware detail extraction
    cfg = middleware.get(m_type) or {}
    if m_type == "rateLimit" and isinstance(cfg, dict):
        lines.append(
            f"**Rate Limit Config:** Average: {cfg.get('average')}, "
            f"Burst: {cfg.get('burst')}, Period: {cfg.get('period', '1s')}"
        )
    elif m_type in ("basicAuth", "digestAuth") and isinstance(cfg, dict):
        users = cfg.get("users") or []
        lines.append(
            f"**Auth Realm:** {cfg.get('realm', 'Traefik')}, Authorized Users Count: {len(users)}"
        )
    elif m_type == "forwardAuth" and isinstance(cfg, dict):
        lines.append(f"**Forward Auth Address:** `{cfg.get('address')}`")
        if cfg.get("trustForwardHeader"):
            lines.append("**Trust Forward Header:** True")
    elif m_type == "ipAllowList" and isinstance(cfg, dict):
        sources = cfg.get("sourceRange") or []
        lines.append(f"**Allowed CIDR / IP Ranges:** {', '.join(sources)}")
    elif m_type == "headers" and isinstance(cfg, dict):
        custom_req = cfg.get("customRequestHeaders") or {}
        custom_res = cfg.get("customResponseHeaders") or {}
        if custom_req:
            lines.append(f"**Custom Request Headers:** {list(custom_req.keys())}")
        if custom_res:
            lines.append(f"**Custom Response Headers:** {list(custom_res.keys())}")
        if cfg.get("sslRedirect"):
            lines.append("**SSL Redirect:** Enforced")

    return {
        "id": f"traefik:http:middleware:{name}",
        "title": f"Traefik Middleware: {name} ({m_type})",
        "content": "\n".join(lines),
        "url": f"{api_url}/dashboard/#/http/middlewares/{name}",
        "_deleted": False,
    }


render_http_middleware = render_middleware


def render_overview(
    overview: dict[str, Any], entrypoints: list[dict[str, Any]], api_url: str
) -> dict[str, Any]:
    """Render cluster topology overview and entrypoints summary."""
    lines = [
        "# Traefik Cluster Routing Topology Overview",
        f"**Traefik API Endpoint:** {api_url}",
    ]

    if entrypoints:
        lines.append("\n### Configured Cluster EntryPoints:")
        for ep in entrypoints:
            ep_name = ep.get("name") or "unnamed"
            ep_addr = ep.get("address") or "unknown"
            lines.append(f"- **{ep_name}**: Listening on `{ep_addr}`")

    http_stats = overview.get("http") or {}
    if http_stats and isinstance(http_stats, dict):
        lines.append("\n### HTTP Resource Counts:")
        for res_type in ("routers", "services", "middlewares"):
            stat = http_stats.get(res_type) or {}
            total = stat.get("total", 0)
            errors = stat.get("errors", 0)
            warnings = stat.get("warnings", 0)
            lines.append(
                f"- **{res_type.capitalize()}**: {total} total "
                f"(Errors: {errors}, Warnings: {warnings})"
            )

    return {
        "id": "traefik:cluster:overview",
        "title": "Traefik Cluster Routing Topology Overview",
        "content": "\n".join(lines),
        "url": f"{api_url}/dashboard/",
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Sync & Walk Pipeline
# ---------------------------------------------------------------------------


class _Walker:
    """Manages incremental extraction, fingerprint comparison, and tombstoning."""

    def __init__(
        self,
        client: TraefikClient,
        state: dict[str, Any],
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        check_active: Callable[[], None] | None = None,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        self.include = include
        self.check_active = check_active

        for key in ("scanned", "emitted", "skipped", "deleted", "failed"):
            self.stats.setdefault(key, 0)

        self.fingerprints: dict[str, str] = self.state.setdefault("fingerprints", {})
        self.seen: set[str] = set()

    def _check_record(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if self.check_active is not None:
            self.check_active()
        row_id = row["id"]
        self.seen.add(row_id)
        self.stats["scanned"] += 1

        fingerprint = _hash(row)
        if self.fingerprints.get(row_id) == fingerprint:
            self.stats["skipped"] += 1
            return None

        self.fingerprints[row_id] = fingerprint
        self.stats["emitted"] += 1
        return row

    def rows(self) -> Iterator[dict[str, Any]]:
        if self.check_active is not None:
            self.check_active()

        # 1. Ingest Overview / Topology
        if "overview" in self.include:
            overview_data = self.client.get_overview()
            entrypoints = self.client.get_entrypoints()
            overview_row = render_overview(overview_data, entrypoints, self.client.api_url)
            item = self._check_record(overview_row)
            if item is not None:
                yield item

        # 2. Ingest HTTP Routers
        if "routers" in self.include:
            for router in self.client.get_http_routers():
                row = render_http_router(router, self.client.api_url)
                item = self._check_record(row)
                if item is not None:
                    yield item

            # TCP Routers
            for tcp_router in self.client.get_tcp_routers():
                row = render_tcp_router(tcp_router, self.client.api_url)
                item = self._check_record(row)
                if item is not None:
                    yield item

        # 3. Ingest Middlewares
        if "middlewares" in self.include:
            for middleware in self.client.get_http_middlewares():
                row = render_middleware(middleware, self.client.api_url)
                item = self._check_record(row)
                if item is not None:
                    yield item

        # 4. Ingest HTTP Services
        if "services" in self.include:
            for service in self.client.get_http_services():
                row = render_http_service(service, self.client.api_url)
                item = self._check_record(row)
                if item is not None:
                    yield item

        # 5. Tombstone forgotten / deleted items
        missing_ids = set(self.fingerprints.keys()) - self.seen
        for gone_id in sorted(missing_ids):
            self.fingerprints.pop(gone_id, None)
            self.stats["deleted"] += 1
            yield {"id": gone_id, "_deleted": True}


def _iter_rows(
    client: TraefikClient,
    state: dict[str, Any],
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed Traefik rows; driven with a plain dict as state in tests."""
    walker = _Walker(
        client,
        state,
        stats,
        include=include,
        check_active=check_active,
    )
    yield from walker.rows()


# ---------------------------------------------------------------------------
# dlt Source / Resource Factory
# ---------------------------------------------------------------------------


def _check_resource_name(name: Any) -> None:
    """Validate resource name conforms to dlt and database table naming conventions."""
    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > 64:
        raise ValueError("resource_name must be at most 64 characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")


def traefik_source(
    api_url: str = DEFAULT_API_URL,
    *,
    token: str | None = None,
    username: str | None = None,
    password: str | None = None,
    resource_name: str = TRAEFIK_TABLE_NAME,
    include: tuple[str, ...] = KINDS,
    client: TraefikClient | None = None,
    check_active: Callable[[], None] | None = None,
) -> Any:
    """Return a dlt resource yielding Traefik reverse proxy routing documents for ``remember``.

    Args:
        api_url: Traefik API URL (defaults to "http://localhost:8080" or TRAEFIK_API_URL).
        token: Optional Bearer token or TRAEFIK_API_TOKEN.
        username: Optional HTTP Basic Auth username or TRAEFIK_USERNAME.
        password: Optional HTTP Basic Auth password or TRAEFIK_PASSWORD.
        resource_name: dlt resource/table name (defaults to "traefik_documents").
        include: Entities to sync ("routers", "services", "middlewares", "overview").
        client: Pre-built TraefikClient, mainly for testing.
        check_active: Optional cancellation checkpoint callback.

    Returns:
        A dlt resource with primary_key="id", write_disposition="merge", and _deleted hard-delete.
    """
    _check_resource_name(resource_name)

    stats: dict[str, int] = {}
    resolved_api_url = api_url or os.environ.get("TRAEFIK_API_URL", DEFAULT_API_URL)

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def traefik_documents() -> Iterator[dict[str, Any]]:
        stats.clear()
        traefik_client = (
            client
            if client is not None
            else TraefikClient(
                api_url=resolved_api_url,
                token=token,
                username=username,
                password=password,
                check_active=check_active,
            )
        )
        try:
            yield from _iter_rows(
                traefik_client,
                dlt.current.resource_state(),
                stats,
                include=include,
                check_active=check_active,
            )
        finally:
            if client is None:
                traefik_client.close()

    resource = traefik_documents()
    # Route into Cognee document pipeline
    setattr(resource, DOCUMENT_SOURCE_ATTR, TRAEFIK_SOURCE_NAME)
    # Scope pipeline state
    if hasattr(dlt_utils, "PIPELINE_SCOPE_ATTR"):
        setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
