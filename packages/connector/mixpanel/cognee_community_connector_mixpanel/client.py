"""Read-only, version-pinned Headless adapter with strict App API envelopes."""

import hashlib
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
from mixpanel_headless import Project, ServiceAccount, Session, WorkspaceRef
from mixpanel_headless._internal.api_client import MixpanelAPIClient
from mixpanel_headless.exceptions import MixpanelHeadlessError
from pydantic import SecretStr

from .documents import canonical
from .errors import MixpanelConnectorError, RequestBudgetExceeded, ResponseContractError


def _unwrap(body: Any) -> Any:
    for _ in range(3):
        if not isinstance(body, dict):
            return body
        if "status" in body and body["status"] not in ("ok", "success"):
            raise ResponseContractError("Mixpanel returned an unsuccessful response.")
        # These routes have no verified paging contract. Never guess a cursor
        # parameter or quietly drop completion metadata from an SDK list helper.
        if any(
            body.get(k) not in (None, False, "", 0)
            for k in (
                "has_more",
                "next",
                "next_cursor",
                "next_page",
                "truncated",
                "is_truncated",
            )
        ):
            raise ResponseContractError(
                "Collection is paginated/truncated; completion is unverified."
            )
        if "results" not in body:
            return body
        allowed = {
            "results",
            "status",
            "has_more",
            "next",
            "next_cursor",
            "next_page",
            "truncated",
            "is_truncated",
            "total",
            "total_count",
        }
        if set(body) - allowed:
            raise ResponseContractError(
                "Unknown collection envelope metadata; refusing partial sync."
            )
        values = body["results"]
        for key in ("total", "total_count"):
            if key in body:
                total = body[key]
                if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                    raise ResponseContractError("Invalid collection total.")
                candidate = values.get("results") if isinstance(values, dict) else values
                if not isinstance(candidate, list) or total != len(candidate):
                    raise ResponseContractError("Collection count does not prove completion.")
        body = values
    if isinstance(body, dict) and "results" in body:
        raise ResponseContractError("Unexpected envelope nesting.")
    return body


def collection(body: Any) -> list[dict]:
    rows = _unwrap(body)
    if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
        raise ResponseContractError("Definition inventory must contain only objects.")
    return rows


class _Budget:
    """Outbound attempts survive failed extraction and are shared across projects."""

    def __init__(self, directory: Path, principal: str, hourly_limit: int):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "requests.sqlite"
        self.principal = principal
        self.limit = hourly_limit
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (principal TEXT, at REAL)")
            db.execute("CREATE INDEX IF NOT EXISTS attempts_principal ON attempts(principal, at)")
            db.execute("CREATE TABLE IF NOT EXISTS lexicon_attempts (principal TEXT, at REAL)")
        os.chmod(self.path, 0o600)

    def take(self, lexicon=False):
        now = time.time()
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE at < ?", (now - 3600,))
            used = db.execute(
                "SELECT COUNT(*) FROM attempts WHERE principal = ?", (self.principal,)
            ).fetchone()[0]
            if used >= self.limit:
                raise RequestBudgetExceeded("Hourly request budget exhausted; retry later.")
            if lexicon:
                db.execute("DELETE FROM lexicon_attempts WHERE at < ?", (now - 60,))
                used_lexicon = db.execute(
                    "SELECT COUNT(*) FROM lexicon_attempts WHERE principal = ?", (self.principal,)
                ).fetchone()[0]
                if used_lexicon >= 5:
                    raise RequestBudgetExceeded(
                        "Lexicon five-per-minute budget exhausted; retry later."
                    )
                db.execute("INSERT INTO lexicon_attempts VALUES (?, ?)", (self.principal, now))
            db.execute("INSERT INTO attempts VALUES (?, ?)", (self.principal, now))


class _BoundedTransport(httpx.BaseTransport):
    def __init__(self, inner, budget, per_run, max_response_bytes, owns_inner=True):
        self.inner, self.budget = inner, budget
        self.owns_inner = owns_inner
        self.per_run, self.max_bytes, self.attempts = per_run, max_response_bytes, 0

    def handle_request(self, request):
        if request.method != "GET":
            raise MixpanelConnectorError("Connector transport allows definition reads only.")
        if self.attempts >= self.per_run:
            raise RequestBudgetExceeded("Per-run request budget exhausted; narrow the selection.")
        self.budget.take(lexicon=request.url.path.endswith("/schemas"))
        self.attempts += 1
        response = self.inner.handle_request(request)
        try:
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > self.max_bytes:
                    raise ResponseContractError("Provider response exceeds configured byte limit.")
                chunks.append(chunk)
            headers = {
                k: v
                for k, v in response.headers.items()
                if k not in {"content-encoding", "content-length"}
            }
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=b"".join(chunks),
                request=request,
            )
        finally:
            response.close()

    def close(self):
        if self.owns_inner:
            self.inner.close()


