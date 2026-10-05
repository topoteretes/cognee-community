import logging
import time
from collections.abc import Iterator
from typing import Any

import dlt
import httpx
from dlt.sources import DltSource

logger = logging.getLogger(__name__)

HONEYCOMB_SOURCE_NAME = "honeycomb"
DOCUMENT_SOURCE_ATTR = f"{HONEYCOMB_SOURCE_NAME}_document"
HONEYCOMB_API_BASE_US = "https://api.honeycomb.io/v1"
HONEYCOMB_API_BASE_EU = "https://api.eu1.honeycomb.io/v1"
_MAX_RETRIES = 3


class HoneycombClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = HONEYCOMB_API_BASE_US,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "X-Honeycomb-Team": self.api_key,
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-honeycomb",
        }

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(_MAX_RETRIES):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=params,
                    )
                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("Retry-After", 2**attempt))
                        time.sleep(retry_after)
                        continue
                    if resp.status_code == 404:
                        return []
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403, 404) or attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
        return []

    def list_datasets(self) -> list[dict[str, Any]]:
        res = self._request("GET", "/datasets")
        return res if isinstance(res, list) else []

    def list_boards(self) -> list[dict[str, Any]]:
        res = self._request("GET", "/boards")
        return res if isinstance(res, list) else []

    def list_triggers(self, dataset: str) -> list[dict[str, Any]]:
        res = self._request("GET", f"/triggers/{dataset}")
        return res if isinstance(res, list) else []

    def list_slos(self, dataset: str) -> list[dict[str, Any]]:
        res = self._request("GET", f"/slos/{dataset}")
        return res if isinstance(res, list) else []


def _format_dataset_to_row(dataset: dict[str, Any]) -> dict[str, Any] | None:
    slug = dataset.get("slug") or dataset.get("name")
    if not slug:
        return None

    name = dataset.get("name") or slug
    description = dataset.get("description", "No description provided.")
    last_written = dataset.get("last_written_at") or dataset.get("created_at") or ""
    created_at = dataset.get("created_at", "")

    text = f"""# Honeycomb Dataset: {name}
- **Dataset Slug:** `{slug}`
- **Last Written At:** {last_written}
- **Created At:** {created_at}

### Description & Scope
{description}""".strip()

    return {
        "id": f"honeycomb_dataset_{slug}",
        "title": f"Honeycomb Dataset: {name}",
        "text": text,
        "url": f"https://ui.honeycomb.io/datasets/{slug}",
        "resource_type": "dataset",
        "last_updated": last_written or created_at,
    }


def _format_board_to_row(board: dict[str, Any]) -> dict[str, Any] | None:
    board_id = board.get("id")
    if not board_id:
        return None

    name = board.get("name", f"Board {board_id}")
    description = board.get("description", "Observability dashboard.")
    board_type = board.get("type", "board")
    updated_at = board.get("updated_at") or board.get("created_at") or ""
    queries = board.get("queries", [])
    query_count = len(queries)

    query_lines = []
    for q in queries:
        caption = q.get("caption") or q.get("name") or "Query"
        q_dataset = q.get("dataset", "")
        query_lines.append(f"  - **{caption}** (Dataset: `{q_dataset}`)")

    query_block = "\n".join(query_lines) if query_lines else "  - None specified."

    text = f"""# Honeycomb Board: {name}
- **Board ID:** `{board_id}`
- **Board Type:** {board_type}
- **Total Queries / Panels:** {query_count}
- **Last Updated:** {updated_at}

### Description
{description}

### Configured Queries & Charts
{query_block}""".strip()

    return {
        "id": f"honeycomb_board_{board_id}",
        "title": f"Honeycomb Board: {name}",
        "text": text,
        "url": f"https://ui.honeycomb.io/boards/{board_id}",
        "resource_type": "board",
        "last_updated": updated_at,
    }


def _format_trigger_to_row(trigger: dict[str, Any], dataset_slug: str) -> dict[str, Any] | None:
    trigger_id = trigger.get("id")
    if not trigger_id:
        return None

    name = trigger.get("name", f"Trigger {trigger_id}")
    description = trigger.get("description", "Alert trigger definition.")
    disabled = trigger.get("disabled", False)
    frequency = trigger.get("frequency", 60)
    threshold = trigger.get("threshold", {})
    op = threshold.get("op", ">")
    value = threshold.get("value", "N/A")
    updated_at = trigger.get("updated_at") or trigger.get("created_at") or ""

    text = f"""# Honeycomb Trigger: {name}
- **Trigger ID:** `{trigger_id}`
- **Dataset:** `{dataset_slug}`
- **Status:** {"Disabled" if disabled else "Active"}
- **Evaluation Frequency:** Every {frequency}s
- **Threshold Rule:** Metric {op} {value}
- **Last Updated:** {updated_at}

### Description & Runbook
{description}""".strip()

    return {
        "id": f"honeycomb_trigger_{trigger_id}",
        "title": f"Honeycomb Trigger: {name}",
        "text": text,
        "url": f"https://ui.honeycomb.io/datasets/{dataset_slug}/triggers/{trigger_id}",
        "resource_type": "trigger",
        "last_updated": updated_at,
    }


