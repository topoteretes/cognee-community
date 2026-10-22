"""In-memory stand-in for the Axiom REST API v1."""

from typing import Any
from cognee_community_connector_axiom.axiom import (
    AxiomNotFoundError,
    AxiomRateLimitedError,
    AxiomAuthError,
    AxiomAccessError,
)


class FakeAxiom:
    def __init__(self, api_key: str = "xapt-mock12345678"):
        self.api_key = api_key
        self.dashboard_url = "https://app.axiom.co"
        self.datasets: list[dict[str, Any]] = []
        self.monitors: list[dict[str, Any]] = []
        self.notifiers: list[dict[str, Any]] = []

        self.calls: list[tuple[str, str]] = []
        self.fail_paths: dict[str, Exception] = {}

    def add_dataset(
        self,
        name: str,
        description: str = "Application structured access logs",
        retention_days: int = 30,
    ) -> dict[str, Any]:
        ds = {
            "id": name,
            "name": name,
            "description": description,
            "retentionDays": retention_days,
            "created": "2026-10-01T10:00:00Z",
        }
        self.datasets.append(ds)
        return ds

    def add_monitor(
        self,
        mon_id: str,
        name: str,
        apl_query: str,
        interval: str = "5m",
        threshold: int = 10,
        disabled: bool = False,
    ) -> dict[str, Any]:
        mon = {
            "id": mon_id,
            "name": name,
            "description": f"Monitor for {name}",
            "aplQuery": apl_query,
            "interval": interval,
            "operator": "above",
            "threshold": threshold,
            "disabledUntil": "2099-01-01T00:00:00Z" if disabled else None,
        }
        self.monitors.append(mon)
        return mon

    def add_notifier(
        self,
        notif_id: str,
        name: str,
        notif_type: str = "slack",
    ) -> dict[str, Any]:
        notif = {
            "id": notif_id,
            "name": name,
            "type": notif_type,
            "description": f"Alerts sent to {name}",
        }
        self.notifiers.append(notif)
        return notif

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

        if clean_path in ("/v1/datasets", "datasets"):
            return list(self.datasets)

        if clean_path in ("/v1/monitors", "monitors"):
            return list(self.monitors)

        if clean_path in ("/v1/notifiers", "notifiers"):
            return list(self.notifiers)

        raise AxiomNotFoundError(f"Endpoint not found: {path}")
