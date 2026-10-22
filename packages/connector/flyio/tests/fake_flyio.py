"""In-memory stand-in for the Fly.io Machines REST API v1."""

from typing import Any
from cognee_community_connector_flyio.flyio import (
    FlyioNotFoundError,
    FlyioRateLimitedError,
    FlyioAuthError,
    FlyioAccessError,
)


class FakeFlyio:
    def __init__(self, api_key: str = "FlyV1 mock12345678"):
        self.api_key = api_key
        self.dashboard_url = "https://fly.io/dashboard"
        self.org_slug = None
        self.apps: dict[str, dict[str, Any]] = {}
        self.machines: dict[str, list[dict[str, Any]]] = {}
        self.volumes: dict[str, list[dict[str, Any]]] = {}

        self.calls: list[tuple[str, str]] = []
        self.fail_paths: dict[str, Exception] = {}

    def add_app(
        self,
        name: str,
        status: str = "deployed",
        org: str = "personal",
        version: int = 1,
    ) -> dict[str, Any]:
        app = {
            "id": f"app_{name}",
            "name": name,
            "status": status,
            "organization": {"slug": org},
            "current_release": {
                "version": version,
                "description": f"deploy release {version}",
            },
        }
        self.apps[name] = app
        self.machines.setdefault(name, [])
        self.volumes.setdefault(name, [])
        return app

    def add_machine(
        self,
        app_name: str,
        mach_id: str,
        name: str,
        region: str = "iad",
        state: str = "started",
        image: str = "registry.fly.io/app:v1",
        cpus: int = 2,
        memory_mb: int = 1024,
    ) -> dict[str, Any]:
        mach = {
            "id": mach_id,
            "name": name,
            "state": state,
            "region": region,
            "config": {
                "image": image,
                "guest": {"cpus": cpus, "memory_mb": memory_mb},
            },
            "checks": [{"name": "http-check", "status": "passing"}],
            "created_at": "2026-10-01T10:00:00Z",
            "updated_at": "2026-10-01T10:05:00Z",
        }
        self.machines[app_name].append(mach)
        return mach

    def add_volume(
        self,
        app_name: str,
        vol_id: str,
        name: str,
        region: str = "iad",
        size_gb: int = 20,
    ) -> dict[str, Any]:
        vol = {
            "id": vol_id,
            "name": name,
            "region": region,
            "size_gb": size_gb,
            "state": "created",
            "encrypted": True,
            "attached_machine_id": None,
        }
        self.volumes[app_name].append(vol)
        return vol

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, path))
        if path in self.fail_paths:
            raise self.fail_paths[path]

        clean_path = path.split("?")[0]

        if clean_path in ("/v1/apps", "apps"):
            return {"apps": list(self.apps.values())}

        if "/machines" in clean_path:
            # path is /v1/apps/<app_name>/machines
            parts = clean_path.strip("/").split("/")
            app_name = parts[2]
            return self.machines.get(app_name, [])

        if "/volumes" in clean_path:
            # path is /v1/apps/<app_name>/volumes
            parts = clean_path.strip("/").split("/")
            app_name = parts[2]
            return self.volumes.get(app_name, [])

        raise FlyioNotFoundError(f"Endpoint not found: {path}")
