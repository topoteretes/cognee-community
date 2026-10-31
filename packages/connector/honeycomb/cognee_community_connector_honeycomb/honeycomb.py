"""DLT source for Honeycomb observability metadata (boards, triggers, SLOs, markers).

Fetches telemetry schema definitions, flexible dashboards/boards, alerting triggers,
SLOs, burn alerts, and deployment markers from Honeycomb REST API v1,
then yields them as a dlt document resource for Cognee's ingestion pipeline.

Declares ``cognee_document_source = "honeycomb"``, so ``resolve_dlt_sources`` routes
each entity through Cognee's cognify entity-extraction and knowledge graph completion pipeline.

Implements a full-snapshot replacement model: ``write_disposition="replace"`` ensures each sync
overwrites staging with the exact set of boards, triggers, and SLO definitions
currently visible in Honeycomb. If an SLO or trigger is deleted, it drops out of subsequent syncs
and is cleanly purged via ``orphan_cleanup``.
"""

import json
import re
import time
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from .config import HoneycombConfig

logger = get_logger("honeycomb_connector")

HONEYCOMB_TABLE_NAME = "honeycomb_entities"
HONEYCOMB_SOURCE_NAME = "honeycomb"

_MAX_RETRIES = 5
_RETRY_BACKOFF_FACTOR = 0.5
_SECRET_PATTERN = re.compile(
    r"(token|secret|password|key|credential|authorization|webhook)", re.IGNORECASE
)


def redact_sensitive_value(key: str, val: Any) -> Any:
    """Redact sensitive values based on key naming."""
    if isinstance(key, str) and _SECRET_PATTERN.search(key):
        return "[REDACTED]"
    if isinstance(val, str) and (
        val.startswith(
            ("http://hooks.slack.com", "https://hooks.slack.com", "https://events.pagerduty.com")
        )
        or re.match(r"^[A-Za-z0-9_-]{20,}$", val)
    ):
        return "[REDACTED]"
    return val


