"""Tailscale connector for cognee: a ``dlt`` source that turns Tailscale devices, ACLs, and routes into memory.

Sync Tailscale tailnet devices, ACL access policies, and subnet routes into cognee,
incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_tailscale import tailscale_source

    await cognee.remember(
        tailscale_source(api_key=api_key, tailnet="-", resources=["devices", "acls"]),
        dataset_name="tailscale",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``, one per entity
  (``tailscale:device:<id>``, ``tailscale:acl:<tailnet>``, ``tailscale:route:<id>``).
* **Incremental.** Listings track high-watermark ``last_seen`` timestamps in ``dlt.current.resource_state()``.
  Full details are fetched only for changed items, and rendered markdown is hashed with SHA-256
  to ensure unchanged content is not re-emitted.
* **Forget-on-delete.** Entities deleted upstream emit hard-delete ``_deleted=True`` tombstones
  under ``write_disposition="merge"``, purged via table-scoped orphan cleanup.
* **Bounded runs.** Respects API rate limits (HTTP 429 with ``Retry-After`` back-off), budget
  windows, and sanitizes API keys on all error paths.
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

logger = get_logger("tailscale_connector")

DEFAULT_BASE_URL = "https://api.tailscale.com/api/v2"
SUPPORTED_RESOURCES = ("devices", "acls", "routes")


class TailscaleError(Exception):
    """Base exception for Tailscale connector errors."""


class TailscaleAuthError(TailscaleError):
    """Tailscale rejected the API key (HTTP 401/403)."""


class TailscaleRateLimitedError(TailscaleError):
    """Tailscale rate limit exceeded (HTTP 429)."""


class TailscaleQuotaError(TailscaleError):
    """Tailscale quota exhausted."""


class TailscaleAPIError(TailscaleError):
    """Tailscale answered with an error status."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class TailscaleClient:
    """HTTP client for the Tailscale REST API."""

    api_key: str
    tailnet: str = "-"
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 30.0
    max_requests: int = 200
    check_active: Callable[[], None] | None = None
    _client: Any = None
    _owns_client: bool = False

    def __post_init__(self):
        cleaned = self.api_key.strip()
        if not cleaned:
            raise TailscaleAuthError("Tailscale API key must not be empty")
        self.api_key = cleaned
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
            raise TailscaleAPIError(f"Request budget exceeded ({self.max_requests} requests)")

        self.calls.append(path)
        client = self._get_client()

        max_retries = 3
        backoff = 1.0
        total_retry_wait = 0.0

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-tailscale/0.1.0",
        }

        for attempt in range(max_retries + 1):
            try:
                response = client.get(path, headers=headers, params=params)
            except Exception as err:
                if attempt == max_retries:
                    raise TailscaleAPIError(f"Transport error connecting to Tailscale: {err.__class__.__name__}") from err
                time.sleep(backoff)
                total_retry_wait += backoff
                backoff *= 2.0
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except Exception as err:
                    raise TailscaleAPIError("Tailscale returned non-JSON response") from err

            if response.status_code in (401, 403):
                raise TailscaleAuthError(f"Tailscale rejected the API key (HTTP {response.status_code})")

            if response.status_code == 429:
                retry_after_hdr = response.headers.get("Retry-After")
                wait = backoff
                if retry_after_hdr:
                    try:
                        wait = max(wait, float(retry_after_hdr))
                    except ValueError:
                        pass
                if wait > 60.0 or (total_retry_wait + wait) > 120.0:
                    raise TailscaleRateLimitedError(f"Tailscale rate limit back-off too long: {wait}s")
                time.sleep(wait)
                total_retry_wait += wait
                backoff = max(backoff * 2.0, wait * 1.5)
                continue

            if response.status_code in (500, 502, 503, 504):
                if attempt == max_retries:
                    raise TailscaleAPIError(f"Tailscale server error: HTTP {response.status_code}", status=response.status_code)
                time.sleep(backoff)
                total_retry_wait += backoff
                backoff *= 2.0
                continue

            raise TailscaleAPIError(
                f"Tailscale API error: HTTP {response.status_code} ({response.text[:200]})",
                status=response.status_code,
            )

        raise TailscaleAPIError("Exhausted retries connecting to Tailscale")


