"""Snyk connector for cognee — a ``dlt`` source that turns Snyk findings into memory.

Read vulnerability findings, with their remediation advice, from Snyk's REST API and hand the
resource straight to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_snyk import snyk_source

    await cognee.remember(
        snyk_source(org_ids=["<org id>"], region="eu-01"),   # SNYK_TOKEN from the environment
        dataset_name="snyk",
        primary_key="id",
        write_disposition="merge",   # matches how the resource is declared
        max_rows_per_table=0,        # 0 = no row cap (the default cap is 50)
    )

Design
------
* **Plan** — the Snyk REST API is only open to Enterprise plans. Free, Team and trial personal
  tokens get HTTP 403. Tokens are region specific (US-01, US-02, EU-01, AU-01).
* **Documents** — one text document per vulnerability per organization. The same
  vulnerability found in many projects is listed once, with every affected package and
  project, every CVE and CWE id, and the fix versions (the "dedupe by CVE" requirement).
  The key is the Snyk vulnerability id, then the first CVE id; a finding with neither stays a
  document of its own. No timestamp, risk score or counter is ever rendered.
* **Privacy by default** — package vulnerabilities only, open and not ignored, no code flows,
  no ``code``/``secrets`` issues, no ignore reasons, no risk scores.
* **A failed listing aborts the run** — any error, including an exhausted 429 or wait budget,
  raises before the first row. Unlike the Asana and Apollo connectors the run does not stop
  early and keep what it has: a document aggregates findings from many projects, so a partial
  listing would overwrite a complete document with an incomplete one.

* **Incremental sync** — every run lists all in-scope findings and renders every document,
  but only a document whose hash (title, content, url) changed is emitted. Nothing relies on
  a timestamp: ``created_at`` (the REST stand-in for ``introducedDate``) never moves when a
  finding is fixed or ignored, and ``updated_at`` is not known to. The state keeps, per
  organization, the hash and a few issue ids of each document.
* **Forget-on-delete** — a document with no in-scope finding left (fixed, ignored, project
  deleted, out of selection) is emitted as ``{"id": ..., "_deleted": True}``. Tombstones are
  yielded before the upserts and the state is saved after the last row.
* **Guard** — Snyk may silently leave out what the token cannot read. A bulk deletion (an
  empty listing, or at least 5 documents and more than half of an organization's) is first
  checked against up to 5 of the findings by id, and refused unless they are really gone or
  no longer match the filters. ``allow_mass_delete=True`` skips the check.
"""

import hashlib
import os
import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("snyk_connector")

# Hosts of the four Snyk regions (https://docs.snyk.io/snyk-data-and-governance/
# regional-hosting-and-data-residency). The REST API lives under "/rest".
REGION_HOSTS = {
    "us-01": "https://api.snyk.io",
    "us-02": "https://api.us.snyk.io",
    "eu-01": "https://api.eu.snyk.io",
    "au-01": "https://api.au.snyk.io",
}
DEFAULT_REGION = "us-01"
DEFAULT_API_VERSION = "2024-10-15"  # the version Snyk's docs recommend

# Enums from the REST OpenAPI spec (GET /orgs/{org_id}/issues, versions 2024-10-15 and
# 2026-03-25). `code` and `secrets` are valid for Snyk but carry source locations, so this
# connector refuses them.
ISSUE_TYPES = ("package_vulnerability", "license", "cloud", "code", "custom", "config", "secrets")
REFUSED_ISSUE_TYPES = frozenset({"code", "secrets"})
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
API_VERSION_PATTERN = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(~(beta|experimental))?$"
)
REGION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

SNYK_ID = re.compile(r"^SNYK-[A-Za-z0-9_-]+$")
CVE_ID = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)
CWE_ID = re.compile(r"^CWE-\d+$", re.IGNORECASE)

ID_PREFIX = "snyk:"
PAGE_SIZE = 100  # the spec's maximum for `limit` (minimum 10, a multiple of 10)
MAX_PAGES = 1000

# Same numbers as the Asana connector: five attempts with 1, 2, 4, 8 s between them (or the
# Retry-After header), no single wait over 60 s, no more than 120 s of waiting in a run.
MAX_ATTEMPTS = 5
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_WAIT_SECONDS = 60
MAX_TOTAL_WAIT_SECONDS = 120

# Forgetting at least this many documents of an organization, and more than half of the stored
# ones, is checked against Snyk before it happens (unless allow_mass_delete=True).
MASS_DELETE_MINIMUM = 5
MAX_SPOT_CHECKS = 5  # issues asked for by id when a deletion looks suspicious
MAX_ORG_CHECKS = 20  # organizations asked for by id per run when a dynamic selection drops one
SAMPLE_ISSUES = 3  # issue ids kept per document, for those spot-checks

DESCRIPTION_LIMIT = 800
REMEDY_LIMIT = 500
MAX_REMEDIES = 5
ERROR_DETAIL_LIMIT = 200

# Replaced in tests so retries do not wait.
_sleep = time.sleep


