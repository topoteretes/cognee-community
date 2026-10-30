"""In-memory stand-in for the Retool API, shared by test modules."""

import pytest
from cognee_community_connector_retool.retool import (
    RetoolAPIError,
    RetoolAuthError,
    RetoolQuotaError,
    RetoolRateLimitedError,
)

class FakeRetool:
    """Implements ``get`` for Retool endpoints: /apps, /workflows, /resources."""

    def __init__(self):
        self.apps: dict[str, dict] = {}
        self.workflows: dict[str, dict] = {}
        self.resources: dict[str, dict] = {}
        self.calls: list[str] = []
        self.bad_token = False
        self.rate_limited = False
        self.quota_exhausted = False
        self.server_error = False

    def add_app(self, app_id: str, name: str, description: str = "", folder: str = "Root", components=None, queries=None):
        app = {
            "id": app_id,
            "name": name,
            "description": description,
            "folder": folder,
            "currentVersion": "1.0.0",
            "updatedAt": "2026-10-25T10:00:00.000Z",
            "components": components or ["table1", "form1"],
            "queries": queries or ["getUsersQuery", "updateProfileQuery"],
        }
        self.apps[app_id] = app
        return app

    def add_workflow(self, wf_id: str, name: str, description: str = "", triggers=None, blocks=None):
        wf = {
            "id": wf_id,
            "name": name,
            "description": description,
            "folder": "Automations",
            "triggers": triggers or ["webhook"],
            "updatedAt": "2026-10-25T10:00:00.000Z",
            "blocks": blocks or [{"name": "validatePayload", "type": "code"}, {"name": "insertDb", "type": "query"}],
        }
        self.workflows[wf_id] = wf
        return wf

    def add_resource(self, res_id: str, name: str, res_type: str = "postgresql"):
        res = {
            "id": res_id,
            "displayName": name,
            "type": res_type,
            "environment": "production",
            "description": f"{name} primary data store",
            "updatedAt": "2026-10-25T10:00:00.000Z",
        }
        self.resources[res_id] = res
        return res

    def get(self, path: str, params=None):
        self.calls.append(path)
        if self.bad_token:
            raise RetoolAuthError("Retool rejected the API token (HTTP 401)")
        if self.rate_limited:
            raise RetoolRateLimitedError("Retool rate limit exceeded (HTTP 429)")
        if self.quota_exhausted:
            raise RetoolQuotaError("Retool quota exhausted")
        if self.server_error:
            raise RetoolAPIError("Retool server error: HTTP 500", status=500)

        clean = path.strip("/")
        parts = clean.split("/")

        if len(parts) == 1:
            collection = parts[0]
            if collection == "apps":
                return list(self.apps.values())
            if collection == "workflows":
                return list(self.workflows.values())
            if collection == "resources":
                return list(self.resources.values())
            raise RetoolAPIError(f"Endpoint not found: {path}", status=404)

        if len(parts) == 2:
            collection, item_id = parts
            if collection == "apps" and item_id in self.apps:
                return dict(self.apps[item_id])
            if collection == "workflows" and item_id in self.workflows:
                return dict(self.workflows[item_id])
            if collection == "resources" and item_id in self.resources:
                return dict(self.resources[item_id])
            raise RetoolAPIError(f"Item not found: {path}", status=404)

        raise RetoolAPIError(f"Unknown path: {path}", status=404)


@pytest.fixture
def retool():
    return FakeRetool()
