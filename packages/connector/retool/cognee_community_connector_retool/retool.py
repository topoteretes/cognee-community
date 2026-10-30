"""Retool connector for cognee: a ``dlt`` source that turns Retool apps, workflows, and resources into memory.

Sync Retool internal tools, workflows, and data source metadata into cognee,
incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_retool import retool_source

    await cognee.remember(
        retool_source(token=token, resources=["apps", "workflows"]),
        dataset_name="retool",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``, one per entity
  (``retool:app:<app id>``, ``retool:workflow:<workflow id>``, ``retool:resource:<resource id>``).
* **Incremental.** Entity listings carry metadata including ``updatedAt`` and folder
  names. Listings are queried each run (one request per resource type), and full details
  are fetched only for items whose ``updatedAt``, name, folder, or configuration changed.
  Rendered text is hashed so untouched content is not re-emitted.
* **Forget-on-delete.** An item missing from its listing (deleted or moved out of selected scope)
  is emitted as a hard-delete tombstone. An access failure (403 or 404) on an endpoint is treated
  as an access error, never as deletion.
* **Bounded runs.** Respects API rate limits (HTTP 429 with ``Retry-After`` back-off), budget
  windows, and sanitizes secrets on all error paths.
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

logger = get_logger("retool_connector")

DEFAULT_BASE_URL = "https://api.retool.com/v1"
SUPPORTED_RESOURCES = ("apps", "workflows", "resources")


class RetoolError(Exception):
    """Base exception for Retool connector errors."""


class RetoolAuthError(RetoolError):
    """Retool rejected the API token (HTTP 401)."""


class RetoolRateLimitedError(RetoolError):
    """Retool rate limit exceeded (HTTP 429)."""


class RetoolQuotaError(RetoolError):
    """Retool plan request quota exhausted."""


class RetoolAPIError(RetoolError):
    """Retool answered with an error status."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class RetoolClient:
    """HTTP client for the Retool REST API."""

    token: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 30.0
    max_requests: int = 200
    check_active: Callable[[], None] | None = None
    _client: Any = None
    _owns_client: bool = False

    def __post_init__(self):
        cleaned = self.token.strip()
        if not cleaned:
            raise RetoolAuthError("Retool API token must not be empty")
        self.token = cleaned
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
            raise RetoolAPIError(f"Request budget exceeded ({self.max_requests} requests)")

        self.calls.append(path)
        client = self._get_client()

        max_retries = 3
        backoff = 1.0
        total_retry_wait = 0.0

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-retool/0.1.0",
        }

        for attempt in range(max_retries + 1):
            try:
                response = client.get(path, headers=headers, params=params)
            except Exception as err:
                if attempt == max_retries:
                    raise RetoolAPIError(f"Transport error connecting to Retool: {err.__class__.__name__}") from err
                time.sleep(backoff)
                total_retry_wait += backoff
                backoff *= 2.0
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except Exception as err:
                    raise RetoolAPIError("Retool returned non-JSON response") from err

            if response.status_code == 401:
                raise RetoolAuthError("Retool rejected the API token (HTTP 401)")

            if response.status_code == 429:
                retry_after_hdr = response.headers.get("Retry-After")
                wait_sec = backoff
                if retry_after_hdr:
                    try:
                        parsed_wait = float(retry_after_hdr)
                        if 0 <= parsed_wait <= 60.0:
                            wait_sec = parsed_wait
                        elif parsed_wait > 60.0:
                            raise RetoolRateLimitedError("Retool Retry-After header exceeds 60s")
                    except ValueError:
                        pass
                if attempt == max_retries or (total_retry_wait + wait_sec) > 300.0:
                    raise RetoolRateLimitedError("Retool rate limit exceeded after retries (HTTP 429)")
                time.sleep(wait_sec)
                total_retry_wait += wait_sec
                backoff *= 2.0
                continue

            if response.status_code in (500, 502, 503, 504):
                if attempt == max_retries:
                    raise RetoolAPIError(f"Retool server error: HTTP {response.status_code}", status=response.status_code)
                time.sleep(backoff)
                total_retry_wait += backoff
                backoff *= 2.0
                continue

            raise RetoolAPIError(f"Retool API error: HTTP {response.status_code}", status=response.status_code)


def _render_app_markdown(app: dict[str, Any], base_web_url: str = "https://retool.com") -> tuple[str, str, str]:
    app_id = str(app.get("id") or "")
    name = str(app.get("name") or "Untitled App")
    description = str(app.get("description") or "No description provided.")
    folder = str(app.get("folder") or "Root")
    version = str(app.get("currentVersion") or "draft")
    updated_at = str(app.get("updatedAt") or "")

    components = app.get("components") or []
    comp_list = ", ".join(str(c) for c in components[:15]) if components else "Standard layout"
    queries = app.get("queries") or []
    query_list = ", ".join(str(q) for q in queries[:15]) if queries else "None"

    url = f"{base_web_url}/apps/{app_id}"

    body = (
        f"# Retool App: {name}\n\n"
        f"- **ID**: `{app_id}`\n"
        f"- **Folder**: {folder}\n"
        f"- **Version**: {version}\n"
        f"- **Last Updated**: {updated_at}\n"
        f"- **URL**: {url}\n\n"
        f"## Description\n{description}\n\n"
        f"## Query Architecture\n{query_list}\n\n"
        f"## Components Summary\n{comp_list}\n"
    )
    return f"retool:app:{app_id}", f"Retool App: {name}", body