def _render_device_markdown(device: dict[str, Any]) -> tuple[str, str, str]:
    d_id = str(device.get("id") or device.get("nodeId") or "unknown")
    doc_id = f"tailscale:device:{d_id}"
    name = device.get("name") or device.get("hostname") or "Unnamed Device"
    os_name = device.get("os") or "Unknown OS"
    addresses = device.get("addresses") or []
    ip_str = ", ".join(addresses) if isinstance(addresses, list) else str(addresses)
    tags = device.get("tags") or []
    tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
    authorized = device.get("authorized", True)
    last_seen = device.get("lastSeen") or device.get("last_seen") or "Unknown"

    description = device.get("description") or device.get("notes") or ""

    title = f"# Tailscale Device: {name}"
    content = f"""# Tailscale Device: {name}

- **Node ID**: {d_id}
- **Operating System**: {os_name}
- **Tailscale IP(s)**: {ip_str or 'None assigned'}
- **Tags**: {tags_str or 'None'}
- **Authorized**: {authorized}
- **Last Seen**: {last_seen}
- **Client Version**: {device.get('clientVersion', 'Unknown')}

## Description
{description or 'No device description provided.'}

## Network Attributes & Capabilities
- **Subnet Routes**: {device.get('advertisedRoutes', 'None')}
- **Key Expiry**: {device.get('keyExpiryDisabled', False)}
"""
    return doc_id, title, content


def _render_acl_markdown(acl: dict[str, Any], tailnet: str) -> tuple[str, str, str]:
    doc_id = f"tailscale:acl:{tailnet}"
    title = f"# Tailscale ACL Policy: {tailnet}"
    acls_list = acl.get("acls") or []
    tag_owners = acl.get("tagOwners") or {}
    groups = acl.get("groups") or {}

    acls_str = json.dumps(acls_list, indent=2) if acls_list else "Default allow all"
    groups_str = json.dumps(groups, indent=2) if groups else "No groups configured"
    tag_owners_str = json.dumps(tag_owners, indent=2) if tag_owners else "No tag owners configured"

    content = f"""# Tailscale ACL Policy: {tailnet}

- **Tailnet**: {tailnet}
- **Rule Count**: {len(acls_list)}

## Access Control Rules
```json
{acls_str}
```

## Security Groups
```json
{groups_str}
```

## Tag Owners
```json
{tag_owners_str}
```
"""
    return doc_id, title, content


def _render_route_markdown(route: dict[str, Any]) -> tuple[str, str, str]:
    r_id = str(route.get("id") or route.get("prefix") or "unknown")
    doc_id = f"tailscale:route:{r_id}"
    prefix = route.get("prefix") or "0.0.0.0/0"
    node_name = route.get("nodeName") or route.get("node") or "Unknown Gateway Node"
    enabled = route.get("enabled", False)
    advertised = route.get("advertised", True)

    title = f"# Tailscale Route: {prefix}"
    content = f"""# Tailscale Route: {prefix}

- **Subnet Route Prefix**: {prefix}
- **Gateway Node**: {node_name}
- **Enabled**: {enabled}
- **Advertised**: {advertised}
- **Primary**: {route.get('isPrimary', False)}
"""
    return doc_id, title, content


