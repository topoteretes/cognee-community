"""A hand-rolled stand-in for Snyk's REST API, shaped like a ``requests`` session.

``FakeSnykSession.get(url, params=..., headers=..., timeout=...)`` is the only method the
connector calls. It serves orgs, group orgs, projects and issues with JSON:API bodies and
cursor pagination through ``links.next``. Fixtures follow the REST OpenAPI spec of the
Snyk API (versions 2024-10-15 and 2026-03-25, ``GET /orgs/{org_id}/issues`` and friends):
``attributes`` with ``key``, ``title``, ``type``, ``status``, ``ignored``, ``problems``,
``coordinates[].representations[].dependency`` and ``coordinates[].remedies[].meta.data``,
and ``relationships.scan_item``. The spec's own list example is minimal, so the shape of
``problems`` and ``remedies`` in a real list response has not been checked against a live
server.
"""

import json
from urllib.parse import parse_qsl, urlsplit

from requests.structures import CaseInsensitiveDict

REST = "https://api.snyk.io/rest"


class FakeResponse:
    def __init__(self, status_code: int = 200, body=None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = CaseInsensitiveDict(headers or {})

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)  # raises ValueError for non-JSON text
        return self._body


def error_response(status: int, detail: str, headers: dict | None = None) -> FakeResponse:
    body = {"jsonapi": {"version": "1.0"}, "errors": [{"status": str(status), "detail": detail}]}
    return FakeResponse(status, body, headers)


class FakeSnykSession:
    """Serves Snyk's listings from mutable fixtures and records every call.

    ``script`` holds items consumed one per request before the normal answer: a
    ``FakeResponse`` is returned as is, an exception instance is raised.
    ``link_style`` is how ``links.next`` is written: ``absolute``, ``relative`` or ``object``.
    """

    def __init__(self, base: str = REST, page_size: int = 10, link_style: str = "absolute"):
        self.base = base
        self.page_size = page_size
        self.link_style = link_style
        self.version_in_links = True
        self.apply_filters = True
        self.orgs: dict[str, str] = {}  # org id -> name
        self.group_orgs: dict[str, list[str]] = {}  # group id -> org ids
        self.projects: dict[str, dict[str, dict]] = {}  # org id -> project id -> project item
        self.issues: dict[str, list[dict]] = {}  # org id -> issue items
        self.script: list = []
        # Answers for exact paths (like "/orgs/o1/issues/i-1"), checked before the fixtures.
        self.fixed: dict[str, FakeResponse | Exception] = {}
        # Make the issue listing come back empty while the issues can still be fetched by id.
        self.hide_listing = False
        self.calls: list[dict] = []

    # --- fixtures -----------------------------------------------------------
    def add_org(self, org_id: str, name: str = "", projects=(), issues=()) -> None:
        self.orgs[org_id] = name or f"org {org_id}"
        self.projects[org_id] = {p["id"]: p for p in projects}
        self.issues[org_id] = list(issues)

    # --- the one method the connector uses ------------------------------------
    def get(self, url, params=None, headers=None, timeout=None):
        query = list(parse_qsl(urlsplit(url).query)) + list(params or [])
        self.calls.append(
            {"url": url, "params": list(params or []), "headers": headers, "timeout": timeout}
        )
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        path = urlsplit(url).path.removeprefix(urlsplit(self.base).path)
        if path in self.fixed:
            answer = self.fixed[path]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return self._route(path, query)

    def close(self):
        pass

    # --- routing --------------------------------------------------------------
    def _route(self, path: str, query: list[tuple[str, str]]) -> FakeResponse:
        parts = [p for p in path.split("/") if p]
        if parts == ["orgs"]:
            return self._page([self._org(i) for i in sorted(self.orgs)], path, query)
        if len(parts) == 2 and parts[0] == "orgs":
            if parts[1] not in self.orgs:
                return error_response(404, f"Org {parts[1]} not found")
            return FakeResponse(200, {"jsonapi": {"version": "1.0"}, "data": self._org(parts[1])})
        if len(parts) == 3 and parts[0] == "groups" and parts[2] == "orgs":
            if parts[1] not in self.group_orgs:
                return error_response(404, f"Group {parts[1]} not found")
            items = [self._org(i) for i in sorted(self.group_orgs[parts[1]])]
            return self._page(items, path, query)
        if len(parts) == 4 and parts[0] == "orgs" and parts[2] == "issues":
            for issue in self.issues.get(parts[1], []):
                if isinstance(issue, dict) and issue.get("id") == parts[3]:
                    return FakeResponse(200, {"jsonapi": {"version": "1.0"}, "data": issue})
            return error_response(404, f"Issue {parts[3]} not found")
        if len(parts) == 3 and parts[0] == "orgs" and parts[2] in ("projects", "issues"):
            if parts[1] not in self.orgs:
                return error_response(404, f"Org {parts[1]} not found")
            if parts[2] == "projects":
                items = [self.projects[parts[1]][i] for i in sorted(self.projects[parts[1]])]
            else:
                issues = [] if self.hide_listing else self.issues[parts[1]]
                items = self._filter_issues(issues, query)
            return self._page(items, path, query)
        return error_response(404, "No such endpoint")

    def _org(self, org_id: str) -> dict:
        return {"type": "org", "id": org_id, "attributes": {"name": self.orgs[org_id]}}

    def _filter_issues(self, issues: list[dict], query) -> list[dict]:
        if not self.apply_filters:
            return list(issues)
        wanted = {}
        for key, value in query:
            wanted[key] = value
        result = []
        for issue in issues:
            attrs = issue["attributes"]
            if "type" in wanted and attrs.get("type") != wanted["type"]:
                continue
            if "status" in wanted and attrs.get("status") not in wanted["status"].split(","):
                continue
            if wanted.get("ignored") == "false" and attrs.get("ignored"):
                continue
            severities = wanted.get("effective_severity_level")
            if severities and attrs.get("effective_severity_level") not in severities.split(","):
                continue
            scan_item = issue["relationships"]["scan_item"]["data"]["id"]
            if "scan_item.id" in wanted and scan_item != wanted["scan_item.id"]:
                continue
            result.append(issue)
        return result

    def _page(self, items: list[dict], path: str, query) -> FakeResponse:
        values = dict(query)
        start = int(values.get("starting_after", 0))
        size = self.page_size
        chunk = items[start : start + size]
        body: dict = {"jsonapi": {"version": "1.0"}, "data": chunk}
        if start + size < len(items):
            carried = [(k, v) for k, v in query if k != "starting_after"]
            if not self.version_in_links:
                carried = [(k, v) for k, v in carried if k != "version"]
            carried.append(("starting_after", str(start + size)))
            target = "&".join(f"{k}={v}" for k, v in carried)
            if self.link_style == "relative":
                link = f"{urlsplit(self.base).path}{path}?{target}"
            else:
                link = f"{self.base}{path}?{target}"
            body["links"] = {"next": {"href": link} if self.link_style == "object" else link}
        return FakeResponse(200, body)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def make_project(project_id: str, name: str, target_file: str = "package.json") -> dict:
    return {
        "type": "project",
        "id": project_id,
        "attributes": {
            "name": name,
            "target_file": target_file,
            "target_reference": "main",
            "status": "active",
        },
    }