@dataclass(frozen=True)
class _SnykConfig:
    rest_base: str
    region: str
    org_ids: tuple[str, ...]
    group_id: str | None
    all_orgs: bool
    project_ids: tuple[str, ...]
    severities: tuple[str, ...]
    issue_types: tuple[str, ...]
    include_resolved: bool
    include_ignored: bool
    include_remedies: bool
    max_projects_per_document: int
    max_issues_per_org: int
    api_version: str
    timeout: float
    allow_mass_delete: bool = False


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class _Client:
    """One run's access to Snyk: the session, the headers, the host's check and the waits."""

    def __init__(
        self,
        session: Any,
        token: str,
        check_active: Callable[[], None] | None = None,
        timeout: float = 60.0,
    ):
        self.session = session
        self.headers = {"Authorization": f"token {token}", "Accept": "application/vnd.api+json"}
        self.check_active = check_active
        self.timeout = timeout
        self.waited = 0.0  # seconds slept this run
        self._secret = token  # only to cut it out of Snyk's error messages

    def __repr__(self) -> str:
        return "_Client(token=<redacted>)"

    def check(self) -> None:
        if self.check_active is not None:
            self.check_active()

    def redact(self, text: str) -> str:
        """``text`` with the token cut out. Too short a token drops the text instead."""
        if not self._secret or self._secret not in text:
            return text
        return text.replace(self._secret, "[token]") if len(self._secret) >= 8 else ""

    def wait(self, seconds: float, what: str) -> None:
        """Sleep, or raise when the wait would break the single-wait cap or the run budget."""
        if seconds > MAX_WAIT_SECONDS or self.waited + seconds > MAX_TOTAL_WAIT_SECONDS:
            raise RuntimeError(
                f"Snyk asked to wait {seconds:.0f}s ({what}), more than this run allows "
                f"({MAX_WAIT_SECONDS}s at once, {MAX_TOTAL_WAIT_SECONDS}s in all). Nothing was "
                "changed; try again later."
            )
        self.check()
        self.waited += seconds
        _sleep(seconds)


def _make_session() -> Any:
    import requests

    return requests.Session()


def _check_token(token: str) -> None:
    """Refuse a token ``requests`` would reject, without ever echoing it.

    A pasted token often ends in a newline; requests then raises an error whose message
    holds the whole header, token included.
    """
    if any(char.isspace() or not char.isprintable() for char in token):
        raise ValueError("The Snyk token contains whitespace or control characters.")


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds before a retry: ``Retry-After`` when Snyk sends it, else 1, 2, 4, 8 plus jitter."""
    try:
        return float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt) + random.uniform(0, 1)


def _error_detail(response: Any, client: _Client) -> str:
    """Snyk's JSON:API ``errors[0].detail`` (with its status), token removed. Never the raw body."""
    try:
        error = response.json()["errors"][0]
        detail = error.get("detail") or error.get("title") or ""
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return ""
    if not isinstance(detail, str):
        return ""
    return client.redact(detail[:ERROR_DETAIL_LIMIT])


def _status_error(response: Any, client: _Client, path: str, what: str | None) -> RuntimeError:
    status = response.status_code
    detail = _error_detail(response, client)
    suffix = f" Snyk said: {detail}" if detail else ""
    if status == 401:
        return RuntimeError(
            "Snyk rejected the API token (HTTP 401): it is invalid or expired, or it belongs to "
            "another region. Tokens are region specific: set region (us-01, us-02, eu-01, au-01) "
            f"or base_url to the region of your organization.{suffix}"
        )
    if status == 403:
        return RuntimeError(
            "Snyk denied access (HTTP 403). The Snyk REST API requires an Enterprise plan: "
            "tokens of Free and Team plans, and personal tokens of trials, cannot call it. With "
            "an Enterprise plan the token also needs the permissions org.read, "
            "org.project.read and org.project.snapshot.read (group.issues.read for a group); "
            f"a service account with a Group Viewer role has them.{suffix}"
        )
    if status == 404:
        return RuntimeError(
            f"Snyk could not find {what or path} (HTTP 404). Check the id and the region.{suffix}"
        )
    return RuntimeError(f"Snyk returned HTTP {status} for {path}.{suffix}")


def _request(client: _Client, url: str, params: list[tuple[str, str]] | None = None) -> Any:
    """GET ``url``; return the first answer that is not transient (429/5xx are retried)."""
    import requests

    path = urlsplit(url).path
    for attempt in range(MAX_ATTEMPTS):
        client.check()
        try:
            response = client.session.get(
                url, params=params or [], headers=client.headers, timeout=client.timeout
            )
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Snyk ({type(exc).__name__}). Check the region or base_url: "
                "us-01 api.snyk.io, us-02 api.us.snyk.io, eu-01 api.eu.snyk.io, "
                "au-01 api.au.snyk.io."
            ) from None
        status = response.status_code
        if status in TRANSIENT_STATUSES:
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(
                    f"Snyk still answered HTTP {status} for {path} after {MAX_ATTEMPTS} attempts."
                )
            delay = _retry_delay(response.headers, attempt)
            logger.warning("Snyk: HTTP %s on %s; retrying in %.0fs.", status, path, delay)
            client.wait(delay, f"HTTP {status}")
            continue
        return response
    raise AssertionError("unreachable")  # pragma: no cover


def _body(response: Any, path: str) -> dict:
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(
            f"Snyk answered {path} with something that is not JSON. Check base_url."
        ) from None
    if not isinstance(body, dict):
        raise RuntimeError(f"Snyk answered {path} with an unexpected body.")
    return body


def _get_json(
    client: _Client,
    url: str,
    params: list[tuple[str, str]] | None = None,
    what: str | None = None,
) -> dict:
    """GET ``url`` and return its JSON body. Any non-2xx answer raises."""
    response = _request(client, url, params)
    path = urlsplit(url).path
    if not 200 <= response.status_code < 300:
        raise _status_error(response, client, path, what)
    return _body(response, path)


def _probe(
    client: _Client, url: str, params: list[tuple[str, str]] | None = None
) -> tuple[int, dict | None]:
    """GET ``url`` and return ``(status, body)`` without raising on 4xx.

    The body is only read for a 2xx answer. Used to ask "does this still exist?", where a
    404 or 403 is an answer and not an error.
    """
    response = _request(client, url, params)
    status = response.status_code
    body = _body(response, urlsplit(url).path) if 200 <= status < 300 else None
    return status, body


