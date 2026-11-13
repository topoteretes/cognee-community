"""In-memory mock for the Unkey REST API used by offline tests."""

from __future__ import annotations

from typing import Any

from cognee_community_connector_unkey.unkey import UnkeyNotFoundError


class FakeUnkey:
    def __init__(self) -> None:
        self.apis: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, dict[str, dict[str, Any]]] = {}
        self.identities: dict[str, dict[str, Any]] = {}

        self.calls: list[tuple[str, str]] = []
        self.params: list[tuple[str, dict[str, Any]]] = []
        self.fail_paths: dict[str, Exception] = {}
        self.fail_after: int | None = None
        self.fail_with: Exception | None = None

    def add_api(self, api_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": api_id,
            "name": name,
            "workspaceId": fields.pop("workspaceId", "ws_default"),
            "description": fields.pop("description", f"API {name}"),
            "createdAt": fields.pop("createdAt", 1700000000000),
            **fields,
        }
        self.apis[api_id] = rec
        self.keys.setdefault(api_id, {})
        return rec

    def add_key(
        self,
        api_id: str,
        key_id: str,
        name: str,
        **fields: Any,
    ) -> dict[str, Any]:
        rec = {
            "id": key_id,
            "apiId": api_id,
            "name": name,
            "ownerId": fields.pop("ownerId", "cust_default"),
            "enabled": fields.pop("enabled", True),
            "remaining": fields.pop("remaining", 1000),
            "expires": fields.pop("expires", None),
            "ratelimit": fields.pop("ratelimit", {"limit": 100, "duration": 60000}),
            "plaintext": fields.pop("plaintext", "unkey_live_supersecret_plain123"),
            "hash": fields.pop("hash", "sha256_supersecret_hash456"),
            **fields,
        }
        self.keys.setdefault(api_id, {})[key_id] = rec
        return rec

    def add_identity(self, ident_id: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": ident_id,
            "externalId": fields.pop("externalId", f"ext_{ident_id}"),
            **fields,
        }
        self.identities[ident_id] = rec
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
        cursor = params.get("cursor")

        # 1. APIs
        if clean_path == "v1/apis.listApis":
            return {"apis": list(self.apis.values())}

        if clean_path == "v1/apis.getApi":
            api_id = params.get("apiId")
            if api_id in self.apis:
                return self.apis[api_id]
            raise UnkeyNotFoundError(f"API {api_id} not found")

        # 2. Keys
        if clean_path == "v1/apis.listKeys":
            api_id = params.get("apiId", "")
            items = list(self.keys.get(api_id, {}).values())
            return self._paginate(items, cursor, limit, "keys")

        if clean_path == "v1/keys.getKey":
            key_id = params.get("keyId")
            for api_keys in self.keys.values():
                if key_id in api_keys:
                    return api_keys[key_id]
            raise UnkeyNotFoundError(f"Key {key_id} not found")

        # 3. Identities
        if clean_path == "v1/identities.listIdentities":
            items = list(self.identities.values())
            return self._paginate(items, cursor, limit, "identities")

        if clean_path == "v1/identities.getIdentity":
            ident_id = params.get("identityId")
            if ident_id in self.identities:
                return self.identities[ident_id]
            raise UnkeyNotFoundError(f"Identity {ident_id} not found")

        raise UnkeyNotFoundError(f"Unhandled path {clean_path}")

    @staticmethod
    def _paginate(
        items: list[dict[str, Any]],
        cursor: str | None,
        limit: int,
        key_name: str,
    ) -> dict[str, Any]:
        start = 0
        if cursor:
            for idx, item in enumerate(items):
                if str(item.get("id")) == str(cursor):
                    start = idx + 1
                    break

        page = items[start : start + limit]
        next_cursor = None
        if start + limit < len(items) and page:
            next_cursor = str(page[-1].get("id"))

        return {
            key_name: page,
            "cursor": next_cursor,
            "total": len(items),
        }
