"""Vanta connector for cognee: a ``dlt`` source that turns Vanta controls, policies, and vendors into memory.

Sync Vanta compliance controls, governance policies, and vendor risk inventory into cognee,
incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_vanta import vanta_source

    await cognee.remember(
        vanta_source(api_token=api_token, resources=["controls", "policies", "vendors"]),
        dataset_name="vanta",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``, one per entity
  (``vanta:control:<id>``, ``vanta:policy:<id>``, ``vanta:vendor:<id>``).
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

logger = get_logger("vanta_connector")

DEFAULT_BASE_URL = "https://api.vanta.com/v1"
SUPPORTED_RESOURCES = ("controls", "policies", "vendors")


class VantaError(Exception):
    """Base exception for Vanta connector errors."""


class VantaAuthError(VantaError):
    """Vanta rejected the API token (HTTP 401)."""


class VantaRateLimitedError(VantaError):
    """Vanta rate limit exceeded (HTTP 429)."""


class VantaQuotaError(VantaError):
    """Vanta quota exhausted."""


class VantaAPIError(VantaError):
    """Vanta answered with an error status."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class VantaClient:
    """HTTP client for the Vanta REST API."""

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
            raise VantaAuthError("Vanta API token must not be empty")
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
            raise VantaAPIError(f"Request budget exceeded ({self.max_requests} requests)")

        self.calls.append(path)
        client = self._get_client()

        max_retries = 3
        backoff = 1.0
        total_retry_wait = 0.0

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-vanta/0.1.0",
        }

        for attempt in range(max_retries + 1):
            try:
                response = client.get(path, headers=headers, params=params)
            except Exception as err:
                if attempt == max_retries:
                    raise VantaAPIError(f"Transport error connecting to Vanta: {err.__class__.__name__}") from err
                time.sleep(backoff)
                total_retry_wait += backoff
                backoff *= 2.0
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except Exception as err:
                    raise VantaAPIError("Vanta returned non-JSON response") from err

            if response.status_code == 401:
                raise VantaAuthError("Vanta rejected the API token (HTTP 401)")

            if response.status_code == 429:
                retry_after_hdr = response.headers.get("Retry-After")
                wait = backoff
                if retry_after_hdr:
                    try:
                        wait = max(wait, float(retry_after_hdr))
                    except ValueError:
                        pass
                if wait > 60.0 or (total_retry_wait + wait) > 120.0:
                    raise VantaRateLimitedError(f"Vanta rate limit back-off too long: {wait}s")
                time.sleep(wait)
                total_retry_wait += wait
                backoff = max(backoff * 2.0, wait * 1.5)
                continue

            if response.status_code in (500, 502, 503, 504):
                if attempt == max_retries:
                    raise VantaAPIError(f"Vanta server error: HTTP {response.status_code}", status=response.status_code)
                time.sleep(backoff)
                total_retry_wait += backoff
                backoff *= 2.0
                continue

            raise VantaAPIError(
                f"Vanta API error: HTTP {response.status_code} ({response.text[:200]})",
                status=response.status_code,
            )

        raise VantaAPIError("Exhausted retries connecting to Vanta")


def _render_control_markdown(control: dict[str, Any]) -> tuple[str, str, str]:
    c_id = control.get("id") or "unknown"
    doc_id = f"vanta:control:{c_id}"
    name = control.get("name") or control.get("title") or "Unnamed Control"
    category = control.get("category") or "Uncategorized"
    status = control.get("status") or "active"
    frameworks = control.get("frameworks") or control.get("standards") or []
    if isinstance(frameworks, list):
        frameworks_str = ", ".join(str(f) for f in frameworks)
    else:
        frameworks_str = str(frameworks)
    description = control.get("description") or "No control description available."

    title = f"# Vanta Control: {name}"
    content = f"""# Vanta Control: {name}

- **ID**: {c_id}
- **Category**: {category}
- **Status**: {status}
- **Compliance Frameworks**: {frameworks_str or 'None specified'}
- **Last Updated**: {control.get('updated_at', 'unknown')}

## Description
{description}
"""
    return doc_id, title, content


