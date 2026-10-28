"""Trigger.dev connector for cognee: a dlt resource that turns one environment's jobs into memory.

One source is one Trigger.dev environment (a project's dev, staging, prod or preview
environment), read with that environment's secret key. It yields the environment's
schedules, error groups, failed runs and deployments as flat document rows, and is
meant to be handed to ``cognee.remember``::

    await cognee.remember(
        triggerdev_source(api_key="tr_prod_...", resource_name="triggerdev_prod"),
        dataset_name="triggerdev",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
    )

Schedules are listed in full on every run. One that is missing from a complete listing
is fetched by id before it is forgotten, and only a 404 forgets it. Error groups, runs
and deployments are history: each run reads the last ``lookback_days`` of them, and
records that age out of that window stay in memory.

Nothing volatile is rendered (a schedule's next run, an error group's occurrence count,
a live run's duration), so an unchanged record renders to the same text and is skipped.

On HTTP 429, a spent request budget or a key that stops working mid-run, the source
stops without sleeping or raising, keeps the state it reached and reports the reason in
``cognee_sync_stats``. Errors name the status and endpoint, never the key, a record id
or a response body.
"""

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, NODE_SET_COLUMN

logger = get_logger("triggerdev_connector")

DEFAULT_API_URL = "https://api.trigger.dev"
# what one environment's secret key can list
KINDS = ("schedules", "errors", "runs", "deployments")
# statuses that end a run without success, from the run status enum in the API reference
FAILED_RUN_STATUSES = ("FAILED", "CRASHED", "SYSTEM_FAILURE", "INTERRUPTED")
RUN_STATUSES = (
    "PENDING_VERSION",
    "DELAYED",
    "QUEUED",
    "EXECUTING",
    "REATTEMPTING",
    "FROZEN",
    "COMPLETED",
    "CANCELED",
    *FAILED_RUN_STATUSES,
)
# the largest page[size] that runs (10-100), errors (1-100) and deployments (5-100) accept
PAGE_SIZE = 100
# the schedule listing documents no maximum for perPage, so it gets the same page size
SCHEDULE_PAGE_SIZE = 100
# the runs list answers with the last 7 days when no createdAt filter is given
DEFAULT_LOOKBACK_DAYS = 7
MAX_LOOKBACK_DAYS = 90
# the API allows 1,500 requests a minute; one run stays far below that
DEFAULT_MAX_REQUESTS = 500
# stop early when a rate-limit header says this few requests are left, if the API sends one
_REQUESTS_RESERVE = 50
# a walk longer than this is a pagination bug, not a big environment (100k records)
_MAX_PAGES = 1000
# how much of an error message and stack trace a document keeps
_MESSAGE_LIMIT = 2000
_STACK_LINES = 15
# (connect, read) seconds; our own choice, Trigger.dev documents no expected latency
_TIMEOUT = (10, 30)
# attempts per request on 5xx or network errors, backing off 1s then 2s
_RETRIES = 3
# git fields of a deployment worth remembering; remoteUrl is left out, it can carry a token
_GIT_FIELDS = (
    ("commitRef", "Branch"),
    ("commitSha", "Commit"),
    ("commitMessage", "Commit message"),
    ("commitAuthorName", "Commit author"),
    ("pullRequestNumber", "Pull request"),
    ("pullRequestTitle", "Pull request title"),
)
# record paths whose last segment is an id, replaced in error messages
_RECORD_PATHS = ("/api/v1/schedules/", "/api/v3/runs/")


class TriggerDevSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class TriggerDevAPIError(TriggerDevSourceError):
    """Trigger.dev answered with an error, or could not be reached."""


class TriggerDevAuthError(TriggerDevSourceError):
    """Trigger.dev rejected the secret key (HTTP 401)."""


class TriggerDevAccessError(TriggerDevSourceError):
    """The key may not read an endpoint (HTTP 403), e.g. a named key with a narrow preset."""


class TriggerDevRateLimitedError(TriggerDevSourceError):
    """Trigger.dev answered HTTP 429."""


class TriggerDevNotFoundError(TriggerDevAPIError):
    """The record was deleted or never existed (HTTP 404)."""