def _format_slo_to_row(slo: dict[str, Any], dataset_slug: str) -> dict[str, Any] | None:
    slo_id = slo.get("id")
    if not slo_id:
        return None

    name = slo.get("name", f"SLO {slo_id}")
    description = slo.get("description", "Service Level Objective.")
    target_percentage = slo.get("target_percentage", 99.9)
    time_period_days = slo.get("time_period_days", 30)
    sli = slo.get("sli", {})
    sli_alias = sli.get("alias", "request_success")
    updated_at = slo.get("updated_at") or slo.get("created_at") or ""

    text = f"""# Honeycomb Service Level Objective (SLO): {name}
- **SLO ID:** `{slo_id}`
- **Dataset:** `{dataset_slug}`
- **Target Reliability:** {target_percentage}% over {time_period_days} days
- **SLI Metric Definition:** `{sli_alias}`
- **Last Updated:** {updated_at}

### Target Rationale & Scope
{description}""".strip()

    return {
        "id": f"honeycomb_slo_{slo_id}",
        "title": f"Honeycomb SLO: {name}",
        "text": text,
        "url": f"https://ui.honeycomb.io/datasets/{dataset_slug}/slos/{slo_id}",
        "resource_type": "slo",
        "last_updated": updated_at,
    }


def honeycomb_source(
    api_key: str,
    base_url: str = HONEYCOMB_API_BASE_US,
    include_datasets: bool = True,
    include_boards: bool = True,
    include_triggers: bool = True,
    include_slos: bool = True,
    since: str | None = None,
    client: HoneycombClient | None = None,
) -> DltSource:
    api_client = client or HoneycombClient(api_key=api_key, base_url=base_url)

    @dlt.resource(name="honeycomb_knowledge", write_disposition="replace")
    def honeycomb_knowledge() -> Iterator[dict[str, Any]]:
        count = 0
        datasets = []

        if include_datasets or include_triggers or include_slos:
            datasets = api_client.list_datasets()

        if include_datasets:
            for ds in datasets:
                updated_at = ds.get("last_written_at") or ds.get("created_at") or ""
                if since and updated_at and updated_at < since:
                    continue
                row = _format_dataset_to_row(ds)
                if row:
                    count += 1
                    yield row

        if include_boards:
            for b in api_client.list_boards():
                updated_at = b.get("updated_at") or b.get("created_at") or ""
                if since and updated_at and updated_at < since:
                    continue
                row = _format_board_to_row(b)
                if row:
                    count += 1
                    yield row

        if include_triggers:
            for ds in datasets:
                slug = ds.get("slug") or ds.get("name")
                if not slug:
                    continue
                for tr in api_client.list_triggers(slug):
                    updated_at = tr.get("updated_at") or tr.get("created_at") or ""
                    if since and updated_at and updated_at < since:
                        continue
                    row = _format_trigger_to_row(tr, dataset_slug=slug)
                    if row:
                        count += 1
                        yield row

        if include_slos:
            for ds in datasets:
                slug = ds.get("slug") or ds.get("name")
                if not slug:
                    continue
                for slo in api_client.list_slos(slug):
                    updated_at = slo.get("updated_at") or slo.get("created_at") or ""
                    if since and updated_at and updated_at < since:
                        continue
                    row = _format_slo_to_row(slo, dataset_slug=slug)
                    if row:
                        count += 1
                        yield row

        logger.info("Honeycomb: synced %d knowledge item(s).", count)

    @dlt.source(name=HONEYCOMB_SOURCE_NAME)
    def _honeycomb():
        return honeycomb_knowledge

    source = _honeycomb()
    setattr(source, DOCUMENT_SOURCE_ATTR, HONEYCOMB_SOURCE_NAME)
    return source


__all__ = [
    "DOCUMENT_SOURCE_ATTR",
    "HoneycombClient",
    "honeycomb_source",
]
