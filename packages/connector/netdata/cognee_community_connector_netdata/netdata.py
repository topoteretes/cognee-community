"""Netdata infrastructure monitoring, node topology & active alarm connector for Cognee.

Ingests node hardware/OS information, active health alarms (CRITICAL/WARNING),
and monitored subsystem chart catalogs into structured Markdown documents for Cognee memory.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from typing import Any, Iterable

import dlt
import httpx
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

NETDATA_SOURCE_NAME = "netdata"
NETDATA_TABLE_NAME = "netdata_infrastructure_telemetry"

DEFAULT_AGENT_BASE_URL = "http://localhost:19999"

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0


class NetdataAPIError(Exception):
    """Base exception for Netdata connector errors."""


class NetdataAuthError(NetdataAPIError):
    """Raised when Netdata rejects authentication (401/403)."""


class NetdataRateLimitError(NetdataAPIError):
    """Raised when Netdata throttles requests (429)."""


def _redact_token(text: str, token: str | None) -> str:
    """Mask credential strings in error messages to prevent leakage."""
    if not token or len(token) < 4:
        return text
    return text.replace(token, "[REDACTED_NETDATA_TOKEN]")


class NetdataConfig(BaseModel):
    """Configuration for Netdata data-source connector."""

    base_url: str = Field(
        default=DEFAULT_AGENT_BASE_URL,
        description="Netdata agent or Cloud URL (default: http://localhost:19999)",
    )
    api_token: str | None = Field(
        default=None,
        description="Optional API Bearer token for protected Netdata endpoints",
    )
    include_alarms: bool = Field(
        default=True,
        description="Whether to ingest active health alarms and failure diagnostics",
    )
    include_charts: bool = Field(
        default=True,
        description="Whether to ingest monitored chart catalogs and metrics",
    )
    alarm_status_filter: list[str] | None = Field(
        default=None,
        description="Filter alarms by status (e.g. ['CRITICAL', 'WARNING'], None for all)",
    )
    request_timeout: float = Field(
        default=30.0,
        description="HTTP request timeout in seconds",
    )


class NetdataClient:
    """Synchronous HTTP client for interacting with Netdata REST API v1."""

    def __init__(
        self,
        base_url: str = DEFAULT_AGENT_BASE_URL,
        api_token: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self._external_client = client

    def _get_client(self) -> httpx.Client:
        if self._external_client is not None:
            return self._external_client

        headers = {
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-netdata/0.1.0",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        return httpx.Client(headers=headers, timeout=self.timeout)

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        client = self._get_client()

        headers: dict[str, str] = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                )

                if response.status_code in (401, 403):
                    msg = f"Netdata authentication failed ({response.status_code}): {response.text}"
                    raise NetdataAuthError(_redact_token(msg, self.api_token))

                if response.status_code == 429:
                    if attempt == _MAX_RETRIES:
                        raise NetdataRateLimitError(
                            "Netdata rate limit exceeded (429) after maximum retries"
                        )
                    backoff = _BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5)
                    logger.warning(
                        "Rate limited by Netdata (429). Retrying in %.2fs (attempt %d/%d)",
                        backoff,
                        attempt,
                        _MAX_RETRIES,
                    )
                    time.sleep(backoff)
                    continue

                if response.status_code >= 500:
                    if attempt == _MAX_RETRIES:
                        msg = f"Netdata server error ({response.status_code}): {response.text}"
                        raise NetdataAPIError(_redact_token(msg, self.api_token))
                    backoff = _BASE_BACKOFF * (2 ** (attempt - 1))
                    logger.warning(
                        "Netdata server error (%d). Retrying in %.2fs...",
                        response.status_code,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue

                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, dict) else {"data": data}

            except httpx.HTTPError as exc:
                if attempt == _MAX_RETRIES:
                    msg = f"Netdata connection error: {exc}"
                    raise NetdataAPIError(_redact_token(msg, self.api_token)) from exc
                time.sleep(_BASE_BACKOFF * attempt)

        raise NetdataAPIError("Failed to communicate with Netdata after retries")

    def get_system_info(self) -> dict[str, Any]:
        """Fetch system information, OS, CPU cores, RAM and daemon status."""
        return self._request("GET", "/api/v1/info")

    def get_alarms(self, all_alarms: bool = True) -> dict[str, Any]:
        """Fetch active health alarms and status."""
        params = {"all": "true"} if all_alarms else None
        return self._request("GET", "/api/v1/alarms", params=params)

    def get_charts(self) -> dict[str, Any]:
        """Fetch available charts and metric definitions."""
        return self._request("GET", "/api/v1/charts")


# ---------------------------------------------------------------------------
# Markdown Formatting Helpers
# ---------------------------------------------------------------------------


def _build_node_markdown(info: dict[str, Any], base_url: str) -> str:
    """Format Netdata host and system topology into a clean Markdown document."""
    version = info.get("version", "unknown")
    uid = info.get("uid", "local")
    os_name = info.get("os_name", "Linux")
    os_version = info.get("os_version", "")
    kernel_name = info.get("kernel_name", "")
    kernel_version = info.get("kernel_version", "")
    architecture = info.get("architecture", "x86_64")
    cores = info.get("cores_total", info.get("cores", "n/a"))
    total_ram = info.get("total_ram", 0)
    ram_gb = f"{total_ram / (1024**3):.2f} GB" if isinstance(total_ram, (int, float)) else "n/a"

    alarms_summary = info.get("alarms", {})
    critical_cnt = alarms_summary.get("critical", 0)
    warning_cnt = alarms_summary.get("warning", 0)
    normal_cnt = alarms_summary.get("normal", 0)

    lines = [
        "# Netdata Node System & Infrastructure Overview",
        "",
        f"**Agent Endpoint**: `{base_url}`",
        f"**Node UID**: `{uid}`",
        f"**Netdata Daemon Version**: `{version}`",
        "",
        "## Hardware & Operating System Topology",
        f"- **Operating System**: {os_name} {os_version}".strip(),
        f"- **Kernel**: {kernel_name} {kernel_version}".strip(),
        f"- **CPU Architecture**: `{architecture}`",
        f"- **Total CPU Cores**: {cores}",
        f"- **Total RAM Capacity**: {ram_gb}",
        "",
        "## Real-Time Health Summary",
        f"- **Critical Alarms**: {critical_cnt}",
        f"- **Warning Alarms**: {warning_cnt}",
        f"- **Normal Health Checks**: {normal_cnt}",
        "",
    ]
    return "\n".join(lines)


def _build_alarms_markdown(
    hostname: str,
    alarms_data: dict[str, Any],
    status_filter: list[str] | None = None,
) -> str:
    """Format active Netdata health alarms into Markdown."""
    alarms_dict = alarms_data.get("alarms", {})
    lines = [
        f"# Netdata Active Health Alarms: {hostname}",
        "",
        f"**Monitored Host**: `{hostname}`",
        f"**Total Registered Alarms**: {len(alarms_dict)}",
        "",
        "## Active Alarms & Failure Diagnostics",
    ]

    count = 0
    for alarm_key, alarm in alarms_dict.items():
        status = str(alarm.get("status", "UNKNOWN")).upper()
        if status_filter and status not in [s.upper() for s in status_filter]:
            continue

        count += 1
        name = alarm.get("name", alarm_key)
        chart = alarm.get("chart", "unknown")
        val = alarm.get("value", "n/a")
        units = alarm.get("units", "")
        info = alarm.get("info", "No alarm description")
        recipient = alarm.get("recipient", "sysadmin")

        lines.extend(
            [
                f"### Alarm: `{name}` [{status}]",
                f"- **Chart Subsystem**: `{chart}`",
                f"- **Triggered Value**: {val} {units}".strip(),
                f"- **Diagnostic Info**: {info}",
                f"- **Notification Recipient**: `{recipient}`",
                "",
            ]
        )

    if count == 0:
        lines.append("*[No active alarms matching status criteria]*\n")

    return "\n".join(lines)


def _build_charts_markdown(hostname: str, charts_data: dict[str, Any]) -> str:
    """Format Netdata chart catalog and monitored metrics into Markdown."""
    charts_dict = charts_data.get("charts", {})
    lines = [
        f"# Netdata Monitored Subsystems & Chart Catalog: {hostname}",
        "",
        f"**Monitored Host**: `{hostname}`",
        f"**Total Monitored Charts**: {len(charts_dict)}",
        "",
        "## Subsystem Metrics Catalog",
    ]

    for chart_key, chart in list(charts_dict.items())[:100]:
        title = chart.get("title", chart_key)
        family = chart.get("family", "system")
        units = chart.get("units", "")
        context = chart.get("context", "")

        lines.extend(
            [
                f"### Chart: `{chart_key}`",
                f"- **Title**: {title}",
                f"- **Family Subsystem**: `{family}`",
                f"- **Context**: `{context}`",
                f"- **Measurement Units**: `{units}`",
                "",
            ]
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DLT Source Pipeline
# ---------------------------------------------------------------------------


def netdata_source(
    config: NetdataConfig | None = None,
    base_url: str = DEFAULT_AGENT_BASE_URL,
    api_token: str | None = None,
    include_alarms: bool = True,
    include_charts: bool = True,
    alarm_status_filter: list[str] | None = None,
    client: NetdataClient | None = None,
) -> Any:
    """DLT data source for ingesting Netdata system info, alarms, and charts."""
    if config is not None:
        cfg = config
    else:
        cfg = NetdataConfig(
            base_url=base_url,
            api_token=api_token,
            include_alarms=include_alarms,
            include_charts=include_charts,
            alarm_status_filter=alarm_status_filter,
        )

    nd_client = client or NetdataClient(
        base_url=cfg.base_url,
        api_token=cfg.api_token,
        timeout=cfg.request_timeout,
    )

    @dlt.resource(name=NETDATA_TABLE_NAME, write_disposition="replace")
    def netdata_resource() -> Iterable[dict[str, Any]]:
        # 1. Ingest Node System & Topology Info
        info = nd_client.get_system_info()
        uid = str(info.get("uid", "local_node"))
        hostname = info.get("mirrored_hosts", [uid])[0] if info.get("mirrored_hosts") else uid

        node_md = _build_node_markdown(info, cfg.base_url)
        node_doc_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"netdata:{uid}:node_overview"))
        yield {
            "id": node_doc_id,
            "text": node_md,
            "metadata": {
                "source": NETDATA_SOURCE_NAME,
                "report_type": "node_overview",
                "node_uid": uid,
                "hostname": hostname,
            },
        }

        # 2. Ingest Active Health Alarms
        if cfg.include_alarms:
            alarms_data = nd_client.get_alarms(all_alarms=True)
            alarms_md = _build_alarms_markdown(hostname, alarms_data, cfg.alarm_status_filter)
            alarms_doc_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"netdata:{uid}:active_alarms"))
            yield {
                "id": alarms_doc_id,
                "text": alarms_md,
                "metadata": {
                    "source": NETDATA_SOURCE_NAME,
                    "report_type": "active_alarms",
                    "node_uid": uid,
                    "hostname": hostname,
                },
            }

        # 3. Ingest Monitored Charts & Metrics Catalog
        if cfg.include_charts:
            charts_data = nd_client.get_charts()
            charts_md = _build_charts_markdown(hostname, charts_data)
            charts_doc_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"netdata:{uid}:metrics_catalog"))
            yield {
                "id": charts_doc_id,
                "text": charts_md,
                "metadata": {
                    "source": NETDATA_SOURCE_NAME,
                    "report_type": "metrics_catalog",
                    "node_uid": uid,
                    "hostname": hostname,
                },
            }

    @dlt.source(name=NETDATA_SOURCE_NAME)
    def _source() -> Any:
        return netdata_resource

    source_inst = _source()
    setattr(source_inst, DOCUMENT_SOURCE_ATTR, NETDATA_SOURCE_NAME)
    return source_inst


async def netdata_connector(
    config: NetdataConfig,
    client: NetdataClient | None = None,
) -> list[dict[str, Any]]:
    """Convenience async wrapper returning ingested documents directly for cognee.add()."""
    source = netdata_source(config=config, client=client)
    documents: list[dict[str, Any]] = []

    for resource_func in source.selected_resources.values():
        for item in resource_func():
            documents.append(item)

    return documents
