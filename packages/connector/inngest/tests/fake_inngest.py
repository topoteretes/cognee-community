"""In-memory stand-in for the Inngest REST API."""

from typing import Any
from cognee_community_connector_inngest.inngest import (
    InngestNotFoundError,
    InngestRateLimitedError,
    InngestAuthError,
    InngestAccessError,
)


class FakeInngest:
    def __init__(self, api_key: str = "signkey-prod-mock12345678"):
        self.api_key = api_key
        self.dashboard_url = "https://app.inngest.com"
        self.functions: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

        self.calls: list[tuple[str, str]] = []
        self.fail_paths: dict[str, Exception] = {}

    def add_function(
        self,
        fn_id: str,
        name: str,
        triggers: list[dict[str, Any]] | None = None,
        app_id: str = "app-primary",
        concurrency: int | str = 5,
    ) -> dict[str, Any]:
        fn = {
            "id": fn_id,
            "name": name,
            "triggers": triggers or [{"event": "app/user.created"}],
            "app_id": app_id,
            "concurrency": concurrency,
        }
        self.functions.append(fn)
        return fn

    def add_run(
        self,
        run_id: str,
        fn_id: str,
        status: str = "COMPLETED",
        error: dict[str, Any] | None = None,
        output: Any = None,
    ) -> dict[str, Any]:
        run = {
            "id": run_id,
            "run_id": run_id,
            "function_id": fn_id,
            "status": status,
            "started_at": "2026-10-01T12:00:00Z",
            "ended_at": "2026-10-01T12:00:05Z",
            "error": error,
            "output": output or {"success": True},
        }
        self.runs.append(run)
        return run

    def add_event(self, name: str, event_id: str | None = None) -> dict[str, Any]:
        evt = {
            "id": event_id or f"evt_{name}_1",
            "name": name,
            "timestamp": "2026-10-01T11:59:59Z",
        }
        self.events.append(evt)
        return evt

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

        if clean_path in ("/v1/functions", "functions"):
            return {"data": list(self.functions)}

        if clean_path in ("/v1/events", "events"):
            return {"data": list(self.events)}

        if clean_path in ("/v1/runs", "runs"):
            limit = params.get("limit", 100) if params else 100
            cursor = params.get("cursor") if params else None
            # simple pagination
            start_idx = int(cursor) if cursor and cursor.isdigit() else 0
            end_idx = start_idx + limit
            sliced = self.runs[start_idx:end_idx]
            next_cursor = str(end_idx) if end_idx < len(self.runs) else None
            return {
                "data": sliced,
                "pagination": {"next": next_cursor} if next_cursor else {},
            }

        raise InngestNotFoundError(f"Endpoint not found: {path}")
