"""Hermetic mock Traefik client and API simulator for 100% offline unit tests."""

from __future__ import annotations

from typing import Any

from cognee_community_connector_traefik.traefik import (
    TraefikAPIError,
    TraefikAuthError,
    TraefikClient,
)


class FakeTraefik(TraefikClient):
    """In-memory Traefik client mimicking live Traefik API responses."""

    def __init__(self, api_url: str = "http://traefik.mock:8080"):
        super().__init__(api_url=api_url, session=None)
        self.http_routers: dict[str, dict[str, Any]] = {}
        self.tcp_routers: dict[str, dict[str, Any]] = {}
        self.udp_routers: dict[str, dict[str, Any]] = {}
        self.http_services: dict[str, dict[str, Any]] = {}
        self.http_middlewares: dict[str, dict[str, Any]] = {}
        self.entrypoints: list[dict[str, Any]] = [
            {"name": "web", "address": ":80"},
            {"name": "websecure", "address": ":443"},
        ]
        self.overview_data: dict[str, Any] = {
            "http": {
                "routers": {"total": 0, "errors": 0, "warnings": 0},
                "services": {"total": 0, "errors": 0, "warnings": 0},
                "middlewares": {"total": 0, "errors": 0, "warnings": 0},
            }
        }
        self.fail_with_auth = False
        self.fail_with_500 = False
        self.fail_with_network = False

    def add_http_router(
        self,
        name: str,
        rule: str,
        service: str,
        entrypoints: list[str] | None = None,
        middlewares: list[str] | None = None,
        status: str = "enabled",
        priority: int | None = None,
        tls: dict[str, Any] | None = None,
    ) -> None:
        self.http_routers[name] = {
            "name": name,
            "rule": rule,
            "service": service,
            "entryPoints": entrypoints or ["websecure"],
            "middlewares": middlewares or [],
            "status": status,
            "priority": priority,
            "tls": tls,
            "provider": "docker",
        }
        self.overview_data["http"]["routers"]["total"] = len(self.http_routers)

    def add_tcp_router(
        self,
        name: str,
        rule: str,
        service: str,
        entrypoints: list[str] | None = None,
        status: str = "enabled",
    ) -> None:
        self.tcp_routers[name] = {
            "name": name,
            "rule": rule,
            "service": service,
            "entryPoints": entrypoints or ["tcp-entry"],
            "status": status,
        }

    def add_http_service(
        self,
        name: str,
        servers: list[dict[str, Any]] | None = None,
        service_type: str = "loadbalancer",
        status: str = "enabled",
        health_check: dict[str, Any] | None = None,
    ) -> None:
        lb_config: dict[str, Any] = {
            "servers": servers or [{"url": "http://10.0.0.1:8080", "weight": 1}]
        }
        if health_check:
            lb_config["healthCheck"] = health_check

        self.http_services[name] = {
            "name": name,
            "type": service_type,
            "status": status,
            "provider": "docker",
            "loadBalancer": lb_config,
        }
        self.overview_data["http"]["services"]["total"] = len(self.http_services)

    def add_http_middleware(
        self,
        name: str,
        middleware_type: str,
        config: dict[str, Any] | None = None,
        status: str = "enabled",
    ) -> None:
        m_data: dict[str, Any] = {
            "name": name,
            "type": middleware_type,
            "status": status,
            "provider": "docker",
        }
        if config:
            m_data[middleware_type] = config

        self.http_middlewares[name] = m_data
        self.overview_data["http"]["middlewares"]["total"] = len(self.http_middlewares)

    def remove_http_router(self, name: str) -> None:
        self.http_routers.pop(name, None)
        self.overview_data["http"]["routers"]["total"] = len(self.http_routers)

    def remove_http_service(self, name: str) -> None:
        self.http_services.pop(name, None)
        self.overview_data["http"]["services"]["total"] = len(self.http_services)

    def remove_http_middleware(self, name: str) -> None:
        self.http_middlewares.pop(name, None)
        self.overview_data["http"]["middlewares"]["total"] = len(self.http_middlewares)

    def get_http_routers(self) -> list[dict[str, Any]]:
        self._check_failures()
        return list(self.http_routers.values())

    def get_tcp_routers(self) -> list[dict[str, Any]]:
        self._check_failures()
        return list(self.tcp_routers.values())

    def get_udp_routers(self) -> list[dict[str, Any]]:
        self._check_failures()
        return list(self.udp_routers.values())

    def get_http_services(self) -> list[dict[str, Any]]:
        self._check_failures()
        return list(self.http_services.values())

    def get_http_middlewares(self) -> list[dict[str, Any]]:
        self._check_failures()
        return list(self.http_middlewares.values())

    def get_entrypoints(self) -> list[dict[str, Any]]:
        self._check_failures()
        return list(self.entrypoints)

    def get_overview(self) -> dict[str, Any]:
        self._check_failures()
        return dict(self.overview_data)

    def _check_failures(self) -> None:
        if self.check_active is not None:
            self.check_active()
        if self.fail_with_auth:
            raise TraefikAuthError("Authentication failed for Traefik API (401)")
        if self.fail_with_500:
            raise TraefikAPIError("Traefik 500 Internal Server Error")
        if self.fail_with_network:
            raise TraefikAPIError("Connection refused")
