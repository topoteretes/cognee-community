"""In-memory stand-in for the Prefect REST API, shaped like real Prefect 2/3 responses."""

from typing import Any

from cognee_community_connector_prefect.prefect import PrefectNotFoundError


class FakePrefect:
    def __init__(self, base_url: str = "http://localhost:4200/api"):
        self.base_url = base_url
        self.flows: dict[str, dict[str, Any]] = {}
        self.deployments: dict[str, dict[str, Any]] = {}
        self.flow_runs: dict[str, dict[str, Any]] = {}

        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self.fail_paths: dict[str, Exception] = {}
        self.fail_after: int | None = None
        self.fail_with: Exception | None = None
        self.rate_limit: dict[str, int | None] = {}
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-10-01T10:{self._clock // 60:02d}:{self._clock % 60:02d}.000Z"

    def add_flow(self, flow_id: str, name: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": flow_id,
            "name": name,
            "tags": fields.pop("tags", []),
            "description": fields.pop("description", "Flow description"),
            "created": self._stamp(),
            "updated": self._stamp(),
            **fields,
        }
        self.flows[flow_id] = rec
        return rec

    def add_deployment(self, dep_id: str, name: str, flow_id: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": dep_id,
            "name": name,
            "flow_id": flow_id,
            "status": fields.pop("status", "READY"),
            "is_schedule_active": fields.pop("is_schedule_active", True),
            "schedules": fields.pop("schedules", [{"schedule": {"cron": "0 0 * * *"}}]),
            "work_pool_name": fields.pop("work_pool_name", "default-pool"),
            "work_queue_name": fields.pop("work_queue_name", "default-queue"),
            "entrypoint": fields.pop("entrypoint", "flows/main.py:my_flow"),
            "tags": fields.pop("tags", []),
            # parameters might contain secrets that must never be emitted into content
            "parameters": fields.pop("parameters", {"api_key": "secret_123", "limit": 100}),
            "created": self._stamp(),
            "updated": self._stamp(),
            **fields,
        }
        self.deployments[dep_id] = rec
        return rec

    def add_flow_run(self, run_id: str, name: str, flow_id: str, **fields: Any) -> dict[str, Any]:
        rec = {
            "id": run_id,
            "name": name,
            "flow_id": flow_id,
            "deployment_id": fields.pop("deployment_id", None),
            "state_type": fields.pop("state_type", "COMPLETED"),
            "state_name": fields.pop("state_name", "Completed"),
            "run_count": fields.pop("run_count", 1),
            "start_time": fields.pop("start_time", self._stamp()),
            "end_time": fields.pop("end_time", self._stamp()),
            "total_run_time": fields.pop("total_run_time", 25.4),
            "tags": fields.pop("tags", []),
            "parameters": fields.pop("parameters", {"db_password": "supersecretpassword"}),
            **fields,
        }
        self.flow_runs[run_id] = rec
        return rec

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, path))
        self.bodies.append((path, dict(body or {})))

        if path in self.fail_paths:
            raise self.fail_paths[path]

        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise self.fail_with or RuntimeError("Simulated failure")

        body = body or {}

        # Filter endpoints
        if path == "/flows/filter":
            records = list(self.flows.values())
            flow_filter = body.get("flows", {})
            if "tags" in flow_filter and "all_" in flow_filter["tags"]:
                req_tags = set(flow_filter["tags"]["all_"])
                records = [r for r in records if req_tags.issubset(set(r.get("tags") or []))]
            offset = body.get("offset", 0)
            limit = body.get("limit", 100)
            return records[offset : offset + limit]

        if path == "/deployments/filter":
            records = list(self.deployments.values())
            dep_filter = body.get("deployments", {})
            if "tags" in dep_filter and "all_" in dep_filter["tags"]:
                req_tags = set(dep_filter["tags"]["all_"])
                records = [r for r in records if req_tags.issubset(set(r.get("tags") or []))]
            offset = body.get("offset", 0)
            limit = body.get("limit", 100)
            return records[offset : offset + limit]

        if path == "/flow_runs/filter":
            records = list(self.flow_runs.values())
            run_filter = body.get("flow_runs", {})
            if "state" in run_filter and "type" in run_filter["state"]:
                allowed = set(run_filter["state"]["type"].get("any_", []))
                records = [r for r in records if r.get("state_type") in allowed]
            offset = body.get("offset", 0)
            limit = body.get("limit", 100)
            return records[offset : offset + limit]

        # Single item lookups
        if path.startswith("/flows/"):
            flow_id = path.split("/flows/", 1)[1]
            if flow_id in self.flows:
                return self.flows[flow_id]
            raise PrefectNotFoundError(f"Flow {flow_id} not found")

        if path.startswith("/deployments/"):
            dep_id = path.split("/deployments/", 1)[1]
            if dep_id in self.deployments:
                return self.deployments[dep_id]
            raise PrefectNotFoundError(f"Deployment {dep_id} not found")

        if path.startswith("/flow_runs/"):
            run_id = path.split("/flow_runs/", 1)[1]
            if run_id in self.flow_runs:
                return self.flow_runs[run_id]
            raise PrefectNotFoundError(f"Flow run {run_id} not found")

        raise PrefectNotFoundError(f"Endpoint {path} not recognized")
