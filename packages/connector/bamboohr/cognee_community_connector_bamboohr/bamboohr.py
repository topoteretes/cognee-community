"""DLT source for BambooHR (incremental sync + forget-on-delete)."""

import os
import time
from collections.abc import Iterator, Sequence
from typing import Any

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("bamboohr_connector")

BAMBOOHR_SOURCE_NAME = "bamboohr"
EMPLOYEES_TABLE_NAME = "bamboohr_employees"

# Employee data is sensitive, so only these fields are ever requested (the
# get-employee endpoint returns nothing but ``id`` unless fields are named).
# Deliberately excluded: SSN, date of birth, pay, home address, personal
# contact details, gender, ethnicity. Override with ``fields=``.
DEFAULT_EMPLOYEE_FIELDS = (
    "firstName",
    "lastName",
    "preferredName",
    "jobTitle",
    "department",
    "division",
    "location",
    "workEmail",
    "supervisor",
    "status",
    "hireDate",
)

# First-run cursor: every employee is "changed" since the epoch, so the
# initial backfill and later incremental runs share one code path.
_EPOCH = "1970-01-01T00:00:00Z"

# Retry budget for rate-limited / transient BambooHR API responses.
_MAX_RETRIES = 5
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
_TIMEOUT_SECONDS = 30


def _base_url(company_domain: str) -> str:
    """Build the API root from the subdomain of the BambooHR login URL."""
    return f"https://{company_domain}.bamboohr.com/api/v1"


def _make_session(api_key: str) -> requests.Session:
    """Build a session authenticated with a BambooHR API key.

    BambooHR uses HTTP Basic auth with the API key as the username; the
    password is ignored, so any string works ("x" by convention).
    """
    session = requests.Session()
    session.auth = (api_key, "x")
    # BambooHR answers in XML unless JSON is asked for explicitly.
    session.headers["Accept"] = "application/json"
    return session


def _request(session: Any, url: str, params: dict | None = None) -> requests.Response:
    """GET ``url``, retrying rate-limit / server / network errors with backoff.

    The response is returned as-is (even a 404) so callers can decide what a
    status means; only exhausted retries on a network error raise here.
    """
    for attempt in range(_MAX_RETRIES):
        last_attempt = attempt == _MAX_RETRIES - 1
        try:
            response = session.get(url, params=params, timeout=_TIMEOUT_SECONDS)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if last_attempt:
                raise
            delay, reason = float(2**attempt), str(exc)
        else:
            if response.status_code not in _TRANSIENT_STATUSES or last_attempt:
                return response
            delay = _retry_after(response.headers, attempt)
            reason = f"HTTP {response.status_code}"
        logger.warning(
            "BambooHR: %s — retrying in %.1fs (%d/%d).", reason, delay, attempt + 1, _MAX_RETRIES
        )
        time.sleep(delay)
    raise RuntimeError("unreachable")  # the loop always returns or raises