def _next_url(body: dict, config: _SnykConfig) -> str | None:
    """The absolute URL of the next page, or None on the last one.

    The spec's ``links.next`` is a string or ``{"href": ...}``. Its example is absolute; a
    relative link is resolved against the REST base. A link to another host is refused, and
    the ``version`` query parameter is added when the link lacks it.
    """
    links = body.get("links")
    link = links.get("next") if isinstance(links, dict) else None
    if isinstance(link, dict):
        link = link.get("href")
    if not link or not isinstance(link, str):
        return None
    base, target = urlsplit(config.rest_base), urlsplit(link)
    if target.scheme or target.netloc:
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            raise RuntimeError("Snyk returned a pagination link to another host; not following it.")
        path = target.path
    else:
        path = target.path if target.path.startswith("/") else "/" + target.path
        if path != base.path and not path.startswith(base.path + "/"):
            path = base.path + path
    query = target.query
    if "version" not in dict(parse_qsl(query)):
        query = (
            f"{query}&version={config.api_version}" if query else f"version={config.api_version}"
        )
    return urlunsplit((base.scheme, base.netloc, path, query, ""))


def _paginate(
    client: _Client,
    config: _SnykConfig,
    path: str,
    params: list[tuple[str, str]] | None = None,
    what: str | None = None,
) -> Iterator[dict]:
    """Yield every item of a listing, following ``links.next``."""
    url: str | None = f"{config.rest_base}{path}"
    first = [("version", config.api_version), ("limit", str(PAGE_SIZE)), *(params or [])]
    seen: set[str] = set()
    for page in range(MAX_PAGES):
        body = _get_json(client, url, first if page == 0 else None, what)
        data = body.get("data")
        yield from (item for item in data or [] if isinstance(item, dict))
        url = _next_url(body, config)
        if url is None:
            return
        if url in seen:
            raise RuntimeError(f"Snyk's pagination for {path} loops; stopping.")
        seen.add(url)
    raise RuntimeError(f"Snyk {path}: more than {MAX_PAGES} pages; narrow the selection.")


# ---------------------------------------------------------------------------
# Selection: organizations, projects and issues
# ---------------------------------------------------------------------------
def _org_entry(item: dict) -> dict:
    attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    return {"id": str(item.get("id") or ""), "name": attributes.get("name") or ""}


def _select_orgs(client: _Client, config: _SnykConfig) -> list[dict]:
    """The organizations to read: explicit ids, a group's, or every readable one."""
    orgs: dict[str, dict] = {}
    for org_id in config.org_ids:
        body = _get_json(
            client,
            f"{config.rest_base}/orgs/{org_id}",
            [("version", config.api_version)],
            what=f"organization {org_id}",
        )
        data = body.get("data")
        orgs[org_id] = _org_entry(data if isinstance(data, dict) else {"id": org_id})
        orgs[org_id]["id"] = org_id
    if config.group_id:
        path = f"/groups/{config.group_id}/orgs"
        for item in _paginate(client, config, path, what=f"group {config.group_id}"):
            entry = _org_entry(item)
            if entry["id"]:
                orgs[entry["id"]] = entry
    if config.all_orgs:
        for item in _paginate(client, config, "/orgs"):
            entry = _org_entry(item)
            if entry["id"]:
                orgs[entry["id"]] = entry
    return [orgs[org_id] for org_id in sorted(orgs)]


def _list_projects(client: _Client, config: _SnykConfig, org_id: str) -> dict[str, dict]:
    """``project id -> {name, target_file}`` for an organization."""
    projects: dict[str, dict] = {}
    for item in _paginate(
        client, config, f"/orgs/{org_id}/projects", what=f"organization {org_id}"
    ):
        attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        if item.get("id"):
            projects[str(item["id"])] = {
                "name": attributes.get("name") or "",
                "target_file": attributes.get("target_file") or "",
            }
    return projects


def _issue_params(
    config: _SnykConfig, issue_type: str, project_id: str | None
) -> list[tuple[str, str]]:
    """Query filters for one issue listing. ``status`` and severities are comma lists."""
    statuses = ["open", "resolved"] if config.include_resolved else ["open"]
    params = [("type", issue_type), ("status", ",".join(statuses))]
    if not config.include_ignored:
        params.append(("ignored", "false"))
    if config.severities:
        params.append(("effective_severity_level", ",".join(config.severities)))
    if project_id:
        params += [("scan_item.id", project_id), ("scan_item.type", "project")]
    return params


def _list_issues(
    client: _Client, config: _SnykConfig, org_id: str, project_ids: list[str] | None
) -> list[dict]:
    """Every issue of an organization that matches the filters. Too many raises."""
    issues: list[dict] = []
    for issue_type in config.issue_types:
        for project_id in project_ids or [None]:
            params = _issue_params(config, issue_type, project_id)
            for item in _paginate(
                client, config, f"/orgs/{org_id}/issues", params, f"organization {org_id}"
            ):
                issues.append(item)
                if len(issues) > config.max_issues_per_org:
                    raise RuntimeError(
                        f"Snyk organization {org_id} has more than {config.max_issues_per_org} "
                        "matching issues. Narrow the selection (project_ids, severities, "
                        "issue_types) or raise max_issues_per_org."
                    )
    return issues


# ---------------------------------------------------------------------------
# Reading an issue
# ---------------------------------------------------------------------------
def _attributes(issue: dict) -> dict:
    attributes = issue.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _project_id(issue: dict) -> str:
    relation = (issue.get("relationships") or {}).get("scan_item")
    data = relation.get("data") if isinstance(relation, dict) else None
    return str(data.get("id")) if isinstance(data, dict) and data.get("id") else "unknown"


def _clean_ids(entries: Any, key: str = "id") -> list[str]:
    return [
        entry[key].strip()
        for entry in entries or []
        if isinstance(entry, dict) and isinstance(entry.get(key), str) and entry[key].strip()
    ]


