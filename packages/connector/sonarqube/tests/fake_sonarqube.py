"""A hand-rolled fake SonarQube Web API session for tests (no respx/mock)."""

from __future__ import annotations

import copy
from typing import Any


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def error_body(msg: str) -> dict:
    return {"errors": [{"msg": msg}]}


def _paginate(items: list, params: dict) -> tuple[int, list]:
    """Slice ``items`` the way a real SonarQube page-based listing would,
    honoring ``p``/``ps`` instead of returning everything on every page --
    the connector's own pagination loop (page advance, last-page detection,
    window-limit guard) is only ever genuinely exercised if this does.
    """
    page = int(params.get("p", 1) or 1)
    page_size = int(params.get("ps") or len(items) or 1)
    start = (page - 1) * page_size
    return page, items[start : start + page_size]


class FakeSonarQubeSession:
    """Serves the subset of the SonarQube Web API this connector calls.

    ``projects``: dict of project_key -> {"name": str, "issues": [...],
    "hotspots": [...], "gate": {...} or None}. ``missing`` is a set of project
    keys that 404 on every call (simulating a deleted/inaccessible project).
    ``scripted``: optional list of (status, body, headers) tuples consumed in
    order for the *next* matching path before falling back to normal behavior
    -- lets tests inject transient errors or a one-off 403/404.
    """

    def __init__(self, projects: dict[str, dict] | None = None, organization: str | None = None):
        self.projects: dict[str, dict] = projects or {}
        self.missing: set[str] = set()
        self.organization = organization
        self.calls: list[tuple[str, dict]] = []
        self.scripted: dict[str, list[tuple[int, Any, dict | None]]] = {}

    def script(self, path: str, status: int, body: Any = None, headers: dict | None = None):
        self.scripted.setdefault(path, []).append((status, body, headers))

    # -- requests.Session-like interface -----------------------------------
    def get(self, url: str, params: dict | None = None, timeout: float | None = None):
        params = dict(params or {})
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path if not path.startswith("/") else path
        self.calls.append((path, params))

        queued = self.scripted.get(path)
        if queued:
            status, body, headers = queued.pop(0)
            return FakeResponse(status, body, headers)

        if path == "/api/components/show":
            return self._components_show(params)
        if path == "/api/components/search":
            return self._components_search(params)
        if path == "/api/issues/search":
            return self._issues_search(params)
        if path == "/api/hotspots/search":
            return self._hotspots_search(params)
        if path == "/api/hotspots/show":
            return self._hotspots_show(params)
        if path == "/api/qualitygates/project_status":
            return self._project_status(params)
        return FakeResponse(404, error_body(f"unknown path {path}"))

    # -- handlers -------------------------------------------------------------
    def _components_show(self, params):
        key = params.get("component")
        if key not in self.projects or key in self.missing:
            return FakeResponse(404, error_body(f"Component key '{key}' not found"))
        return FakeResponse(200, {"component": {"key": key, "name": self.projects[key]["name"]}})

    def _components_search(self, params):
        keys = sorted(k for k in self.projects if k not in self.missing)
        components = [{"key": k, "name": self.projects[k]["name"]} for k in keys]
        return FakeResponse(200, {"components": components, "paging": {"total": len(components)}})

    def _issues_search(self, params):
        project_key = params.get("components")
        if project_key not in self.projects or project_key in self.missing:
            return FakeResponse(404, error_body(f"Component key '{project_key}' not found"))
        issues = copy.deepcopy(self.projects[project_key].get("issues") or [])

        if params.get("issues"):
            wanted = set(params["issues"].split(","))
            issues = [i for i in issues if i.get("key") in wanted]
        else:
            if params.get("resolved") == "false":
                issues = [i for i in issues if not i.get("resolution")]
            severities = params.get("severities")
            if severities:
                wanted = set(severities.split(","))
                issues = [i for i in issues if i.get("severity") in wanted]
            types = params.get("types")
            if types:
                wanted = set(types.split(","))
                issues = [i for i in issues if i.get("type") in wanted]
            created_after = params.get("createdAfter")
            if created_after:
                issues = [i for i in issues if (i.get("creationDate") or "") >= created_after]

        total = len(issues)
        page, page_issues = _paginate(issues, params)

        rules = {}
        if params.get("additionalFields") == "rules":
            for issue in page_issues:
                rule_key = issue.get("rule")
                if rule_key:
                    rules[rule_key] = {"key": rule_key, "name": f"Rule name for {rule_key}"}

        paging = {"pageIndex": page, "pageSize": params.get("ps"), "total": total}
        body = {"issues": page_issues, "rules": list(rules.values()), "paging": paging}
        return FakeResponse(200, body)

    def _hotspots_search(self, params):
        project_key = params.get("project")
        if project_key not in self.projects or project_key in self.missing:
            return FakeResponse(404, error_body(f"Component key '{project_key}' not found"))
        hotspots = copy.deepcopy(self.projects[project_key].get("hotspots") or [])
        status = params.get("status")
        if status:
            hotspots = [h for h in hotspots if h.get("status") == status]
        total = len(hotspots)
        page, page_hotspots = _paginate(hotspots, params)
        paging = {"pageIndex": page, "total": total}
        return FakeResponse(200, {"hotspots": page_hotspots, "paging": paging})

    def _hotspots_show(self, params):
        key = params.get("hotspot")
        for project in self.projects.values():
            for hotspot in project.get("hotspots") or []:
                if hotspot.get("key") == key:
                    return FakeResponse(200, dict(hotspot))
        return FakeResponse(404, error_body(f"Hotspot '{key}' not found"))

    def _project_status(self, params):
        project_key = params.get("projectKey")
        if project_key not in self.projects or project_key in self.missing:
            return FakeResponse(404, error_body(f"Component key '{project_key}' not found"))
        gate = self.projects[project_key].get("gate")
        if gate is None:
            return FakeResponse(200, {"projectStatus": {"status": "NONE", "conditions": []}})
        return FakeResponse(200, {"projectStatus": gate})


def issue(
    key,
    *,
    message="Do not use this pattern.",
    severity="MAJOR",
    type_="CODE_SMELL",
    status="OPEN",
    resolution=None,
    component="proj:src/main.py",
    line=10,
    rule="python:S1234",
    effort="5min",
    tags=None,
    created="2026-01-01T00:00:00+0000",
    author=None,
):
    data = {
        "key": key,
        "message": message,
        "severity": severity,
        "type": type_,
        "status": status,
        "component": component,
        "line": line,
        "rule": rule,
        "effort": effort,
        "tags": tags or [],
        "creationDate": created,
    }
    if resolution:
        data["resolution"] = resolution
    if author:
        data["author"] = author
    return data


def hotspot(
    key,
    *,
    message="Review this hotspot.",
    category="sql-injection",
    probability="HIGH",
    status="TO_REVIEW",
    resolution=None,
    component="proj:src/main.py",
    line=20,
    created="2026-01-01T00:00:00+0000",
):
    data = {
        "key": key,
        "message": message,
        "securityCategory": category,
        "vulnerabilityProbability": probability,
        "status": status,
        "component": component,
        "line": line,
        "creationDate": created,
    }
    if resolution:
        data["resolution"] = resolution
    return data


def gate(status="ERROR", conditions=None):
    return {"status": status, "conditions": conditions or []}
