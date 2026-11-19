"""In-memory stand-in for the Checkly REST API, shaped like real Checkly v1 responses."""

from typing import Any

from cognee_community_connector_checkly.checkly import ChecklyNotFoundError


class FakeCheckly:
    def __init__(self, base_url: str = "https://api.checklyhq.com/v1"):
        self.base_url = base_url
        self.checks: dict[str, dict[str, Any]] = {}
        self.check_groups: dict[str, dict[str, Any]] = {}
        self.alert_channels: dict[str, dict[str, Any]] = {}
        self.maintenance_windows: dict[str, dict[str, Any]] = {}
        self.check_statuses: dict[str, dict[str, Any]] = {}

        self.calls: list[tuple[str, str]] = []
        self.params: list[tuple[str, dict[str, Any]]] = []
        self.fail_paths: dict[str, Exception] = {}
        self.fail_after: int | None = None
        self.fail_with: Exception | None = None
        self.rate_limit: dict[str, int | None] = {}
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-10-01T10:{self._clock // 60:02d}:{self._clock % 60:02d}.000Z"

    def add_check(self, check_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": check_id,
            "name": name,
            "checkType": fields.pop("checkType", "API"),
            "activated": fields.pop("activated", True),
            "frequency": fields.pop("frequency", 5),
            "locations": fields.pop("locations", ["us-east-1"]),
            "groupId": fields.pop("groupId", None),
            "tags": fields.pop("tags", []),
            "request": fields.pop(
                "request",
                {
                    "method": "GET",
                    "url": "https://api.example.com/health",
                    # Sensitive headers must never be emitted in text
                    "headers": [
                        {"key": "Authorization", "value": "Bearer secret_jwt_token_123"},
                        {"key": "X-Api-Key", "value": "topsecretapikey"},
                    ],
                },
            ),
            "created_at": self._stamp(),
            "updated_at": self._stamp(),
            **fields,
        }
        self.checks[check_id] = rec
        return rec

    def add_check_group(self, group_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": group_id,
            "name": name,
            "activated": fields.pop("activated", True),
            "concurrency": fields.pop("concurrency", 3),
            "locations": fields.pop("locations", ["us-east-1"]),
            "tags": fields.pop("tags", []),
            # Environment variables must have secret values redacted
            "environmentVariables": fields.pop(
                "environmentVariables",
                [{"key": "DB_PASSWORD", "value": "secretpass"}],
            ),
            "created_at": self._stamp(),
            "updated_at": self._stamp(),
            **fields,
        }
        self.check_groups[group_id] = rec
        return rec

    def add_alert_channel(self, ch_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": ch_id,
            "name": name,
            "type": fields.pop("type", "SLACK"),
            "sendRecovery": fields.pop("sendRecovery", True),
            "sendFailure": fields.pop("sendFailure", True),
            "sendDegraded": fields.pop("sendDegraded", False),
            "config": fields.pop("config", {"url": "https://hooks.slack.com/services/secret"}),
            "created_at": self._stamp(),
            "updated_at": self._stamp(),
            **fields,
        }
        self.alert_channels[ch_id] = rec
        return rec

    def add_maintenance_window(self, mw_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": mw_id,
            "name": name,
            "startsAt": fields.pop("startsAt", "2026-10-25T02:00:00Z"),
            "endsAt": fields.pop("endsAt", "2026-10-25T04:00:00Z"),
            "repeatUnit": fields.pop("repeatUnit", "WEEK"),
            "tags": fields.pop("tags", []),
            **fields,
        }
        self.maintenance_windows[mw_id] = rec
        return rec

    def set_check_status(
        self, check_id: str, *, has_failures: bool = False, is_degraded: bool = False
    ) -> None:
        self.check_statuses[check_id] = {
            "checkId": check_id,
            "hasFailures": has_failures,
            "isDegraded": is_degraded,
            "created_at": self._stamp(),
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, path))
        self.params.append((path, dict(params or {})))

        if path in self.fail_paths:
            raise self.fail_paths[path]

        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise self.fail_with or RuntimeError("Simulated failure")

        params = params or {}
        page = params.get("page", 1)
        limit = params.get("limit", 100)
        start = (page - 1) * limit
        end = start + limit

        if path == "/checks":
            records = list(self.checks.values())
            return records[start:end]

        if path == "/check-groups":
            records = list(self.check_groups.values())
            return records[start:end]

        if path == "/alert-channels":
            records = list(self.alert_channels.values())
            return records[start:end]

        if path == "/maintenance-windows":
            records = list(self.maintenance_windows.values())
            return records[start:end]

        if path == "/check-statuses":
            return list(self.check_statuses.values())

        if path.startswith("/checks/"):
            c_id = path.split("/checks/", 1)[1]
            if c_id in self.checks:
                return self.checks[c_id]
            raise ChecklyNotFoundError(f"Check {c_id} not found")

        if path.startswith("/check-groups/"):
            g_id = path.split("/check-groups/", 1)[1]
            if g_id in self.check_groups:
                return self.check_groups[g_id]
            raise ChecklyNotFoundError(f"Check group {g_id} not found")

        if path.startswith("/alert-channels/"):
            ch_id = path.split("/alert-channels/", 1)[1]
            if ch_id in self.alert_channels:
                return self.alert_channels[ch_id]
            raise ChecklyNotFoundError(f"Alert channel {ch_id} not found")

        if path.startswith("/maintenance-windows/"):
            mw_id = path.split("/maintenance-windows/", 1)[1]
            if mw_id in self.maintenance_windows:
                return self.maintenance_windows[mw_id]
            raise ChecklyNotFoundError(f"Maintenance window {mw_id} not found")

        raise ChecklyNotFoundError(f"Endpoint {path} not recognized")