def _identifiers(issue: dict) -> tuple[list[str], list[str], list[str]]:
    """Sorted (Snyk ids, CVE ids, CWE ids) of an issue, told apart by their pattern.

    The spec does not enumerate ``problems[].source`` (its example is "CVE"), so the ids are
    classified by what they look like, not by their source.
    """
    attributes = _attributes(issue)
    ids = _clean_ids(attributes.get("problems")) + _clean_ids(attributes.get("classes"))
    snyk = {i for i in ids if SNYK_ID.match(i)}
    cves = {i.upper() for i in ids if CVE_ID.match(i)}
    cwes = {i.upper() for i in ids if CWE_ID.match(i)}
    return sorted(snyk), sorted(cves), sorted(cwes)


def _document_key(issue: dict, project_id: str) -> str:
    """The grouping key: Snyk id, else first CVE, else the single occurrence."""
    snyk, cves, _ = _identifiers(issue)
    if snyk:
        return f"vuln:{snyk[0]}"
    if cves:
        return f"vuln:{cves[0]}"
    return f"issue:{project_id}:{_attributes(issue).get('key') or issue.get('id') or 'unknown'}"


def _fix_label(coordinate: dict) -> str:
    for flag, label in (
        ("is_upgradeable", "upgrade"),
        ("is_patchable", "patch"),
        ("is_pinnable", "pin"),
        ("is_fixable_manually", "manual"),
    ):
        if coordinate.get(flag):
            return label
    return "none"


def _occurrences(issue: dict, project_id: str, stats: dict) -> list[dict]:
    """One entry per package a finding sits in: package, version, state, fix and remedies."""
    attributes = _attributes(issue)
    found: list[dict] = []
    for coordinate in attributes.get("coordinates") or []:
        if not isinstance(coordinate, dict):
            stats["skipped_coordinates"] += 1
            continue
        dependency = None
        for representation in coordinate.get("representations") or []:
            if isinstance(representation, dict) and isinstance(
                representation.get("dependency"), dict
            ):
                dependency = representation["dependency"]
                break
        if dependency is None:
            stats["skipped_coordinates"] += 1
            continue
        fixed_in: list[str] = []
        ranges: list[str] = []
        descriptions: list[str] = []
        for remedy in coordinate.get("remedies") or []:
            if not isinstance(remedy, dict):
                continue
            data = (remedy.get("meta") or {}).get("data") or {}
            if isinstance(data, dict):
                fixed_in += [str(v) for v in data.get("fixed_in") or []]
                ranges += [str(v) for v in data.get("semver_vulnerable") or []]
            if isinstance(remedy.get("description"), str):
                descriptions.append(remedy["description"])
        found.append(
            {
                "project": project_id,
                "package": dependency.get("package_name") or None,
                "version": dependency.get("package_version") or None,
                # The issue's own status is what the listing filters on; the coordinate's
                # state is the fallback.
                "state": attributes.get("status") or coordinate.get("state") or "open",
                "ignored": bool(attributes.get("ignored")),
                "fix": _fix_label(coordinate),
                "fixed_in": fixed_in,
                "ranges": ranges,
                "remedies": descriptions,
            }
        )
    if not found:
        found.append(
            {
                "project": project_id,
                "package": None,
                "version": None,
                "state": attributes.get("status") or "open",
                "ignored": bool(attributes.get("ignored")),
                "fix": "none",
                "fixed_in": [],
                "ranges": [],
                "remedies": [],
            }
        )
    return found