def _int_header(headers: Any, name: str) -> int | None:
    try:
        return int(headers.get(name))
    except (TypeError, ValueError):
        return None


def _endpoint(path: str) -> str:
    """The path with a record id replaced, so error messages never carry one."""
    for prefix in _RECORD_PATHS:
        if path.startswith(prefix):
            return prefix + "{id}"
    return path


def _base_url(api_url: str) -> str:
    """The API origin. A path would be prefixed to every request path, so it is refused."""
    parsed = urlparse((api_url or "").strip())
    local = parsed.hostname in ("localhost", "127.0.0.1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("api_url must be an https URL (plain http only for localhost)")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("api_url must be a host such as https://api.trigger.dev")
    if parsed.path.strip("/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("api_url must be the API origin only, e.g. https://api.trigger.dev")
    return f"{parsed.scheme}://{parsed.netloc}"


class TriggerDevClient:
    """Minimal synchronous REST client for the Trigger.dev management API.

    After every successful call ``rate_limit`` holds the requests left in the current
    window, when the API reports it. Tests can pass any object with the same
    ``request`` method and ``rate_limit`` attribute instead.
    """

    def __init__(
        self,
        api_key: str,
        *,
        api_url: str = DEFAULT_API_URL,
        sleep: Callable[[float], None] = time.sleep,
    ):
        key = (api_key or "").strip()
        # reject early and without echoing the key: requests would put it in its exception
        if not key or not key.isascii() or not key.isprintable() or " " in key:
            raise ValueError("The Trigger.dev secret key is empty or has an invalid format")
        self._api_key = key
        self.api_url = _base_url(api_url)
        self._sleep = sleep
        self.rate_limit: dict[str, int | None] = {}

    def __repr__(self) -> str:
        return f"TriggerDevClient(api_url={self.api_url!r}, api_key=<redacted>)"

    def request(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        error = TriggerDevAPIError(f"Trigger.dev request failed for {_endpoint(path)}")
        for attempt in range(_RETRIES):
            if attempt:
                self._sleep(2 ** (attempt - 1))
            try:
                response = requests.get(
                    self.api_url + path, params=params, headers=headers, timeout=_TIMEOUT
                )
            except requests.RequestException as exc:
                error = TriggerDevAPIError(f"Trigger.dev request failed: {type(exc).__name__}")
                continue
            if response.status_code < 500:
                return self._parse(response, path)
            error = TriggerDevAPIError(
                f"Trigger.dev request failed: HTTP {response.status_code} for {_endpoint(path)}"
            )
        raise error

    def _parse(self, response: Any, path: str) -> dict[str, Any]:
        status = response.status_code
        endpoint = _endpoint(path)
        if status == 401:
            raise TriggerDevAuthError("Trigger.dev rejected the secret key (HTTP 401)")
        if status == 403:
            raise TriggerDevAccessError(f"The Trigger.dev key may not read {endpoint} (HTTP 403)")
        if status == 404:
            raise TriggerDevNotFoundError(f"Trigger.dev found nothing at {endpoint} (HTTP 404)")
        if status == 429:
            raise TriggerDevRateLimitedError("Trigger.dev rate limit reached (HTTP 429)")
        if status != 200:
            raise TriggerDevAPIError(f"Trigger.dev request failed: HTTP {status} for {endpoint}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        # every endpoint used answers with an object
        if not isinstance(payload, dict):
            raise TriggerDevAPIError(f"Trigger.dev sent an unreadable answer for {endpoint}")
        self.rate_limit = {"remaining": _int_header(response.headers, "x-ratelimit-remaining")}
        return payload


# ---------------------------------------------------------------------------
# Rendering: deterministic, nothing volatile, never payloads or outputs.
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _lines(fields: list[tuple[str, str]]) -> list[str]:
    return [f"{label}: {value}" for label, value in fields if value]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " [cut]"


def _moment(stamp: Any) -> datetime | None:
    text = _text(stamp)
    if not text:
        return None
    try:
        # python 3.10 can't read a trailing "Z"
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _day(stamp: Any) -> str:
    moment = _moment(stamp)
    return moment.strftime("%Y-%m-%d") if moment else _text(stamp)


def _minute(stamp: Any) -> str:
    moment = _moment(stamp)
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else _text(stamp)


def _record_id(record: Any) -> str | None:
    record_id = record.get("id") if isinstance(record, dict) else None
    return record_id.strip() if isinstance(record_id, str) and record_id.strip() else None


def _row(row_id: str, title: str, lines: list[str], node_set: str) -> dict[str, Any]:
    return {
        "id": row_id,
        "title": title,
        "content": "\n".join(lines).strip() or title,
        # cognee namespaces it under the source tag: triggerdev:task:<task id>
        NODE_SET_COLUMN: [node_set],
        "_deleted": False,
    }


def _task_node_set(task: str, fallback: str) -> str:
    return f"task:{task}" if task else fallback


def render_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    generator = schedule.get("generator")
    generator = generator if isinstance(generator, dict) else {}
    task = _text(schedule.get("task"))
    expression = _text(generator.get("expression"))
    description = _text(generator.get("description"))
    title = f"Schedule of {task or 'an unknown task'}: {description or expression or 'no cron'}"
    fields = [
        ("Task", task),
        ("Cron", expression),
        ("Runs", description),
        ("Timezone", _text(schedule.get("timezone")) or "UTC"),
        ("Status", "active" if schedule.get("active") else "inactive"),
        ("Type", _text(schedule.get("type")).lower()),
        ("External ID", _text(schedule.get("externalId"))),
        ("Deduplication key", _text(schedule.get("deduplicationKey"))),
    ]
    return _row(
        f"schedule:{_record_id(schedule)}",
        title,
        _lines(fields),
        _task_node_set(task, "schedules"),
    )


def render_error(group: dict[str, Any]) -> dict[str, Any]:
    task = _text(group.get("taskIdentifier"))
    error_type = _text(group.get("errorType")) or "Error"
    message = _text(group.get("errorMessage"))
    headline = _clip(message.splitlines()[0], 120) if message else ""
    title = f"{error_type} in {task or 'an unknown task'}"
    fields = [
        ("Task", task),
        ("Error type", error_type),
        ("Status", _text(group.get("status")).lower()),
        ("First seen", _day(group.get("firstSeen"))),
        # a day, not the instant: an error that keeps recurring re-renders once a day at most
        ("Last seen", _day(group.get("lastSeen"))),
        ("Resolved on", _day(group.get("resolvedAt"))),
        ("Ignored until", _day(group.get("ignoredUntil"))),
        ("Fingerprint", _text(group.get("fingerprint"))),
    ]
    lines = _lines(fields)
    if message:
        lines += ["", "Message:", _clip(message, _MESSAGE_LIMIT)]
    return _row(
        f"error:{_record_id(group)}",
        f"{title}: {headline}" if headline else title,
        lines,
        _task_node_set(task, "errors"),
    )


def _last_error(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = sorted(attempts, key=lambda a: (_text(a.get("createdAt")), _text(a.get("id"))))
    errors = [a["error"] for a in ordered if isinstance(a.get("error"), dict)]
    return errors[-1] if errors else None


def _run_detail_lines(detail: dict[str, Any]) -> list[str]:
    schedule = detail.get("schedule")
    schedule = schedule if isinstance(schedule, dict) else {}
    attempts = [a for a in detail.get("attempts") or [] if isinstance(a, dict)]
    lines = _lines(
        [
            ("Schedule", _text(schedule.get("id"))),
            ("Attempts", str(len(attempts)) if attempts else ""),
        ]
    )
    error = _last_error(attempts)
    if error:
        name = _text(error.get("name")) or "Error"
        message = _clip(_text(error.get("message")), _MESSAGE_LIMIT)
        lines += ["", f"Error: {name}: {message}" if message else f"Error: {name}"]
        stack = _text(error.get("stackTrace")).splitlines()[:_STACK_LINES]
        if stack:
            lines += ["Stack trace (top):", *stack]
    return lines


def render_run(run: dict[str, Any], detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """A run as listed, plus the schedule and last error a retrieved run adds."""
    run_id = _record_id(run)
    task = _text(run.get("taskIdentifier"))
    status = _text(run.get("status")).upper()
    finished = _text(run.get("finishedAt"))
    duration = run.get("durationMs")
    tags = run.get("tags")
    fields = [
        ("Task", task),
        ("Status", status),
        ("Version", _text(run.get("version"))),
        ("Created", _minute(run.get("createdAt"))),
        ("Started", _minute(run.get("startedAt"))),
        ("Finished", _minute(finished)),
        # a live run's duration grows on every read, so only a finished one is rendered
        (
            "Duration",
            f"{duration / 1000:.1f}s"
            if finished and isinstance(duration, int | float) and not isinstance(duration, bool)
            else "",
        ),
        (
            "Tags",
            ", ".join(sorted({_text(t) for t in tags} - {""})) if isinstance(tags, list) else "",
        ),
        ("Test run", "yes" if run.get("isTest") is True else ""),
    ]
    lines = _lines(fields)
    if detail:
        lines += _run_detail_lines(detail)
    title = f"{task or 'Unknown task'} run {run_id}: {status.lower() or 'unknown'}"
    return _row(f"run:{run_id}", title, lines, _task_node_set(task, "runs"))


def _git_lines(git: Any) -> list[tuple[str, str]]:
    if not isinstance(git, dict):
        return []
    fields = []
    for key, label in _GIT_FIELDS:
        value = git.get(key)
        if isinstance(value, bool) or not isinstance(value, str | int):
            continue
        text = _text(value)
        if key == "commitSha":
            text = text[:12]
        elif key == "commitMessage":
            text = _clip(text.splitlines()[0], 200) if text else ""
        fields.append((label, text))
    return fields


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        name, message = _text(error.get("name")), _text(error.get("message"))
        return _clip(": ".join(part for part in (name, message) if part), _MESSAGE_LIMIT)
    return _clip(error.strip(), _MESSAGE_LIMIT) if isinstance(error, str) else ""


def render_deployment(deployment: dict[str, Any]) -> dict[str, Any]:
    deployment_id = _record_id(deployment)
    version = _text(deployment.get("version"))
    status = _text(deployment.get("status")).upper()
    runtime = " ".join(
        part
        for part in (_text(deployment.get("runtime")), _text(deployment.get("runtimeVersion")))
        if part
    )
    fields = [
        ("Version", version),
        ("Status", status),
        ("Short code", _text(deployment.get("shortCode"))),
        ("Created", _minute(deployment.get("createdAt"))),
        ("Deployed", _minute(deployment.get("deployedAt"))),
        ("Runtime", runtime),
        *_git_lines(deployment.get("git")),
    ]
    lines = _lines(fields)
    error = _error_text(deployment.get("error"))
    if error:
        lines += ["", f"Error: {error}"]
    title = f"Deployment {version or deployment_id}: {status.lower() or 'unknown'}"
    return _row(f"deployment:{deployment_id}", title, lines, "deployments")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class _CutShortError(Exception):
    """Internal: stop the run here, keeping the state reached so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Walker:
    """One run over the selected kinds, writing progress into ``state``.

    ``state["fingerprints"]`` maps every row id this source stored to the hash of what
    it rendered, so a record that renders the same is never sent to cognee again.
    """

    def __init__(
        self,
        client: Any,
        state: dict,
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        run_statuses: tuple[str, ...] | None,
        since: str,
        max_requests: int,
        check_active: Callable[[], None] | None,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed"):
            stats.setdefault(key, 0)
        self.include = include
        self.run_statuses = run_statuses
        self.since = since
        self.max_requests = max_requests
        self.check_active = check_active
        self.requests = 0
        self.emitted = 0
        self.fingerprints: dict[str, str] = state.setdefault("fingerprints", {})

    # -- requests ----------------------------------------------------------
    def _call(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.check_active is not None:
            self.check_active()
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")
        left = (getattr(self.client, "rate_limit", None) or {}).get("remaining")
        if left is not None and left < _REQUESTS_RESERVE:
            raise _CutShortError("rate_limit")
        self.requests += 1
        try:
            return self.client.request(path, params=params)
        except TriggerDevRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except TriggerDevAuthError:
            # a key revoked mid-run keeps what was stored; a bad key fails loudly
            if self.emitted:
                raise _CutShortError("auth") from None
            raise

    def _cursor_walk(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield every record of a ``page[after]`` listing, newest first."""
        after, used = None, set()
        for _ in range(_MAX_PAGES):
            page = {**params, "page[size]": PAGE_SIZE}
            if after:
                page["page[after]"] = after
            data = self._call(path, page)
            records = data.get("data")
            if not isinstance(records, list):
                raise TriggerDevAPIError(f"Trigger.dev sent no data list for {path}")
            yield from (record for record in records if isinstance(record, dict))
            pagination = data.get("pagination")
            cursor = pagination.get("next") if isinstance(pagination, dict) else None
            if not cursor:
                return
            if cursor in used:
                # a walk that can't move forward must not loop or look finished
                raise TriggerDevAPIError(f"Trigger.dev pagination did not advance for {path}")
            used.add(cursor)
            after = cursor
        raise TriggerDevAPIError(f"Trigger.dev listed more than {_MAX_PAGES} pages for {path}")

    # -- bookkeeping ---------------------------------------------------------
    def _changed(self, row_id: str, fingerprint: str) -> bool:
        self.stats["scanned"] += 1
        if self.fingerprints.get(row_id) == fingerprint:
            self.stats["skipped"] += 1
            return False
        return True

    def _emit(self, row: dict[str, Any], fingerprint: str) -> dict[str, Any]:
        self.fingerprints[row["id"]] = fingerprint
        self.emitted += 1
        return row

    def _maybe(self, row: dict[str, Any]) -> Iterator[dict[str, Any]]:
        fingerprint = _hash(row)
        if self._changed(row["id"], fingerprint):
            yield self._emit(row, fingerprint)

    def _failed(self, step: str, exc: Exception) -> None:
        self.stats["failed"] += 1
        # the message names the status and endpoint, never the record id
        logger.warning("Trigger.dev %s failed for one record, retried next run: %s", step, exc)

    def _tombstone(self, row_id: str) -> dict[str, Any]:
        self.fingerprints.pop(row_id, None)
        self.stats["deleted"] += 1
        return {"id": row_id, "_deleted": True}

    def _forget_history(self, prefix: str, listed: set[str]) -> None:
        """After a complete walk, stop tracking history rows older than the window.

        Their documents stay in memory; only the fingerprint goes, so state stays the
        size of the lookback window instead of growing with every run ever seen.
        """
        for row_id in [r for r in self.fingerprints if r.startswith(prefix)]:
            if row_id not in listed:
                del self.fingerprints[row_id]

    def _check_environment(self, run: dict[str, Any]) -> None:
        """Refuse state written for another environment, which would forget its schedules."""
        env = run.get("env")
        env_id = env.get("id") if isinstance(env, dict) else None
        if not isinstance(env_id, str) or not env_id:
            return
        stored = self.state.setdefault("environment_id", env_id)
        if stored != env_id:
            raise TriggerDevSourceError(
                "This resource_name already holds another Trigger.dev environment's sync; "
                "give each environment's key its own resource_name"
            )

    # -- kinds -------------------------------------------------------------
    def _forget_schedules(self) -> Iterator[dict[str, Any]]:
        """Schedules taken out of ``include`` leave memory: nothing would keep them current.

        History (errors, runs, deployments) stays when its kind is taken out, the same
        way it stays once it is older than the lookback window.
        """
        for row_id in sorted(r for r in self.fingerprints if r.startswith("schedule:")):
            yield self._tombstone(row_id)

    def _all_schedules(self) -> list[dict[str, Any]]:
        """The full schedule list; raises unless every page was read."""
        schedules: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            data = self._call("/api/v1/schedules", {"page": page, "perPage": SCHEDULE_PAGE_SIZE})
            records = data.get("data")
            pagination = data.get("pagination")
            total = pagination.get("totalPages") if isinstance(pagination, dict) else None
            if (
                not isinstance(records, list)
                or isinstance(total, bool)
                or not isinstance(total, int)
            ):
                # without the page count a listing can't be trusted as complete
                raise TriggerDevAPIError(
                    "Trigger.dev sent a schedule page without data or totalPages"
                )
            schedules.extend(record for record in records if isinstance(record, dict))
            if page >= total or not records:
                return schedules
        raise TriggerDevAPIError(f"Trigger.dev listed more than {_MAX_PAGES} schedule pages")

    def _schedule_gone(self, row_id: str) -> bool:
        schedule_id = row_id.split(":", 1)[1]
        try:
            self._call(f"/api/v1/schedules/{quote(schedule_id, safe='')}")
        except TriggerDevNotFoundError:
            return True
        except (TriggerDevAPIError, TriggerDevAccessError) as exc:
            # kept; still missing from the next listing, so it is checked again
            self._failed("deletion check", exc)
            return False
        return False  # still there: a listing that missed it is no reason to forget it

    def _schedule_rows(self) -> Iterator[dict[str, Any]]:
        listed: set[str] = set()
        for schedule in self._all_schedules():
            if not _record_id(schedule):
                self.stats["failed"] += 1
                continue
            row = render_schedule(schedule)
            listed.add(row["id"])
            yield from self._maybe(row)
        # only a complete listing gets here, so a missing id is a deletion candidate
        missing = sorted(
            r for r in self.fingerprints if r.startswith("schedule:") and r not in listed
        )
        for row_id in missing:
            if self._schedule_gone(row_id):
                yield self._tombstone(row_id)

    def _error_rows(self) -> Iterator[dict[str, Any]]:
        listed: set[str] = set()
        for group in self._cursor_walk("/api/v1/errors", {"filter[from]": self.since}):
            if not _record_id(group):
                self.stats["failed"] += 1
                continue
            row = render_error(group)
            listed.add(row["id"])
            yield from self._maybe(row)
        self._forget_history("error:", listed)

    def _run_rows(self) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"filter[createdAt][from]": self.since}
        if self.run_statuses:
            params["filter[status]"] = ",".join(self.run_statuses)
        listed: set[str] = set()
        for run in self._cursor_walk("/api/v1/runs", params):
            run_id = _record_id(run)
            if not run_id:
                self.stats["failed"] += 1
                continue
            self._check_environment(run)
            row = render_run(run)
            listed.add(row["id"])
            # the listing decides whether anything changed; only then is the run retrieved
            fingerprint = _hash(row)
            if not self._changed(row["id"], fingerprint):
                continue
            if _text(run.get("status")).upper() in FAILED_RUN_STATUSES:
                try:
                    detail = self._call(f"/api/v3/runs/{quote(run_id, safe='')}")
                except TriggerDevNotFoundError:
                    detail = None  # gone since the listing: keep what the listing said
                except (TriggerDevAPIError, TriggerDevAccessError) as exc:
                    # no fingerprint is stored, so the next run retrieves it again
                    self._failed("run retrieval", exc)
                    continue
                if detail is not None:
                    row = render_run(run, detail)
            yield self._emit(row, fingerprint)
        self._forget_history("run:", listed)

    def _deployment_rows(self) -> Iterator[dict[str, Any]]:
        listed: set[str] = set()
        for deployment in self._cursor_walk("/api/v1/deployments", {"from": self.since}):
            if not _record_id(deployment):
                self.stats["failed"] += 1
                continue
            row = render_deployment(deployment)
            listed.add(row["id"])
            yield from self._maybe(row)
        self._forget_history("deployment:", listed)

    # -- the run -----------------------------------------------------------
    def rows(self) -> Iterator[dict[str, Any]]:
        # schedules first: they are few, and the only kind whose deletions are tracked
        if "schedules" in self.include:
            yield from self._schedule_rows()
        else:
            yield from self._forget_schedules()
        if "errors" in self.include:
            yield from self._error_rows()
        if "runs" in self.include:
            yield from self._run_rows()
        if "deployments" in self.include:
            yield from self._deployment_rows()


def _since(now: datetime, lookback_days: int) -> str:
    start = now.astimezone(timezone.utc) - timedelta(days=lookback_days)
    return start.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _iter_rows(
    client: Any,
    state: dict,
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
    run_statuses: tuple[str, ...] | None = FAILED_RUN_STATUSES,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield the changed rows of an environment. Pure of dlt, so tests drive it with a dict."""
    walker = _Walker(
        client,
        state,
        stats,
        include=include,
        run_statuses=run_statuses,
        since=_since(now or datetime.now(timezone.utc), lookback_days),
        max_requests=max_requests,
        check_active=check_active,
    )
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning(
            "Trigger.dev sync stopped early (%s); it resumes on the next run.", cut.reason
        )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _check_options(
    resource_name: Any,
    include: tuple[str, ...],
    run_statuses: tuple[str, ...] | None,
    lookback_days: Any,
    max_requests: Any,
) -> None:
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("triggerdev_source needs a resource_name, one per Trigger.dev environment")
    if resource_name != resource_name.strip() or len(resource_name) > 64:
        raise ValueError("resource_name must be 1-64 characters without surrounding spaces")
    if not include or set(include) - set(KINDS):
        raise ValueError(f"include must be a non-empty subset of {KINDS}")
    if run_statuses is not None and (not run_statuses or set(run_statuses) - set(RUN_STATUSES)):
        raise ValueError(f"run_statuses must be None or a non-empty subset of {RUN_STATUSES}")
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int):
        raise ValueError("lookback_days must be a whole number of days")
    if not 1 <= lookback_days <= MAX_LOOKBACK_DAYS:
        raise ValueError(f"lookback_days must be between 1 and {MAX_LOOKBACK_DAYS}")
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
        raise ValueError("max_requests must be a positive whole number")


def triggerdev_source(
    *,
    resource_name: str,
    api_key: str | None = None,
    api_url: str = DEFAULT_API_URL,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    include: tuple[str, ...] = KINDS,
    run_statuses: tuple[str, ...] | None = FAILED_RUN_STATUSES,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
):
    """Return a ``dlt`` resource that yields a Trigger.dev environment's documents.

    Args:
        resource_name: Stable name of this connection, one per Trigger.dev environment.
            It scopes the sync state and the cleanup of deleted schedules, so keep it
            fixed across runs. There is no default: two environments sharing a name
            would forget each other's schedules, and a run refuses state written for
            another environment once it has seen one of its runs.
        api_key: The environment's secret key (``tr_dev_...``, ``tr_prod_...``, or a
            named ``..._sk_...`` key). Used to build the client when ``service`` is
            omitted.
        api_url: API origin, for a self-hosted Trigger.dev.
        service: Pre-built client, mainly an injection point for tests.
        check_active: Optional host authorization checkpoint, called before each request.
        include: Which of ``"schedules"``, ``"errors"``, ``"runs"`` and
            ``"deployments"`` to sync. A kind taken out later is forgotten.
        run_statuses: Statuses of the runs to sync, the failed ones by default. ``None``
            syncs every run, which on a busy project is many documents.
        lookback_days: How far back each run reads errors, runs and deployments.
        max_requests: Request budget of one run. A bigger environment finishes over
            several runs.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    # per-row node sets (cognee_node_set) arrived with document sync version 2
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError("The Trigger.dev connector requires cognee>=1.6.3.")
    include = tuple(include)
    if run_statuses is not None:
        run_statuses = tuple(status.upper() for status in run_statuses)
    _check_options(resource_name, include, run_statuses, lookback_days, max_requests)
    if service is None and not api_key:
        raise ValueError("triggerdev_source needs a service or an api_key")

    # built here so a malformed key fails at construction and the closure holds no raw key
    client = service if service is not None else TriggerDevClient(api_key or "", api_url=api_url)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def triggerdev_environment():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        # the quota a reused client saw last run is stale once its window resets
        client.rate_limit = {}
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            include=include,
            run_statuses=run_statuses,
            lookback_days=lookback_days,
            max_requests=max_requests,
            check_active=check_active,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = triggerdev_environment()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "triggerdev")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
