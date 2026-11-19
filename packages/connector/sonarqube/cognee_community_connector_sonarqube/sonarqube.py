"""SonarQube connector for cognee: a ``dlt`` source that turns issues, Security
Hotspots and quality-gate outcomes into memory.

Auth is a SonarQube user token (My Account > Security > Generate Tokens), sent as
``Authorization: Bearer <token>`` -- SonarSource's documented, recommended scheme
for both SonarQube Server/Community Build and SonarQube Cloud. Hand the result to
:func:`cognee.remember`::

    await cognee.remember(
        sonarqube_source(project_keys=["my_project"]),  # SONARQUBE_TOKEN from env
        dataset_name="sonarqube",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Why ``createdAfter`` is a scope filter, not a change detector
---------------------------------------------------------------
SonarQube's ``api/issues/search`` has no ``updatedAfter``-style parameter
(confirmed against the live Web API reference): ``createdAfter`` only narrows by
an issue's *creation* time, so it cannot see a status change, a resolution, a
reassignment or a re-rated severity on an issue created before the cursor. This
connector therefore re-lists every in-scope issue/hotspot on every run and
decides what changed with a content hash, using ``created_after`` only as the
caller's own scope filter (e.g. "ignore anything created before our rollout").
Filtering to unresolved issues above a severity threshold means a fixed,
reassigned or closed issue simply stops matching the filter and is forgotten the
same way a deleted one is -- SonarQube answers both cases identically (silent
omission from the listing, never a 404), which is exactly what the guard/
spot-check logic below exists to tell apart from a lost permission.

See the README for the full design, defaults and what each parameter means.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

from cognee.shared.logging_utils import get_logger

logger = get_logger("sonarqube_connector")

SONARQUBE_TABLE_NAME = "sonarqube_documents"
SONARQUBE_SOURCE_NAME = "sonarqube"

_SEVERITIES_ENUM = ("INFO", "MINOR", "MAJOR", "CRITICAL", "BLOCKER")
_ISSUE_TYPES_ENUM = ("CODE_SMELL", "BUG", "VULNERABILITY")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_CLOUD_HOSTS = {"sonarcloud.io"}

# Confirmed against the live Web API reference (issues/search, components/search):
# "ps" (page size) tops out at 500. hotspots/search's own maximum is UNVERIFIED by
# direct fetch; 500 is used as the same working assumption (see research notes).
_ISSUES_PAGE_SIZE = 500
_HOTSPOTS_PAGE_SIZE = 500
_PROJECTS_PAGE_SIZE = 500
# Confirmed: issues/search enforces a hard p*ps <= 10000 result window
# (HTTP 400 "Can return only the first 10000 results" past it).
_SEARCH_WINDOW_LIMIT = 10_000

# Retry/wait-budget numbers taken from the asana connector, per spec.
_MAX_RETRIES = 5
_TRANSIENT_STATUSES = (429, 502, 503, 504)
_MAX_WAIT_SECONDS = 60.0
_MAX_TOTAL_WAIT_SECONDS = 120.0

_MAX_TITLE_MESSAGE = 100
_MAX_CONTENT_MESSAGE = 1000

# Mass-delete guard thresholds (see `_check_guards`): refuse to forget this
# much of a project's previously-synced documents in one pass unless
# `allow_mass_delete=True` or a spot-check confirms it's real.
_MASS_DELETE_MIN_COUNT = 5
_MASS_DELETE_RATIO = 0.5
_SPOT_CHECK_MAX = 5
# At most this many prior-synced-but-now-missing projects are probed (via
# components/show) in one `all_projects` run.
_MAX_PROJECT_PROBES = 20

_TOKEN_HINT = (
    "Generate a user token in SonarQube under My Account > Security > Generate Tokens, "
    "then pass token= or set SONARQUBE_TOKEN."
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Config:
    base_url: str
    organization: str | None
    project_keys: tuple[str, ...] | None
    all_projects: bool
    branch: str | None
    severities: tuple[str, ...]
    issue_types: tuple[str, ...]
    include_issues: bool
    include_hotspots: bool
    include_quality_gate: bool
    created_after: str | None
    max_issues_per_project: int
    max_hotspots_per_project: int
    include_author: bool
    timeout: float
    allow_mass_delete: bool


# ---------------------------------------------------------------------------
# Validation (never echoes the token)
# ---------------------------------------------------------------------------
def _check_token(token: str) -> None:
    if any(ch.isspace() or not ch.isprintable() for ch in token):
        raise ValueError("The SonarQube token contains whitespace or control characters.")


def _validate_base_url(raw: str) -> str:
    if not raw:
        raise ValueError("SonarQube base_url is required: pass base_url= or set SONARQUBE_URL.")
    parsed = urlsplit(raw.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"SonarQube base_url must start with http:// or https://, got {raw!r}.")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in _LOCAL_HOSTS:
        raise ValueError(
            "SonarQube base_url must use https:// (http:// is only allowed for "
            "localhost/127.0.0.1/::1)."
        )
    if parsed.username or parsed.password:
        raise ValueError("SonarQube base_url must not contain credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("SonarQube base_url must not contain a query string or fragment.")
    if parsed.path not in ("", "/"):
        raise ValueError("SonarQube base_url must not contain a path.")
    if not parsed.netloc:
        raise ValueError("SonarQube base_url must include a host.")
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_created_after(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("created_after must be a non-empty ISO date or datetime string.")
    value = value.strip()
    try:
        if len(value) <= 10:
            datetime.strptime(value, "%Y-%m-%d")
        else:
            datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"created_after must be an ISO date or datetime, got {value!r}.") from exc
    return value


def _normalize_project_keys(project_keys: list[str] | str | None) -> tuple[str, ...] | None:
    if project_keys is None:
        return None
    if isinstance(project_keys, str):
        project_keys = [project_keys]
    cleaned = tuple(dict.fromkeys(str(p).strip() for p in project_keys if str(p).strip()))
    if not cleaned:
        raise ValueError("project_keys, if given, must be a non-empty list.")
    return cleaned


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is."""
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > 64:
        raise ValueError("resource_name must be at most 64 characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")


