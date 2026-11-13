"""Mailgun connector for Cognee.

Ingests sending domains, DNS health, delivery statistics, email templates,
and bounce failure logs from Mailgun into structured Markdown documents for Cognee memory.
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

MAILGUN_SOURCE_NAME = "mailgun"
MAILGUN_TABLE_NAME = "mailgun_email_telemetry"

DEFAULT_US_BASE_URL = "https://api.mailgun.net/v3"
DEFAULT_EU_BASE_URL = "https://api.eu.mailgun.net/v3"

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0


class MailgunAPIError(Exception):
    """Base exception for Mailgun connector errors."""


class MailgunAuthError(MailgunAPIError):
    """Raised when Mailgun rejects credentials (401/403)."""


class MailgunRateLimitError(MailgunAPIError):
    """Raised when Mailgun throttles requests (429)."""


def _redact_token(text: str, token: str | None) -> str:
    """Mask credential strings in error messages to prevent leakage."""
    if not token or len(token) < 4:
        return text
    return text.replace(token, "[REDACTED_MAILGUN_KEY]")


class MailgunConfig(BaseModel):
    """Configuration for Mailgun data-source connector."""

    api_key: str = Field(
        ...,
        description="Mailgun API key (e.g. key-abc123xyz)",
    )
    domains: list[str] | None = Field(
        default=None,
        description="Specific sending domains to ingest (None for all configured)",
    )
    is_eu_region: bool = Field(
        default=False,
        description="Whether to route requests to the Mailgun EU endpoint",
    )
    include_stats: bool = Field(
        default=True,
        description="Whether to ingest delivery, bounce, and engagement statistics",
    )
    include_templates: bool = Field(
        default=True,
        description="Whether to ingest email templates and active versions",
    )
    include_bounces: bool = Field(
        default=True,
        description="Whether to ingest recent bounce events and failure diagnostics",
    )
    limit: int = Field(
        default=50,
        description="Pagination limit for bounce records",
    )
    request_timeout: float = Field(
        default=30.0,
        description="HTTP request timeout in seconds",
    )


class MailgunClient:
    """Synchronous HTTP client for interacting with Mailgun REST API v3."""

    def __init__(
        self,
        api_key: str,
        is_eu_region: bool = False,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.is_eu_region = is_eu_region
        self.timeout = timeout
        self.base_url = DEFAULT_EU_BASE_URL if is_eu_region else DEFAULT_US_BASE_URL
        self._client = client or httpx.Client(
            auth=("api", api_key),
            timeout=timeout,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        retries = 0

        while True:
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    params=params,
                )

                if response.status_code in (401, 403):
                    msg = (
                        f"Mailgun authentication rejected ({response.status_code}): {response.text}"
                    )
                    raise MailgunAuthError(_redact_token(msg, self.api_key))

                if response.status_code == 429:
                    retries += 1
                    if retries > _MAX_RETRIES:
                        raise MailgunRateLimitError("Mailgun rate limit exceeded after retries")
                    backoff = _BASE_BACKOFF * (2 ** (retries - 1)) + random.uniform(0.1, 0.5)
                    logger.warning("Mailgun rate-limited (429). Retrying in %.2fs...", backoff)
                    time.sleep(backoff)
                    continue

                if response.status_code >= 500:
                    retries += 1
                    if retries > _MAX_RETRIES:
                        response.raise_for_status()
                    backoff = _BASE_BACKOFF * (2 ** (retries - 1)) + random.uniform(0.1, 0.5)
                    logger.warning(
                        "Mailgun server error (%s). Retrying in %.2fs...",
                        response.status_code,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue

                response.raise_for_status()
                return response.json()

            except httpx.HTTPError as exc:
                if retries >= _MAX_RETRIES:
                    raise MailgunAPIError(_redact_token(str(exc), self.api_key)) from exc
                retries += 1
                time.sleep(_BASE_BACKOFF * retries)

    def get_domains(self) -> list[dict[str, Any]]:
        """Fetch list of sending domains configured in Mailgun."""
        result = self._request("GET", "/domains")
        items = result.get("items", [])
        if isinstance(items, list):
            return items
        return []

    def get_stats(self, domain: str) -> dict[str, Any]:
        """Fetch aggregated delivery and engagement statistics for a domain."""
        result = self._request(
            "GET",
            f"/{domain}/stats/total",
            params={"event": ["accepted", "delivered", "failed", "opened", "clicked"]},
        )
        if isinstance(result, dict):
            return result
        return {}

    def get_templates(self, domain: str) -> list[dict[str, Any]]:
        """Fetch all registered email templates for a domain."""
        result = self._request("GET", f"/{domain}/templates")
        items = result.get("items", [])
        if isinstance(items, list):
            return items
        return []

    def get_template_versions(
        self,
        domain: str,
        template_name: str,
    ) -> list[dict[str, Any]]:
        """Fetch version history for a specific email template."""
        result = self._request("GET", f"/{domain}/templates/{template_name}/versions")
        items = result.get("template", {}).get("versions", [])
        if isinstance(items, list):
            return items
        return []

    def get_bounces(self, domain: str, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch recent bounce events and diagnostics for a domain."""
        result = self._request("GET", f"/{domain}/bounces", params={"limit": limit})
        items = result.get("items", [])
        if isinstance(items, list):
            return items
        return []


