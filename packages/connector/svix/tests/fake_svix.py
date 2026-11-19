"""In-memory mock for the Svix REST API used by offline tests."""

from __future__ import annotations

from typing import Any

from cognee_community_connector_svix.svix import SvixNotFoundError


class FakeSvix:
    def __init__(self) -> None:
        self.apps: dict[str, dict[str, Any]] = {}
        self.endpoints: dict[str, dict[str, dict[str, Any]]] = {}
        self.event_types: dict[str, dict[str, Any]] = {}

        self.calls: list[tuple[str, str]] = []
        self.params: list[tuple[str, dict[str, Any]]] = []
        self.fail_paths: dict[str, Exception] = {}
        self.fail_after: int | None = None
        self.fail_with: Exception | None = None

    def add_app(self, app_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": app_id,
            "name": name,
            "uid": fields.pop("uid", f"usr_{app_id}"),
            "createdAt": fields.pop("createdAt", "2026-10-20T10:00:00Z"),
            "rateLimit": fields.pop("rateLimit", 100),
            **fields,
        }
        self.apps[app_id] = rec
        self.endpoints.setdefault(app_id, {})
        return rec

    def add_endpoint(
        self,
        app_id: str,
        ep_id: str,
        url: str,
        **fields: Any,
    ) -> dict[str, Any]:
        rec = {
            "id": ep_id,
            "url": url,
            "description": fields.pop("description", f"Webhook endpoint {ep_id}"),
            "version": fields.pop("version", 1),
            "disabled": fields.pop("disabled", False),
            "filterTypes": fields.pop("filterTypes", ["user.created"]),
            "channels": fields.pop("channels", ["default"]),
            "secret": fields.pop("secret", "whsec_supersecretkey123"),
            **fields,
        }
        self.endpoints.setdefault(app_id, {})[ep_id] = rec
        return rec

    def add_event_type(self, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "name": name,
            "description": fields.pop("description", f"Event type {name}"),
            "archived": fields.pop("archived", False),
            "schemas": fields.pop(
                "schemas",
                {"1": {"type": "object", "properties": {"id": {"type": "string"}}}},
            ),
            **fields,
        }
        self.event_types[name] = rec
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
        limit = params.get("limit", 50)
        iterator = params.get("iterator")

        # 1. Applications
        if clean_path == "api/v1/app":
            app_list = list(self.apps.values())
            return self._paginate(app_list, iterator, limit)

        if clean_path.startswith("api/v1/app/") and "/endpoint" not in clean_path:
            app_id = clean_path.split("/")[-1]
            if app_id in self.apps:
                return self.apps[app_id]
            raise SvixNotFoundError(f"Application {app_id} not found")

        # 2. Endpoints
        if clean_path.startswith("api/v1/app/") and clean_path.endswith("/endpoint"):
            parts = clean_path.split("/")
            app_id = parts[3]
            ep_list = list(self.endpoints.get(app_id, {}).values())
            return self._paginate(ep_list, iterator, limit)

        if clean_path.startswith("api/v1/app/") and "/endpoint/" in clean_path:
            parts = clean_path.split("/")
            app_id = parts[3]
            ep_id = parts[5]
            if app_id in self.endpoints and ep_id in self.endpoints[app_id]:
                return self.endpoints[app_id][ep_id]
            raise SvixNotFoundError(f"Endpoint {ep_id} not found in app {app_id}")

        # 3. Event Types
        if clean_path == "api/v1/event-type":
            include_archived = params.get("include_archived", False)
            ev_list = [
                ev for ev in self.event_types.values() if include_archived or not ev.get("archived")
            ]
            return self._paginate(ev_list, iterator, limit)

        if clean_path.startswith("api/v1/event-type/"):
            name = clean_path.split("/")[-1]
            if name in self.event_types:
                return self.event_types[name]
            raise SvixNotFoundError(f"Event type {name} not found")

        raise SvixNotFoundError(f"Unhandled path {clean_path}")

    @staticmethod
    def _paginate(items: list[dict[str, Any]], iterator: str | None, limit: int) -> dict[str, Any]:
        start = 0
        if iterator:
            for idx, item in enumerate(items):
                key = item.get("id") or item.get("name")
                if str(key) == str(iterator):
                    start = idx + 1
                    break

        page = items[start : start + limit]
        next_iter = None
        if start + limit < len(items) and page:
            last_item = page[-1]
            next_iter = str(last_item.get("id") or last_item.get("name"))

        return {
            "data": page,
            "iterator": next_iter,
            "done": next_iter is None,
        }