_counter = [0]


def make_issue(
    *,
    project: str = "p1",
    org: str = "o1",
    title: str = "Prototype Pollution in lodash",
    snyk_id: str | None = "SNYK-JS-LODASH-567746",
    cves: tuple[str, ...] = ("CVE-2020-8203",),
    cwes: tuple[str, ...] = ("CWE-1321",),
    package: str | None = "lodash",
    version: str | None = "4.17.15",
    severity: str = "high",
    status: str = "open",
    ignored: bool = False,
    issue_type: str = "package_vulnerability",
    fixed_in: tuple[str, ...] = ("4.17.19",),
    ranges: tuple[str, ...] = ("<4.17.19",),
    remedy: str | None = "Upgrade to lodash@4.17.19 or higher.",
    upgradeable: bool = True,
    score: float | None = 7.4,
    vector: str = "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:H/A:H",
    maturity: str | None = None,
    description: str | None = "Affected versions of lodash are vulnerable to Prototype Pollution.",
    url: str | None = None,
    key: str | None = None,
    updated_at: str = "2026-10-01T10:00:00Z",
    risk: int = 500,
    issue_id: str | None = None,
) -> dict:
    _counter[0] += 1
    problems = [{"id": c, "source": "CVE", "type": "vulnerability"} for c in cves]
    if snyk_id:
        problems.append({"id": snyk_id, "source": "SNYK", "type": "vulnerability"})
    if url and problems:
        problems[-1] = dict(problems[-1], url=url)
    coordinate: dict = {
        "state": status,
        "is_upgradeable": upgradeable,
        "is_patchable": False,
        "is_pinnable": False,
        "created_at": updated_at,
        "updated_at": updated_at,
        "remedies": [
            {
                "type": "indeterminate",
                "description": remedy or "",
                "meta": {
                    "schema_version": "package_vulnerability/v0",
                    "data": {"fixed_in": list(fixed_in), "semver_vulnerable": list(ranges)},
                },
            }
        ],
        "representations": [],
    }
    if package:
        coordinate["representations"] = [
            {"dependency": {"package_name": package, "package_version": version}}
        ]
    attributes: dict = {
        "key": key or f"npm:{package}:{_counter[0]}",
        "title": title,
        "type": issue_type,
        "status": status,
        "ignored": ignored,
        "effective_severity_level": severity,
        "created_at": updated_at,
        "updated_at": updated_at,
        "problems": problems,
        "classes": [{"id": c, "source": "CWE", "type": "weakness"} for c in cwes],
        "coordinates": [coordinate],
        "risk": {"score": {"model": "v4", "value": risk}},
    }
    if description is not None:
        attributes["description"] = description
    if score is not None:
        attributes["severities"] = [
            {
                "level": severity,
                "score": score,
                "source": "NVD",
                "vector": vector,
                "version": "3.1",
                "modification_time": updated_at,
            }
        ]
    if maturity:
        attributes["exploit_details"] = {
            "maturity_levels": [{"format": "CVSSv3", "level": maturity}]
        }
    return {
        "type": "issue",
        "id": issue_id or f"issue-{_counter[0]}",
        "attributes": attributes,
        "relationships": {
            "organization": {"data": {"id": org, "type": "organization"}},
            "scan_item": {"data": {"id": project, "type": "project"}},
        },
    }