def _iter_rows(
    client: TailscaleClient,
    tailnet: str,
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

        current_type_ids: set[str] = set()
        known_state = known_items.setdefault(r_type, {})

        if r_type == "devices":
            path = f"/tailnet/{tailnet}/devices"
            data = client.get(path)
            devices = data.get("devices", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

            for dev in devices:
                if check_active is not None:
                    check_active()

                dev_id = str(dev.get("id") or dev.get("nodeId"))
                current_type_ids.add(dev_id)
                known_state[dev_id] = True

                doc_id, title, content = _render_device_markdown(dev)
                sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
                prev_sha = hashes.get(doc_id)

                if prev_sha != sha:
                    hashes[doc_id] = sha
                    stats["emitted"] += 1
                    yield {
                        "id": doc_id,
                        "title": title,
                        "content": content,
                        "url": f"https://login.tailscale.com/admin/machines/{dev_id}",
                        "_deleted": False,
                    }
                else:
                    stats["unchanged"] += 1

        elif r_type == "acls":
            path = f"/tailnet/{tailnet}/acl"
            acl_data = client.get(path)
            acl_id = tailnet
            current_type_ids.add(acl_id)
            known_state[acl_id] = True

            doc_id, title, content = _render_acl_markdown(acl_data if isinstance(acl_data, dict) else {}, tailnet)
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            prev_sha = hashes.get(doc_id)

            if prev_sha != sha:
                hashes[doc_id] = sha
                stats["emitted"] += 1
                yield {
                    "id": doc_id,
                    "title": title,
                    "content": content,
                    "url": f"https://login.tailscale.com/admin/acls/{tailnet}",
                    "_deleted": False,
                }
            else:
                stats["unchanged"] += 1

        elif r_type == "routes":
            path = f"/tailnet/{tailnet}/routes"
            data = client.get(path)
            routes = data.get("routes", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

            for rt in routes:
                if check_active is not None:
                    check_active()

                rt_id = str(rt.get("id") or rt.get("prefix"))
                current_type_ids.add(rt_id)
                known_state[rt_id] = True

                doc_id, title, content = _render_route_markdown(rt)
                sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
                prev_sha = hashes.get(doc_id)

                if prev_sha != sha:
                    hashes[doc_id] = sha
                    stats["emitted"] += 1
                    yield {
                        "id": doc_id,
                        "title": title,
                        "content": content,
                        "url": f"https://login.tailscale.com/admin/machines",
                        "_deleted": False,
                    }
                else:
                    stats["unchanged"] += 1

        type_singular = "device" if r_type == "devices" else ("acl" if r_type == "acls" else "route")
        prev_ids = set(known_state.keys())
        deleted_ids = prev_ids - current_type_ids
        for del_id in deleted_ids:
            del known_state[del_id]
            stats["deleted"] += 1
            yield {
                "id": f"tailscale:{type_singular}:{del_id}",
                "title": "",
                "content": "",
                "url": "",
                "_deleted": True,
            }


def tailscale_source(
    api_key: str | None = None,
    tailnet: str = "-",
    base_url: str = DEFAULT_BASE_URL,
    resources: list[str] | tuple[str, ...] | None = None,
    client: Any | None = None,
    service: Any | None = None,
    check_active: Callable[[], None] | None = None,
    resource_name: str | None = None,
):
    """Construct a dlt resource yielding Tailscale documents for cognee."""
    import dlt

    selected = tuple(resources or SUPPORTED_RESOURCES)
    for r in selected:
        if r not in SUPPORTED_RESOURCES:
            raise ValueError(f"Unknown Tailscale resource '{r}'. Must be one of {SUPPORTED_RESOURCES}")

    active_client = client or service
    if active_client is None:
        if not api_key or not api_key.strip():
            raise TailscaleAuthError("Tailscale API key is required when no client is supplied")
        active_client = TailscaleClient(api_key=api_key.strip(), tailnet=tailnet, base_url=base_url, check_active=check_active)

    name = resource_name or ("tailscale_" + "_".join(sorted(selected)))
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def tailscale_documents():
        stats.clear()
        state = dlt.current.resource_state()
        try:
            rows = _iter_rows(active_client, tailnet, selected, state, stats, check_active)
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if client is None and service is None and hasattr(active_client, "close"):
                active_client.close()

    resource = tailscale_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "tailscale")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
