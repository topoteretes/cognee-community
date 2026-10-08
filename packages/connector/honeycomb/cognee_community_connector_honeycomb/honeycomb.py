import logging
import time
from collections.abc import Iterator
from typing import Any

import dlt
import httpx
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.sources import DltSource

logger = logging.getLogger(__name__)

HONEYCOMB_SOURCE_NAME = "honeycomb"
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

    def list_queries(self, dataset: str) -> list[dict[str, Any]]:
        res = self._request("GET", f"/query_annotations/{dataset}")
        if isinstance(res, list):
            return res
        res_queries = self._request("GET", f"/queries/{dataset}")
        return res_queries if isinstance(res_queries, list) else []

    def list_triggers(self, dataset: str) -> list[dict[str, Any]]:
        res = self._request("GET", f"/triggers/{dataset}")
        return res if isinstance(res, list) else []

    def list_slos(self, dataset: str) -> list[dict[str, Any]]:
        res = self._request("GET", f"/slos/{dataset}")
        return res if isinstance(res, list) else []

    def list_markers(self, dataset: str) -> list[dict[str, Any]]:
        res = self._request("GET", f"/markers/{dataset}")
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
    board_style = board.get("style") or board.get("type", "flexible")
    updated_at = board.get("updated_at") or board.get("created_at") or ""

    panel_lines = []
    # Support flexible boards with panels
    panels = board.get("panels", [])
    if panels:
        for p in panels:
            p_type = p.get("type", "query")
            if p_type == "text":
                content = p.get("text") or p.get("content") or ""
                panel_lines.append(f"  - **Text Panel:** {content}")
            else:
                p_name = p.get("name") or p.get("caption") or "Query Panel"
                p_dataset = p.get("dataset", "")
                ds_str = f" (Dataset: `{p_dataset}`)" if p_dataset else ""
                panel_lines.append(f"  - **Query Panel:** {p_name}{ds_str}")
    else:
        # Fallback for queries list
        queries = board.get("queries", [])
        for q in queries:
            caption = q.get("caption") or q.get("name") or "Query"
            q_dataset = q.get("dataset", "")
            panel_lines.append(f"  - **{caption}** (Dataset: `{q_dataset}`)")

    panel_block = "\n".join(panel_lines) if panel_lines else "  - No panels or charts configured."

    text = f"""# Honeycomb Board: {name}
- **Board ID:** `{board_id}`
- **Board Layout Style:** {board_style}
- **Total Panels / Charts:** {len(panels) or len(board.get("queries", []))}
- **Last Updated:** {updated_at}

### Description
{description}

### Configured Panels & Queries
{panel_block}""".strip()

    return {
        "id": f"honeycomb_board_{board_id}",
        "title": f"Honeycomb Board: {name}",
        "text": text,
        "url": f"https://ui.honeycomb.io/boards/{board_id}",
        "resource_type": "board",
        "last_updated": updated_at,
    }


def _format_query_to_row(query: dict[str, Any], dataset_slug: str) -> dict[str, Any] | None:
    query_id = query.get("id") or query.get("query_id")
    if not query_id:
        return None

    name = query.get("name") or query.get("caption") or f"Query {query_id}"
    description = query.get("description", "Saved query specification.")
    updated_at = query.get("updated_at") or query.get("created_at") or ""
    query_spec = query.get("query") or query.get("query_spec") or {}

    calcs = [str(c) for c in query_spec.get("calculations", [])]
    filters = [str(f) for f in query_spec.get("filters", [])]
    breakdowns = query_spec.get("breakdowns", [])

    text = f"""# Honeycomb Saved Query: {name}
- **Query ID:** `{query_id}`
- **Dataset:** `{dataset_slug}`
- **Calculations:** {", ".join(calcs) or "COUNT"}
- **Breakdowns / Group By:** {", ".join(breakdowns) or "None"}
- **Filters:** {", ".join(filters) or "None"}
- **Last Updated:** {updated_at}

### Description & Analysis Intent
{description}""".strip()

    return {
        "id": f"honeycomb_query_{query_id}",
        "title": f"Honeycomb Query: {name}",
        "text": text,
        "url": f"https://ui.honeycomb.io/datasets/{dataset_slug}/queries/{query_id}",
        "resource_type": "query",
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


def _format_marker_to_row(marker: dict[str, Any], dataset_slug: str) -> dict[str, Any] | None:
    marker_id = marker.get("id")
    if not marker_id:
        return None

    m_type = marker.get("type", "deploy")
    message = marker.get("message", "Release/Incident marker.")
    start_time = marker.get("start_time") or marker.get("created_at") or ""
    end_time = marker.get("end_time", "")
    url = marker.get("url", "")

    text = f"""# Honeycomb Timeline Marker: {message}
- **Marker ID:** `{marker_id}`
- **Marker Type:** `{m_type}`
- **Dataset / Environment:** `{dataset_slug}`
- **Start Time:** {start_time}
- **End Time:** {end_time or "Point in time"}
- **External URL:** {url or "None"}

### Event Message & Context
{message}""".strip()

    return {
        "id": f"honeycomb_marker_{marker_id}",
        "title": f"Honeycomb Marker: {message}",
        "text": text,
        "url": url or f"https://ui.honeycomb.io/datasets/{dataset_slug}/markers/{marker_id}",
        "resource_type": "marker",
        "last_updated": start_time,
    }


def honeycomb_source(
    api_key: str,
    base_url: str = HONEYCOMB_API_BASE_US,
    include_datasets: bool = True,
    include_boards: bool = True,
    include_queries: bool = True,
    include_triggers: bool = True,
    include_slos: bool = True,
    include_markers: bool = True,
    client: HoneycombClient | None = None,
) -> DltSource:
    api_client = client or HoneycombClient(api_key=api_key, base_url=base_url)

    @dlt.resource(name="honeycomb_knowledge", write_disposition="replace")
    def honeycomb_knowledge() -> Iterator[dict[str, Any]]:
        count = 0
        datasets = []

        if (
            include_datasets
            or include_queries
            or include_triggers
            or include_slos
            or include_markers
        ):
            datasets = api_client.list_datasets()

        if include_datasets:
            for ds in datasets:
                row = _format_dataset_to_row(ds)
                if row:
                    count += 1
                    yield row

        if include_boards:
            for b in api_client.list_boards():
                row = _format_board_to_row(b)
                if row:
                    count += 1
                    yield row

        if include_queries:
            for ds in datasets:
                slug = ds.get("slug") or ds.get("name")
                if not slug:
                    continue
                for q in api_client.list_queries(slug):
                    row = _format_query_to_row(q, dataset_slug=slug)
                    if row:
                        count += 1
                        yield row

        if include_triggers:
            for ds in datasets:
                slug = ds.get("slug") or ds.get("name")
                if not slug:
                    continue
                for tr in api_client.list_triggers(slug):
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
                    row = _format_slo_to_row(slo, dataset_slug=slug)
                    if row:
                        count += 1
                        yield row

        if include_markers:
            # Check environment-wide markers (__all__) and per-dataset markers
            for marker in api_client.list_markers("__all__"):
                row = _format_marker_to_row(marker, dataset_slug="__all__")
                if row:
                    count += 1
                    yield row

            for ds in datasets:
                slug = ds.get("slug") or ds.get("name")
                if not slug:
                    continue
                for marker in api_client.list_markers(slug):
                    row = _format_marker_to_row(marker, dataset_slug=slug)
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