def _render_workflow_markdown(wf: dict[str, Any], base_web_url: str = "https://retool.com") -> tuple[str, str, str]:
    wf_id = str(wf.get("id") or "")
    name = str(wf.get("name") or "Untitled Workflow")
    description = str(wf.get("description") or "No description provided.")
    folder = str(wf.get("folder") or "Root")
    triggers = ", ".join(str(t) for t in wf.get("triggers", [])) or "Manual"
    updated_at = str(wf.get("updatedAt") or "")

    blocks = wf.get("blocks") or []
    block_summary = "\n".join(f"- `{b.get('name', 'block')}` ({b.get('type', 'step')})" for b in blocks[:20]) if blocks else "- Standard workflow steps"

    url = f"{base_web_url}/workflows/{wf_id}"

    body = (
        f"# Retool Workflow: {name}\n\n"
        f"- **ID**: `{wf_id}`\n"
        f"- **Folder**: {folder}\n"
        f"- **Triggers**: {triggers}\n"
        f"- **Last Updated**: {updated_at}\n"
        f"- **URL**: {url}\n\n"
        f"## Description\n{description}\n\n"
        f"## Workflow Steps\n{block_summary}\n"
    )
    return f"retool:workflow:{wf_id}", f"Retool Workflow: {name}", body


def _render_resource_markdown(res: dict[str, Any]) -> tuple[str, str, str]:
    res_id = str(res.get("id") or "")
    display_name = str(res.get("displayName") or res.get("name") or "Unnamed Resource")
    res_type = str(res.get("type") or "database")
    env = str(res.get("environment") or "production")
    description = str(res.get("description") or "Connected Retool resource.")

    body = (
        f"# Retool Data Resource: {display_name}\n\n"
        f"- **ID**: `{res_id}`\n"
        f"- **Type**: `{res_type}`\n"
        f"- **Environment**: {env}\n\n"
        f"## Description\n{description}\n"
    )
    return f"retool:resource:{res_id}", f"Retool Resource: {display_name}", body


def _iter_rows(
    client: Any,
    selected_resources: tuple[str, ...],
    state: dict | None = None,
    stats: dict[str, int] | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    if state is None:
        import dlt
        state = dlt.current.resource_state() if dlt else {}
    if stats is None:
        stats = {}
    stats.setdefault("emitted", 0)
    stats.setdefault("unchanged", 0)
    stats.setdefault("deleted", 0)

    for r_type in selected_resources:
        if check_active is not None:
            check_active()
        try:
            listing = client.get(f"/{r_type}")
        except RetoolError:
            raise
        except Exception as err:
            raise RetoolAPIError(f"Failed to query /{r_type}: {err}") from err

        if not isinstance(listing, list):
            if isinstance(listing, dict) and "data" in listing and isinstance(listing["data"], list):
                listing = listing["data"]
            else:
                raise RetoolAPIError(f"Retool listing for /{r_type} did not return a list")

        known_state = state.setdefault(r_type, {})
        current_type_ids = set()

        for item in listing:
            if check_active is not None:
                check_active()
            item_id = str(item.get("id") or "")
            if not item_id:
                continue

            current_type_ids.add(item_id)

            # Fetch details if needed
            detail = item
            if r_type in ("apps", "workflows") and not item.get("components") and not item.get("blocks"):
                try:
                    detail_res = client.get(f"/{r_type}/{item_id}")
                    if isinstance(detail_res, dict):
                        detail = {**item, **detail_res}
                except Exception:
                    detail = item

            if r_type == "apps":
                doc_id, title, content = _render_app_markdown(detail)
            elif r_type == "workflows":
                doc_id, title, content = _render_workflow_markdown(detail)
            else:
                doc_id, title, content = _render_resource_markdown(detail)

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            prev_hash = known_state.get(item_id, {}).get("hash")

            if prev_hash != content_hash:
                known_state[item_id] = {
                    "hash": content_hash,
                    "updatedAt": item.get("updatedAt"),
                }
                stats["emitted"] += 1
                yield {
                    "id": doc_id,
                    "title": title,
                    "content": content,
                    "url": detail.get("url") or f"https://retool.com/{r_type}/{item_id}",
                    "_deleted": False,
                }
            else:
                stats["unchanged"] += 1

        # Check for tombstones (forget-on-delete)
        # Type singular: apps -> app, workflows -> workflow, resources -> resource
        type_singular = "app" if r_type == "apps" else ("workflow" if r_type == "workflows" else "resource")
        prev_ids = set(known_state.keys())
        deleted_ids = prev_ids - current_type_ids
        for del_id in deleted_ids:
            del known_state[del_id]
            stats["deleted"] += 1
            yield {
                "id": f"retool:{type_singular}:{del_id}",
                "title": "",
                "content": "",
                "url": "",
                "_deleted": True,
            }


def retool_source(
    token: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    resources: list[str] | tuple[str, ...] | None = None,
    client: Any | None = None,
    service: Any | None = None,
    check_active: Callable[[], None] | None = None,
    resource_name: str | None = None,
):
    """Construct a dlt resource yielding Retool metadata documents for cognee."""
    import dlt

    selected = tuple(resources or SUPPORTED_RESOURCES)
    for r in selected:
        if r not in SUPPORTED_RESOURCES:
            raise ValueError(f"Unknown Retool resource '{r}'. Must be one of {SUPPORTED_RESOURCES}")

    active_client = client or service
    if active_client is None:
        if not token or not token.strip():
            raise RetoolAuthError("Retool API token is required when no client is supplied")
        active_client = RetoolClient(token=token.strip(), base_url=base_url, check_active=check_active)

    name = resource_name or ("retool_" + "_".join(sorted(selected)))
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def retool_documents():
        stats.clear()
        state = dlt.current.resource_state()
        try:
            rows = _iter_rows(active_client, selected, state, stats, check_active)
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if client is None and service is None and hasattr(active_client, "close"):
                active_client.close()

    resource = retool_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "retool")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