def _retry_after(headers, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    try:
        return float((headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt)


def _get_changed(session: Any, base_url: str, since: str) -> dict:
    """Call the change feed: employees inserted/updated/deleted since ``since``."""
    response = _request(session, f"{base_url}/employees/changed", params={"since": since})
    response.raise_for_status()
    return response.json()


def _get_employee(session: Any, base_url: str, employee_id: str, fields: Sequence[str]):
    """Fetch one employee's allowlisted fields, or ``None`` if it no longer exists."""
    response = _request(
        session, f"{base_url}/employees/{employee_id}", params={"fields": ",".join(fields)}
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Employees → document rows
# ---------------------------------------------------------------------------


def _employee_row_id(employee_id: str) -> str:
    """Prefix ids so an employee and a company file can never share one."""
    return f"employee:{employee_id}"


def _deleted_row(row_id: str) -> dict[str, Any]:
    """A hard-delete marker: dlt drops this row on merge, cognee forgets it."""
    return {"id": row_id, "_deleted": True}


def _employee_to_row(employee: dict, fields: Sequence[str]) -> dict[str, Any]:
    """Render an employee as a document row: ``{id, title, content}``.

    Only the allowlisted ``fields`` are written into ``content``, one
    ``field: value`` line each; empty values are left out.
    """
    first = employee.get("preferredName") or employee.get("firstName") or ""
    name = f"{first} {employee.get('lastName') or ''}".strip()
    job_title = employee.get("jobTitle")
    title = f"{name} — {job_title}" if name and job_title else name

    lines = [f"{field}: {employee[field]}" for field in fields if employee.get(field)]
    return {
        "id": _employee_row_id(employee["id"]),
        "title": title,
        "content": "\n".join(lines),
        "_deleted": False,
    }


def sync_employees(
    session: Any,
    base_url: str,
    state: dict,
    fields: Sequence[str] = DEFAULT_EMPLOYEE_FIELDS,
    include_inactive: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield employee rows changed since the cursor in ``state``, then advance it.

    Deleted employees, employees that 404, and (unless ``include_inactive``)
    terminated employees are yielded as hard-delete markers.
    """
    since = state.get("since", _EPOCH)
    changed = _get_changed(session, base_url, since)
    # "status" is always requested so inactive employees can be detected, but
    # it is only rendered into content if the caller's fields include it.
    requested = list(dict.fromkeys([*fields, "status"]))

    upserted = deleted = 0
    # ``or {}``: an empty result may come back as [] rather than {}.
    for employee_id, change in (changed.get("employees") or {}).items():
        row_id = _employee_row_id(employee_id)
        if change.get("action") == "Deleted":
            deleted += 1
            yield _deleted_row(row_id)
            continue

        employee = _get_employee(session, base_url, employee_id, requested)
        if employee is None or (not include_inactive and employee.get("status") == "Inactive"):
            deleted += 1
            yield _deleted_row(row_id)
            continue

        upserted += 1
        yield _employee_to_row(employee, fields)

    # Only advance once every change has been yielded. dlt persists resource
    # state together with the load, so a run that fails midway keeps the old
    # cursor and the next run retries the same window.
    if changed.get("latest"):
        state["since"] = changed["latest"]
    logger.info("BambooHR: %d employee(s) upserted, %d forgotten.", upserted, deleted)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def bamboohr_source(
    company_domain: str | None = None,
    api_key: str | None = None,
    fields: Sequence[str] = DEFAULT_EMPLOYEE_FIELDS,
    include_inactive: bool = False,
    session: Any = None,
):
    """Create a dlt source that syncs BambooHR employees into cognee.

    Args:
        company_domain: Subdomain of your BambooHR login URL ("acme" for
            acme.bamboohr.com). Falls back to ``BAMBOOHR_COMPANY_DOMAIN``.
        api_key: BambooHR API key. Falls back to ``BAMBOOHR_API_KEY``.
        fields: Employee fields to ingest (the allowlist). Defaults to
            ``DEFAULT_EMPLOYEE_FIELDS``, which excludes sensitive data.
        include_inactive: Keep terminated employees. By default they are
            forgotten, like deleted ones.
        session: Pre-built ``requests`` session. Mainly an injection point for
            tests; when omitted one is built from ``api_key``.

    Returns:
        A dlt source for ``cognee.remember(..., write_disposition="merge")``.
        ``merge`` is required: cognee's default ``replace`` would drop every
        employee not in the latest change window.
    """
    company_domain = company_domain or os.environ.get("BAMBOOHR_COMPANY_DOMAIN")
    if not company_domain:
        raise ValueError(
            "BambooHR company domain required: pass company_domain= or set BAMBOOHR_COMPANY_DOMAIN."
        )
    if session is None:
        api_key = api_key or os.environ.get("BAMBOOHR_API_KEY")
        if not api_key:
            raise ValueError("BambooHR API key required: pass api_key= or set BAMBOOHR_API_KEY.")
        session = _make_session(api_key)
    base_url = _base_url(company_domain)

    @dlt.resource(
        name=EMPLOYEES_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from staging on merge, and
        # cognee's orphan_cleanup then forgets them from the graph.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def bamboohr_employees():
        state = dlt.current.resource_state()
        yield from sync_employees(session, base_url, state, fields, include_inactive)

    @dlt.source(name=BAMBOOHR_SOURCE_NAME)
    def _bamboohr():
        return bamboohr_employees

    source = _bamboohr()
    # Opt into the document ingestion path (row → text document → cognify).
    setattr(source, DOCUMENT_SOURCE_ATTR, BAMBOOHR_SOURCE_NAME)
    return source
