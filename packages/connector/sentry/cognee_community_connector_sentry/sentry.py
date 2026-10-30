"""Sentry connector for cognee: a dlt resource that turns grouped issues into memory.

Each *grouped issue* becomes one document (never one per raw event), with a bounded
sample of its events' in-app stack frames, and is meant to be handed to
``cognee.remember``::

    await cognee.remember(
        sentry_source(auth_token="...", org_slug="acme"),
        dataset_name="sentry",
        primary_key="id",
        write_disposition="merge",  # required: the add pipeline defaults to "replace"
        self_improvement=False,  # no enrichment pass on every scheduled sync
    )
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, NODE_SET_COLUMN
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("sentry_connector")

# Sentry's SaaS data regions; a self-hosted install passes its own host
DEFAULT_HOST = "sentry.io"
API_ROOT = "/api/0"
# read-only scopes an internal integration needs: the org and project listings,
# the issue listing, and the sampled event
# the largest ``limit`` the issues endpoint accepts (API reference: "<= 100")
PAGE_SIZE = 100
# only three event endpoints are documented per issue (recommended/latest/oldest),
# so that is the hard cap on the event sample
_EVENT_SLOTS = ("recommended", "latest", "oldest")
MAX_EVENT_SAMPLE = len(_EVENT_SLOTS)
# one event is enough to explain an issue; more is opt-in
DEFAULT_EVENT_SAMPLE = 1
# frames kept per sampled event: enough for the call path into the app, bounded so a
# 200-frame framework trace can't become the document
MAX_FRAMES = 5
# when no frame is marked in-app (minified or native traces), the innermost frames
_FALLBACK_FRAMES = 3
# a run of the issues listing: 100 issues a page, so this covers 200k issues
DEFAULT_MAX_REQUESTS = 2000
# attempts per request on 5xx or network errors, backing off 1s then 2s
_RETRIES = 3
# 429s waited out before the run is cut short
_RATE_LIMIT_WAITS = 2
# a longer wait cuts the run short instead of blocking cognee's dlt worker
_MAX_WAIT = 60.0
_TIMEOUT = 30.0  # seconds
# runs in a row one issue may fail before it is given up on; it is then re-read once a
# week, so an issue whose access returns (or that was deleted) is still caught later
_MAX_ATTEMPTS = 5
_RETRY_GIVEN_UP_DAYS = 7
# an issue the listing dropped but that still answers 200 (aged out of the retention
# window) is re-checked this often instead of on every run
_AGED_RECHECK_DAYS = 7
# longest resource_name accepted: dlt turns it into a table name, and the shortest
# identifier limit among cognee's destinations (Postgres) is 63 characters. dlt appends
# "__<child>" for nested tables, so this bounds the root table's name, not every
# identifier derived from it
_MAX_RESOURCE_NAME = 63
# Fields that move on their own without changing what the document says, so they must
# not bypass the fast skip. The ones below were taken from a live organization's issue
# payload (2026-10-28) on top of the documented ones; anything rendered into a document
# must stay *out* of this list.
_NOISE_FIELDS = (
    "stats",
    "priority",
    "priorityLockedAt",
    "issueType",
    "issueCategory",
    "seerFixabilityScore",
    "seerAutofixLastTriggered",
    "seerExplorerAutofixLastTriggered",
    "filtered",
    "inbox",
    "seenBy",
    "hasSeen",
    "isSubscribed",
    "subscriptionDetails",
    "isBookmarked",
    "isPublic",
    "shareId",
    "participants",
    "pluginActions",
    "pluginIssues",
    "pluginContexts",
    "annotations",
    "lifetime",
    "sessionCount",
)
# Sentry's 404 body for a resource that is not there: {"detail": "The requested
# resource does not exist"}. Recorded 2026-10-28 against a deleted issue and an id that
# never existed on the org-scoped path, and 2026-10-29 against a never-existing id on
# the unscoped /api/0/issues/<id>/ path this client actually calls - byte-identical.
# Sentry sends no error code with it, so the message is the only signal, and it is
# matched whole: any other 404 body keeps the document.
_MISSING_DETAIL = "the requested resource does not exist"
_FRACTION = re.compile(r"\.(\d+)")
# the id in /api/0/issues/<id>/..., for messages and logs
_ID_SEGMENT = re.compile(r"(/api/0/issues)/[^/]+")
# the slot in .../events/<recommended|latest|oldest>/; the three share one rate-limit
# budget, so they share one window key
_EVENT_SLOT_SEGMENT = re.compile(r"(/events)/[^/]+")
_LINK_PART = re.compile(r'(\w+)="([^"]*)"')
# Sentry's statsPeriod is a count and a unit, e.g. "24h", "14d"; anything else comes
# back as an opaque HTTP 400 naming the endpoint rather than the argument
_STATS_PERIOD = re.compile(r"^[1-9]\d*[smhdw]$")


class SentrySourceError(RuntimeError):
    """Base class of the errors this source raises."""


class SentryAPIError(SentrySourceError):
    """Sentry answered with an error, or could not be reached."""


class SentryUnavailableError(SentryAPIError):
    """Sentry could not be reached, or kept answering 5xx: not about any one issue."""


class SentryAuthError(SentrySourceError):
    """Sentry refused the token (HTTP 401): no request can succeed, nothing is deleted."""


class SentryAccessError(SentryAPIError):
    """Sentry refused one request (HTTP 403), e.g. a scope the token does not hold."""


class SentryNotFoundError(SentryAPIError):
    """Sentry has no such resource (HTTP 404 with its "does not exist" body)."""


class SentryRateLimitedError(SentrySourceError):
    """Sentry kept rate limiting, or asked for a wait longer than this run may take."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class SentryClient:
    """Sentry REST API client: bearer token, retries, Link paging, rate-limit pacing."""

    def __init__(
        self,
        auth_token: str,
        *,
        org_slug: str,
        host: str = DEFAULT_HOST,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not auth_token:
            raise ValueError("Sentry needs an auth token (internal integration or user token)")
        if not org_slug:
            raise ValueError("Sentry needs an organization slug")
        self._token = auth_token
        self.org_slug = org_slug
        self.base_url = _base_url(host)
        # ties sync state to one organization on one install, so another org's state is
        # never reused under the same resource_name
        self.identity = _identity(self.base_url, org_slug)
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        # only a client this object made is this object's to close
        self._owns_http = http is None
        self._sleep = sleep
        # seconds this client has already spent waiting in this run. Each wait is capped
        # at _MAX_WAIT, but nothing stopped them accumulating: a run with a spent window
        # on every request slept 38 minutes over 39 waits while cognee held its
        # process-wide dlt staging lock, which is the thing the per-wait cap exists for
        self._slept = 0.0
        # {window key: epoch second that window resets}, for the windows that are used
        # up. Sentry's budgets are per endpoint, so one number for the whole client
        # would let a spent issue-detail budget stall the listing as well
        self._resume_at: dict[str, float] = {}

    def __repr__(self) -> str:
        return f"SentryClient(base_url={self.base_url!r}, org_slug={self.org_slug!r})"

    def close(self) -> None:
        """Release the connection pool. A caller-supplied client is left alone."""
        if self._owns_http:
            self._http.close()

    @property
    def issues_path(self) -> str:
        return f"{API_ROOT}/organizations/{self.org_slug}/issues/"

    @property
    def projects_path(self) -> str:
        return f"{API_ROOT}/organizations/{self.org_slug}/projects/"

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` and return its parsed JSON body."""
        body, _ = self._get_page(path, params)
        return body

    def _get_page(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        """GET ``path`` and return ``(body, next cursor)``.

        The cursor is ``None`` on the last page: see :func:`next_cursor`.
        """
        response = self._send("GET", path, params=params)
        body = self._parse(response, path)
        return body, next_cursor(response.headers.get("link"))

    def get_listing(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[list, str | None]:
        """GET a listing endpoint and return ``(records, next cursor)``.

        A body that is not a JSON array raises, so a malformed or HTML answer can never
        be read as "the organization has no issues" (which would check every stored
        issue for deletion).
        """
        body, cursor = self._get_page(path, params)
        if not isinstance(body, list):
            raise SentryAPIError(f"Sentry sent a listing that is not a JSON array for {path}")
        if any(not isinstance(record, dict) for record in body):
            raise SentryAPIError(f"Sentry sent a listing with non-object entries for {path}")
        return body, cursor

    def _send(
        self, method: str, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        failures = waits = 0
        while True:
            self._wait_for_window(path)
            try:
                response = self._http.request(
                    method, self.base_url + path, params=params, headers=headers
                )
            except httpx.TransportError as exc:
                failures += 1
                if failures == _RETRIES:
                    raise SentryUnavailableError(
                        f"Sentry could not be reached for {_endpoint(path)}"
                    ) from exc
                self._pause(2 ** (failures - 1), path)
                continue
            if response.status_code == 429:
                waits += 1
                delay = _rate_limit_wait(response)
                if waits > _RATE_LIMIT_WAITS or delay > _MAX_WAIT:
                    raise SentryRateLimitedError(f"Sentry kept rate limiting {_endpoint(path)}")
                self._pause(delay, path)
                continue
            if response.status_code >= 500:
                failures += 1
                if failures < _RETRIES:
                    self._pause(2 ** (failures - 1), path)
                    continue
            self._note_window(response, path)
            return response

    def _pause(self, delay: float, path: str) -> None:
        """Sleep, keeping the run's total wait inside ``_MAX_TOTAL_WAIT``.

        Each wait is already short; without a running total a run could still spend
        hours asleep in many short waits, holding cognee's process-wide dlt staging
        lock. Past the budget the run stops cleanly and resumes next time.
        """
        if self._slept + delay > _MAX_TOTAL_WAIT:
            raise SentryRateLimitedError(
                f"Sentry rate limiting has cost this run {self._slept:.0f}s already and "
                f"{_endpoint(path)} wants {delay:.0f}s more, over the "
                f"{_MAX_TOTAL_WAIT:.0f}s budget; the run stops and resumes next time"
            )
        self._slept += delay
        self._sleep(delay)

    def _note_window(self, response: httpx.Response, path: str) -> None:
        """Remember when a used-up rate-limit window resets.

        Sentry's rate limits are per caller and per endpoint, and an internal
        integration's token is keyed to the organization, so the budget is shared by
        everything syncing that org. ``X-Sentry-Rate-Limit-Remaining`` at zero means the
        *next* request would be a 429, so the wait is taken before it (never after the
        answer this one already produced, which would throw a good page away).
        """
        remaining = _header_number(response, "x-sentry-rate-limit-remaining")
        if remaining is not None and remaining <= 0:
            key = _window_key(path)
            self._resume_at[key] = max(
                self._resume_at.get(key, 0.0), time.time() + _reset_delay(response)
            )

    def _wait_for_window(self, path: str) -> None:
        """Wait out this endpoint's used-up window, or cut the run short if it is long."""
        key = _window_key(path)
        delay = self._resume_at.get(key, 0.0) - time.time()
        if delay <= 0:
            return
        if delay > _MAX_WAIT:
            raise SentryRateLimitedError(
                f"Sentry's rate limit for {_endpoint(path)} resets in {delay:.0f}s, "
                f"more than the {_MAX_WAIT:.0f}s this run may wait"
            )
        logger.info(
            "Sentry rate limit for %s used up; waiting %.0fs for the window to reset.",
            _endpoint(path),
            delay,
        )
        self._pause(delay, path)
        self._resume_at.pop(key, None)

    @staticmethod
    def _parse(response: httpx.Response, path: str) -> Any:
        status = response.status_code
        endpoint = _endpoint(path)
        if status == 401:
            # a revoked or wrong token: every request fails the same way, and no
            # deletion check can be confirmed, so the run must abort
            raise SentryAuthError(f"Sentry refused {endpoint} (HTTP 401); check the auth token")
        if status == 403:
            raise SentryAccessError(
                f"Sentry refused {endpoint} (HTTP 403); the token needs {_scope_for(path)}"
            )
        if status == 404:
            if _resource_missing(response):
                raise SentryNotFoundError(f"Sentry has no resource at {endpoint}")
            raise SentryAPIError(
                f"Sentry answered HTTP 404 for {endpoint} with an unrecognised body "
                f"({_detail(response)!r}); nothing is treated as deleted"
            )
        if status >= 500:
            raise SentryUnavailableError(f"Sentry answered HTTP {status} for {endpoint}")
        if status >= 400:
            raise SentryAPIError(
                f"Sentry answered HTTP {status} for {endpoint}: {_detail(response)!r}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise SentryAPIError(f"Sentry sent a non-JSON body for {endpoint}") from exc


def _base_url(host: str) -> str:
    """``https://<host>`` for a host, or the URL itself for a self-hosted install."""
    host = (host or DEFAULT_HOST).strip().rstrip("/")
    if not host:
        raise ValueError("Sentry host must not be empty")
    if "://" not in host:
        host = f"https://{host}"
    if not host.startswith(("http://", "https://")):
        raise ValueError(f"Sentry host must be an http(s) URL or a host name, got {host!r}")
    return host


def _endpoint(path: str) -> str:
    """``path`` with issue ids replaced, for messages and logs."""
    return _ID_SEGMENT.sub(r"\1/{id}", path)


def _scope_for(path: str) -> str:
    """The scope Sentry's reference lists for ``path``, to name in a 403.

    Naming all three would leave the user to guess which one is missing.
    """
    return "project:read" if path.endswith("/projects/") else "event:read"


def _window_key(path: str) -> str:
    """The endpoint whose rate-limit budget ``path`` draws on.

    Sentry meters per endpoint, and measured on a live organization the three per-issue
    event slots share one budget (15 a window) while the issue detail has its own (5)
    and the organization listing its own (10).
    """
    return _EVENT_SLOT_SEGMENT.sub(r"\1/{slot}", _endpoint(path))


def _detail(response: httpx.Response) -> str:
    """Sentry's error body is ``{"detail": "..."}`` (sometimes ``{"detail": {...}}``)."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    detail = body.get("detail")
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("code")
    return str(detail or "")


def _resource_missing(response: httpx.Response) -> bool:
    """A 404 whose body says the resource is gone, rather than any other 404.

    A dead token answers 401, not 404, so this cannot be confused with revoked access;
    any 404 body that does not say this raises instead of deleting anything.

    Matched against the whole detail rather than as a substring. Sentry sends no error
    code, so this text is the only signal a tombstone can rest on, and a 404 that merely
    *mentions* a missing resource - one naming a project, a filter or a sub-resource that
    is gone while the issue is not - must not be read as "this issue was deleted".
    """
    detail = _detail(response).strip().lower().rstrip(".")
    return detail == _MISSING_DETAIL


def next_cursor(link_header: str | None) -> str | None:
    """The cursor of the next page, or ``None`` when this was the last one.

    Sentry's ``Link`` header *always* carries a ``rel="next"`` entry, and marks the end
    of a listing with ``results="false"`` on it::

        <https://sentry.io/api/0/.../issues/?&cursor=0:0:1>; rel="previous";
          results="false"; cursor="0:0:1",
        <https://sentry.io/api/0/.../issues/?&cursor=0:100:0>; rel="next";
          results="true"; cursor="0:100:0"

    dlt's ``HeaderLinkPaginator`` follows ``rel="next"`` without reading ``results``, so
    it would page forever here; this reads ``results`` and takes the cursor from the
    header rather than following the URL.
    """
    for part in (link_header or "").split(","):
        fields = dict(_LINK_PART.findall(part))
        if fields.get("rel") != "next":
            continue
        if fields.get("results") != "true":
            return None
        cursor = fields.get("cursor")
        if not cursor:
            # a next page with no cursor would restart the listing from the top
            raise SentryAPIError("Sentry sent a next page link without a cursor")
        return cursor
    return None


def _header_number(response: httpx.Response, name: str) -> float | None:
    try:
        return float(response.headers[name])
    except (KeyError, TypeError, ValueError):
        return None


def _reset_delay(response: httpx.Response) -> float:
    """Seconds until ``X-Sentry-Rate-Limit-Reset`` (an epoch second)."""
    reset = _header_number(response, "x-sentry-rate-limit-reset")
    if reset is None:
        return 0.0
    return max(0.0, reset - time.time())


def _rate_limit_wait(response: httpx.Response) -> float:
    """How long a 429 asks the caller to wait."""
    delay = _reset_delay(response)
    if delay > 0:
        return delay
    retry_after = _header_number(response, "retry-after")
    if retry_after is not None:
        return max(0.0, retry_after)
    # no usable header: a short wait, so one missing header can't spin the retries
    return 5.0


def _identity(base_url: str, org_slug: str) -> str:
    return hashlib.sha256(f"{base_url}|{org_slug}".encode()).hexdigest()[:16]


def _row_id(issue_id: Any) -> str:
    return f"issue:{issue_id}"


def _issue_id(row_id: str) -> str:
    return row_id.removeprefix("issue:")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
# Sentry puts no bound on a title, a culprit or metadata.value: an exception
# message can carry a serialized query or a blob. These keep one issue's document the
# bounded thing the README promises.
_MAX_TITLE = 500
_MAX_CULPRIT = 300
_MAX_MESSAGE = 2000
_MAX_FRAME_LINE = 300
# total seconds one run may spend waiting out rate-limit windows. The per-wait cap keeps
# any single wait short; this keeps a run of many short waits from holding cognee's dlt
# staging lock for hours. Past it the run stops cleanly and resumes next time.
_MAX_TOTAL_WAIT = 300.0
# A stack deeper than this is not a document any more. Sentry itself keeps at most a few
# hundred frames an event, and the point of the sample is a readable trace, so the cap is
# an order of magnitude above the default rather than a guess at Sentry's own limit.
MAX_FRAMES_LIMIT = 50


def render_issue(issue: dict, samples: list[str] | None = None, *, aged: bool = False) -> dict:
    """Turn one grouped issue (and its sampled frames) into a document row.

    ``samples`` are the lines rendered by :func:`event_lines`, already bounded. With
    ``aged``, the document says it is a last known state: the issue has dropped out of
    Sentry's listing window (event retention) but still exists.
    """
    issue_id = issue.get("id")
    if not issue_id:
        raise SentryAPIError("Sentry sent an issue without an id")
    title = _clipped(_text(issue.get("title")) or f"Sentry issue {issue_id}", _MAX_TITLE)
    project = _project_slug(issue)
    level = _text(issue.get("level"))
    status = _text(issue.get("status"))
    substatus = _text(issue.get("substatus"))

    facts = [f"Project: {project or 'unknown'}"]
    if level:
        facts.append(f"Level: {level}")
    if status:
        facts.append(f"Status: {status}" + (f" ({substatus})" if substatus else ""))
    if short_id := _text(issue.get("shortId")):
        facts.append(f"Short id: {short_id}")

    lines = [title, " · ".join(facts)]
    if seen := _seen_line(issue):
        lines.append(seen)
    if culprit := _text(issue.get("culprit")):
        lines.append(f"Culprit: {_clipped(culprit, _MAX_CULPRIT)}")
    if value := _clipped(_metadata_value(issue), _MAX_MESSAGE):
        lines.append(f"Message: {value}")
    if assignee := _assignee(issue):
        lines.append(f"Assigned to: {assignee}")
    if issue.get("isUnhandled"):
        lines.append("Unhandled: the code did not catch this.")
    if aged:
        lines.append(
            "Outside Sentry's current listing window (event retention); "
            "this is the last state Sentry reported."
        )
    for sample in samples or []:
        lines.append(sample)
    if permalink := _text(issue.get("permalink")):
        lines.append(permalink)

    node_sets = [f"sentry:project:{project}"] if project else []
    if level:
        node_sets.append(f"sentry:level:{level}")
    return {
        "id": _row_id(issue_id),
        "title": title,
        "content": "\n".join(lines),
        NODE_SET_COLUMN: node_sets or None,
        "_deleted": False,
    }


def _seen_line(issue: dict) -> str:
    count = _text(issue.get("count"))
    users = _text(issue.get("userCount"))
    first, last = _date(issue.get("firstSeen")), _date(issue.get("lastSeen"))
    parts = []
    if count:
        parts.append(f"{count} event{'s' if count != '1' else ''}")
    if users:
        parts.append(f"{users} user{'s' if users != '1' else ''} affected")
    if first and last:
        parts.append(f"first seen {first}, last seen {last}")
    elif last:
        parts.append(f"last seen {last}")
    return "Seen: " + ", ".join(parts) if parts else ""


def _clipped(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, marked when anything was dropped.

    Exception titles and messages are attacker- and bug-shaped: a serialized query, a
    blob or a repr of a large object all end up in them, and this connector's whole
    pitch is a bounded document per issue. Without a cap one issue can carry hundreds
    of kilobytes into the graph and the embedding model.
    """
    if len(text) <= limit:
        return text
    # the marker counts toward the limit, or "bounded at 500" quietly meant 523
    dropped = len(text) - limit
    marker = f"… [{dropped} more characters]"
    kept = max(0, limit - len(marker))
    return text[:kept].rstrip() + marker


def _metadata_value(issue: dict) -> str:
    """The exception value (or log message) Sentry keeps on the issue's metadata."""
    metadata = issue.get("metadata") if isinstance(issue.get("metadata"), dict) else {}
    value = _text(metadata.get("value")) or _text(metadata.get("message"))
    # the title already starts with "<type>: <value>" for exceptions
    return "" if not value or value in _text(issue.get("title")) else value


def _assignee(issue: dict) -> str:
    assigned = issue.get("assignedTo")
    if not isinstance(assigned, dict):
        return ""
    name = _text(assigned.get("name")) or _text(assigned.get("email")) or _text(assigned.get("id"))
    kind = _text(assigned.get("type"))
    return f"{name} ({kind})" if name and kind and kind != "user" else name


def _project_slug(issue: dict) -> str:
    project = issue.get("project")
    return _text(project.get("slug")) if isinstance(project, dict) else ""


def _project_id(issue: dict) -> str:
    """The issue's project id, which (unlike its slug) survives a project rename."""
    project = issue.get("project")
    return _text(project.get("id")) if isinstance(project, dict) else ""


def event_lines(event: dict, slot: str, *, max_frames: int = MAX_FRAMES) -> list[str]:
    """The bounded rendering of one sampled event: its exception and in-app frames.

    Only frames marked ``in_app`` are kept, newest call last and capped at
    ``max_frames``; when Sentry marks none (minified or native traces), the innermost
    few frames are used instead. Nothing else of the event is read: breadcrumbs,
    request bodies and contexts are where the volume (and the PII) is.
    """
    exceptions = _exception_values(event)
    if not exceptions:
        return []
    frames: list[str] = []
    header = ""
    for value in exceptions:
        kind = _text(value.get("type"))
        message = _text(value.get("value"))
        header = (
            _clipped(": ".join(part for part in (kind, message) if part), _MAX_MESSAGE) or header
        )
        stack = value.get("stacktrace")
        raw = stack.get("frames") if isinstance(stack, dict) else None
        if not isinstance(raw, list):
            continue
        in_app = [frame for frame in raw if isinstance(frame, dict) and _frame_in_app(frame)]
        chosen = (
            in_app[-max_frames:]
            if in_app
            else [frame for frame in raw[-_FALLBACK_FRAMES:] if isinstance(frame, dict)]
        )
        frames = [_frame_line(frame, bool(in_app)) for frame in chosen]
        if frames:
            # The first value that carries a stack trace. Sentry does not document the
            # order of chained exceptions and nothing in the recorded responses pins it,
            # so this is "a trace from this event", not "the one that was raised".
            break
    if not header and not frames:
        return []
    lines = [f"Event sample ({slot}): {header}" if header else f"Event sample ({slot})"]
    lines.extend(f"  {_clipped(frame, _MAX_FRAME_LINE)}" for frame in frames if frame)
    return lines


def _frame_in_app(frame: dict) -> bool:
    """Whether Sentry marked this frame as application code.

    The web API serializes frames in camelCase (``inApp``), while the ingest payload a
    user may have seen in their SDK uses ``in_app``. Reading only one spelling silently
    classifies every frame as library code: recorded frames from the live API carry
    ``inApp`` (observations/experiment.json), so the camelCase key wins when present.
    """
    camel = frame.get("inApp")
    return bool(frame.get("in_app") if camel is None else camel)


def _frame_line(frame: dict, in_app: bool) -> str:
    where = (
        _text(frame.get("filename"))
        or _text(frame.get("module"))
        or _text(frame.get("absPath"))
        or _text(frame.get("abs_path"))
    )
    function = _text(frame.get("function"))
    lineno = frame.get("lineNo") if frame.get("lineNo") is not None else frame.get("lineno")
    place = f"{where}:{lineno}" if where and lineno is not None else where
    line = f"in {function}" if function else "in an unnamed frame"
    if place:
        line += f" ({place})"
    return line if in_app else f"{line} [not in-app]"


def _exception_values(event: dict) -> list[dict]:
    """``entries[type=exception].data.values`` of an event, as the API returns it."""
    entries = event.get("entries")
    if not isinstance(entries, list):
        return []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "exception":
            continue
        data = entry.get("data")
        values = data.get("values") if isinstance(data, dict) else None
        if isinstance(values, list):
            return [value for value in values if isinstance(value, dict)]
    return []


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _date(value: Any) -> str:
    moment = _normal_time(_text(value) or None)
    return moment[:10] if moment else ""


def _hash(value: Any) -> str:
    # 64 bits is plenty to notice a change, and keeps per-issue dlt state small
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode())
    return digest.hexdigest()[:16]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normal_time(value: str | None) -> str | None:
    """An ISO 8601 time as UTC ``YYYY-MM-DDTHH:MM:SSZ``, so times compare as text."""
    moment = _parse_time(value)
    return _iso(moment) if moment else None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    # Python 3.10's fromisoformat only takes 3 or 6 fractional digits
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], value.replace("Z", "+00:00"))
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class _CutShortError(Exception):
    """Internal: stop the run here, keeping the state reached so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Walker:
    """One run, writing its progress into ``state`` (dlt resource state, or a dict)."""

    def __init__(
        self,
        client: Any,
        state: dict,
        stats: dict[str, int],
        *,
        project_slugs: list[str] | None = None,
        event_sample: int = DEFAULT_EVENT_SAMPLE,
        max_frames: int = MAX_FRAMES,
        stats_period: str | None = None,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        check_active: Callable[[], None] | None = None,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed", "aged"):
            stats.setdefault(key, 0)
        # False until a run finishes, so a caller never reads a crashed run as complete
        stats.setdefault("completed", False)
        self.project_slugs = sorted(set(project_slugs or []))
        self.event_sample = event_sample
        self.max_frames = max_frames
        self.stats_period = stats_period
        self.max_requests = max_requests
        self.check_active = check_active
        self.requests = 0
        self.run_start = _iso(datetime.now(timezone.utc))
        self.scope = _hash([self.project_slugs, event_sample, max_frames, stats_period])
        # {row id: {"fp", "seen", "key", "sample", "sample_seen"}}
        self.rows: dict[str, dict] = state.setdefault("rows", {})
        # {row id: time of the last confirming GET} for issues the listing no longer
        # returns but Sentry still serves (aged out of the retention window)
        self.aged: dict[str, str] = state.setdefault("aged", {})
        self.recheck: list[str] = state.setdefault("recheck", [])
        self.queued: set[str] = set(self.recheck)
        # the selected projects' ids, resolved once the walk starts; held for the
        # whole run because _close_walk clears state["walk"] before the re-check
        # pass, which still has to know what the selection is
        self.scope_ids: list[str] = []
        # {row id: issue body} for the by-id reads of this run, so the confirm-only
        # pass before the listing never costs the pass after it a second request
        self.confirmed: dict[str, dict] = {}
        # row ids the confirm-only pass found gone (a 404, or a merge redirect). It may
        # not tombstone them itself: the listing may still return the issue, and a
        # tombstone plus an upsert for one primary key in one load lets dlt keep either
        self.gone: set[str] = set()
        self.failed: list[str] = state.setdefault("recheck_failed", [])
        # {"<issue id>|<step>": [failed reads in a row, time of the last one]}
        # {"<issue id>|<step>": [count, last run]}: the two reads an issue can fail are
        # counted apart, because giving up on its event sample must not also stop the
        # by-id read that is the only thing able to notice it was deleted
        self.attempts: dict[str, list] = state.setdefault("attempts", {})

    # -- requests ----------------------------------------------------------
    def _budget(self) -> None:
        if self.check_active is not None:
            self.check_active()
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")
        self.requests += 1

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._budget()
        try:
            return self.client.get(path, params)
        except SentryRateLimitedError:
            raise _CutShortError("rate_limit") from None

    def _get_listing(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[list, str | None]:
        self._budget()
        try:
            return self.client.get_listing(path, params)
        except SentryRateLimitedError:
            raise _CutShortError("rate_limit") from None

    def _pages(self, path: str, params: dict[str, Any], progress: dict | None = None):
        """Yield each page of a Link-paginated listing, stopping on ``results="false"``.

        With ``progress``, the cursor of the next unread page is kept there, so a run
        cut short by its budget or a 429 resumes at the page it was reading.
        """
        cursor = (progress or {}).get("cursor")
        while True:
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            used = cursor
            records, cursor = self._get_listing(path, query)
            yield records
            if cursor and cursor == used:
                # the same cursor again: the listing would repeat this page forever,
                # spend the whole budget on it and never reach the end - and a listing
                # that never completes also never confirms what is gone
                raise SentryAPIError(
                    f"Sentry returned the same page cursor twice for {_endpoint(path)}"
                )
            if progress is not None:
                progress["cursor"] = cursor
            if not cursor:
                return

    # -- selection ---------------------------------------------------------
    def _project_ids(self) -> list[int]:
        """The numeric ids of the selected project slugs (the listing filters by id).

        Resolved once per run, and only when slugs are given. A slug that resolved on an
        earlier run and no longer does has almost certainly been **renamed**: the id is
        what the listing filters on and the id survives a rename, so the last known id is
        used and the run carries on. Raising instead would fail every later run, and a
        project rename nobody told the caller about would stop the sync for good.

        A slug that has *never* resolved is still an error: syncing the whole
        organization instead would ingest projects the caller did not ask for.
        """
        if not self.project_slugs:
            return []
        known: dict[str, int] = {
            slug: int(pid) for slug, pid in self.state.get("project_ids", {}).items()
        }
        found: dict[str, int] = {}
        for page in self._pages(self.client.projects_path, {}):
            for project in page:
                slug = _text(project.get("slug"))
                if slug in self.project_slugs and project.get("id") is not None:
                    found[slug] = int(project["id"])
        unresolved = [slug for slug in self.project_slugs if slug not in found]
        renamed = [slug for slug in unresolved if slug in known]
        never = [slug for slug in unresolved if slug not in known]
        if never:
            raise SentrySourceError(
                f"Sentry organization {self.client.org_slug!r} has no project(s) "
                f"{', '.join(never)}; check project_slugs and the token's project:read scope"
            )
        for slug in renamed:
            logger.warning(
                "Sentry no longer lists a project with slug %r; it was most likely "
                "renamed, so its last known id (%s) is still being synced. Update "
                "project_slugs to the new slug.",
                slug,
                known[slug],
            )
            found[slug] = known[slug]
        self.state["project_ids"] = {slug: found[slug] for slug in self.project_slugs}
        return [found[slug] for slug in self.project_slugs]

    def _in_scope(self, issue: dict) -> bool:
        """Whether the issue still belongs to the selected projects.

        Sentry filters the listing server-side by project *id*, so this is only a guard
        against a row that arrived from a wider query. It matches on the id, because a
        project rename changes the slug while the id and the server-side filter stay
        put; matching on the slug would forget every live issue of a renamed project.
        An issue whose project the payload does not describe is kept: the server already
        applied the filter, and absence of evidence must never delete a live issue.
        """
        if not self.project_slugs:
            return True
        issue_project = _project_id(issue)
        if not issue_project:
            return True
        wanted = set(self.scope_ids)
        if not wanted:
            return True
        return issue_project in wanted

    def _listing_params(self, project_ids: list[int]) -> dict[str, Any]:
        params: dict[str, Any] = {
            # Sentry's issue listing defaults to is:unresolved (DEFAULT_QUERY in
            # group_index/index.py). An explicit empty query returns every status, so
            # resolving or archiving an issue does not look like a deletion.
            "query": "",
            # the only sort whose order does not change as events arrive is not
            # available, so paging is kept stable by sorting on the documented
            # "date" (last seen) and by re-listing everything each run
            "sort": "date",
            "limit": PAGE_SIZE,
        }
        if self.stats_period is not None:
            params["statsPeriod"] = self.stats_period
        if project_ids:
            params["project"] = project_ids
        return params

    # -- rows --------------------------------------------------------------
    def _process(self, issue: dict, *, aged: bool = False) -> dict | None:
        """The row to emit for one issue, or None when its document did not change."""
        row_id = _row_id(issue["id"])
        self.stats["scanned"] += 1
        stable = {k: v for k, v in issue.items() if k not in _NOISE_FIELDS}
        input_key = _hash([self.scope, stable, aged])
        stored = self.rows.get(row_id)
        if stored and stored.get("key") == input_key:
            self.stats["skipped"] += 1
            return None
        if not self._in_scope(issue):
            # the issue belongs to a project that is no longer selected
            return self._tombstone(row_id) if stored else None

        last_seen = _normal_time(_text(issue.get("lastSeen")) or None)
        sample, sample_seen = self._sample_for(issue, stored, last_seen)
        row = render_issue(issue, sample, aged=aged)
        fingerprint = _hash(row)
        self.rows[row_id] = {
            "fp": fingerprint,
            "seen": last_seen,
            "key": input_key,
            "sample": sample,
            "sample_seen": sample_seen,
            "sample_scope": self.scope,
        }
        if stored and stored.get("fp") == fingerprint:
            self.stats["skipped"] += 1
            return None
        return row

    def _sample_for(
        self, issue: dict, stored: dict | None, last_seen: str | None
    ) -> tuple[list[str], str | None]:
        """The event sample for an issue, re-read only when new events arrived.

        A status change (resolve, archive, assign) does not move ``lastSeen``, so the
        stored sample is reused and the run costs one request per issue, not two.
        """
        if self.event_sample <= 0:
            return [], last_seen
        reusable = (
            stored is not None
            and stored.get("sample") is not None
            and stored.get("sample_seen") == last_seen
            # a changed event_sample or max_frames changes the scope, so the stored
            # sample is not reused after the caller asks for a different one
            and stored.get("sample_scope") == self.scope
        )
        if reusable and stored is not None:
            return list(stored["sample"]), stored.get("sample_seen")
        lines: list[str] = []
        seen_events: set[str] = set()
        for slot in _EVENT_SLOTS[: self.event_sample]:
            path = f"{API_ROOT}/issues/{issue['id']}/events/{slot}/"
            try:
                event = self._get(path)
            except SentryNotFoundError:
                # no event within the retention window, or none matching this slot
                continue
            if not isinstance(event, dict):
                raise SentryAPIError(f"Sentry sent a non-object event for {_endpoint(path)}")
            # the slots overlap: on an issue with few events "recommended", "latest" and
            # "oldest" are often the same event, and rendering it once per slot would put
            # the same trace in the document two or three times
            event_id = _text(event.get("id")) or _text(event.get("eventID"))
            if event_id and event_id in seen_events:
                continue
            if event_id:
                seen_events.add(event_id)
            lines.extend(event_lines(event, slot, max_frames=self.max_frames))
        return lines, last_seen

    def _tombstone(self, row_id: str) -> dict:
        self.rows.pop(row_id, None)
        self.aged.pop(row_id, None)
        for key in (f"{_issue_id(row_id)}|{step}" for step in ("listing", "refresh")):
            self.attempts.pop(key, None)
        self.stats["deleted"] += 1
        return {"id": row_id, "_deleted": True}

    # -- the listing -------------------------------------------------------
    def _walk(self) -> Iterator[dict]:
        """List every issue of the selection; resumable through ``state["walk"]``.

        The whole listing is re-read on every run on purpose: Sentry writes no
        ``updated`` field and leaves ``lastSeen`` untouched when an issue is resolved,
        archived, assigned or re-prioritised, so no time-based cursor can catch those
        changes - and a complete listing is also what deletion detection needs.
        """
        walk = self.state["walk"] = self.state.get("walk") or {
            "cursor": None,
            "seen": [],
            "projects": None,
        }
        if walk["projects"] is None:
            walk["projects"] = self._project_ids()
        self.scope_ids = [str(pid) for pid in walk["projects"]]
        seen = set(walk["seen"])
        for page in self._pages(
            self.client.issues_path, self._listing_params(walk["projects"]), walk
        ):
            for issue in page:
                if not issue.get("id"):
                    raise SentryAPIError("Sentry listed an issue without an id")
                row_id = _row_id(issue["id"])
                if row_id not in seen:
                    seen.add(row_id)
                    walk["seen"].append(row_id)
                # back in the listing: not aged out any more
                self.aged.pop(row_id, None)
                row = self._guarded(_text(issue["id"]), "listing", self._process, issue)
                if row is not None:
                    yield row
        self._close_walk(seen)

    def _close_walk(self, seen: set[str]) -> None:
        """A complete listing: queue stored issues it did not return for a GET check.

        Only a listing that ran to its last page gets here, so a run cut short part way
        through never queues (and never deletes) anything.
        """
        horizon = _iso(_parse_time(self.run_start) - timedelta(days=_AGED_RECHECK_DAYS))
        for row_id in self.rows:
            if row_id in seen:
                continue
            # an issue already confirmed present while out of the listing window is
            # re-checked weekly instead of on every run
            if self.aged.get(row_id, "") > horizon:
                continue
            self._queue(_issue_id(row_id))
        self.state["walk"] = None

    # -- re-reads by id: the deletion checks -------------------------------
    def _queue(self, issue_id: str) -> None:
        if issue_id not in self.queued:
            self.queued.add(issue_id)
            self.recheck.append(issue_id)

    def _refresh(self, issue_id: str, *, confirm_only: bool = False) -> dict | None:
        """One ``GET /issues/{id}/``: the only thing that may tombstone an issue.

        Tombstoned on a confirmed 404 (``DELETE`` is asynchronous: it answers 202 and
        the issue then 404s), or when Sentry answers 200 with a *different* id - it
        keeps a redirect from a merged-away issue to the surviving one, so that old id
        is gone and has to be forgotten too. Anything else keeps the document.

        With ``confirm_only`` the live answer is recorded and nothing is emitted. That
        is what the queue pass *before* the listing wants: whether the issue is still
        outside the listing is not known yet, and marking it aged there would emit a
        "last known state" document for an issue the listing is about to return - two
        rows for one primary key in one load, with the stale one free to win.
        """
        row_id = _row_id(issue_id)
        if not confirm_only and row_id in self.gone:
            # the pass before the listing already read this id as gone, and a complete
            # listing has since failed to return it, so the tombstone is now safe - and
            # needs no second request
            return self._tombstone(row_id) if row_id in self.rows else None
        try:
            issue = self.confirmed.get(row_id) or self._get(f"{API_ROOT}/issues/{issue_id}/")
        except SentryNotFoundError:
            if confirm_only:
                self.gone.add(row_id)
                return None
            return self._tombstone(row_id) if row_id in self.rows else None
        if not isinstance(issue, dict) or not issue.get("id"):
            raise SentryAPIError(f"Sentry sent no issue for /api/0/issues/{{id}}/ ({issue_id})")
        if _text(issue["id"]) != _text(issue_id):
            logger.info(
                "Sentry redirected issue %s to %s (merged); forgetting the old id.",
                issue_id,
                issue["id"],
            )
            if confirm_only:
                self.gone.add(row_id)
                return None
            return self._tombstone(row_id) if row_id in self.rows else None
        if row_id not in self.rows:
            return None
        if confirm_only:
            # alive, and that is all this pass may conclude; the listing decides the
            # rest, and _close_walk queues the id again if it really did not return it
            self.confirmed[row_id] = issue
            return None
        if not self._in_scope(issue):
            # left the selection: forgotten, and never counted as aged
            return self._tombstone(row_id)
        # present, but the listing no longer returns it: aged out of the retention
        # window, kept with its last known state and re-checked weekly
        self.aged[row_id] = self.run_start
        self.stats["aged"] += 1
        return self._process(issue, aged=True)

    def _recheck_queue(self, *, confirm_only: bool = False) -> Iterator[dict]:
        while self.recheck:
            issue_id = self.recheck[0]
            # ids that already failed this run, or were given up on, are skipped; a
            # cut-short read raises before the pop, so the id stays queued
            skip = issue_id in self.failed or self._given_up(issue_id)
            row = (
                None
                if skip
                else self._guarded(
                    issue_id, "refresh", self._refresh, issue_id, confirm_only=confirm_only
                )
            )
            self.recheck.pop(0)
            self.queued.discard(issue_id)
            if row is not None:
                yield row

    def _guarded(
        self,
        issue_id: str,
        step_name: str,
        step: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run one issue's step; an API error on it is retried next run, not fatal.

        A refused token (:class:`SentryAuthError`) and a rate limit still stop the run:
        neither is about one issue. ``step_name`` separates the give-up counters of the
        listing read and the by-id read, so one failing read never silences the other.
        """
        counter = f"{issue_id}|{step_name}"
        try:
            result = step(*args, **kwargs)
        except SentryAPIError as exc:
            # an outage (5xx, network) says nothing about the issue, so only errors
            # about this issue (403, an unrecognised 404) count toward giving up
            if not isinstance(exc, SentryUnavailableError):
                attempts = self.attempts.get(counter, [0, None])[0] + 1
                self.attempts[counter] = [attempts, self.run_start]
                if attempts >= _MAX_ATTEMPTS:
                    # an error that never clears: stop re-reading it by id (a listing
                    # that returns it still tries); a failed read never deletes
                    if attempts == _MAX_ATTEMPTS:
                        self.stats["abandoned"] = self.stats.get("abandoned", 0) + 1
                        logger.warning("Sentry kept failing on one issue; giving up: %s", exc)
                    else:
                        logger.debug("Sentry still failing on a given-up issue: %s", exc)
                    return None
            self.stats["failed"] += 1
            if issue_id not in self.failed:
                self.failed.append(issue_id)
            logger.warning("Sentry failed on one issue, retried next run: %s", exc)
            return None
        self.attempts.pop(counter, None)
        return result

    def _given_up(self, issue_id: str) -> bool:
        """Given up on the by-id read, and re-read less than a week ago."""
        count, last = self.attempts.get(f"{issue_id}|refresh", [0, None])
        if count < _MAX_ATTEMPTS:
            return False
        retry_from = _parse_time(self.run_start) - timedelta(days=_RETRY_GIVEN_UP_DAYS)
        return (last or "") > _iso(retry_from)

    # -- the run -----------------------------------------------------------
    def _check_state(self) -> None:
        identity = getattr(self.client, "identity", None)
        known = self.state.get("identity")
        if identity and known and known != identity:
            raise SentrySourceError(
                "This resource_name holds another Sentry organization's sync state. Use "
                "one resource_name per organization (and per host); reusing it would "
                "forget that organization's issues."
            )
        if identity:
            self.state["identity"] = identity
        if self.state.get("scope") not in (None, self.scope):
            # the selection changed: list everything again (the listing is full anyway),
            # re-check every stored issue, and give each a fresh set of attempts
            self.state.update(walk=None)
            self.aged.clear()
            self.attempts.clear()
        self.state["scope"] = self.scope

    def run(self) -> Iterator[dict]:
        self._check_state()
        # last run's failures go to the back of the queue, so they can't block it
        for issue_id in self.failed:
            self._queue(issue_id)
        self.failed.clear()
        # the pass before the listing only confirms what is still alive (and forgets
        # what 404s); only the pass after a complete listing knows what is missing
        yield from self._recheck_queue(confirm_only=True)
        yield from self._walk()
        yield from self._recheck_queue()


def _iter_rows(
    client: Any,
    state: dict,
    stats: dict[str, int],
    *,
    project_slugs: list[str] | None = None,
    event_sample: int = DEFAULT_EVENT_SAMPLE,
    max_frames: int = MAX_FRAMES,
    stats_period: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield the changed rows of a Sentry organization.

    Independent of dlt, so tests can drive it with a plain dict.
    """
    walker = _Walker(
        client,
        state,
        stats,
        project_slugs=project_slugs,
        event_sample=event_sample,
        max_frames=max_frames,
        stats_period=stats_period,
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.run()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        stats["completed"] = False
        # error, not warning: the load succeeds, so this line and the stats are the only
        # places a cut-short run is visible at all
        logger.error(
            "Sentry sync stopped early (%s) and did not finish; it resumes on the next run.",
            cut.reason,
        )
    except (SentryAuthError, SentryAccessError):
        # recorded as well as raised: the raise is what keeps dlt from committing, but a
        # host that holds the stats dict can then tell a refused token from any other
        # failure without parsing the message
        stats["failed"] = max(1, stats.get("failed", 0))
        stats["failed_auth"] = 1
        raise
    else:
        stats["completed"] = True


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def sentry_source(
    *,
    auth_token: str | None = None,
    org_slug: str | None = None,
    host: str = DEFAULT_HOST,
    resource_name: str | None = None,
    project_slugs: list[str] | None = None,
    event_sample: int = DEFAULT_EVENT_SAMPLE,
    max_frames: int = MAX_FRAMES,
    stats_period: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
):
    """Return a ``dlt`` resource that yields a Sentry org's issues for ``remember``.

    Args:
        auth_token: An internal integration token (Settings → Developer Settings →
            Internal Integration) or a user auth token, with the ``org:read``,
            ``project:read`` and ``event:read`` scopes.
        org_slug: The organization to sync.
        host: API host of the data region (``sentry.io``, ``de.sentry.io``) or the base
            URL of a self-hosted install.
        resource_name: Stable name of this connection, one per organization. It scopes
            the sync state and the cleanup of forgotten issues, so keep it fixed.
            Defaults to ``sentry_<org slug>``, with a suffix derived from
            ``project_slugs`` when a selection is given.
        project_slugs: Only sync these projects (default: every project the token can
            read). Under the default ``resource_name`` the selection is part of that
            name, so changing it starts a fresh scope and leaves the old selection's
            documents in the dataset; pass a fixed ``resource_name`` across the change
            to have the issues that leave be forgotten instead.
        event_sample: Events sampled per issue for the stack trace, 0 to
            ``MAX_EVENT_SAMPLE`` (recommended, then latest, then oldest).
        max_frames: In-app frames kept per sampled event.
        stats_period: ``statsPeriod`` for the listing (e.g. ``"14d"``); by default none
            is sent. Whatever window Sentry then applies, this connector keeps issues
            that drop out of it instead of forgetting them.
        max_requests: Request budget of one run, counted in reads rather than in HTTP
            attempts (a read retried through an outage is charged once). A big backfill
            finishes over several runs, and a cut-short run resumes at the page it was
            reading.
        service: Pre-built client, mainly an injection point for tests.
        check_active: Optional host authorization checkpoint, called before each request.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.

    A run stopped early by ``max_requests`` or by a rate-limit wait longer than
    ``_MAX_WAIT`` **returns normally** rather than raising: the rows it read are kept
    and the next run resumes. That is only visible in ``cognee_sync_stats``, which then
    carries ``failed_budget`` or ``failed_rate_limit`` and a non-zero ``failed``, so a
    caller that needs to know whether a sync completed has to read them. Errors that
    are not about one issue - a refused token, a malformed listing, an outage - raise,
    and dlt then commits neither rows nor state.
    """
    import dlt

    found = getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    if found < 2:
        raise RuntimeError(
            f"Sentry sync needs cognee's document sync version 2 or later (cognee 1.6.4+), "
            f"for per-row node sets; this cognee reports {found}."
        )
    if not 0 <= event_sample <= MAX_EVENT_SAMPLE:
        raise ValueError(f"event_sample must be between 0 and {MAX_EVENT_SAMPLE}")
    if not 1 <= max_frames <= MAX_FRAMES_LIMIT:
        # without a ceiling the caller can undo the bound on the document (and on the
        # sample cached in dlt state) just by asking for more frames
        raise ValueError(f"max_frames must be between 1 and {MAX_FRAMES_LIMIT}")
    if stats_period is not None and not _STATS_PERIOD.match(_text(stats_period)):
        raise ValueError('stats_period must be a count and a unit, e.g. "24h" or "14d", or None')
    if max_requests < 3:
        # a listing page, an event read and a deletion check must fit, or a run stalls
        raise ValueError("max_requests must be at least 3")
    if project_slugs is not None and (
        not isinstance(project_slugs, list)
        or not project_slugs
        or any(not _text(slug) for slug in project_slugs)
    ):
        raise ValueError("project_slugs must be a non-empty list of project slugs, or None")
    if resource_name is not None:
        if not isinstance(resource_name, str) or not resource_name.strip():
            raise ValueError("resource_name must be a non-empty name, one per organization")
        if resource_name != resource_name.strip():
            # dlt normalizes the name into a table name, so a padded name writes to the
            # unpadded one's table while keeping its own state: two sources, one table
            raise ValueError("resource_name must not start or end with whitespace")
        if len(resource_name) > _MAX_RESOURCE_NAME:
            # dlt derives table and state keys from it; keep it short enough to stay
            # readable in the destination's identifiers
            raise ValueError(f"resource_name must be at most {_MAX_RESOURCE_NAME} characters")
        if NamingConvention().normalize_table_identifier(resource_name) != resource_name:
            # state is kept under the name as given while rows go to the normalized
            # table, so "Sentry_A" and "sentry-a" would share one table and keep two
            # separate states - each run then tombstones what the other just wrote
            raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")
    if service is None:
        # fail here rather than on the first row, while the same checks the client makes
        # stay in one place
        if not auth_token:
            raise ValueError(
                "sentry_source needs auth_token (an internal integration or user token), "
                "or a prepared client in service="
            )
        if not _text(org_slug):
            raise ValueError("sentry_source needs org_slug, the organization to sync")

    def _client() -> Any:
        """A client per run, so its connection pool does not outlive the run."""
        return SentryClient(auth_token or "", org_slug=org_slug or "", host=host)

    name = resource_name or _default_resource_name(
        getattr(service, "org_slug", org_slug), project_slugs
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            **dlt_utils.NODE_SET_COLUMN_HINT,
        },
    )
    def sentry_issues():
        stats.clear()
        # completed starts False so a run that dies mid-stream never reads as finished
        stats.update(scanned=0, skipped=0, deleted=0, failed=0, aged=0, completed=False)
        client = service if service is not None else _client()
        try:
            rows = _iter_rows(
                client,
                dlt.current.resource_state(),
                stats,
                project_slugs=project_slugs,
                event_sample=event_sample,
                max_frames=max_frames,
                stats_period=stats_period,
                max_requests=max_requests,
                check_active=check_active,
            )
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            # a run that raised still has to give its sockets back; an injected client
            # belongs to the caller (the tests), so it is left open
            if service is None:
                client.close()

    resource = sentry_issues()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "sentry")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource


def _default_resource_name(org_slug: str | None, project_slugs: Sequence[str] | None = None) -> str:
    """``sentry_<org slug>``, plus a suffix when the source selects projects.

    Two organizations in one dataset must not collide, and neither must two sources for
    the *same* organization that select different projects: they share one dlt resource
    state, and each run would then treat the other's issues as gone from its selection
    and tombstone them, run after run, without ever converging. The selection is folded
    into the default name so that case cannot arise by accident.
    """
    slug = _text(org_slug).lower()
    # dlt table names allow letters, digits and underscores
    cleaned = re.sub(r"[^a-z0-9_]+", "_", slug).strip("_")
    if not cleaned:
        raise ValueError(
            "sentry_source needs an org_slug (or a resource_name) to name the resource"
        )
    selected = sorted({_text(p) for p in (project_slugs or []) if _text(p)})
    suffix = f"_{_hash(selected)[:8]}" if selected else ""
    # a long org slug plus the suffix can outgrow what dlt will take as a table name;
    # the cleaned slug is what gets shortened, so the suffix stays distinguishing
    room = _MAX_RESOURCE_NAME - len("sentry_") - len(suffix)
    return f"sentry_{cleaned[:room].rstrip('_')}{suffix}"
