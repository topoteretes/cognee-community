"""In-memory stand-in for the Trigger.dev REST API, shaped like its real responses."""

from typing import Any
from cognee_community_connector_triggerdev.triggerdev import TriggerDevNotFoundError, TriggerDevRateLimitedError, TriggerDevAuthError


class FakeTriggerDev:
    def __init__(self, api_key: str = "tr_prod_test_key_12345"):
        self.api_key = api_key
        self.dashboard_url = "https://cloud.trigger.dev"
        self.tasks: dict[str, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.schedules: dict[str, dict[str, Any]] = {}

        self.calls: list[tuple[str, str]] = []
        self.fail_paths: dict[str, Exception] = {}
        self.rate_limit_calls = 0

    def add_task(self, slug: str, file_path: str = "src/tasks/billing.ts", export_name: str = "billingTask") -> dict[str, Any]:
        task = {
            "id": f"task_{slug}",
            "slug": slug,
            "filePath": file_path,
            "exportName": export_name,
            "triggerSource": "code",
            "createdAt": "2026-10-01T12:00:00.000Z",
            "updatedAt": "2026-10-01T12:00:00.000Z",
        }
        self.tasks[slug] = task
        return task

    def add_run(
        self,
        run_id: str,
        task_slug: str,
        status: str = "COMPLETED",
        error: dict[str, Any] | None = None,
        duration_ms: int = 450,
        output: Any = None,
    ) -> dict[str, Any]:
        run = {
            "id": run_id,
            "taskIdentifier": task_slug,
            "number": len(self.runs) + 1,
            "status": status,
            "createdAt": "2026-10-01T12:01:00.000Z",
            "startedAt": "2026-10-01T12:01:01.000Z",
            "finishedAt": "2026-10-01T12:01:02.000Z",
            "durationMs": duration_ms,
            "isTest": False,
            "tags": ["production", "v3"],
            "error": error,
            "output": output or {"success": True},
        }
        self.runs.append(run)
        return run

    def add_schedule(
        self,
        sched_id: str,
        task_slug: str,
        cron: str = "0 0 * * *",
        active: bool = True,
    ) -> dict[str, Any]:
        schedule = {
            "id": sched_id,
            "taskIdentifier": task_slug,
            "cron": cron,
            "timezone": "UTC",
            "active": active,
            "lastRunTimestamp": "2026-10-01T00:00:00.000Z",
            "nextRunTimestamp": "2026-10-02T00:00:00.000Z",
        }
        self.schedules[sched_id] = schedule
        return schedule

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

        if clean_path in ("/api/v1/tasks", "/api/v3/tasks"):
            return {"tasks": list(self.tasks.values())}

        if clean_path == "/api/v3/schedules":
            return {"schedules": list(self.schedules.values())}

        if clean_path == "/api/v3/runs":
            limit = int(params.get("limit", 100)) if params else 100
            cursor = params.get("cursor") if params else None

            start_idx = 0
            if cursor:
                for idx, r in enumerate(self.runs):
                    if r["id"] == cursor:
                        start_idx = idx + 1
                        break

            chunk = self.runs[start_idx : start_idx + limit]
            next_cursor = self.runs[start_idx + limit - 1]["id"] if (start_idx + limit < len(self.runs)) else None

            return {
                "runs": chunk,
                "pagination": {
                    "next": next_cursor,
                    "previous": None,
                },
            }

        raise TriggerDevNotFoundError(f"Endpoint not found: {path}")