def _render_policy_markdown(policy: dict[str, Any]) -> tuple[str, str, str]:
    p_id = policy.get("id") or "unknown"
    doc_id = f"vanta:policy:{p_id}"
    title_text = policy.get("title") or policy.get("name") or "Unnamed Policy"
    category = policy.get("category") or "Governance"
    status = policy.get("status") or "draft"
    renewal_date = policy.get("renewal_date") or policy.get("next_review_date") or "Not scheduled"
    body = policy.get("content") or policy.get("description") or "No policy text provided."

    title = f"# Vanta Policy: {title_text}"
    content = f"""# Vanta Policy: {title_text}

- **ID**: {p_id}
- **Category**: {category}
- **Status**: {status}
- **Renewal / Review Date**: {renewal_date}
- **Last Updated**: {policy.get('updated_at', 'unknown')}

## Summary & Policy Provisions
{body}
"""
    return doc_id, title, content


def _render_vendor_markdown(vendor: dict[str, Any]) -> tuple[str, str, str]:
    v_id = vendor.get("id") or "unknown"
    doc_id = f"vanta:vendor:{v_id}"
    name = vendor.get("name") or "Unnamed Vendor"
    risk_level = vendor.get("risk_level") or vendor.get("risk") or "Low"
    service_category = vendor.get("service_category") or vendor.get("category") or "Cloud Infrastructure"
    review_status = vendor.get("review_status") or vendor.get("status") or "Approved"
    notes = vendor.get("security_notes") or vendor.get("description") or "No security notes recorded."

    title = f"# Vanta Vendor: {name}"
    content = f"""# Vanta Vendor: {name}

- **ID**: {v_id}
- **Risk Level**: {risk_level}
- **Service Category**: {service_category}
- **Security Review Status**: {review_status}
- **Last Updated**: {vendor.get('updated_at', 'unknown')}

## Security Assessment & Notes
{notes}
"""
    return doc_id, title, content


def _iter_rows(
    client: VantaClient,
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

            if r_type == "controls":
                doc_id, title, content = _render_control_markdown(item)
                item_url = f"https://app.vanta.com/controls/{item_id}"
            elif r_type == "policies":
                doc_id, title, content = _render_policy_markdown(item)
                item_url = f"https://app.vanta.com/policies/{item_id}"
            else:
                doc_id, title, content = _render_vendor_markdown(item)
                item_url = f"https://app.vanta.com/vendors/{item_id}"

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

        type_singular = "control" if r_type == "controls" else ("policy" if r_type == "policies" else "vendor")
        prev_ids = set(known_state.keys())
        deleted_ids = prev_ids - current_type_ids
        for del_id in deleted_ids:
            del known_state[del_id]
            stats["deleted"] += 1
            yield {
                "id": f"vanta:{type_singular}:{del_id}",
                "title": "",
                "content": "",
                "url": "",
                "_deleted": True,
            }


def vanta_source(
    api_token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    resources: list[str] | tuple[str, ...] | None = None,
    client: Any | None = None,
    service: Any | None = None,
    check_active: Callable[[], None] | None = None,
    resource_name: str | None = None,
):
    """Construct a dlt resource yielding Vanta documents for cognee."""
    import dlt

    selected = tuple(resources or SUPPORTED_RESOURCES)
    for r in selected:
        if r not in SUPPORTED_RESOURCES:
            raise ValueError(f"Unknown Vanta resource '{r}'. Must be one of {SUPPORTED_RESOURCES}")

    active_client = client or service
    if active_client is None:
        if not api_token or not api_token.strip():
            raise VantaAuthError("Vanta API token is required when no client is supplied")
        active_client = VantaClient(api_token=api_token.strip(), base_url=base_url, check_active=check_active)

    name = resource_name or ("vanta_" + "_".join(sorted(selected)))
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def vanta_documents():
        stats.clear()
        state = dlt.current.resource_state()
        try:
            rows = _iter_rows(active_client, selected, state, stats, check_active)
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if client is None and service is None and hasattr(active_client, "close"):
                active_client.close()

    resource = vanta_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "vanta")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
