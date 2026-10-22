"""In-memory stand-in for the Doppler REST API v3."""

from typing import Any
from cognee_community_connector_doppler.doppler import DopplerNotFoundError, DopplerRateLimitedError, DopplerAuthError


class FakeDoppler:
    def __init__(self, api_key: str = "dp.st.test_key_12345"):
        self.api_key = api_key
        self.dashboard_url = "https://dashboard.doppler.com"
        self.projects: dict[str, dict[str, Any]] = {}
        self.environments: dict[str, list[dict[str, Any]]] = {}
        self.configs: dict[str, list[dict[str, Any]]] = {}
        self.secrets: dict[str, dict[str, Any]] = {}
        self.logs: dict[str, list[dict[str, Any]]] = {}

        self.calls: list[tuple[str, str]] = []
        self.fail_paths: dict[str, Exception] = {}

    def add_project(self, name: str, description: str = "Backend microservice") -> dict[str, Any]:
        proj = {
            "id": f"proj_{name}",
            "name": name,
            "description": description,
            "created_at": "2026-10-01T10:00:00Z",
        }
        self.projects[name] = proj
        self.environments.setdefault(name, [])
        self.configs.setdefault(name, [])
        self.logs.setdefault(name, [])
        return proj

    def add_environment(self, project: str, slug: str, name: str) -> dict[str, Any]:
        env = {
            "id": f"env_{slug}",
            "slug": slug,
            "name": name,
            "created_at": "2026-10-01T10:05:00Z",
        }
        self.environments[project].append(env)
        return env

    def add_config(self, project: str, name: str, environment: str, variable_keys: list[str]) -> dict[str, Any]:
        cfg = {
            "name": name,
            "project": project,
            "environment": environment,
            "root": True,
            "locked": False,
        }
        self.configs[project].append(cfg)
        key = f"{project}:{name}"
        self.secrets[key] = {k: {"raw": f"secret_value_for_{k}", "computed": f"computed_{k}"} for k in variable_keys}
        return cfg

    def add_log(self, project: str, text: str, user: str = "Alice Engineer") -> dict[str, Any]:
        log_entry = {
            "id": f"log_{len(self.logs[project]) + 1}",
            "text": text,
            "user": {"name": user, "email": f"{user.lower().replace(' ', '.')}@acme.com"},
            "created_at": "2026-10-01T11:00:00Z",
            "config": "prd",
        }
        self.logs[project].append(log_entry)
        return log_entry

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

        if clean_path == "/v3/projects":
            return {"projects": list(self.projects.values())}

        if clean_path == "/v3/environments":
            p = params.get("project") if params else None
            return {"environments": self.environments.get(p, [])}

        if clean_path == "/v3/configs":
            p = params.get("project") if params else None
            return {"configs": self.configs.get(p, [])}

        if clean_path == "/v3/configs/config/secrets":
            p = params.get("project") if params else None
            c = params.get("config") if params else None
            key = f"{p}:{c}"
            return {"secrets": self.secrets.get(key, {})}

        if clean_path == "/v3/logs":
            p = params.get("project") if params else None
            return {"logs": self.logs.get(p, [])}

        raise DopplerNotFoundError(f"Endpoint not found: {path}")