def redact_dictionary(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive key-value pairs in a dictionary."""
    redacted = {}
    for k, v in data.items():
        if isinstance(v, dict):
            redacted[k] = redact_dictionary(v)
        elif isinstance(v, list):
            redacted[k] = [
                redact_dictionary(elem)
                if isinstance(elem, dict)
                else redact_sensitive_value(k, elem)
                for elem in v
            ]
        else:
            redacted[k] = redact_sensitive_value(k, v)
    return redacted


class HoneycombClient:
    """HTTP client for querying Honeycomb REST API v1 endpoints with retry backoff."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.honeycomb.io",
        timeout: float = 15.0,
        max_retries: int = _MAX_RETRIES,
        retry_backoff_factor: float = _RETRY_BACKOFF_FACTOR,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_factor = retry_backoff_factor

    def _get_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": "cognee-honeycomb-connector/0.1.0",
            "X-Honeycomb-Team": self.api_key,
        }

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Perform GET request with exponential backoff and Retry-After support."""
        import httpx

        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = self._get_headers()

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url, headers=headers, params=params)
                    if resp.status_code == 404:
                        logger.warning("Honeycomb endpoint %s not found (404)", url)
                        return None
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("Retry-After")
                        backoff = (
                            float(retry_after)
                            if retry_after and retry_after.isdigit()
                            else self.retry_backoff_factor * (2 ** (attempt - 1))
                        )
                        logger.warning(
                            "Rate limited (429) on %s. Retrying in %.2fs (attempt %d/%d)...",
                            url,
                            backoff,
                            attempt,
                            self.max_retries,
                        )
                        time.sleep(backoff)
                        continue
                    resp.raise_for_status()
                    return resp.json()
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                resp_code = getattr(getattr(exc, "response", None), "status_code", 0)
                is_transient = isinstance(
                    exc, httpx.ConnectError | httpx.TimeoutException
                ) or resp_code in (500, 502, 503, 504)
                if is_transient and attempt < self.max_retries:
                    backoff = self.retry_backoff_factor * (2 ** (attempt - 1))
                    logger.warning(
                        "Transient error contacting %s (%s). Retrying in %.2fs (attempt %d/%d)...",
                        url,
                        exc,
                        backoff,
                        attempt,
                        self.max_retries,
                    )
                    time.sleep(backoff)
                else:
                    logger.error("Failed contacting Honeycomb %s: %s", url, exc)
                    raise

        return None

    def list_datasets(self) -> list[dict[str, Any]]:
        """Retrieve list of datasets."""
        res = self.get("1/datasets")
        return res if isinstance(res, list) else []

    def list_columns(self, dataset_slug: str) -> list[dict[str, Any]]:
        """Retrieve schema columns for a dataset."""
        res = self.get(f"1/columns/{dataset_slug}")
        return res if isinstance(res, list) else []

    def list_boards(self) -> list[dict[str, Any]]:
        """Retrieve list of boards."""
        res = self.get("1/boards")
        return res if isinstance(res, list) else []

    def list_triggers(self, dataset_slug: str) -> list[dict[str, Any]]:
        """Retrieve triggers for a dataset."""
        res = self.get(f"1/triggers/{dataset_slug}")
        return res if isinstance(res, list) else []

    def list_slos(self, dataset_slug: str) -> list[dict[str, Any]]:
        """Retrieve SLOs for a dataset."""
        res = self.get(f"1/slos/{dataset_slug}")
        return res if isinstance(res, list) else []

    def list_burn_alerts(self, dataset_slug: str) -> list[dict[str, Any]]:
        """Retrieve burn alerts for a dataset."""
        res = self.get(f"1/burn_alerts/{dataset_slug}")
        return res if isinstance(res, list) else []

    def list_markers(self, dataset_slug: str) -> list[dict[str, Any]]:
        """Retrieve markers for a dataset."""
        res = self.get(f"1/markers/{dataset_slug}")
        return res if isinstance(res, list) else []


def _render_dataset_markdown(dataset: dict[str, Any], columns: list[dict[str, Any]]) -> str:
    """Render a dataset and its column schema into Markdown."""
    name = dataset.get("name", "unnamed")
    slug = dataset.get("slug", "unknown")
    desc = dataset.get("description") or "No description provided."
    last_written = dataset.get("last_written_at", "-")

    lines = [
        f"# Honeycomb Dataset: {name} (`{slug}`)",
        "",
        f"- **Name**: `{name}`",
        f"- **Slug**: `{slug}`",
        f"- **Description**: {desc}",
        f"- **Last Written At**: {last_written}",
        f"- **Column Count**: {len(columns)}",
        "",
        "## Schema Columns",
        "| Column Name | Type | Description | Hidden |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for c in columns:
        c_name = c.get("key_name", c.get("name", "-"))
        c_type = c.get("type", "string")
        c_desc = c.get("description") or "-"
        c_hidden = "Yes" if c.get("hidden") else "No"
        lines.append(f"| `{c_name}` | `{c_type}` | {c_desc} | {c_hidden} |")

    return "\n".join(lines).strip()


def _render_board_markdown(board: dict[str, Any]) -> str:
    """Render a Honeycomb board into Markdown."""
    name = board.get("name", "unnamed_board")
    board_id = board.get("id", "unknown")
    desc = board.get("description") or "No description provided."
    style = board.get("style", "visual")
    queries = board.get("queries", [])

    lines = [
        f"# Honeycomb Board: {name}",
        "",
        f"- **Board ID**: `{board_id}`",
        f"- **Name**: `{name}`",
        f"- **Style**: `{style}`",
        f"- **Description**: {desc}",
        f"- **Panel Count**: {len(queries)}",
        "",
        "## Board Panels & Queries",
    ]
    for q in queries:
        caption = q.get("caption") or "Query Panel"
        dataset = q.get("dataset") or "-"
        q_annot = q.get("query_annotation_id") or "-"
        lines.append(f"- **Panel**: `{caption}` (Dataset: `{dataset}`, Annotation: `{q_annot}`)")

    return "\n".join(lines).strip()


def _render_trigger_markdown(dataset_slug: str, trigger: dict[str, Any]) -> str:
    """Render a Honeycomb trigger into Markdown."""
    name = trigger.get("name", "unnamed_trigger")
    t_id = trigger.get("id", "unknown")
    desc = trigger.get("description") or "No description provided."
    disabled = trigger.get("disabled", False)
    threshold = trigger.get("threshold", {})
    op = threshold.get("op", ">")
    value = threshold.get("value", "-")
    frequency = trigger.get("frequency", "-")

    recipients = redact_dictionary({"recipients": trigger.get("recipients", [])}).get(
        "recipients", []
    )
    recipients_str = (
        ", ".join(f"`{r.get('type')}:{r.get('target', '[REDACTED]')}`" for r in recipients)
        if recipients
        else "None"
    )

    query_spec = trigger.get("query", {})
    query_json = json.dumps(redact_dictionary(query_spec), indent=2)

    return f"""# Honeycomb Trigger: {name}

