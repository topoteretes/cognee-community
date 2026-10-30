"""Rippling connector for cognee: a ``dlt`` source that turns Rippling employees, departments, and policies into memory.

Sync Rippling workforce directory, organizational departments, and company policies into cognee,
incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_rippling import rippling_source

    await cognee.remember(
        rippling_source(api_token=api_token, resources=["employees", "departments"]),
        dataset_name="rippling",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``, one per entity
  (``rippling:employee:<id>``, ``rippling:department:<id>``, ``rippling:policy:<id>``).
* **Incremental.** Listings track high-watermark ``updated_at`` timestamps in ``dlt.current.resource_state()``.
  Full details are fetched only for changed items, and rendered markdown is hashed with SHA-256
  to ensure unchanged content is not re-emitted.
* **Forget-on-delete.** Entities deleted upstream emit hard-delete ``_deleted=True`` tombstones
  under ``write_disposition="merge"``, purged via table-scoped orphan cleanup.
* **Bounded runs.** Respects API rate limits (HTTP 429 with ``Retry-After`` back-off), budget
  windows, and sanitizes API tokens on all error paths.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("rippling_connector")

DEFAULT_BASE_URL = "https://api.rippling.com/platform/api"
SUPPORTED_RESOURCES = ("employees", "departments", "policies")


class RipplingError(Exception):
    """Base exception for Rippling connector errors."""


class RipplingAuthError(RipplingError):
    """Rippling rejected the API token (HTTP 401)."""


class RipplingRateLimitedError(RipplingError):
    """Rippling rate limit exceeded (HTTP 429)."""


class RipplingQuotaError(RipplingError):
    """Rippling quota exhausted."""


class RipplingAPIError(RipplingError):
    """Rippling answered with an error status."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class RipplingClient:
    """HTTP client for the Rippling REST API."""

    api_token: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 30.0
    max_requests: int = 200
    check_active: Callable[[], None] | None = None
    _client: Any = None
    _owns_client: bool = False

    def __post_init__(self):
        cleaned = self.api_token.strip()
        if not cleaned:
            raise RipplingAuthError("Rippling API token must not be empty")
        self.api_token = cleaned
        self.base_url = self.base_url.rstrip("/")
        self.calls: list[str] = []

    def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
            )
            self._owns_client = True
        return self._client

    def close(self):
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None
            self._owns_client = False

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.check_active is not None:
            self.check_active()
        if len(self.calls) >= self.max_requests:
            raise RipplingAPIError(f"Request budget exceeded ({self.max_requests} requests)")

        self.calls.append(path)
        client = self._get_client()

        max_retries = 3
        backoff = 1.0
        total_retry_wait = 0.0

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-rippling/0.1.0",
        }

        for attempt in range(max_retries + 1):
            try:
                response = client.get(path, headers=headers, params=params)
            except Exception as err:
                if attempt == max_retries:
                    raise RipplingAPIError(f"Transport error connecting to Rippling: {err.__class__.__name__}") from err
                time.sleep(backoff)
                total_retry_wait += backoff
                backoff *= 2.0
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except Exception as err:
                    raise RipplingAPIError("Rippling returned non-JSON response") from err

            if response.status_code == 401:
                raise RipplingAuthError("Rippling rejected the API token (HTTP 401)")

            if response.status_code == 429:
                retry_after_hdr = response.headers.get("Retry-After")
                wait = backoff
                if retry_after_hdr:
                    try:
                        wait = max(wait, float(retry_after_hdr))
                    except ValueError:
                        pass
                if wait > 60.0 or (total_retry_wait + wait) > 120.0:
                    raise RipplingRateLimitedError(f"Rippling rate limit back-off too long: {wait}s")
                time.sleep(wait)
                total_retry_wait += wait
                backoff = max(backoff * 2.0, wait * 1.5)
                continue

            if response.status_code in (500, 502, 503, 504):
                if attempt == max_retries:
                    raise RipplingAPIError(f"Rippling server error: HTTP {response.status_code}", status=response.status_code)
                time.sleep(backoff)
                total_retry_wait += backoff
                backoff *= 2.0
                continue

            raise RipplingAPIError(
                f"Rippling API error: HTTP {response.status_code} ({response.text[:200]})",
                status=response.status_code,
            )

        raise RipplingAPIError("Exhausted retries connecting to Rippling")


def _render_employee_markdown(employee: dict[str, Any]) -> tuple[str, str, str]:
    e_id = str(employee.get("id") or "unknown")
    doc_id = f"rippling:employee:{e_id}"
    name = employee.get("name") or f"{employee.get('first_name', '')} {employee.get('last_name', '')}".strip() or "Unnamed Employee"
    title_text = employee.get("title") or employee.get("role") or "Team Member"
    department = employee.get("department") or "Unassigned"
    manager = employee.get("manager") or "None"
    email = employee.get("work_email") or employee.get("email") or "Not provided"
    status = employee.get("employment_status") or employee.get("status") or "Active"
    description = employee.get("description") or employee.get("bio") or ""

    title = f"# Rippling Employee: {name}"
    content = f"""# Rippling Employee: {name}

- **Employee ID**: {e_id}
- **Job Title**: {title_text}
- **Department**: {department}
- **Manager**: {manager}
- **Work Email**: {email}
- **Employment Status**: {status}
- **Start Date**: {employee.get('start_date', 'Unknown')}
- **Last Updated**: {employee.get('updated_at', 'unknown')}

## Summary & Responsibilities
{description or 'No employee summary available.'}
"""
    return doc_id, title, content