# ---------------------------------------------------------------------------
# HTTP client with retry
# ---------------------------------------------------------------------------
class SonarQubeAPIError(RuntimeError):
    """An error answer from SonarQube (``status``), or no answer at all (``status`` 0).

    The message never includes the token; SonarQube's own ``errors[].msg`` is
    folded in when present, but the raw response body never is.
    """

    def __init__(self, status: int, path: str, message: str = ""):
        answer = f"HTTP {status}" if status else "no answer"
        detail = f": {message}" if message else ""
        super().__init__(f"SonarQube {path}: {answer}{detail}")
        self.status = status
        self.path = path


class _NotFoundError(SonarQubeAPIError):
    """A confirmed 404: the project, issue or hotspot does not exist (or is hidden)."""


def _sleep_default(seconds: float) -> None:
    time.sleep(seconds)


class _Client:
    """One run's access to SonarQube: the session, base URL, and the wait budget."""

    def __init__(
        self,
        session: Any,
        base_url: str,
        timeout: float,
        check_active: Callable[[], None] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.session = session
        self.base_url = base_url
        self.timeout = timeout
        self.check_active = check_active
        self.sleep = sleep or _sleep_default
        self.waited = 0.0

    def check(self) -> None:
        if self.check_active is not None:
            self.check_active()

    def can_wait(self, seconds: float) -> bool:
        if seconds > _MAX_WAIT_SECONDS:
            return False
        return self.waited + seconds <= _MAX_TOTAL_WAIT_SECONDS

    def wait(self, seconds: float) -> None:
        self.check()
        self.waited += seconds
        self.sleep(seconds)


def _error_message(response: Any) -> str:
    """SonarQube's ``errors[0].msg``, or nothing. Never the raw body."""
    try:
        body = response.json()
    except ValueError:
        return ""
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        msg = errors[0].get("msg")
        if isinstance(msg, str):
            return msg[:200]
    return ""


def _retry_delay(headers: Any, attempt: int) -> float:
    retry_after = (headers or {}).get("Retry-After")
    if retry_after is not None:
        try:
            return float(retry_after)
        except (TypeError, ValueError):
            pass
    return float(2**attempt)


def _connection_hint(base_url: str, exc: Exception) -> str:
    return f"could not connect to {urlsplit(base_url).netloc}: {type(exc).__name__}"


def _get_json(
    client: _Client, path: str, params: dict | None = None, project: str | None = None
) -> dict:
    """GET a SonarQube Web API path and return its parsed JSON body.

    429/502/503/504 are retried with backoff (``Retry-After`` if present) up to
    ``_MAX_RETRIES`` times, bounded by the single-wait cap and the run's total
    wait budget. 401/403/404 raise with a specific, credential-free message;
    404 raises :class:`_NotFoundError` so callers can tell "gone" from any other
    failure. Nothing is ever swallowed: a failure here aborts the whole sync so
    a partial listing can never be mistaken for a deletion.
    """
    url = f"{client.base_url}{path}"
    attempt = 0
    while True:
        client.check()
        try:
            response = client.session.get(url, params=params or {}, timeout=client.timeout)
        except Exception as exc:
            delay = float(2**attempt)
            if attempt >= _MAX_RETRIES - 1 or not client.can_wait(delay):
                raise SonarQubeAPIError(0, path, _connection_hint(client.base_url, exc)) from None
            logger.warning(
                "SonarQube: %s on %s; retrying in %.0fs.", type(exc).__name__, path, delay
            )
            client.wait(delay)
            attempt += 1
            continue

        status = response.status_code
        if status == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise SonarQubeAPIError(status, path, "response was not JSON") from exc

        if status in _TRANSIENT_STATUSES and attempt < _MAX_RETRIES - 1:
            delay = _retry_delay(getattr(response, "headers", None), attempt)
            if client.can_wait(delay):
                logger.warning("SonarQube: HTTP %d on %s; retrying in %.0fs.", status, path, delay)
                client.wait(delay)
                attempt += 1
                continue

        message = _error_message(response)
        if status == 401:
            raise SonarQubeAPIError(
                status,
                path,
                "invalid or expired token. " + _TOKEN_HINT + (f" ({message})" if message else ""),
            )
        if status == 403:
            where = f" on project '{project}'" if project else ""
            raise SonarQubeAPIError(
                status,
                path,
                f"missing 'Browse' (or another required) permission{where}."
                + (f" ({message})" if message else ""),
            )
        if status == 404:
            where = f"project '{project}'" if project else path
            raise _NotFoundError(
                status, path, f"{where} not found." + (f" ({message})" if message else "")
            )
        raise SonarQubeAPIError(status, path, message)


def _make_session(token: str, auth_scheme: str = "bearer") -> Any:
    import requests

    session = requests.Session()
    if auth_scheme == "basic":
        session.auth = (token, "")  # token as username, empty password
    else:
        session.headers["Authorization"] = f"Bearer {token}"
    session.headers["Accept"] = "application/json"
    return session


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def _q(value: Any) -> str:
    return quote(str(value), safe="")


def _host_key(base_url: str, organization: str | None) -> str:
    netloc = urlsplit(base_url).netloc
    key = netloc.replace(":", "_")
    return f"{key}/{organization}" if organization else key


def _branch_scope(branch: str | None) -> str:
    """A ``:branch:<name>`` id/state-key segment, or "" when unscoped.

    Without this, two configs for the same project that differ only by
    ``branch`` would share both document ids and the project's state key --
    syncing one branch would tombstone the other's issues as "vanished".
    Omitted entirely when ``branch`` is unset, so the common single-branch
    case keeps its existing ids.
    """
    return f":branch:{branch.replace(':', '_')}" if branch else ""


def _project_state_key(host: str, project_key: str, branch: str | None) -> str:
    return f"{host}:{project_key}{_branch_scope(branch)}"


def _strip_project_prefix(component: str | None, project_key: str) -> str:
    component = component or ""
    prefix = f"{project_key}:"
    return component[len(prefix) :] if component.startswith(prefix) else component


def _clip(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
def _fetch_project_name(client: _Client, config: _Config, project_key: str) -> str:
    params: dict[str, Any] = {"component": project_key}
    if config.organization:
        params["organization"] = config.organization
    body = _get_json(client, "/api/components/show", params, project=project_key)
    component = body.get("component") or {}
    return component.get("name") or project_key


def _list_all_projects(client: _Client, config: _Config) -> dict[str, str]:
    """Every project (``qualifiers=TRK``) the token can Browse, key -> name."""
    params: dict[str, Any] = {"qualifiers": "TRK", "ps": _PROJECTS_PAGE_SIZE}
    if config.organization:
        params["organization"] = config.organization

    results: dict[str, str] = {}
    page = 1
    max_pages = 200  # loop guard: 500 * 200 = 100,000 projects, far beyond real use
    while True:
        body = _get_json(client, "/api/components/search", {**params, "p": page})
        components = body.get("components") or []
        for component in components:
            key = component.get("key")
            if key:
                results[key] = component.get("name") or key
        if len(components) < _PROJECTS_PAGE_SIZE:
            return results
        page += 1
        if page > max_pages:
            raise RuntimeError(
                "SonarQube: components/search pagination exceeded the page-count guard."
            )


def _prior_projects_by_key(
    prior_projects_state: dict[str, dict], host: str, branch: str | None
) -> dict[str, tuple[str, dict]]:
    """``state["projects"]`` entries for this (host, branch) scope, by project key.

    Filters on the stored ``branch`` field, not on string-slicing the state
    key: a prior entry for a *different* branch of the same host must never
    be matched here (it would otherwise be treated as this project and
    tombstoned/overwritten by a sync that never touched that branch). An
    entry with no ``project_key`` recorded (shouldn't happen for state this
    code wrote) is left alone rather than guessed at.
    """
    by_key = {}
    prefix = f"{host}:"
    for state_key, prior in prior_projects_state.items():
        if not state_key.startswith(prefix):
            continue
        if prior.get("branch") != branch:
            continue
        project_key = prior.get("project_key")
        if not project_key:
            continue
        by_key[project_key] = (state_key, prior)
    return by_key


def _forget_project_docs(prior: dict) -> list[dict]:
    return [{"id": doc_id, "_deleted": True} for doc_id in sorted(prior.get("docs") or {})]


def _resolve_explicit_projects(
    client: _Client, config: _Config, prior_projects_state: dict[str, dict], host: str
) -> tuple[dict[str, str], list[dict], set[str]]:
    """Explicit ``project_keys``: resolve names, and forget deselected/404 projects.

    A project no longer in ``project_keys`` at all is forgotten without any
    request. A project that 404s is forgotten if it was previously synced
    (confirmed gone), or raised as a config error if it never was.
    """
    wanted = list(config.project_keys or ())
    prior_by_key = _prior_projects_by_key(prior_projects_state, host, config.branch)

    resolved_names: dict[str, str] = {}
    forgotten_tombstones: list[dict] = []
    forgotten_state_keys: set[str] = set()

    for project_key in wanted:
        try:
            resolved_names[project_key] = _fetch_project_name(client, config, project_key)
        except _NotFoundError:
            state_key, prior = prior_by_key.get(project_key, (None, None))
            if prior is None:
                raise RuntimeError(
                    f"SonarQube: project '{project_key}' was not found (404) and was never "
                    "previously synced. Check the project key and the token's Browse permission."
                ) from None
            logger.warning("SonarQube: project '%s' not found (404); forgetting it.", project_key)
            forgotten_tombstones.extend(_forget_project_docs(prior))
            forgotten_state_keys.add(state_key)

    wanted_set = set(wanted)
    for project_key, (state_key, prior) in prior_by_key.items():
        if project_key in wanted_set:
            continue
        logger.info("SonarQube: project '%s' no longer selected; forgetting it.", project_key)
        forgotten_tombstones.extend(_forget_project_docs(prior))
        forgotten_state_keys.add(state_key)

    return resolved_names, forgotten_tombstones, forgotten_state_keys


def _resolve_all_projects(
    client: _Client, config: _Config, prior_projects_state: dict[str, dict], host: str
) -> tuple[dict[str, str], list[dict], set[str]]:
    """``all_projects=True``: list every Browse-able project, and verify (by id,
    capped at ``_MAX_PROJECT_PROBES`` per run) any previously-synced project
    that's missing from the listing before forgetting it.
    """
    resolved_names = _list_all_projects(client, config)
    prior_by_key = _prior_projects_by_key(prior_projects_state, host, config.branch)

    forgotten_tombstones: list[dict] = []
    forgotten_state_keys: set[str] = set()
    missing = sorted(key for key in prior_by_key if key not in resolved_names)

    for project_key in missing[:_MAX_PROJECT_PROBES]:
        state_key, prior = prior_by_key[project_key]
        try:
            _fetch_project_name(client, config, project_key)
        except _NotFoundError:
            logger.warning(
                "SonarQube: project '%s' confirmed gone (404); forgetting it.", project_key
            )
            forgotten_tombstones.extend(_forget_project_docs(prior))
            forgotten_state_keys.add(state_key)
        except SonarQubeAPIError as exc:
            logger.warning(
                "SonarQube: could not verify whether project '%s' still exists (%s); "
                "keeping its state as-is.",
                project_key,
                exc,
            )
        else:
            raise RuntimeError(
                f"SonarQube: project '{project_key}' is missing from the all_projects listing "
                "but still exists (200) -- the listing looks inconsistent (e.g. a permission "
                "change mid-run). Refusing to guess; investigate before the next sync."
            )

    if len(missing) > _MAX_PROJECT_PROBES:
        logger.warning(
            "SonarQube: %d project(s) missing from the all_projects listing exceeded the "
            "per-run verification cap (%d); the rest keep their state as-is this run.",
            len(missing) - _MAX_PROJECT_PROBES,
            _MAX_PROJECT_PROBES,
        )

    return resolved_names, forgotten_tombstones, forgotten_state_keys


def _resolve_projects(
    client: _Client, config: _Config, prior_projects_state: dict[str, dict], host: str
) -> tuple[dict[str, str], list[dict], set[str]]:
    """Resolve this run's projects, plus any project-level forgetting.

    Returns ``(project_key -> name, forgotten_tombstones, forgotten_state_keys)``.
    """
    if config.project_keys is not None:
        return _resolve_explicit_projects(client, config, prior_projects_state, host)
    return _resolve_all_projects(client, config, prior_projects_state, host)


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------
def _list_issues(
    client: _Client, config: _Config, project_key: str
) -> tuple[list[dict], dict[str, str]]:
    """Every unresolved, in-scope issue for a project, plus a rule key -> name map.

    Raises if the project has more issues matching the filters than
    ``max_issues_per_project`` (never truncates silently -- a partial listing
    would later look like deleted issues), and if the listing would cross
    SonarQube's 10,000-result search window.
    """
    params: dict[str, Any] = {
        "components": project_key,
        "resolved": "false",
        "severities": ",".join(config.severities),
        "types": ",".join(config.issue_types),
        "ps": _ISSUES_PAGE_SIZE,
        # Resolves rule names for rendering; exact response shape UNVERIFIED by
        # direct fetch (see research notes) -- handled defensively below.
        "additionalFields": "rules",
    }
    if config.branch:
        params["branch"] = config.branch
    if config.organization:
        params["organization"] = config.organization
    if config.created_after:
        params["createdAfter"] = config.created_after

    issues: list[dict] = []
    rule_names: dict[str, str] = {}
    page = 1
    while True:
        body = _get_json(client, "/api/issues/search", {**params, "p": page}, project=project_key)
        page_issues = body.get("issues") or []
        issues.extend(page_issues)
        for rule in body.get("rules") or []:
            key = rule.get("key")
            if key:
                rule_names[key] = rule.get("name") or key

        paging = body.get("paging") or {}
        total = paging.get("total", body.get("total"))
        if isinstance(total, int) and total > config.max_issues_per_project:
            raise RuntimeError(
                f"SonarQube: project '{project_key}' has {total} unresolved issue(s) matching "
                f"the current filters, exceeding max_issues_per_project="
                f"{config.max_issues_per_project}. Raise max_issues_per_project, set "
                "created_after, raise the severity threshold, or narrow issue_types."
            )
        # A short page always means "last page". A full page can too, exactly
        # when `total` is known and already reached -- without this, a project
        # whose issue count is an exact multiple of the page size (e.g.
        # max_issues_per_project=10000 landing precisely on the window limit)
        # would wrongly be sent on to request a page past the window and raise,
        # even though every matching issue was already fetched.
        if len(page_issues) < _ISSUES_PAGE_SIZE or (
            isinstance(total, int) and len(issues) >= total
        ):
            return issues, rule_names
        page += 1
        if page * _ISSUES_PAGE_SIZE > _SEARCH_WINDOW_LIMIT:
            raise RuntimeError(
                f"SonarQube: project '{project_key}' issue listing crossed the 10,000-result "
                "search window before finishing; narrow the filters."
            )


def _line_info(issue: dict) -> str:
    text_range = issue.get("textRange") or {}
    start = text_range.get("startLine") or issue.get("line")
    if not start:
        return ""
    end = text_range.get("endLine")
    if end and end != start:
        return f"{start}-{end}"
    return str(start)


def _issue_doc_id(host: str, project_key: str, branch: str | None, key: str) -> str:
    return f"sonarqube:{host}:{project_key}{_branch_scope(branch)}:issue:{key}"


def _render_issue(
    issue: dict,
    project_name: str,
    project_key: str,
    config: _Config,
    rule_names: dict[str, str],
    host: str,
) -> dict[str, Any]:
    """Render one issue as a deterministic document. Excludes volatile fields
    (``updateDate``, counters) so an unchanged issue keeps its content hash.
    """
    key = issue["key"]
    severity = issue.get("severity") or "UNKNOWN"
    issue_type = issue.get("type") or "ISSUE"
    message = _clip((issue.get("message") or "").strip(), _MAX_CONTENT_MESSAGE)
    title = f"[SonarQube {severity} {issue_type}] {_clip(message, _MAX_TITLE_MESSAGE)}"

    rule_key = issue.get("rule") or ""
    rule_name = rule_names.get(rule_key) or ""
    path = _strip_project_prefix(issue.get("component"), project_key)
    line_info = _line_info(issue)
    location = path + (f":{line_info}" if line_info else "")

    lines = [f"Project: {project_name} ({project_key})"]
    if config.branch:
        lines.append(f"Branch: {config.branch}")
    rule_line = f"Rule: {rule_key}"
    if rule_name:
        rule_line += f" ({rule_name})"
    lines.append(rule_line)
    lines.append(f"Severity: {severity}")
    lines.append(f"Type: {issue_type}")
    status_line = f"Status: {issue.get('status') or 'UNKNOWN'}"
    if issue.get("resolution"):
        status_line += f" ({issue['resolution']})"
    lines.append(status_line)
    if location:
        lines.append(f"File: {location}")
    effort = issue.get("effort") or issue.get("debt")
    if effort:
        lines.append(f"Effort: {effort}")
    tags = sorted(issue.get("tags") or [])
    if tags:
        lines.append("Tags: " + ", ".join(tags))
    clean_code_attribute = issue.get("cleanCodeAttribute")
    if clean_code_attribute:
        lines.append(f"Clean code attribute: {clean_code_attribute}")
    impacts = issue.get("impacts") or []
    if impacts:
        impact_lines = sorted(
            f"{impact.get('softwareQuality', '?')}: {impact.get('severity', '?')}"
            for impact in impacts
        )
        lines.append("Impacts: " + ", ".join(impact_lines))
    created = issue.get("creationDate")
    if created:
        lines.append(f"Created: {created}")
    if config.include_author:
        author = issue.get("author")
        if author:
            lines.append(f"Author: {author}")
    lines.append("")
    lines.append(message)
    lines.append("")
    lines.append(
        f"Issue {key} (rule {rule_key or 'unknown'}) in {location or 'an unknown file'} "
        f"of project {project_name}."
    )
    content = "\n".join(lines)

    url = f"{config.base_url}/project/issues?id={_q(project_key)}&open={_q(key)}"
    if config.branch:
        url += f"&branch={_q(config.branch)}"

    return {
        "id": _issue_doc_id(host, project_key, config.branch, key),
        "title": title,
        "content": content,
        "url": url,
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Hotspots
# ---------------------------------------------------------------------------
def _list_hotspots(client: _Client, config: _Config, project_key: str) -> list[dict]:
    """Every TO_REVIEW Security Hotspot for a project.

    Raises if the project has more matching hotspots than
    ``max_hotspots_per_project``, and if the listing would cross SonarQube's
    10,000-result search window (see ``_list_issues`` for the same reasoning).
    """
    params: dict[str, Any] = {
        "project": project_key,
        "status": "TO_REVIEW",
        "ps": _HOTSPOTS_PAGE_SIZE,
    }
    if config.branch:
        params["branch"] = config.branch
    if config.organization:
        params["organization"] = config.organization

    hotspots: list[dict] = []
    page = 1
    while True:
        body = _get_json(client, "/api/hotspots/search", {**params, "p": page}, project=project_key)
        page_hotspots = body.get("hotspots") or []
        hotspots.extend(page_hotspots)

        paging = body.get("paging") or {}
        total = paging.get("total")
        if isinstance(total, int) and total > config.max_hotspots_per_project:
            raise RuntimeError(
                f"SonarQube: project '{project_key}' has {total} TO_REVIEW hotspot(s), "
                f"exceeding max_hotspots_per_project={config.max_hotspots_per_project}. "
                "Raise max_hotspots_per_project."
            )
        # See `_list_issues`: a full last page whose count already reached a
        # known `total` is also the last page, not a window violation.
        if len(page_hotspots) < _HOTSPOTS_PAGE_SIZE or (
            isinstance(total, int) and len(hotspots) >= total
        ):
            return hotspots
        page += 1
        if page * _HOTSPOTS_PAGE_SIZE > _SEARCH_WINDOW_LIMIT:
            raise RuntimeError(
                f"SonarQube: project '{project_key}' hotspot listing crossed the 10,000-result "
                "search window before finishing."
            )


def _hotspot_doc_id(host: str, project_key: str, branch: str | None, key: str) -> str:
    return f"sonarqube:{host}:{project_key}{_branch_scope(branch)}:hotspot:{key}"


def _render_hotspot(
    hotspot: dict, project_name: str, project_key: str, config: _Config, host: str
) -> dict[str, Any]:
    """Render one Security Hotspot as a deterministic document (see `_render_issue`)."""
    key = hotspot["key"]
    category = hotspot.get("securityCategory") or "unknown"
    probability = hotspot.get("vulnerabilityProbability") or "UNKNOWN"
    message = _clip((hotspot.get("message") or "").strip(), _MAX_CONTENT_MESSAGE)
    title = f"[SonarQube Hotspot {probability}] {_clip(message, _MAX_TITLE_MESSAGE)}"

    path = _strip_project_prefix(hotspot.get("component"), project_key)
    line = hotspot.get("line")
    location = path + (f":{line}" if line else "")

    lines = [f"Project: {project_name} ({project_key})"]
    if config.branch:
        lines.append(f"Branch: {config.branch}")
    lines.append(f"Security category: {category}")
    lines.append(f"Vulnerability probability: {probability}")
    status_line = f"Status: {hotspot.get('status') or 'UNKNOWN'}"
    if hotspot.get("resolution"):
        status_line += f" ({hotspot['resolution']})"
    lines.append(status_line)
    if location:
        lines.append(f"File: {location}")
    created = hotspot.get("creationDate")
    if created:
        lines.append(f"Created: {created}")
    lines.append("")
    lines.append(message)
    content = "\n".join(lines)

    url = f"{config.base_url}/security_hotspots?id={_q(project_key)}&hotspots={_q(key)}"
    if config.branch:
        url += f"&branch={_q(config.branch)}"

    return {
        "id": _hotspot_doc_id(host, project_key, config.branch, key),
        "title": title,
        "content": content,
        "url": url,
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------
def _fetch_gate_status(client: _Client, config: _Config, project_key: str) -> dict:
    params: dict[str, Any] = {"projectKey": project_key}
    if config.branch:
        params["branch"] = config.branch
    if config.organization:
        params["organization"] = config.organization
    return _get_json(client, "/api/qualitygates/project_status", params, project=project_key)


def _gate_doc_id(host: str, project_key: str, branch: str | None) -> str:
    return f"sonarqube:{host}:{project_key}{_branch_scope(branch)}:quality-gate"


def _render_gate(
    gate_body: dict, project_name: str, project_key: str, config: _Config, host: str
) -> dict[str, Any] | None:
    """Render a project's quality-gate outcome, or ``None`` when it was never
    analyzed (status ``NONE`` -- nothing to say yet, so nothing to ingest).
    """
    project_status = gate_body.get("projectStatus") or {}
    status = project_status.get("status") or "NONE"
    if status == "NONE":
        return None

    conditions = [
        c for c in (project_status.get("conditions") or []) if c.get("status") in ("ERROR", "WARN")
    ]
    conditions.sort(key=lambda c: c.get("metricKey") or "")

    title = f"[SonarQube Quality Gate {status}] {project_name}"
    lines = [f"Project: {project_name} ({project_key})"]
    if config.branch:
        lines.append(f"Branch: {config.branch}")
    lines.append(f"Gate status: {status}")
    if conditions:
        lines.append("Failing/warning conditions:")
        for condition in conditions:
            lines.append(
                f"- {condition.get('metricKey', 'unknown')}: actual "
                f"{condition.get('actualValue', '?')} {condition.get('comparator', '?')} "
                f"threshold {condition.get('errorThreshold', '?')} "
                f"[{condition.get('status', '?')}]"
            )
    else:
        lines.append("No failing or warning conditions.")
    content = "\n".join(lines)

    url = f"{config.base_url}/dashboard?id={_q(project_key)}"
    if config.branch:
        url += f"&branch={_q(config.branch)}"

    return {
        "id": _gate_doc_id(host, project_key, config.branch),
        "title": title,
        "content": content,
        "url": url,
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Hash gate
# ---------------------------------------------------------------------------
def _hash_row(row: dict) -> str:
    parts = (row.get("title") or "", row.get("content") or "", row.get("url") or "")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Guards + spot-check (see the README for the full rationale)
# ---------------------------------------------------------------------------
def _spot_check_issue(client: _Client, config: _Config, project_key: str, key: str) -> bool:
    """True if issue ``key`` still matches the current unresolved/severity/type filters."""
    params: dict[str, Any] = {"issues": key, "components": project_key}
    if config.organization:
        params["organization"] = config.organization
    body = _get_json(client, "/api/issues/search", params, project=project_key)
    issues = body.get("issues") or []
    if not issues:
        return False
    found = issues[0]
    if found.get("resolution") or found.get("status") in ("RESOLVED", "CLOSED"):
        return False
    if found.get("severity") not in config.severities:
        return False
    return found.get("type") in config.issue_types


def _spot_check_hotspot(client: _Client, config: _Config, project_key: str, key: str) -> bool:
    """True if hotspot ``key`` still has status TO_REVIEW (the default scope)."""
    params: dict[str, Any] = {"hotspot": key}
    if config.organization:
        params["organization"] = config.organization
    try:
        body = _get_json(client, "/api/hotspots/show", params, project=project_key)
    except _NotFoundError:
        return False
    hotspot = body.get("hotspot") if isinstance(body.get("hotspot"), dict) else body
    return (hotspot or {}).get("status") == "TO_REVIEW"


_SPOT_CHECKS: dict[str, Callable[[_Client, _Config, str, str], bool]] = {
    "issue": _spot_check_issue,
    "hotspot": _spot_check_hotspot,
}


def _check_guards(
    client: _Client,
    config: _Config,
    project_key: str,
    vanished: list[str],
    prior_docs: dict[str, dict],
    desired_is_empty: bool,
    stats: dict,
) -> None:
    """Refuse to tombstone too much of a project's documents in one pass unless
    ``allow_mass_delete`` or a spot-check confirms the vanished documents really
    no longer match the current filters (see the README: SonarQube answers a
    lost permission identically to a real deletion -- silent omission, no 404).
    """
    if not vanished or config.allow_mass_delete:
        return
    total_prior = len(prior_docs)
    empty_trigger = desired_is_empty and total_prior > 0
    ratio_trigger = len(vanished) >= _MASS_DELETE_MIN_COUNT and len(vanished) > (
        total_prior * _MASS_DELETE_RATIO
    )
    if not (empty_trigger or ratio_trigger):
        return

    # The quality-gate document is excluded: it is a single doc per project
    # whose presence/content comes straight from this run's own fresh
    # qualitygates/project_status call, so there is no silent-omission
    # ambiguity left to spot-check -- unlike issues/hotspots, which are only
    # known to have vanished because they stopped appearing in a *listing*.
    checkable = [
        doc_id for doc_id in vanished if (prior_docs.get(doc_id) or {}).get("kind") in _SPOT_CHECKS
    ]
    sample = checkable[:_SPOT_CHECK_MAX]
    stats["spot_checks"] += len(sample)
    for doc_id in sample:
        entry = prior_docs[doc_id]
        probe = entry.get("probe") or []
        if not probe:
            continue
        key = probe[0]
        spot_check = _SPOT_CHECKS[entry["kind"]]
        if spot_check(client, config, project_key, key):
            raise RuntimeError(
                f"SonarQube: project '{project_key}' would forget {len(vanished)} of "
                f"{total_prior} previously-synced document(s), but spot-checking "
                f"{entry['kind']} '{key}' shows it still matches the current filters. "
                "Refusing -- a lost permission can look identical to real deletions. Pass "
                "allow_mass_delete=True if this is intentional."
            )
    logger.warning(
        "SonarQube: project '%s' would forget %d of %d previously-synced document(s); "
        "%d spot-check(s) confirmed they no longer match the current filters.",
        project_key,
        len(vanished),
        total_prior,
        len(sample),
    )


# ---------------------------------------------------------------------------
# Per-project sync
# ---------------------------------------------------------------------------
def _sync_project(
    client: _Client,
    config: _Config,
    project_key: str,
    project_name: str,
    host: str,
    prior_project_state: dict,
    stats: dict,
) -> tuple[list[dict], list[dict], dict]:
    """Sync one project's documents. Returns ``(tombstones, upserts, new_state)``.

    Builds the full desired-document set in memory (every API call happens
    here, before any tombstone/guard decision), diffs it against the prior
    state by content hash, and tombstones anything no longer desired --
    subject to the mass-delete guard.
    """
    desired: dict[str, tuple[dict, str, list[str]]] = {}

    if config.include_issues:
        issues, rule_names = _list_issues(client, config, project_key)
        for issue in issues:
            key = issue.get("key")
            if not key:
                stats["missing_fields"] += 1
                continue
            row = _render_issue(issue, project_name, project_key, config, rule_names, host)
            desired[row["id"]] = (row, "issue", [key])

    if config.include_hotspots:
        hotspots = _list_hotspots(client, config, project_key)
        for hotspot in hotspots:
            key = hotspot.get("key")
            if not key:
                stats["missing_fields"] += 1
                continue
            row = _render_hotspot(hotspot, project_name, project_key, config, host)
            desired[row["id"]] = (row, "hotspot", [key])

    if config.include_quality_gate:
        gate_body = _fetch_gate_status(client, config, project_key)
        gate_row = _render_gate(gate_body, project_name, project_key, config, host)
        if gate_row is not None:
            desired[gate_row["id"]] = (gate_row, "gate", [project_key])

    prior_docs: dict[str, dict] = dict(prior_project_state.get("docs") or {})
    upserts: list[dict] = []
    new_docs: dict[str, dict] = {}
    for doc_id, (row, kind, probe) in desired.items():
        content_hash = _hash_row(row)
        prior_entry = prior_docs.get(doc_id)
        if prior_entry is None or prior_entry.get("hash") != content_hash:
            upserts.append(row)
        new_docs[doc_id] = {"hash": content_hash, "probe": sorted(set(probe))[:3], "kind": kind}

    vanished = sorted(set(prior_docs) - set(desired))
    _check_guards(client, config, project_key, vanished, prior_docs, not desired, stats)
    tombstones = [{"id": doc_id, "_deleted": True} for doc_id in vanished]

    stats["emitted"] += len(upserts)
    stats["unchanged"] += len(desired) - len(upserts)
    stats["kept"] += len(desired)
    stats["deleted"] += len(tombstones)
    stats["projects"] += 1

    new_state = {"project_key": project_key, "branch": config.branch, "docs": new_docs}
    return tombstones, upserts, new_state


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict, so it is unit-testable)
# ---------------------------------------------------------------------------
def _iter_rows(client: _Client, config: _Config, state: dict, stats: dict) -> Iterable[dict]:
    """Yield tombstones, then upserts, for every document that changed this run.

    The whole plan (every project's desired documents, every guard decision) is
    built in memory before the first yield, so any failure -- anywhere -- raises
    before a single row is emitted and leaves ``state`` untouched. ``state`` is
    only assigned after the last row, once the caller has fully drained this
    generator (dlt always does, before considering the resource's extraction done).
    """
    stats.clear()
    stats.update(
        emitted=0, unchanged=0, kept=0, deleted=0, spot_checks=0, projects=0, missing_fields=0
    )

    host = _host_key(config.base_url, config.organization)
    prior_projects_state: dict[str, dict] = dict(state.get("projects") or {})

    resolved_names, forgotten_tombstones, forgotten_state_keys = _resolve_projects(
        client, config, prior_projects_state, host
    )

    tombstones: list[dict] = list(forgotten_tombstones)
    upserts: list[dict] = []
    new_projects_state: dict[str, dict] = {}

    for project_key in sorted(resolved_names):
        state_key = _project_state_key(host, project_key, config.branch)
        project_tombstones, project_upserts, new_state = _sync_project(
            client,
            config,
            project_key,
            resolved_names[project_key],
            host,
            prior_projects_state.get(state_key) or {},
            stats,
        )
        tombstones.extend(project_tombstones)
        upserts.extend(project_upserts)
        new_projects_state[state_key] = new_state

    # Anything not actively resolved this run (forgotten, a different
    # host/org/branch scope, or -- in all_projects mode -- beyond the
    # per-run probe cap and so neither confirmed gone nor re-synced) is
    # carried forward untouched. Dropping it here instead would silently
    # stop tracking it without ever tombstoning its documents, orphaning
    # them in the destination with no way to ever clean them up.
    for state_key, prior in prior_projects_state.items():
        if state_key in forgotten_state_keys or state_key in new_projects_state:
            continue
        new_projects_state[state_key] = prior

    if stats["missing_fields"]:
        logger.warning(
            "SonarQube: %d issue(s)/hotspot(s) arrived without a key and were skipped.",
            stats["missing_fields"],
        )

    yield from sorted(tombstones, key=lambda r: r["id"])
    yield from sorted(upserts, key=lambda r: r["id"])

    state["projects"] = new_projects_state
    logger.info(
        "SonarQube: %d emitted, %d unchanged, %d deleted, %d spot-check(s), across %d project(s).",
        stats["emitted"],
        stats["unchanged"],
        stats["deleted"],
        stats["spot_checks"],
        stats["projects"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def sonarqube_source(
    token: str | None = None,
    *,
    base_url: str | None = None,
    organization: str | None = None,
    project_keys: list[str] | None = None,
    all_projects: bool = False,
    branch: str | None = None,
    severities: tuple[str, ...] = ("MAJOR", "CRITICAL", "BLOCKER"),
    issue_types: tuple[str, ...] = ("CODE_SMELL", "BUG", "VULNERABILITY"),
    include_issues: bool = True,
    include_hotspots: bool = True,
    include_quality_gate: bool = True,
    created_after: str | None = None,
    max_issues_per_project: int = 5000,
    max_hotspots_per_project: int = 1000,
    include_author: bool = False,
    timeout: float = 60.0,
    allow_mass_delete: bool = False,
    auth_scheme: str = "bearer",
    resource_name: str = SONARQUBE_TABLE_NAME,
    check_active: Callable[[], None] | None = None,
    session: Any = None,
):
    """Return a ``dlt`` resource yielding SonarQube documents for ``remember``.

    Args:
        token: SonarQube user token. Falls back to ``SONARQUBE_TOKEN``.
        base_url: SonarQube Server/Community Build or Cloud base URL (e.g.
            ``https://sonarcloud.io`` or ``https://sonarqube.example.com``).
            Falls back to ``SONARQUBE_URL``. ``http://`` is only accepted for
            localhost/127.0.0.1/::1.
        organization: SonarQube Cloud organization key. Required when
            ``base_url`` is sonarcloud.io. Falls back to ``SONARQUBE_ORGANIZATION``.
        project_keys: Projects to sync. Falls back to a comma-separated
            ``SONARQUBE_PROJECTS``. Mutually exclusive with ``all_projects`` in
            effect (either is accepted; ``project_keys`` takes precedence).
        all_projects: Sync every project the token can Browse. An explicit
            opt-in since it can be a lot of data; mutually informative with
            ``project_keys`` -- set this when you don't want to list keys.
        branch: Branch to scope issues/hotspots/quality-gate calls to; the
            project's main branch when omitted.
        severities: Legacy SonarQube severities to include (subset of
            ``INFO, MINOR, MAJOR, CRITICAL, BLOCKER``). Deliberately the legacy
            ``severities`` parameter, not the newer ``impactSeverities`` (clean
            code/MQR) taxonomy -- see the README.
        issue_types: Issue types to include (subset of ``CODE_SMELL, BUG,
            VULNERABILITY``).
        include_issues: Ingest issues.
        include_hotspots: Ingest Security Hotspots.
        include_quality_gate: Ingest quality-gate outcomes.
        created_after: Scope filter only (see the module docstring for why this
            is not a change detector) -- an ISO date or datetime.
        max_issues_per_project: Refuse (rather than truncate) a project with
            more unresolved, in-scope issues than this.
        max_hotspots_per_project: Same, for Security Hotspots.
        include_author: Include the issue author in rendered content. Off by
            default for privacy.
        timeout: Per-request timeout in seconds.
        allow_mass_delete: Bypass the mass-delete guards (see the README).
        auth_scheme: ``"bearer"`` (default, ``Authorization: Bearer <token>``)
            or ``"basic"`` (the token as the HTTP Basic username, empty
            password). Both are documented SonarQube schemes; Bearer is
            SonarSource's recommended one.
        resource_name: Stable dlt resource name, which also scopes the sync
            state. Give each token/selection its own name in a shared dataset.
        check_active: Optional host authorization checkpoint, called before
            every request and wait, and around every row; it stops the sync by
            raising.
        session: Pre-built ``requests``-like session, mainly for tests; when
            omitted one is built from the token for each run and closed after.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The SonarQube connector needs dlt: pip install "dlt[sqlalchemy]".'
        ) from exc
    from cognee.tasks.ingestion import dlt_utils
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    if not (hasattr(dlt_utils, "PIPELINE_SCOPE_ATTR") and hasattr(dlt_utils, "guarded_rows")):
        raise RuntimeError(
            "The SonarQube connector needs cognee's dlt_utils.PIPELINE_SCOPE_ATTR and "
            "guarded_rows (cognee>=1.6.3)."
        )

    resolved_token = token or os.environ.get("SONARQUBE_TOKEN") or ""
    if session is None:
        if not resolved_token:
            raise ValueError("SonarQube user token required: pass token= or set SONARQUBE_TOKEN.")
        _check_token(resolved_token)

    base_url_normalized = _validate_base_url(base_url or os.environ.get("SONARQUBE_URL") or "")
    resolved_organization = (
        organization if organization is not None else os.environ.get("SONARQUBE_ORGANIZATION")
    ) or None
    cloud_host = (urlsplit(base_url_normalized).hostname or "").lower() in _CLOUD_HOSTS
    if cloud_host and not resolved_organization:
        raise ValueError("SonarQube Cloud requires organization= (or SONARQUBE_ORGANIZATION).")

    if project_keys is None:
        env_projects = os.environ.get("SONARQUBE_PROJECTS")
        if env_projects:
            project_keys = [p.strip() for p in env_projects.split(",") if p.strip()]
    project_keys = _normalize_project_keys(project_keys)
    if not project_keys and not all_projects:
        raise ValueError("sonarqube_source requires project_keys=[...] or all_projects=True.")

    if not severities:
        raise ValueError("severities must not be empty.")
    invalid_severities = sorted(set(severities) - set(_SEVERITIES_ENUM))
    if invalid_severities:
        raise ValueError(
            f"Invalid severities {invalid_severities}; must be a subset of {_SEVERITIES_ENUM}."
        )
    if not issue_types:
        raise ValueError("issue_types must not be empty.")
    invalid_types = sorted(set(issue_types) - set(_ISSUE_TYPES_ENUM))
    if invalid_types:
        raise ValueError(
            f"Invalid issue_types {invalid_types}; must be a subset of {_ISSUE_TYPES_ENUM}."
        )
    if not (include_issues or include_hotspots or include_quality_gate):
        raise ValueError(
            "At least one of include_issues, include_hotspots, include_quality_gate must be True."
        )
    if not (1 <= max_issues_per_project <= _SEARCH_WINDOW_LIMIT):
        raise ValueError(f"max_issues_per_project must be between 1 and {_SEARCH_WINDOW_LIMIT}.")
    if not (1 <= max_hotspots_per_project <= _SEARCH_WINDOW_LIMIT):
        raise ValueError(f"max_hotspots_per_project must be between 1 and {_SEARCH_WINDOW_LIMIT}.")
    created_after = _validate_created_after(created_after)
    if timeout <= 0:
        raise ValueError("timeout must be > 0.")
    if not isinstance(allow_mass_delete, bool):
        raise ValueError("allow_mass_delete must be a bool.")
    if auth_scheme not in ("bearer", "basic"):
        raise ValueError("auth_scheme must be 'bearer' or 'basic'.")
    _check_resource_name(resource_name)

    config = _Config(
        base_url=base_url_normalized,
        organization=resolved_organization,
        project_keys=project_keys,
        all_projects=bool(all_projects),
        branch=branch,
        severities=tuple(severities),
        issue_types=tuple(issue_types),
        include_issues=include_issues,
        include_hotspots=include_hotspots,
        include_quality_gate=include_quality_gate,
        created_after=created_after,
        max_issues_per_project=max_issues_per_project,
        max_hotspots_per_project=max_hotspots_per_project,
        include_author=include_author,
        timeout=timeout,
        allow_mass_delete=allow_mass_delete,
    )

    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def sonarqube_documents():
        stats.clear()
        http = session if session is not None else _make_session(resolved_token, auth_scheme)
        try:
            client = _Client(http, config.base_url, config.timeout, check_active)
            yield from dlt_utils.guarded_rows(
                _iter_rows(client, config, dlt.current.resource_state(), stats), check_active
            )
        finally:
            if session is None:
                http.close()

    resource = sonarqube_documents()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(resource, DOCUMENT_SOURCE_ATTR, SONARQUBE_SOURCE_NAME)
    # Give the sync state its own dlt pipeline, apart from other sources.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
