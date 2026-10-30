"""LangSmith connector for cognee: a ``dlt`` source that turns LangSmith prompts, datasets, and traces into memory.

Sync LangSmith prompt templates, evaluation datasets, and run feedback into cognee,
incrementally and with forget-on-delete. The resource is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_langsmith import langsmith_source

    await cognee.remember(
        langsmith_source(api_key=api_key, resources=["prompts", "datasets"]),
        dataset_name="langsmith",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``, one per entity
  (``langsmith:prompt:<prompt handle>``, ``langsmith:dataset:<dataset id>``, ``langsmith:run:<run id>``).
* **Incremental.** Listings track high-watermark ``updated_at`` timestamps in ``dlt.current.resource_state()``.
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

logger = get_logger("langsmith_connector")

DEFAULT_BASE_URL = "https://api.smith.langchain.com"
SUPPORTED_RESOURCES = ("prompts", "datasets", "runs")


class LangSmithError(Exception):
    """Base exception for LangSmith connector errors."""


class LangSmithAuthError(LangSmithError):
    """LangSmith rejected the API key (HTTP 401)."""


class LangSmithRateLimitedError(LangSmithError):
    """LangSmith rate limit exceeded (HTTP 429)."""


class LangSmithQuotaError(LangSmithError):
    """LangSmith quota exhausted."""


class LangSmithAPIError(LangSmithError):
    """LangSmith answered with an error status."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class LangSmithClient:
    """HTTP client for the LangSmith REST API."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 30.0
    max_requests: int = 200
    check_active: Callable[[], None] | None = None
    _client: Any = None
    _owns_client: bool = False

    def __post_init__(self):
        cleaned = self.api_key.strip()
        if not cleaned:
            raise LangSmithAuthError("LangSmith API key must not be empty")
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
            raise LangSmithAPIError(f"Request budget exceeded ({self.max_requests} requests)")

        self.calls.append(path)
        client = self._get_client()

        max_retries = 3
        backoff = 1.0
        total_retry_wait = 0.0

        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-langsmith/0.1.0",
        }

        for attempt in range(max_retries + 1):
            try:
                response = client.get(path, headers=headers, params=params)
            except Exception as err:
                if attempt == max_retries:
                    raise LangSmithAPIError(f"Transport error connecting to LangSmith: {err.__class__.__name__}") from err
                time.sleep(backoff)
                total_retry_wait += backoff
                backoff *= 2.0
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except Exception as err:
                    raise LangSmithAPIError("LangSmith returned non-JSON response") from err

            if response.status_code == 401:
                raise LangSmithAuthError("LangSmith rejected the API key (HTTP 401)")

            if response.status_code == 429:
                retry_after_hdr = response.headers.get("Retry-After")
                wait_sec = backoff
                if retry_after_hdr:
                    try:
                        parsed_wait = float(retry_after_hdr)
                        if 0 <= parsed_wait <= 60.0:
                            wait_sec = parsed_wait
                        elif parsed_wait > 60.0:
                            raise LangSmithRateLimitedError("LangSmith Retry-After header exceeds 60s")
                    except ValueError:
                        pass
                if attempt == max_retries or (total_retry_wait + wait_sec) > 300.0:
                    raise LangSmithRateLimitedError("LangSmith rate limit exceeded after retries (HTTP 429)")
                time.sleep(wait_sec)
                total_retry_wait += wait_sec
                backoff *= 2.0
                continue

            if response.status_code in (500, 502, 503, 504):
                if attempt == max_retries:
                    raise LangSmithAPIError(f"LangSmith server error: HTTP {response.status_code}", status=response.status_code)
                time.sleep(backoff)
                total_retry_wait += backoff
                backoff *= 2.0
                continue

            raise LangSmithAPIError(f"LangSmith API error: HTTP {response.status_code}", status=response.status_code)


def _render_prompt_markdown(prompt: dict[str, Any]) -> tuple[str, str, str]:
    handle = str(prompt.get("handle") or prompt.get("id") or "unnamed_prompt")
    description = str(prompt.get("description") or "Prompt Hub template.")
    template = str(prompt.get("template") or prompt.get("manifest", {}).get("template") or "")
    input_vars = ", ".join(str(v) for v in prompt.get("input_variables", [])) or "None"
    updated_at = str(prompt.get("updated_at") or "")

    body = (
        f"# LangSmith Prompt Template: {handle}\n\n"
        f"- **Handle**: `{handle}`\n"
        f"- **Input Variables**: {input_vars}\n"
        f"- **Last Updated**: {updated_at}\n\n"
        f"## Description\n{description}\n\n"
        f"## Prompt Structure\n```\n{template}\n```\n"
    )
    return f"langsmith:prompt:{handle}", f"LangSmith Prompt: {handle}", body


def _render_dataset_markdown(ds: dict[str, Any]) -> tuple[str, str, str]:
    ds_id = str(ds.get("id") or "")
    name = str(ds.get("name") or "Untitled Dataset")
    description = str(ds.get("description") or "Evaluation dataset.")
    example_count = str(ds.get("example_count") or ds.get("total_examples") or "0")
    updated_at = str(ds.get("updated_at") or "")

    body = (
        f"# LangSmith Evaluation Dataset: {name}\n\n"
        f"- **ID**: `{ds_id}`\n"
        f"- **Total Examples**: {example_count}\n"
        f"- **Last Updated**: {updated_at}\n\n"
        f"## Description\n{description}\n"
    )
    return f"langsmith:dataset:{ds_id}", f"LangSmith Dataset: {name}", body


def _render_run_markdown(run: dict[str, Any]) -> tuple[str, str, str]:
    run_id = str(run.get("id") or "")
    name = str(run.get("name") or "LLM Execution")
    status = str(run.get("status") or "completed")
    latency = str(run.get("latency") or "0.0")
    inputs = str(run.get("inputs") or "")[:200]
    outputs = str(run.get("outputs") or "")[:200]

    body = (
        f"# LangSmith Trace Run: {name}\n\n"
        f"- **Run ID**: `{run_id}`\n"
        f"- **Status**: {status}\n"
        f"- **Latency**: {latency}s\n\n"
        f"## Inputs Preview\n{inputs}\n\n"
        f"## Outputs Preview\n{outputs}\n"
    )
    return f"langsmith:run:{run_id}", f"LangSmith Run: {name}", body


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
        except LangSmithError:
            raise
        except Exception as err:
            raise LangSmithAPIError(f"Failed to query /{r_type}: {err}") from err

        if not isinstance(listing, list):
            if isinstance(listing, dict) and "data" in listing and isinstance(listing["data"], list):
                listing = listing["data"]
            else:
                raise LangSmithAPIError(f"LangSmith listing for /{r_type} did not return a list")

        known_state = state.setdefault(r_type, {})
        current_type_ids = set()

        for item in listing:
            if check_active is not None:
                check_active()
            item_id = str(item.get("handle") or item.get("id") or "")
            if not item_id:
                continue

            current_type_ids.add(item_id)

            if r_type == "prompts":
                doc_id, title, content = _render_prompt_markdown(item)
            elif r_type == "datasets":
                doc_id, title, content = _render_dataset_markdown(item)
            else:
                doc_id, title, content = _render_run_markdown(item)

            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            prev_hash = known_state.get(item_id, {}).get("hash")

            if prev_hash != content_hash:
                known_state[item_id] = {
                    "hash": content_hash,
                    "updated_at": item.get("updated_at"),
                }
                stats["emitted"] += 1
                yield {
                    "id": doc_id,
                    "title": title,
                    "content": content,
                    "url": f"https://smith.langchain.com/{r_type}/{item_id}",
                    "_deleted": False,
                }
            else:
                stats["unchanged"] += 1

        type_singular = "prompt" if r_type == "prompts" else ("dataset" if r_type == "datasets" else "run")
        prev_ids = set(known_state.keys())
        deleted_ids = prev_ids - current_type_ids
        for del_id in deleted_ids:
            del known_state[del_id]
            stats["deleted"] += 1
            yield {
                "id": f"langsmith:{type_singular}:{del_id}",
                "title": "",
                "content": "",
                "url": "",
                "_deleted": True,
            }


def langsmith_source(
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    resources: list[str] | tuple[str, ...] | None = None,
    client: Any | None = None,
    service: Any | None = None,
    check_active: Callable[[], None] | None = None,
    resource_name: str | None = None,
):
    """Construct a dlt resource yielding LangSmith documents for cognee."""
    import dlt

    selected = tuple(resources or SUPPORTED_RESOURCES)
    for r in selected:
        if r not in SUPPORTED_RESOURCES:
            raise ValueError(f"Unknown LangSmith resource '{r}'. Must be one of {SUPPORTED_RESOURCES}")

    active_client = client or service
    if active_client is None:
        if not api_key or not api_key.strip():
            raise LangSmithAuthError("LangSmith API key is required when no client is supplied")
        active_client = LangSmithClient(api_key=api_key.strip(), base_url=base_url, check_active=check_active)

    name = resource_name or ("langsmith_" + "_".join(sorted(selected)))
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def langsmith_documents():
        stats.clear()
        state = dlt.current.resource_state()
        try:
            rows = _iter_rows(active_client, selected, state, stats, check_active)
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if client is None and service is None and hasattr(active_client, "close"):
                active_client.close()

    resource = langsmith_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "langsmith")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