def _build_domains_markdown(domains: list[dict[str, Any]], region: str) -> str:
    """Format sending domains and DNS verification into structured Markdown."""
    lines = [
        "# Mailgun Sending Domains & DNS Reputation",
        f"\n**Total Managed Domains**: {len(domains)}",
        f"**Region Endpoint**: `{region}`\n",
        "## Domain Inventory & Verification Status",
    ]

    for d in domains:
        name = d.get("name", "unknown")
        state = d.get("state", "unverified")
        d_type = d.get("type", "custom")
        spam_action = d.get("spam_action", "disabled")
        created = d.get("created_at", "n/a")

        lines.append(f"### Domain: `{name}`")
        lines.append(f"- **Sending Status**: {state.upper()}")
        lines.append(f"- **Domain Type**: {d_type}")
        lines.append(f"- **Spam Action**: {spam_action}")
        lines.append(f"- **Created At**: {created}\n")

    return "\n".join(lines) + "\n"


def _build_stats_markdown(domain: str, stats_data: dict[str, Any]) -> str:
    """Format delivery, bounce, and engagement analytics into structured Markdown."""
    stats_list = stats_data.get("stats", [])
    lines = [
        f"# Mailgun Delivery & Engagement Analytics: {domain}",
        f"\n**Domain**: `{domain}`\n",
        "## Delivery Metrics Summary",
    ]

    if not stats_list:
        lines.append("\nNo delivery event metrics recorded for the queried period.\n")
        return "\n".join(lines) + "\n"

    for entry in stats_list:
        time_slot = entry.get("time", "Recent Window")
        accepted = entry.get("accepted", {}).get("total", 0)
        delivered = entry.get("delivered", {}).get("total", 0)
        failed_perm = entry.get("failed", {}).get("permanent", {}).get("total", 0)
        failed_temp = entry.get("failed", {}).get("temporary", {}).get("total", 0)
        opened = entry.get("opened", {}).get("total", 0)
        clicked = entry.get("clicked", {}).get("total", 0)
        complained = entry.get("complained", {}).get("total", 0)

        lines.append(f"### Time Window: {time_slot}")
        lines.append(f"- **Accepted Messages**: {accepted}")
        lines.append(f"- **Successfully Delivered**: {delivered}")
        lines.append(f"- **Permanent Failures (Bounces)**: {failed_perm}")
        lines.append(f"- **Temporary Failures (Deferred)**: {failed_temp}")
        lines.append(f"- **Recipient Opens**: {opened}")
        lines.append(f"- **Link Clicks**: {clicked}")
        lines.append(f"- **Spam Complaints**: {complained}\n")

    return "\n".join(lines) + "\n"


