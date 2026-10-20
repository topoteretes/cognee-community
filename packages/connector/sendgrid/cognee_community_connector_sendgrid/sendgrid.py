"""DLT source for SendGrid templates, marketing lists, and email metadata (full snapshot + forget-on-delete).

Fetches SendGrid dynamic transactional email templates, subject lines, versions,
marketing lists, and sender identities, rendering them into clean Markdown documents.
Yields them as a dlt resource for cognee's ingestion pipeline.

Unlike relational dlt paths, SendGrid templates and communications are ingested as
*normal documents*: the source declares ``cognee_document_source = "sendgrid"``,
so ``resolve_dlt_sources`` tags each item with ``external_metadata["source"] = "sendgrid"``.
This ensures each document flows through cognee's standard cognify entity-extraction
pipeline — extracting entities, relationships, and concepts for agent reasoning —
instead of the flat dlt-row schema path.

The source uses a full snapshot synchronization model: ``write_disposition="replace"``
rewrites staging on each run with the templates and lists currently active. Deletions
propagate cleanly — an archived or deleted template drops out of the snapshot, so
cognee's existing ``orphan_cleanup`` removes it from the graph and vector stores.
Unchanged items preserve stable content-hash ``data_id`` keys, preventing redundant
re-ingestion.
"""

import os
import re
import time
from collections.abc import Iterator
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("sendgrid_connector")

SENDGRID_TABLE_NAME = "sendgrid_documents"
SENDGRID_SOURCE_NAME = "sendgrid"
SENDGRID_BASE_URL = "https://api.sendgrid.com/v3"

# Retry budget for rate-limited (429) / transient SendGrid API responses
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0


