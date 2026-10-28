"""An in-memory Trigger.dev management API that answers at the HTTP layer.

``install`` swaps the ``requests`` module the connector uses, so the real
``TriggerDevClient`` builds every URL, header and query string the fake checks.
Response shapes follow the API reference: ``data`` plus ``pagination`` with
``currentPage``/``totalPages``/``count`` for schedules and ``next`` for the
cursor listings. Unknown query parameters and out-of-range page sizes answer 400,
which is what makes a wrong parameter name fail a test instead of passing quietly.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urlsplit

import requests

BASE = "https://api.trigger.dev"
KEY = "tr_prod_sk_test_secret_key_123"
ENV_ID = "env_prod_1"

_PAGE_SIZES = {
    "/api/v1/runs": (10, 100, 25),
    "/api/v1/errors": (1, 100, 25),
    "/api/v1/deployments": (5, 100, 20),
}
_PARAMS = {
    "/api/v1/schedules": {"page", "perPage"},
    "/api/v1/runs": {
        "page[size]",
        "page[after]",
        "filter[status]",
        "filter[createdAt][from]",
    },
    "/api/v1/errors": {"page[size]", "page[after]", "filter[from]"},
    "/api/v1/deployments": {"page[size]", "page[after]", "from"},
}


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ago(**delta: float) -> str:
    return iso(datetime.now(timezone.utc) - timedelta(**delta))


class FakeResponse:
    def __init__(self, status: int, body: Any = None, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _Requests:
    """Stand-in for the ``requests`` module inside the connector."""

    RequestException = requests.RequestException

    def __init__(self, fake: "FakeTriggerDev"):
        self.get = fake.get


class FakeTriggerDev:
    def __init__(self, env_id: str = ENV_ID, schedule_page_size: int | None = None):
        self.env_id = env_id
        # the server may page schedules smaller than perPage asks for
        self.schedule_page_size = schedule_page_size
        self.schedules: dict[str, dict] = {}
        self.errors: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.details: dict[str, dict] = {}
        self.deployments: dict[str, dict] = {}
        # schedules that still exist but a listing leaves out
        self.unlisted: set[str] = set()
        self.calls: list[tuple[str, dict]] = []
        # path -> status answered for it; a count limits how many times
        self.fail: dict[str, int] = {}
        self.fail_times: dict[str, int] = {}
        self.extra_headers: dict[str, str] = {}
        # an answer forced for one path, e.g. a broken pagination object
        self.override: dict[str, Any] = {}

    def install(self, monkeypatch) -> None:
        from cognee_community_connector_triggerdev import triggerdev

        monkeypatch.setattr(triggerdev, "requests", _Requests(self))

    # -- records -------------------------------------------------------------
    def add_schedule(
        self,
        schedule_id: str,
        task: str,
        *,
        cron: str = "0 * * * *",
        description: str = "Every hour",
        active: bool = True,
        next_run: str = "2026-10-28T13:00:00.000Z",
    ) -> dict:
        self.schedules[schedule_id] = {
            "id": schedule_id,
            "task": task,
            "type": "DECLARATIVE",
            "active": active,
            "deduplicationKey": f"dedup-{schedule_id}",
            "externalId": None,
            "generator": {"type": "CRON", "expression": cron, "description": description},
            "timezone": "UTC",
            "nextRun": next_run,
            "environments": [{"id": self.env_id, "type": "PRODUCTION", "userName": None}],
        }
        return self.schedules[schedule_id]

    def add_error(
        self,
        error_id: str,
        task: str,
        *,
        message: str = "Cannot read properties of undefined (reading 'email')",
        status: str = "unresolved",
        count: int = 3,
        last_seen: str | None = None,
    ) -> dict:
        self.errors[error_id] = {
            "id": error_id,
            "fingerprint": f"fp-{error_id}",
            "taskIdentifier": task,
            "errorType": "TypeError",
            "errorMessage": message,
            "status": status,
            "count": count,
            "firstSeen": ago(days=2),
            "lastSeen": last_seen or ago(hours=1),
            "resolvedAt": None,
            "ignoredUntil": None,
        }
        return self.errors[error_id]

    def add_run(
        self,
        run_id: str,
        task: str,
        *,
        status: str = "FAILED",
        created_at: str | None = None,
        error_message: str | None = "Stripe webhook signature mismatch",
        schedule_id: str | None = None,
        env_id: str | None = None,
    ) -> dict:
        created = created_at or ago(hours=2)
        finished = status not in ("QUEUED", "EXECUTING", "REATTEMPTING", "FROZEN")
        self.runs[run_id] = {
            "id": run_id,
            "status": status,
            "taskIdentifier": task,
            "version": "20261028.1",
            "createdAt": created,
            "updatedAt": created,
            "startedAt": created,
            "finishedAt": created if finished else None,
            "durationMs": 1532,
            "costInCents": 0.0012,
            "isTest": False,
            "tags": ["customer_42", "billing"],
            "env": {"id": env_id or self.env_id, "name": "prod"},
        }
        attempts = [
            {
                "id": f"attempt_{run_id}_1",
                "status": "FAILED",
                "createdAt": created,
                "error": {
                    "name": "Error",
                    "message": error_message,
                    "stackTrace": (
                        f"Error: {error_message}\n    at handler (src/trigger/{task}.ts:42:11)"
                    ),
                },
            }
        ]
        self.details[run_id] = {
            "payload": {"card": "4242 4242 4242 4242", "email": "ada@example.com"},
            "output": {"secret": "do-not-store"},
            "attempts": attempts if error_message else [],
            "schedule": {"id": schedule_id} if schedule_id else None,
        }
        return self.runs[run_id]

    def add_deployment(self, deployment_id: str, version: str, *, status: str = "DEPLOYED") -> dict:
        self.deployments[deployment_id] = {
            "id": deployment_id,
            "createdAt": ago(days=1),
            "shortCode": f"sc-{deployment_id}",
            "version": version,
            "runtime": "node",
            "runtimeVersion": "22.11.0",
            "status": status,
            "deployedAt": ago(days=1) if status == "DEPLOYED" else None,
            "git": {
                "commitSha": "8f3c2a1b9d7e6f5a4b3c",
                "commitRef": "main",
                "commitMessage": "fix: retry Stripe webhooks\n\nlong body",
                "remoteUrl": "https://x-access-token:ghs_leak@github.com/acme/jobs.git",
            },
            "error": None,
        }
        return self.deployments[deployment_id]

    # -- transport -----------------------------------------------------------
    def get(self, url, params=None, headers=None, timeout=None):
        parts = urlsplit(url)
        path = unquote(parts.path)
        params = dict(params or {})
        self.calls.append((path, params))
        if f"{parts.scheme}://{parts.netloc}" != BASE or parts.query:
            return FakeResponse(404, {"error": "Not found"})
        if (headers or {}).get("Authorization") != f"Bearer {KEY}":
            return FakeResponse(401, {"error": "Invalid or Missing API key"})
        if path in self.fail and self.fail_times.get(path, 1) > 0:
            if path in self.fail_times:
                self.fail_times[path] -= 1
            return FakeResponse(self.fail[path], "<html>upstream error</html>")
        if path in self.override:
            return FakeResponse(200, self.override[path], self.extra_headers)
        status, body = self._route(path, params)
        return FakeResponse(status, body, self.extra_headers)

    def _route(self, path: str, params: dict) -> tuple[int, Any]:
        allowed = _PARAMS.get(path)
        if allowed is not None and set(params) - allowed:
            return 400, {"error": "Invalid query", "details": sorted(set(params) - allowed)}
        if path in _PAGE_SIZES:
            low, high, default = _PAGE_SIZES[path]
            size = int(params.get("page[size]", default))
            if not low <= size <= high:
                return 400, {"error": "Invalid page size"}
        if path == "/api/v1/schedules":
            return 200, self._schedule_page(params)
        if path.startswith("/api/v1/schedules/"):
            schedule = self.schedules.get(path.rsplit("/", 1)[1])
            return (200, schedule) if schedule else (404, {"error": "Schedule not found"})
        if path == "/api/v1/errors":
            since = params.get("filter[from]", "")
            groups = [g for g in self.errors.values() if g["lastSeen"] >= since]
            return 200, self._cursor_page(groups, "lastSeen", params, 25)
        if path == "/api/v1/runs":
            since = params.get("filter[createdAt][from]", ago(days=7))
            wanted = params.get("filter[status]")
            runs = [r for r in self.runs.values() if r["createdAt"] >= since]
            if wanted:
                runs = [r for r in runs if r["status"] in wanted.split(",")]
            return 200, self._cursor_page(runs, "createdAt", params, 25)
        if path.startswith("/api/v3/runs/"):
            run_id = path.rsplit("/", 1)[1]
            if run_id not in self.runs:
                return 404, {"error": "Run not found"}
            return 200, {**self.runs[run_id], **self.details.get(run_id, {})}
        if path == "/api/v1/deployments":
            since = params.get("from", "")
            deployments = [d for d in self.deployments.values() if d["createdAt"] >= since]
            return 200, self._cursor_page(deployments, "createdAt", params, 20)
        return 404, {"error": "Not found"}

    def _schedule_page(self, params: dict) -> dict:
        size = self.schedule_page_size or int(params.get("perPage", 10))
        page = int(params.get("page", 1))
        listed = [s for k, s in sorted(self.schedules.items()) if k not in self.unlisted]
        total_pages = (len(listed) + size - 1) // size
        return {
            "data": listed[(page - 1) * size : page * size],
            "pagination": {"currentPage": page, "totalPages": total_pages, "count": len(listed)},
        }

    def _cursor_page(self, records: list, stamp: str, params: dict, default: int) -> dict:
        size = int(params.get("page[size]", default))
        ordered = sorted(records, key=lambda r: (r[stamp], r["id"]), reverse=True)
        after = params.get("page[after]")
        if after:
            ids = [r["id"] for r in ordered]
            ordered = ordered[ids.index(after) + 1 :] if after in ids else []
        page = ordered[:size]
        more = len(ordered) > size
        return {"data": page, "pagination": {"next": page[-1]["id"]} if more else {}}

    # -- assertions helpers ----------------------------------------------------
    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]