- **Trigger ID**: `{t_id}`
- **Dataset**: `{dataset_slug}`
- **Status**: `{"Disabled" if disabled else "Active"}`
- **Frequency**: Every `{frequency}s`
- **Threshold**: `{op} {value}`
- **Description**: {desc}
- **Recipients**: {recipients_str}

## Evaluated Query Specification
```json
{query_json}
```
""".strip()


def _render_slo_markdown(
    dataset_slug: str, slo: dict[str, Any], burn_alerts: list[dict[str, Any]]
) -> str:
    """Render a Honeycomb SLO and its burn alerts into Markdown."""
    name = slo.get("name", "unnamed_slo")
    slo_id = slo.get("id", "unknown")
    desc = slo.get("description") or "No description provided."
    target_pct = slo.get("target_per_million", 990000) / 10000.0
    time_period_days = slo.get("time_period_days", 30)
    sli_expr = slo.get("sli", "-")

    lines = [
        f"# Honeycomb SLO: {name}",
        "",
        f"- **SLO ID**: `{slo_id}`",
        f"- **Dataset**: `{dataset_slug}`",
        f"- **Target**: `{target_pct}%`",
        f"- **Time Window**: `{time_period_days} days`",
        f"- **SLI Filter**: `{sli_expr}`",
        f"- **Description**: {desc}",
        "",
        "## Configured Burn Alerts",
        "| Burn Alert ID | Exhaustion Hours | Created At |",
        "| :--- | :--- | :--- |",
    ]
    for ba in burn_alerts:
        ba_id = ba.get("id", "-")
        ex_hours = ba.get("exhaustion_minutes", 0) / 60.0
        c_at = ba.get("created_at", "-")
        lines.append(f"| `{ba_id}` | `{ex_hours:.1f}h` | {c_at} |")

    return "\n".join(lines).strip()


def _render_markers_markdown(dataset_slug: str, markers: list[dict[str, Any]]) -> str:
    """Render deployment / incident markers into Markdown."""
    lines = [
        f"# Honeycomb Markers: `{dataset_slug}`",
        "",
        f"- **Dataset**: `{dataset_slug}`",
        f"- **Marker Count**: {len(markers)}",
        "",
        "## Recent Markers",
        "| Message | Type | Start Time | URL |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for m in markers[:50]:
        msg = m.get("message", "-")
        m_type = m.get("type", "deploy")
        start = m.get("start_time", "-")
        url = m.get("url", "-")
        lines.append(f"| {msg} | `{m_type}` | {start} | `{url}` |")

    return "\n".join(lines).strip()


def honeycomb_source(
    config: HoneycombConfig | None = None,
    client: HoneycombClient | None = None,
):
    """Create a dlt source yielding Honeycomb observability telemetry as Markdown documents.

    Args:
        config: Configuration instance.
        client: Optional injected HoneycombClient (or mock) for testing.
    """
    import dlt

    cfg = config or HoneycombConfig()
    resolved_client = client or HoneycombClient(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_seconds,
        max_retries=cfg.max_retries,
        retry_backoff_factor=cfg.retry_backoff_factor,
    )

    @dlt.source(name=HONEYCOMB_SOURCE_NAME)
    def _honeycomb():
        @dlt.resource(
            name=HONEYCOMB_TABLE_NAME,
            write_disposition=cfg.write_disposition,
        )
        def honeycomb_entities() -> Iterator[dict[str, Any]]:
            # 1. Fetch datasets
            try:
                datasets = resolved_client.list_datasets()
            except Exception as exc:
                logger.error("Failed to list Honeycomb datasets: %s", exc)
                raise

            for ds in datasets:
                slug = ds.get("slug")
                if not slug:
                    continue
                if cfg.dataset_slugs and slug not in cfg.dataset_slugs:
                    continue

                columns = resolved_client.list_columns(slug) if cfg.include_columns else []
                ds_content = _render_dataset_markdown(ds, columns)

                yield {
                    "id": f"dataset:{slug}",
                    "url": f"{resolved_client.base_url}/{slug}",
                    "title": f"Honeycomb Dataset: {ds.get('name', slug)}",
                    "content": ds_content,
                    "entity_type": "dataset",
                    "dataset_slug": slug,
                }

                # Sync Triggers for this dataset
                if cfg.include_triggers:
                    try:
                        triggers = resolved_client.list_triggers(slug)
                        for t in triggers:
                            t_id = t.get("id", "unknown")
                            t_content = _render_trigger_markdown(slug, t)
                            yield {
                                "id": f"trigger:{slug}:{t_id}",
                                "url": f"{resolved_client.base_url}/{slug}/triggers/{t_id}",
                                "title": f"Honeycomb Trigger: {t.get('name', t_id)}",
                                "content": t_content,
                                "entity_type": "trigger",
                                "dataset_slug": slug,
                            }
                    except Exception as exc:
                        logger.warning("Could not sync triggers for dataset %s: %s", slug, exc)

                # Sync SLOs and burn alerts
                if cfg.include_slos:
                    try:
                        slos = resolved_client.list_slos(slug)
                        burn_alerts = resolved_client.list_burn_alerts(slug)
                        burn_by_slo: dict[str, list[dict[str, Any]]] = {}
                        for ba in burn_alerts:
                            burn_by_slo.setdefault(ba.get("slo_id", ""), []).append(ba)

                        for s in slos:
                            s_id = s.get("id", "unknown")
                            s_content = _render_slo_markdown(slug, s, burn_by_slo.get(s_id, []))
                            yield {
                                "id": f"slo:{slug}:{s_id}",
                                "url": f"{resolved_client.base_url}/{slug}/slos/{s_id}",
                                "title": f"Honeycomb SLO: {s.get('name', s_id)}",
                                "content": s_content,
                                "entity_type": "slo",
                                "dataset_slug": slug,
                            }
                    except Exception as exc:
                        logger.warning("Could not sync SLOs for dataset %s: %s", slug, exc)

                # Sync Markers
                if cfg.include_markers:
                    try:
                        markers = resolved_client.list_markers(slug)
                        if markers:
                            m_content = _render_markers_markdown(slug, markers)
                            yield {
                                "id": f"markers:{slug}",
                                "url": f"{resolved_client.base_url}/{slug}/markers",
                                "title": f"Honeycomb Markers: {slug}",
                                "content": m_content,
                                "entity_type": "markers",
                                "dataset_slug": slug,
                            }
                    except Exception as exc:
                        logger.warning("Could not sync markers for dataset %s: %s", slug, exc)

            # 2. Sync Boards
            if cfg.include_boards:
                try:
                    boards = resolved_client.list_boards()
                    for b in boards:
                        b_id = b.get("id", "unknown")
                        b_content = _render_board_markdown(b)
                        yield {
                            "id": f"board:{b_id}",
                            "url": f"{resolved_client.base_url}/boards/{b_id}",
                            "title": f"Honeycomb Board: {b.get('name', b_id)}",
                            "content": b_content,
                            "entity_type": "board",
                        }
                except Exception as exc:
                    logger.warning("Could not sync Honeycomb boards: %s", exc)

        return honeycomb_entities

    source = _honeycomb()
    setattr(source, DOCUMENT_SOURCE_ATTR, HONEYCOMB_SOURCE_NAME)
    return source