def _wanted(config: _SnykConfig, issue: dict) -> bool:
    """The API filters should already have done this; the connector checks again."""
    attributes = _attributes(issue)
    resolved = attributes.get("status") == "resolved" and not config.include_resolved
    ignored = attributes.get("ignored") and not config.include_ignored
    return not (resolved or ignored)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _clip(value: Any, limit: int) -> str:
    """One-line text, cut to ``limit`` characters."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _version_key(version: str) -> list[tuple[int, Any]]:
    """Sort key that puts 4.17.9 before 4.17.10."""
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.\-+]", version) if p]


def _severity(issues: list[dict]) -> str | None:
    """Highest effective severity, with the highest CVSS score and its vector."""
    levels = [
        _attributes(i).get("effective_severity_level")
        for i in issues
        if _attributes(i).get("effective_severity_level") in SEVERITY_RANK
    ]
    scored: list[tuple[float, str]] = []
    for issue in issues:
        for entry in _attributes(issue).get("severities") or []:
            if isinstance(entry, dict) and isinstance(entry.get("score"), int | float):
                scored.append((float(entry["score"]), str(entry.get("vector") or "")))
    if not levels and not scored:
        return None
    level = max(levels, key=SEVERITY_RANK.__getitem__) if levels else "unknown"
    if not scored:
        return level
    score, vector = max(scored)
    return f"{level} (CVSS {score:g}{', ' + vector if vector else ''})"


def _exploit_maturity(issues: list[dict]) -> list[str]:
    levels = set()
    for issue in issues:
        details = _attributes(issue).get("exploit_details")
        for entry in (details.get("maturity_levels") if isinstance(details, dict) else None) or []:
            if isinstance(entry, dict) and isinstance(entry.get("level"), str):
                levels.add(entry["level"])
    return sorted(levels)


def _affected_lines(entries: list[dict], projects: dict, config: _SnykConfig) -> list[str]:
    """Affected packages and projects, sorted, for at most max_projects_per_document projects."""

    def project_name(project_id: str) -> str:
        return projects.get(project_id, {}).get("name") or project_id

    unique = {
        (
            project_name(e["project"]),
            e["package"] or "",
            e["version"] or "",
            e["state"],
            e["ignored"],
            e["fix"],
            e["project"],
        )
        for e in entries
    }
    ordered = sorted(unique, key=lambda u: (u[0], u[1], _version_key(u[2]), u[3], u[4], u[5], u[6]))
    names = list(dict.fromkeys(u[0] for u in ordered))
    shown = set(names[: config.max_projects_per_document])
    lines = []
    for name, package, version, state, ignored, fix, project_id in ordered:
        if name not in shown:
            continue
        subject = f"{package}@{version}" if package and version else package or "(package unknown)"
        target = projects.get(project_id, {}).get("target_file")
        where = f"{name} ({target})" if target else name
        status = f"{state}, ignored" if ignored else state
        lines.append(f"- {subject} in project {where}, status {status}, fix: {fix}")
    if len(names) > len(shown):
        lines.append(f"- and {len(names) - len(shown)} more projects")
    return lines


def _remediation(entries: list[dict]) -> tuple[list[str], dict[str, list[str]]]:
    """Remediation lines, and the fix versions per package (for the lineage)."""
    fixes: dict[str, set[str]] = {}
    ranges: dict[str, set[str]] = {}
    for entry in entries:
        package = entry["package"] or ""
        fixes.setdefault(package, set()).update(entry["fixed_in"])
        ranges.setdefault(package, set()).update(entry["ranges"])
    lines = []
    fixed_by_package = {}
    for package in sorted(fixes):
        if not fixes[package]:
            continue
        versions = sorted(fixes[package], key=_version_key)
        fixed_by_package[package] = versions
        line = f"- Fixed in {package or 'the package'} {', '.join(versions)}"
        if ranges[package]:
            line += f" (vulnerable ranges: {'; '.join(sorted(ranges[package]))})"
        lines.append(line + ".")
    descriptions = sorted({_clip(d, REMEDY_LIMIT) for e in entries for d in e["remedies"] if d})
    lines += [f"- {d}" for d in descriptions[:MAX_REMEDIES]]
    return lines, fixed_by_package


def _lineage(
    key: str, cves: list[str], entries: list[dict], projects: dict, fixes: dict, config: _SnykConfig
) -> str:
    label = f"{key} ({', '.join(cves)})" if cves else key
    sentences = []
    seen = set()
    for entry in sorted(
        entries, key=lambda e: (e["package"] or "", e["version"] or "", e["project"])
    ):
        name = projects.get(entry["project"], {}).get("name") or entry["project"]
        subject = (
            f"{entry['package']}@{entry['version']}"
            if entry["package"] and entry["version"]
            else entry["package"]
        )
        if not subject or (subject, name) in seen:
            continue
        seen.add((subject, name))
        sentences.append(
            f"Vulnerability {label} affects {subject}, which is used by project {name}."
        )
        if len(seen) >= config.max_projects_per_document:
            break
    if config.include_remedies:
        for package, versions in fixes.items():
            if package:
                sentences.append(f"Upgrading {package} to {', '.join(versions)} fixes {key}.")
    return "Lineage: " + " ".join(sentences) if sentences else ""


def _render_document(
    config: _SnykConfig, org: dict, key: str, issues: list[dict], projects: dict, stats: dict
) -> dict:
    """The row for one vulnerability, built from every finding that shares its key."""
    entries = [e for issue in issues for e in _occurrences(issue, _project_id(issue), stats)]
    identifiers = [_identifiers(issue) for issue in issues]
    snyk_ids = sorted({i for ids in identifiers for i in ids[0]})
    cves = sorted({i for ids in identifiers for i in ids[1]})
    cwes = sorted({i for ids in identifiers for i in ids[2]})
    titles = sorted({t for i in issues if (t := _attributes(i).get("title"))})
    title = titles[0] if titles else key
    descriptions = sorted({d for i in issues if (d := _attributes(i).get("description"))})
    types = sorted({str(_attributes(i).get("type") or "").replace("_", " ") for i in issues} - {""})
    urls = sorted(
        {
            p["url"]
            for i in issues
            for p in _attributes(i).get("problems") or []
            if isinstance(p, dict) and isinstance(p.get("url"), str) and p["url"]
        }
    )

    display = key.split(":", 1)[1] if key.startswith("vuln:") else ""
    lines = [f"Snyk vulnerability: {title}"]
    if snyk_ids:
        lines.append(f"Snyk id: {', '.join(snyk_ids)}")
    if cves:
        lines.append(f"CVE: {', '.join(cves)}")
    if cwes:
        lines.append(f"Weakness: {', '.join(cwes)}")
    if types:
        lines.append(f"Type: {', '.join(types)}")
    severity = _severity(issues)
    if severity:
        lines.append(f"Severity: {severity}")
    maturity = _exploit_maturity(issues)
    if maturity:
        lines.append(f"Exploit maturity: {', '.join(maturity)}")
    lines.append(f"Organization: {org['name'] or org['id']} ({config.region})")
    if descriptions:
        lines.append(f"Description: {_clip(descriptions[0], DESCRIPTION_LIMIT)}")
    lines += ["", "Affected packages and projects:", *_affected_lines(entries, projects, config)]
    fixes: dict[str, list[str]] = {}
    if config.include_remedies:
        remedy_lines, fixes = _remediation(entries)
        if remedy_lines:
            lines += ["", "Remediation:", *remedy_lines]
    lineage = _lineage(display or title, cves, entries, projects, fixes, config)
    if lineage:
        lines += ["", lineage]

    row = {
        "id": f"{ID_PREFIX}{config.region}:{org['id']}:{key}",
        "title": f"Snyk vulnerability {display}: {title}"
        if display
        else f"Snyk vulnerability: {title}",
        "content": "\n".join(lines),
        "_deleted": False,
    }
    if urls:
        row["url"] = urls[0]
    return row


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _org_documents(
    config: _SnykConfig, org: dict, issues: list[dict], projects: dict, stats: dict
) -> dict[str, dict]:
    """Group an organization's issues by vulnerability and render one document per group.

    Returns ``{document id: {"row", "hash", "issues"}}``. The hash covers the title, the
    content and the url, none of which holds a timestamp, risk score or counter, so a change
    in a hash is a change a reader would see. ``issues`` is a sorted sample of up to
    ``SAMPLE_ISSUES`` Snyk issue ids, kept in the state to double-check a deletion later.
    """
    groups: dict[str, list[dict]] = {}
    for issue in issues:
        if not _wanted(config, issue):
            stats["skipped_issues"] += 1
            continue
        groups.setdefault(_document_key(issue, _project_id(issue)), []).append(issue)
    stats["fallback_documents"] += sum(1 for key in groups if key.startswith("issue:"))
    documents: dict[str, dict] = {}
    for key in sorted(groups):
        row = _render_document(config, org, key, groups[key], projects, stats)
        text = "\n".join((row["title"], row["content"], row.get("url", "")))
        sample = sorted({str(i["id"]) for i in groups[key] if i.get("id")})[:SAMPLE_ISSUES]
        documents[row["id"]] = {"row": row, "hash": _digest(text), "issues": sample}
    return documents


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
def _matches_filters(config: _SnykConfig, issue: dict) -> bool:
    """Whether an issue fetched by id is one the current selection would list."""
    attributes = _attributes(issue)
    statuses = {"open", "resolved"} if config.include_resolved else {"open"}
    if attributes.get("status") not in statuses:
        return False
    if attributes.get("ignored") and not config.include_ignored:
        return False
    if config.severities and attributes.get("effective_severity_level") not in config.severities:
        return False
    if attributes.get("type") not in config.issue_types:
        return False
    return not config.project_ids or _project_id(issue) in config.project_ids


def _refusal(reason: str) -> RuntimeError:
    return RuntimeError(
        f"{reason} Snyk may silently leave out what the token cannot read, so this can be a "
        "missing permission, a wrong selection or an unreliable listing, and not a real "
        "deletion. Nothing was forgotten and the saved state is unchanged. Set "
        "allow_mass_delete=True only if those findings are really gone."
    )


def _check_deletions(
    client: _Client,
    config: _SnykConfig,
    org_id: str,
    stored: dict,
    desired: dict,
    candidates: list[str],
    stats: dict,
) -> None:
    """Double-check a suspicious bulk deletion before it happens, or raise.

    Suspicious means the listing is empty while documents are stored, or at least
    MASS_DELETE_MINIMUM documents and more than half of the stored ones would go. Up to
    MAX_SPOT_CHECKS of the findings behind the doomed documents are then fetched by id: a 404
    or a finding the current filters would not list confirms the deletion; a finding that
    matches the filters means the listing left out something live; any other answer is
    ambiguous. A small deletion is trusted without a check.
    """
    if config.allow_mass_delete or not candidates:
        return
    emptied = not desired and bool(stored)
    mass = len(candidates) >= MASS_DELETE_MINIMUM and len(candidates) > len(stored) / 2
    if not (emptied or mass):
        return
    reason = (
        f"Snyk organization {org_id} returned no matching findings, but earlier runs saw some."
        if emptied
        else f"{len(candidates)} of {len(stored)} known vulnerabilities of organization "
        f"{org_id} would be forgotten."
    )
    sample = [
        (doc_id, stored[doc_id]["issues"][0])
        for doc_id in sorted(candidates)
        if stored[doc_id].get("issues")
    ][:MAX_SPOT_CHECKS]
    if not sample:
        raise _refusal(f"{reason} There is no finding to check it against.")
    for _doc_id, issue_id in sample:
        stats["spot_checks"] += 1
        status, body = _probe(
            client,
            f"{config.rest_base}/orgs/{org_id}/issues/{issue_id}",
            [("version", config.api_version)],
        )
        if status == 404:
            continue
        if status == 200:
            data = (body or {}).get("data")
            if isinstance(data, dict) and _matches_filters(config, data):
                raise _refusal(f"{reason} A finding that should have been listed still exists.")
            continue
        raise _refusal(f"{reason} Snyk answered HTTP {status} when one of them was checked.")


def _settle_absent_orgs(
    client: _Client, config: _SnykConfig, stored: dict, selected: set[str], stats: dict
) -> tuple[list[str], dict]:
    """Decide what happens to stored organizations this run did not select.

    Returns the document ids to forget and the state entries to keep. With an explicit
    ``org_ids`` list an absent organization was deselected: it is forgotten without a
    request. With ``all_orgs`` or ``group_id`` the organization may only be missing from the
    listing, so it is fetched by id: 404 forgets it, 200 raises (the listing is inconsistent),
    anything else keeps it. At most MAX_ORG_CHECKS are asked per run.
    """
    forgotten: list[str] = []
    kept: dict = {}
    dynamic = bool(config.all_orgs or config.group_id)
    prefix = f"{config.region}:"
    checks = 0
    for key in sorted(stored):
        org_id = key[len(prefix) :]
        if not key.startswith(prefix) or org_id in selected:
            continue
        docs = list(stored[key].get("docs", {}))
        if not dynamic:
            forgotten += docs
            continue
        if checks >= MAX_ORG_CHECKS:
            logger.warning("Snyk: too many organizations to check in one run; keeping the rest.")
            kept[key] = stored[key]
            stats["kept"] += len(docs)
            continue
        checks += 1
        status, _ = _probe(
            client, f"{config.rest_base}/orgs/{org_id}", [("version", config.api_version)]
        )
        if status == 404:
            forgotten += docs
        elif 200 <= status < 300:
            raise _refusal(
                f"Organization {org_id} is missing from Snyk's organization listing, but it "
                "still exists."
            )
        else:
            logger.warning(
                "Snyk: organization %s could not be checked (HTTP %s); keeping its documents.",
                org_id,
                status,
            )
            kept[key] = stored[key]
            stats["kept"] += len(docs)
    return forgotten, kept


def _iter_rows(
    client: _Client, config: _SnykConfig, state: dict, stats: dict | None = None
) -> Iterator[dict]:
    """Yield tombstones for vulnerabilities that are gone, then rows for new or changed ones.

    ``state`` is a plain dict standing in for dlt's resource state. Per organization it keeps
    the hash (and a few issue ids) of every document. Every request, all rendering, the
    diff and the deletion checks happen before the first yield, and the state is only
    written after the last one, so a failed or interrupted run changes nothing.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(
        orgs=0,
        projects=0,
        issues=0,
        vulnerabilities=0,
        emitted=0,
        unchanged=0,
        deleted=0,
        kept=0,
        spot_checks=0,
        skipped_issues=0,
        skipped_coordinates=0,
        fallback_documents=0,
        sparse_issues=0,
    )

    stored_orgs: dict = state.get("orgs") or {}
    prefix = f"{config.region}:"
    # Organizations of another region are not this run's business: carry them over.
    new_orgs = {k: v for k, v in stored_orgs.items() if not k.startswith(prefix)}
    forgotten: list[str] = []
    upserts: dict[str, dict] = {}
    found_projects: set[str] = set()

    selected = _select_orgs(client, config)
    for org in selected:
        stats["orgs"] += 1
        stored_docs: dict = dict((stored_orgs.get(prefix + org["id"]) or {}).get("docs") or {})
        projects = _list_projects(client, config, org["id"])
        stats["projects"] += len(projects)
        scope = None
        issues: list[dict] = []
        if config.project_ids:
            scope = [p for p in config.project_ids if p in projects]
            found_projects.update(scope)
        if not config.project_ids or scope:
            issues = _list_issues(client, config, org["id"], scope)
        stats["issues"] += len(issues)
        stats["sparse_issues"] += sum(
            1
            for i in issues
            if not (_attributes(i).get("problems") and _attributes(i).get("coordinates"))
        )
        if not issues:
            logger.warning("Snyk organization %s returned no matching issues.", org["id"])

        desired = _org_documents(config, org, issues, projects, stats)
        stats["vulnerabilities"] += len(desired)
        changed = [
            d["row"]
            for doc_id, d in desired.items()
            if stored_docs.get(doc_id, {}).get("hash") != d["hash"]
        ]
        stats["unchanged"] += len(desired) - len(changed)
        candidates = sorted(set(stored_docs) - set(desired))
        _check_deletions(client, config, org["id"], stored_docs, desired, candidates, stats)
        forgotten += candidates
        upserts.update({row["id"]: row for row in changed})
        new_orgs[prefix + org["id"]] = {
            "docs": {i: {"hash": d["hash"], "issues": d["issues"]} for i, d in desired.items()}
        }

    missing = sorted(set(config.project_ids) - found_projects)
    if missing:
        raise RuntimeError(
            f"Snyk projects not found in the selected organizations: {', '.join(missing)}."
        )
    absent_forgotten, kept_orgs = _settle_absent_orgs(
        client, config, stored_orgs, {o["id"] for o in selected}, stats
    )
    forgotten += absent_forgotten
    new_orgs.update(kept_orgs)
    if stats["sparse_issues"]:
        logger.warning(
            "Snyk returned %d issues without `problems` or `coordinates`. Vulnerabilities may "
            "not be told apart across projects and remediation may be incomplete (Snyk's list "
            "responses can leave these out).",
            stats["sparse_issues"],
        )

    tombstones = sorted(set(forgotten))
    stats["emitted"] = len(upserts)
    stats["deleted"] = len(tombstones)
    logger.info("Snyk: sync stats %s", stats)
    for document_id in tombstones:
        yield {"id": document_id, "_deleted": True}
    for document_id in sorted(upserts):
        yield upserts[document_id]
    state["orgs"] = new_orgs


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _resolve_list(value: list[str] | str | None, env_name: str, label: str) -> tuple[str, ...]:
    """Explicit argument first, then the environment. An explicit empty list is an error."""
    if value is None:
        value = [item for item in os.getenv(env_name, "").split(",") if item.strip()] or None
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    cleaned = tuple(dict.fromkeys(item.strip() for item in value if item and item.strip()))
    if not cleaned:
        raise ValueError(f"{label} is empty. Leave it out to use the default.")
    return cleaned


