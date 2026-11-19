"""In-memory mock for the Novu REST API used by offline tests."""

from __future__ import annotations

from typing import Any

from cognee_community_connector_novu.novu import NovuNotFoundError


class FakeNovu:
    def __init__(self) -> None:
        self.workflows: dict[str, dict[str, Any]] = {}
        self.topics: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, dict[str, Any]] = {}
        self.integrations: dict[str, dict[str, Any]] = {}

        self.calls: list[tuple[str, str]] = []
        self.params: list[tuple[str, dict[str, Any]]] = []
        self.fail_paths: dict[str, Exception] = {}
        self.fail_after: int | None = None
        self.fail_with: Exception | None = None

    def add_workflow(self, wf_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "_id": wf_id,
            "name": name,
            "description": fields.pop("description", f"Workflow {name}"),
            "active": fields.pop("active", True),
            "steps": fields.pop(
                "steps",
                [
                    {"template": {"type": "email"}},
                    {"template": {"type": "in_app"}},
                ],
            ),
            "tags": fields.pop("tags", ["security", "transactional"]),
            **fields,
        }
        self.workflows[wf_id] = rec
        return rec

    def add_topic(self, key: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "_id": f"top_{key}",
            "key": key,
            "name": name,
            "subscribers": fields.pop("subscribers", ["usr_1", "usr_2"]),
            **fields,
        }
        self.topics[key] = rec
        return rec

    def add_group(self, group_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "_id": group_id,
            "name": name,
            **fields,
        }
        self.groups[group_id] = rec
        return rec

    def add_integration(
        self,
        integ_id: str,
        provider_id: str,
        channel: str,
        **fields: Any,
    ) -> dict[str, Any]:
        rec = {
            "_id": integ_id,
            "providerId": provider_id,
            "channel": channel,
            "name": fields.pop("name", f"{provider_id.capitalize()} Channel"),
            "active": fields.pop("active", True),
            "primary": fields.pop("primary", False),
            "credentials": fields.pop(
                "credentials",
                {"apiKey": "sg_secret_key_12345", "secret": "whsec_live_12345"},
            ),
            **fields,
        }
        self.integrations[integ_id] = rec
        return rec

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_path = path.lstrip("/")
        self.calls.append((method.upper(), clean_path))
        self.params.append((clean_path, dict(params or {})))

        if clean_path in self.fail_paths:
            raise self.fail_paths[clean_path]
        if f"{method.upper()}:{clean_path}" in self.fail_paths:
            raise self.fail_paths[f"{method.upper()}:{clean_path}"]

        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise self.fail_with or RuntimeError("Simulated failure")

        params = params or {}
        page = params.get("page", 0)
        limit = params.get("limit", 50)

        # 1. Workflows
        if clean_path == "v1/workflows":
            items = list(self.workflows.values())
            start = page * limit
            return {"data": items[start : start + limit], "totalCount": len(items)}

        if clean_path.startswith("v1/workflows/"):
            wf_id = clean_path.split("/")[-1]
            if wf_id in self.workflows:
                return {"data": self.workflows[wf_id]}
            raise NovuNotFoundError(f"Workflow {wf_id} not found")

        # 2. Topics
        if clean_path == "v1/topics":
            items = list(self.topics.values())
            start = page * limit
            return {"data": items[start : start + limit], "totalCount": len(items)}

        if clean_path.startswith("v1/topics/"):
            key = clean_path.split("/")[-1]
            if key in self.topics:
                return {"data": self.topics[key]}
            raise NovuNotFoundError(f"Topic {key} not found")

        # 3. Notification Groups
        if clean_path == "v1/notification-groups":
            return {"data": list(self.groups.values())}

        # 4. Integrations
        if clean_path == "v1/integrations":
            return {"data": list(self.integrations.values())}

        raise NovuNotFoundError(f"Unhandled path {clean_path}")