def _request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute an HTTP request with exponential backoff on 429 rate limits and 5xx errors."""
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.request(method, url, headers=headers, params=params, timeout=30.0)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", _BASE_BACKOFF * (2**attempt)))
                logger.warning("SendGrid rate limit reached, waiting %.1f seconds", retry_after)
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if attempt == _MAX_RETRIES - 1:
                raise RuntimeError(f"SendGrid request failed after {_MAX_RETRIES} attempts: {exc}") from exc
            time.sleep(_BASE_BACKOFF * (2**attempt))
    raise RuntimeError(f"SendGrid request exceeded retry budget for {url}")


def _strip_html_tags(text: str) -> str:
    """Helper to convert HTML email templates to clean readable text."""
    if not text:
        return ""
    clean = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL)
    clean = re.sub(r"<script.*?</script>", "", clean, flags=re.DOTALL)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def _format_template_markdown(template: dict[str, Any]) -> str:
    """Render a SendGrid template deterministically to Markdown for entity extraction."""
    t_id = template.get("id", "")
    name = template.get("name", "").strip()
    generation = template.get("generation", "dynamic")
    updated_at = template.get("updated_at", "")

    # Extract versions (ordered deterministically by version id)
    versions = template.get("versions") or []
    sorted_versions = sorted(versions, key=lambda v: str(v.get("id", "")))

    active_version = next((v for v in sorted_versions if v.get("active") == 1), None)
    if not active_version and sorted_versions:
        active_version = sorted_versions[-1]

    subject = (active_version or {}).get("subject", "")
    editor = (active_version or {}).get("editor", "code")

    # Content body preference: plain_content if available, otherwise strip html_content
    plain_content = (active_version or {}).get("plain_content", "")
    html_content = (active_version or {}).get("html_content", "")
    body_text = plain_content.strip() if plain_content else _strip_html_tags(html_content)

    doc_parts = [
        f"# SendGrid Template: {name}",
        f"- **Template ID**: {t_id}",
        f"- **Generation**: {generation}",
        f"- **Subject Line**: {subject if subject else 'None'}",
        f"- **Editor**: {editor}",
    ]
    if updated_at:
        doc_parts.append(f"- **Last Updated**: {updated_at}")

    doc_parts.append("\n## Email Content")
    doc_parts.append(body_text if body_text else "_No email body content available._")

    # If there are multiple versions, list them for context
    if len(sorted_versions) > 1:
        doc_parts.append("\n## Version History")
        for v in sorted_versions:
            v_name = v.get("name", "Version")
            v_active = " (Active)" if v.get("active") == 1 else ""
            doc_parts.append(f"- **{v_name}**{v_active} (ID: {v.get('id')})")

    return "\n".join(doc_parts)


def _format_marketing_list_markdown(mlist: dict[str, Any]) -> str:
    """Render a SendGrid marketing contact list deterministically to Markdown."""
    l_id = mlist.get("id", "")
    name = mlist.get("name", "").strip()
    contact_count = mlist.get("contact_count", 0)

    doc_parts = [
        f"# SendGrid Marketing List: {name}",
        f"- **List ID**: {l_id}",
        f"- **Total Contacts**: {contact_count}",
        "\n## Description",
        f"SendGrid contact segment containing {contact_count} subscribers.",
    ]
    return "\n".join(doc_parts)


def sendgrid_source(
    api_key: str | None = None,
    include_marketing_lists: bool = True,
    template_ids: list[str] | None = None,
    client: Any = None,
):
    """Create a dlt source that yields SendGrid email templates and marketing lists as markdown documents.

    Args:
        api_key: SendGrid API key (Bearer token). Falls back to ``SENDGRID_API_KEY``.
        include_marketing_lists: Ingest marketing contact lists in addition to templates (default: True).
        template_ids: Restrict ingestion to these specific template IDs. When omitted, all templates are ingested.
        client: Pre-built ``httpx.Client`` (primarily for test mock injection).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The SendGrid connector requires dlt: pip install "dlt[sqlalchemy] httpx"'
        ) from exc

    resolved_key = api_key or os.environ.get("SENDGRID_API_KEY")
    if not resolved_key:
        raise ValueError(
            "SendGrid API key required: pass api_key= or set SENDGRID_API_KEY."
        )

    http_client = client or httpx.Client(timeout=30.0)
    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
    }

    @dlt.resource(name=SENDGRID_TABLE_NAME, primary_key="id", write_disposition="replace")
    def sendgrid_documents() -> Iterator[dict[str, Any]]:
        # 1. Fetch Dynamic & Legacy Transactional Email Templates
        templates_url = f"{SENDGRID_BASE_URL}/templates?generations=dynamic,legacy&page_size=100"
        templates_data = _request_with_retry(http_client, "GET", templates_url, headers)
        templates_list = templates_data.get("result") or templates_data.get("templates") or []

        for summary in templates_list:
            t_id = str(summary.get("id"))
            if template_ids and t_id not in template_ids:
                continue

            # Fetch full template details to get active version content and subject
            detail_url = f"{SENDGRID_BASE_URL}/templates/{t_id}"
            full_template = _request_with_retry(http_client, "GET", detail_url, headers)

            doc_markdown = _format_template_markdown(full_template)
            yield {
                "id": f"template_{t_id}",
                "name": full_template.get("name", ""),
                "type": "email_template",
                "body": doc_markdown,
            }

        # 2. Ingest Marketing Contact Lists if enabled
        if include_marketing_lists:
            lists_url = f"{SENDGRID_BASE_URL}/marketing/lists?page_size=100"
            lists_data = _request_with_retry(http_client, "GET", lists_url, headers)
            lists = lists_data.get("result") or []

            for mlist in lists:
                l_id = str(mlist.get("id"))
                doc_markdown = _format_marketing_list_markdown(mlist)
                yield {
                    "id": f"list_{l_id}",
                    "name": mlist.get("name", ""),
                    "type": "marketing_list",
                    "body": doc_markdown,
                }

    # Declare document source attribute so cognee runs full entity extraction (document-mode)
    setattr(sendgrid_documents, DOCUMENT_SOURCE_ATTR, SENDGRID_SOURCE_NAME)

    @dlt.source(name=SENDGRID_SOURCE_NAME)
    def source():
        return sendgrid_documents

    src_instance = source()
    setattr(src_instance, DOCUMENT_SOURCE_ATTR, SENDGRID_SOURCE_NAME)
    return src_instance