def _choice_list(values: Any, allowed: Any, label: str) -> tuple[str, ...]:
    cleaned = _resolve_list(list(values) if values is not None else None, "", label)
    unknown = [v for v in cleaned if v not in allowed]
    if unknown:
        raise ValueError(
            f"{label} has unknown values: {', '.join(unknown)}. Allowed: {', '.join(allowed)}."
        )
    return cleaned


def _resolve_rest_base(base_url: str | None, region: str | None) -> tuple[str, str]:
    """Validate the host settings; return (REST base URL, region key used in document ids)."""
    region = (region or os.getenv("SNYK_REGION") or DEFAULT_REGION).strip().lower()
    raw = base_url or os.getenv("SNYK_API_URL")
    if not raw:
        if region not in REGION_HOSTS:
            raise ValueError(f"region must be one of {', '.join(REGION_HOSTS)} (or pass base_url).")
        return f"{REGION_HOSTS[region]}/rest", region
    if not REGION_PATTERN.match(region):
        raise ValueError("region may only contain letters, digits and '-'.")
    try:
        parts = urlsplit(raw.strip())
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        raise ValueError("base_url is not a valid URL.") from None
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("base_url must look like https://api.eu.snyk.io/rest (https only).")
    if "@" in parts.netloc:
        raise ValueError("base_url must not contain credentials; pass api_token instead.")
    if parts.query or parts.fragment:
        raise ValueError("base_url must not contain a query or fragment.")
    path = parts.path.rstrip("/") or "/rest"
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")), region