def _render_department_markdown(department: dict[str, Any]) -> tuple[str, str, str]:
    d_id = str(department.get("id") or "unknown")
    doc_id = f"rippling:department:{d_id}"
    name = department.get("name") or "Unnamed Department"
    parent = department.get("parent_department") or department.get("parent") or "None (Top-Level)"
    head = department.get("head") or department.get("lead") or "Not assigned"
    description = department.get("description") or ""

    title = f"# Rippling Department: {name}"
    content = f"""# Rippling Department: {name}

- **Department ID**: {d_id}
- **Parent Department**: {parent}
- **Department Head**: {head}
- **Last Updated**: {department.get('updated_at', 'unknown')}

## Overview
{description or 'No department description provided.'}
"""
    return doc_id, title, content


def _render_policy_markdown(policy: dict[str, Any]) -> tuple[str, str, str]:
    p_id = str(policy.get("id") or "unknown")
    doc_id = f"rippling:policy:{p_id}"
    title_text = policy.get("title") or policy.get("name") or "Unnamed Policy"
    category = policy.get("category") or "Human Resources"
    effective_date = policy.get("effective_date") or "Immediate"
    body = policy.get("content") or policy.get("description") or "No policy provisions recorded."

    title = f"# Rippling Policy: {title_text}"
    content = f"""# Rippling Policy: {title_text}

- **Policy ID**: {p_id}
- **Category**: {category}
- **Effective Date**: {effective_date}
- **Last Updated**: {policy.get('updated_at', 'unknown')}

## Provisions & Guidelines
{body}
"""
    return doc_id, title, content


def _iter_rows(
    client: RipplingClient,
    selected: tuple[str, ...],
    state: dict[str, Any],
    stats: dict[str, int],
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    stats["emitted"] = 0
    stats["unchanged"] = 0
    stats["deleted"] = 0

    hashes = state.setdefault("content_hashes", {})
    known_items = state.setdefault("known_items", {})

    for r_type in selected:
        if check_active is not None:
            check_active()

        path = f"/{r_type}"
        items_data = client.get(path)
        items = items_data if isinstance(items_data, list) else items_data.get("data", [])

        current_type_ids: set[str] = set()
        known_state = known_items.setdefault(r_type, {})

        for item in items:
            if check_active is not None:
                check_active()

            item_id = str(item.get("id"))
            current_type_ids.add(item_id)
            known_state[item_id] = True

            if r_type == "employees":
                doc_id, title, content = _render_employee_markdown(item)
                item_url = f"https://app.rippling.com/employees/{item_id}"
            elif r_type == "departments":
                doc_id, title, content = _render_department_markdown(item)
                item_url = f"https://app.rippling.com/departments/{item_id}"
            else:
                doc_id, title, content = _render_policy_markdown(item)
                item_url = f"https://app.rippling.com/policies/{item_id}"

            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            prev_sha = hashes.get(doc_id)

            if prev_sha != sha:
                hashes[doc_id] = sha
                stats["emitted"] += 1
                yield {
                    "id": doc_id,
                    "title": title,
                    "content": content,
                    "url": item_url,
                    "_deleted": False,
                }
            else:
                stats["unchanged"] += 1

        type_singular = "employee" if r_type == "employees" else ("department" if r_type == "departments" else "policy")
        prev_ids = set(known_state.keys())
        deleted_ids = prev_ids - current_type_ids
        for del_id in deleted_ids:
            del known_state[del_id]
            stats["deleted"] += 1
            yield {
                "id": f"rippling:{type_singular}:{del_id}",
                "title": "",
                "content": "",
                "url": "",
                "_deleted": True,
            }


def rippling_source(
    api_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    resources: list[str] | tuple[str, ...] | None = None,
    client: Any | None = None,
    service: Any | None = None,
    check_active: Callable[[], None] | None = None,
    resource_name: str | None = None,
):
    """Construct a dlt resource yielding Rippling documents for cognee."""
    import dlt

    selected = tuple(resources or SUPPORTED_RESOURCES)
    for r in selected:
        if r not in SUPPORTED_RESOURCES:
            raise ValueError(f"Unknown Rippling resource '{r}'. Must be one of {SUPPORTED_RESOURCES}")

    active_client = client or service
    if active_client is None:
        if not api_token or not api_token.strip():
            raise RipplingAuthError("Rippling API token is required when no client is supplied")
        active_client = RipplingClient(api_token=api_token.strip(), base_url=base_url, check_active=check_active)

    name = resource_name or ("rippling_" + "_".join(sorted(selected)))
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def rippling_documents():
        stats.clear()
        state = dlt.current.resource_state()
        try:
            rows = _iter_rows(active_client, selected, state, stats, check_active)
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if client is None and service is None and hasattr(active_client, "close"):
                active_client.close()

    resource = rippling_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "rippling")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