def _build_templates_markdown(
    domain: str,
    templates: list[dict[str, Any]],
    versions_map: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """Format email templates and version history into structured Markdown."""
    versions_map = versions_map or {}
    lines = [
        f"# Mailgun Email Templates Catalog: {domain}",
        f"\n**Total Registered Templates**: {len(templates)}\n",
        "## Template Catalog",
    ]

    for t in templates:
        name = t.get("name", "untitled")
        desc = t.get("description", "No description provided.")
        created = t.get("createdAt", "n/a")

        lines.append(f"### Template: `{name}`")
        lines.append(f"- **Description**: {desc}")
        lines.append(f"- **Created At**: {created}")

        versions = versions_map.get(name, [])
        if versions:
            lines.append("- **Versions**:")
            for v in versions:
                tag = v.get("tag", "initial")
                comment = v.get("comment", "")
                active = "ACTIVE" if v.get("active") else "Inactive"
                lines.append(f"  - Tag `{tag}` [{active}]: {comment}")
        else:
            lines.append("- **Versions**: None available")

        lines.append("")

    return "\n".join(lines) + "\n"


def _build_bounces_markdown(domain: str, bounces: list[dict[str, Any]]) -> str:
    """Format bounce and delivery failure events into structured Markdown."""
    lines = [
        f"# Mailgun Bounce & Failure Diagnostics: {domain}",
        f"\n**Total Bounces Logged**: {len(bounces)}\n",
        "## Failure Diagnostics",
    ]

    if not bounces:
        lines.append("\nZero bounce events recorded. All recipient endpoints operational.\n")
        return "\n".join(lines) + "\n"

    for b in bounces:
        addr = b.get("address", "unknown@domain")
        code = b.get("code", "550")
        error_msg = b.get("error", "SMTP delivery failure")
        created = b.get("created_at", "n/a")

        lines.append(f"### Recipient: `{addr}`")
        lines.append(f"- **SMTP Error Code**: `{code}`")
        lines.append(f"- **Error Diagnostic**: {error_msg}")
        lines.append(f"- **Timestamp**: {created}\n")

    return "\n".join(lines) + "\n"


@dlt.source(name=MAILGUN_SOURCE_NAME)
def mailgun_source(
    config: MailgunConfig | None = None,
    api_key: str | None = None,
    domains: list[str] | None = None,
    is_eu_region: bool = False,
    include_stats: bool = True,
    include_templates: bool = True,
    include_bounces: bool = True,
    client: MailgunClient | None = None,
) -> Iterable[Any]:
    """DLT Source for Mailgun email telemetry, templates, and bounce diagnostics."""
    if config:
        cfg = config
    else:
        if not api_key and not client:
            raise ValueError("Either 'config', 'api_key', or 'client' must be provided.")
        cfg = MailgunConfig(
            api_key=api_key or (client.api_key if client else ""),
            domains=domains,
            is_eu_region=is_eu_region,
            include_stats=include_stats,
            include_templates=include_templates,
            include_bounces=include_bounces,
        )

    k_client = client or MailgunClient(
        api_key=cfg.api_key,
        is_eu_region=cfg.is_eu_region,
        timeout=cfg.request_timeout,
    )

    region_label = (
        "EU Region (api.eu.mailgun.net)" if cfg.is_eu_region else "US Region (api.mailgun.net)"
    )

    @dlt.resource(name=MAILGUN_TABLE_NAME, write_disposition="replace")
    def mailgun_resource() -> Iterable[dict[str, Any]]:
        domain_items = k_client.get_domains()

        if cfg.domains:
            domain_items = [d for d in domain_items if d.get("name") in cfg.domains]

        # 1. Ingest Sending Domains & DNS Reputation
        if domain_items:
            dom_md = _build_domains_markdown(domain_items, region_label)
            dom_doc_id = str(
                uuid.uuid5(uuid.NAMESPACE_OID, f"mailgun:domains_overview:{region_label}")
            )
            yield {
                "id": dom_doc_id,
                "text": dom_md,
                "metadata": {
                    "source": MAILGUN_SOURCE_NAME,
                    "report_type": "domains_overview",
                    "region": region_label,
                    "total_domains": len(domain_items),
                },
            }

        # Per-domain telemetry
        for d in domain_items:
            d_name = d.get("name")
            if not d_name:
                continue

            # 2. Ingest Delivery & Engagement Stats
            if cfg.include_stats:
                stats = k_client.get_stats(d_name)
                stats_md = _build_stats_markdown(d_name, stats)
                stats_doc_id = str(
                    uuid.uuid5(uuid.NAMESPACE_OID, f"mailgun:{d_name}:delivery_analytics")
                )
                yield {
                    "id": stats_doc_id,
                    "text": stats_md,
                    "metadata": {
                        "source": MAILGUN_SOURCE_NAME,
                        "report_type": "delivery_analytics",
                        "domain": d_name,
                    },
                }

            # 3. Ingest Templates & Version Previews
            if cfg.include_templates:
                templates = k_client.get_templates(d_name)
                versions_map: dict[str, list[dict[str, Any]]] = {}
                for t in templates:
                    t_name = t.get("name")
                    if t_name:
                        versions_map[t_name] = k_client.get_template_versions(d_name, t_name)

                if templates:
                    tmpl_md = _build_templates_markdown(d_name, templates, versions_map)
                    tmpl_doc_id = str(
                        uuid.uuid5(uuid.NAMESPACE_OID, f"mailgun:{d_name}:templates_catalog")
                    )
                    yield {
                        "id": tmpl_doc_id,
                        "text": tmpl_md,
                        "metadata": {
                            "source": MAILGUN_SOURCE_NAME,
                            "report_type": "templates_catalog",
                            "domain": d_name,
                            "total_templates": len(templates),
                        },
                    }

            # 4. Ingest Bounce & Failure Diagnostics
            if cfg.include_bounces:
                bounces = k_client.get_bounces(d_name, limit=cfg.limit)
                bounces_md = _build_bounces_markdown(d_name, bounces)
                bounces_doc_id = str(
                    uuid.uuid5(uuid.NAMESPACE_OID, f"mailgun:{d_name}:bounces_and_failures")
                )
                yield {
                    "id": bounces_doc_id,
                    "text": bounces_md,
                    "metadata": {
                        "source": MAILGUN_SOURCE_NAME,
                        "report_type": "bounces_and_failures",
                        "domain": d_name,
                        "total_bounces": len(bounces),
                    },
                }

    @dlt.source(name=MAILGUN_SOURCE_NAME)
    def _source() -> Any:
        return mailgun_resource

    source_inst = _source()
    # Cognee invariant: Must set DOCUMENT_SOURCE_ATTR to enable cognify()
    setattr(source_inst, DOCUMENT_SOURCE_ATTR, MAILGUN_SOURCE_NAME)
    return source_inst


async def mailgun_connector(
    config: MailgunConfig,
    client: MailgunClient | None = None,
) -> list[dict[str, Any]]:
    """Convenience async wrapper returning ingested documents directly for cognee.add()."""
    source = mailgun_source(config=config, client=client)
    documents: list[dict[str, Any]] = []

    for resource_func in source.selected_resources.values():
        for item in resource_func():
            documents.append(item)

    return documents