def snyk_source(
    api_token: str | None = None,
    *,
    region: str | None = None,
    base_url: str | None = None,
    org_ids: list[str] | None = None,
    group_id: str | None = None,
    all_orgs: bool = False,
    project_ids: list[str] | None = None,
    severities: list[str] | None = None,
    issue_types: tuple[str, ...] = ("package_vulnerability",),
    include_resolved: bool = False,
    include_ignored: bool = False,
    include_remedies: bool = True,
    max_projects_per_document: int = 50,
    max_issues_per_org: int = 20_000,
    api_version: str = DEFAULT_API_VERSION,
    timeout: float = 60.0,
    allow_mass_delete: bool = False,
    resource_name: str = "snyk_vulnerabilities",
    check_active: Callable[[], None] | None = None,
    session: Any = None,
):
    """Return a ``dlt`` resource yielding one row per Snyk vulnerability.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``,
    ``primary_key="id"`` and ``max_rows_per_table=0``. The Snyk REST API needs an Enterprise
    plan; a Free, Team or trial personal token gets HTTP 403.

    Args:
        api_token: Snyk API token (``SNYK_TOKEN``): a service account token (recommended, with a
            Group Viewer role) or a personal access token. Never logged or put in state.
        region: ``us-01`` (default), ``us-02``, ``eu-01`` or ``au-01`` (``SNYK_REGION``). Tokens
            are region specific, and the region is part of every document id.
        base_url: REST base URL override, https only (``SNYK_API_URL``).
        org_ids: Organizations to read (``SNYK_ORG_IDS``, comma-separated).
        group_id: Read every organization of this group (``SNYK_GROUP_ID``).
        all_orgs: Read every organization the token can see. An explicit opt-in.
        project_ids: Only the findings of these projects.
        severities: ``info``, ``low``, ``medium``, ``high``, ``critical``. Default: all.
        issue_types: Issue types to read. Default: package vulnerabilities only. ``code`` and
            ``secrets`` are refused: they carry source locations.
        include_resolved: Also read resolved findings. Off by default.
        include_ignored: Also read ignored findings. Off by default.
        include_remedies: Add fix versions and remedy text. On by default.
        max_projects_per_document: Projects listed in one document before "and N more".
        max_issues_per_org: A larger organization raises instead of being cut short.
        api_version: REST API version, ``YYYY-MM-DD``.
        timeout: Seconds to wait for each request.
        allow_mass_delete: Skip the double-check that normally guards a bulk deletion (an
            empty listing, or more than half of an organization's documents, at least 5).
            Off by default: a missing permission or a wrong selection looks the same.
        resource_name: Stable dlt resource name. Give each Snyk account its own name when
            several sync into one dataset, so their state stays separate.
        check_active: Optional host authorization checkpoint, called before every request,
            every wait and every row.
        session: A pre-built ``requests``-style session, mainly for tests.
    """
    try:
        import dlt
        import requests  # noqa: F401 - checked here so a missing install fails early
    except ImportError as exc:
        raise ImportError(
            "The Snyk connector needs dlt and requests: pip install cognee-community-connector-snyk"
        ) from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Snyk sync requires a Cognee build with table-scoped DLT document "
            "cleanup. Upgrade Cognee before syncing to avoid deleting another source's data."
        )
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("snyk_source needs a resource_name, one per Snyk account.")

    token = api_token or os.getenv("SNYK_TOKEN")
    if not token:
        raise ValueError(
            "A Snyk API token is required. Pass api_token or set SNYK_TOKEN (a service account "
            "token, or a personal access token for a one-off test)."
        )
    _check_token(token)
    rest_base, region_key = _resolve_rest_base(base_url, region)
    orgs = _resolve_list(org_ids, "SNYK_ORG_IDS", "org_ids")
    group = (group_id or os.getenv("SNYK_GROUP_ID") or "").strip() or None
    if not (orgs or group or all_orgs):
        raise ValueError(
            "Choose what to read: pass org_ids (or set SNYK_ORG_IDS), group_id, or all_orgs=True."
        )
    types = _choice_list(issue_types, ISSUE_TYPES, "issue_types")
    refused = [t for t in types if t in REFUSED_ISSUE_TYPES]
    if refused:
        raise ValueError(
            f"issue_types {', '.join(refused)} is not supported: those findings carry source "
            "locations and secrets."
        )
    if not API_VERSION_PATTERN.match(api_version or ""):
        raise ValueError("api_version must look like 2024-10-15.")
    if max_projects_per_document < 1 or max_issues_per_org < 1:
        raise ValueError("max_projects_per_document and max_issues_per_org must be at least 1.")
    if timeout <= 0:
        raise ValueError("timeout must be greater than 0.")
    if not isinstance(allow_mass_delete, bool):
        raise ValueError("allow_mass_delete must be True or False.")

    config = _SnykConfig(
        rest_base=rest_base,
        region=region_key,
        org_ids=orgs,
        group_id=group,
        all_orgs=bool(all_orgs),
        project_ids=_resolve_list(project_ids, "", "project_ids"),
        severities=_choice_list(severities, tuple(SEVERITY_RANK), "severities")
        if severities is not None
        else (),
        issue_types=types,
        include_resolved=bool(include_resolved),
        include_ignored=bool(include_ignored),
        include_remedies=bool(include_remedies),
        max_projects_per_document=max_projects_per_document,
        max_issues_per_org=max_issues_per_org,
        api_version=api_version,
        timeout=timeout,
        allow_mass_delete=allow_mass_delete,
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        write_disposition="merge",
        primary_key="id",
        # `_deleted` is a boolean hard-delete marker: rows where it is True are removed
        # from the dlt destination on merge, which cognee's orphan cleanup then forgets.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def snyk_vulnerabilities():
        http = session if session is not None else _make_session()
        client = _Client(http, token, check_active, timeout)
        try:
            yield from dlt_utils.guarded_rows(
                _iter_rows(client, config, dlt.current.resource_state(), stats), check_active
            )
        finally:
            if session is None:
                http.close()

    resource = snyk_vulnerabilities()
    # Opt into the document ingestion path: each vulnerability becomes a text document that
    # flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, "snyk")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics contain counts only, never names or content.
    resource.cognee_sync_stats = stats
    return resource