class MixpanelClient:
    """Only GET methods are exposed. No event exports, query execution or writes.

    The SDK's internal client/_raw seam is deliberately isolated here and pinned
    to v0.4.0. No SDK ambient config, discovery cache or persisted login is used.
    """

    def __init__(
        self,
        *,
        project_id: str,
        username: str,
        secret: str,
        region="us",
        workspace_id: int | None = None,
        state_directory: str | Path | None = None,
        max_requests=50,
        hourly_limit=60,
        max_response_bytes=2_000_000,
        max_retries=2,
        transport=None,
        sleep=time.sleep,
    ):
        if region not in {"us", "eu", "in"}:
            raise ValueError("region must be us, eu or in")
        if not isinstance(project_id, str) or not project_id.isdigit() or int(project_id) <= 0:
            raise ValueError("project_id must be a positive numeric string")
        if (
            not isinstance(username, str)
            or not isinstance(secret, str)
            or not username
            or not secret
        ):
            raise ValueError("Service-account username and secret are required")
        if workspace_id is not None and (
            isinstance(workspace_id, bool) or not isinstance(workspace_id, int) or workspace_id <= 0
        ):
            raise ValueError("workspace_id must be a positive integer")
        for name, value in (
            ("max_requests", max_requests),
            ("hourly_limit", hourly_limit),
            ("max_response_bytes", max_response_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or not 0 <= max_retries <= 3
        ):
            raise ValueError("max_retries must be between zero and three")
        project_id = str(int(project_id))
        self.project_id, self.workspace_id, self.region = project_id, workspace_id, region
        self.principal = hashlib.sha256(canonical([region, username]).encode()).hexdigest()
        self.scope = canonical([region, project_id, workspace_id, self.principal])
        self.state_directory = Path(state_directory or Path.home() / ".cache/cognee-mixpanel")
        host = {"us": "mixpanel", "eu": "eu.mixpanel", "in": "in.mixpanel"}[region]
        self.project_url = f"https://{host}.com/project/{project_id}"
        self._max_requests, self._sleep, self._retries = max_requests, sleep, max_retries
        self._budget = _Budget(self.state_directory, self.principal, hourly_limit)
        self._injected_transport = transport
        self._max_response_bytes = max_response_bytes
        self._session = Session(
            account=ServiceAccount(
                name="cognee", region=region, username=username, secret=SecretStr(secret)
            ),
            project=Project(id=project_id),
            workspace=WorkspaceRef(id=workspace_id, project_id=project_id)
            if workspace_id
            else None,
        )
        self._api = None

    def __enter__(self):
        if self._api is not None:
            raise MixpanelConnectorError("Client is already in use by another sync.")
        bounded = _BoundedTransport(
            self._injected_transport or httpx.HTTPTransport(),
            self._budget,
            self._max_requests,
            self._max_response_bytes,
            owns_inner=self._injected_transport is None,
        )
        self._api = MixpanelAPIClient(
            session=self._session, _transport=bounded, max_retries=0, timeout=30
        )
        self._api.__enter__()
        return self

    def __exit__(self, *args):
        try:
            self._api.__exit__(*args)
        finally:
            self._api = None

    def _get(self, path: str, params=None):
        if self._api is None:
            raise MixpanelConnectorError("Use MixpanelClient inside a context manager.")
        for attempt in range(self._retries + 1):
            try:
                return self._api.app_request("GET", path, params=params, _raw=True)
            except MixpanelHeadlessError as error:
                status = getattr(error, "status_code", None)
                transient = (
                    status in {429, 500, 502, 503, 504}
                    or getattr(error, "code", None) == "HTTP_ERROR"
                )
                if not transient or attempt == self._retries:
                    # The SDK error stores request and provider payloads. Do not
                    # include it as an exception chain that a logger might emit.
                    raise MixpanelConnectorError(
                        f"Definition read failed (HTTP {status or 'transport'}); "
                        "verify credentials, scope and API access. No batch was published."
                    ) from None
                delay = getattr(error, "retry_after", None)
                if delay is None:
                    delay = 2**attempt
                if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
                    delay = 2**attempt
                if delay > 30:
                    raise RequestBudgetExceeded(
                        "Rate limit requires a later retry; staging retained."
                    ) from None
                self._sleep(delay)
        raise AssertionError("unreachable")

    def access_fingerprint(self) -> str:
        index = _unwrap(self._get("/projects/metadata/index"))
        if not isinstance(index, dict) or not isinstance(index.get(self.project_id), dict):
            raise ResponseContractError(
                "Configured project is absent from accessible project metadata."
            )
        project = index[self.project_id]
        metadata = {
            k: project[k]
            for k in ("role", "permissions", "access", "is_access_restricted")
            if k in project
        }
        if self.workspace_id:
            workspaces = project.get("workspaces")
            if not isinstance(workspaces, dict) or not isinstance(
                workspaces.get(str(self.workspace_id)), dict
            ):
                raise ResponseContractError(
                    "Configured workspace is not verified within the project."
                )
            ws = workspaces[str(self.workspace_id)]
            metadata["workspace"] = {
                k: ws[k] for k in ("role", "permissions", "access", "is_visible") if k in ws
            }
        return canonical(metadata)

    def _path(self, resource: str) -> str:
        if self.workspace_id:
            return f"/workspaces/{self.workspace_id}/{resource}"
        return f"/projects/{self.project_id}/{resource}"

    def inventory(self, kind: str) -> list[dict]:
        if kind == "schema":
            return collection(self._get(f"/projects/{self.project_id}/schemas"))
        resource = {"report": "bookmarks", "cohort": "cohorts"}[kind]
        return collection(self._get(self._path(resource), {"v": "2"} if kind == "report" else None))

    def detail(self, kind: str, remote_id: int) -> dict:
        resource = {"report": "bookmarks", "cohort": "cohorts"}[kind]
        value = _unwrap(
            self._get(
                self._path(f"{resource}/{remote_id}"), {"v": "2"} if kind == "report" else None
            )
        )
        if not isinstance(value, dict) or value.get("id") != remote_id:
            raise ResponseContractError("Definition detail does not match the requested identity.")
        if value.get("can_view") is False or value.get("is_visible") is False:
            raise ResponseContractError(
                "Selected definition is not visible to this service account."
            )
        return value
