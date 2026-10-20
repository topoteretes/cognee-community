"""DLT source for Postmark transactional templates and message history (full snapshot + forget-on-delete).

Fetches Postmark transactional email templates (subject, HTML/plain text body, alias,
layout) and delivery metadata, rendering them into clean Markdown documents.
Yields them as a dlt resource for cognee's ingestion pipeline.

Unlike relational dlt paths, Postmark templates and communications are ingested as
*normal documents*: the source declares ``cognee_document_source = "postmark"``,
so ``resolve_dlt_sources`` tags each item with ``external_metadata["source"] = "postmark"``.
This ensures each document flows through cognee's standard cognify entity-extraction
pipeline — extracting entities, relationships, and concepts for agent reasoning —
instead of the flat dlt-row schema path.

The source uses a full snapshot synchronization model: ``write_disposition="replace"``
rewrites staging on each run with the templates currently active in Postmark. Deletions
propagate cleanly — an archived or deleted template drops out of the snapshot, so
cognee's existing ``orphan_cleanup`` removes it from the graph and vector stores.
Unchanged items preserve stable content-hash ``data_id`` keys, preventing redundant
re-ingestion.
"""

from collections.abc import Iterator
import os
import re
import time
from typing import Any

import httpx

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("postmark_connector")

POSTMARK_TABLE_NAME = "postmark_documents"
POSTMARK_SOURCE_NAME = "postmark"
POSTMARK_BASE_URL = "https://api.postmarkapp.com"

# Retry budget for rate-limited (429) / transient Postmark API responses
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
                logger.warning("Postmark rate limit reached, waiting %.1f seconds", retry_after)
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if attempt == _MAX_RETRIES - 1:
                raise RuntimeError(f"Postmark request failed after {_MAX_RETRIES} attempts: {exc}") from exc
            time.sleep(_BASE_BACKOFF * (2**attempt))
    raise RuntimeError(f"Postmark request exceeded retry budget for {url}")


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
    """Render a Postmark template deterministically to Markdown for entity extraction."""
    t_id = template.get("TemplateId") or template.get("id", "")
    name = (template.get("Name") or "").strip()
    alias = template.get("Alias") or "None"
    template_type = template.get("TemplateType") or "Standard"
    subject = (template.get("Subject") or "").strip()
    layout = template.get("LayoutTemplate") or "None"

    # Prefer TextBody, fallback to stripped HtmlBody
    text_body = (template.get("TextBody") or "").strip()
    html_body = (template.get("HtmlBody") or "").strip()
    body_content = text_body if text_body else _strip_html_tags(html_body)

    doc_parts = [
        f"# Postmark Template: {name}",
        f"- **Template ID**: {t_id}",
        f"- **Alias**: {alias}",
        f"- **Template Type**: {template_type}",
        f"- **Subject Line**: {subject if subject else 'None'}",
        f"- **Layout Template**: {layout}",
        "\n## Email Content",
        body_content if body_content else "_No email body content available._",
    ]
    return "\n".join(doc_parts)


def postmark_source(
    server_token: str | None = None,
    template_ids: list[str | int] | None = None,
    template_type: str = "Standard",
    client: Any = None,
):
    """Create a dlt source that yields Postmark transactional email templates as markdown documents.

    Args:
        server_token: Postmark Server API Token. Falls back to ``POSTMARK_SERVER_TOKEN``.
        template_ids: Restrict ingestion to specific template IDs or aliases. When omitted, all templates are ingested.
        template_type: Type of templates to ingest ('Standard', 'Layout', or 'All', default: 'Standard').
        client: Pre-built ``httpx.Client`` (primarily for test mock injection).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` / ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Postmark connector requires dlt: pip install "dlt[sqlalchemy] httpx"'
        ) from exc

    resolved_token = server_token or os.environ.get("POSTMARK_SERVER_TOKEN")
    if not resolved_token:
        raise ValueError(
            "Postmark Server API Token required: pass server_token= or set POSTMARK_SERVER_TOKEN."
        )

    http_client = client or httpx.Client(timeout=30.0)
    headers = {
        "X-Postmark-Server-Token": resolved_token,
        "Accept": "application/json",
    }

    @dlt.resource(name=POSTMARK_TABLE_NAME, primary_key="id", write_disposition="replace")
    def postmark_documents() -> Iterator[dict[str, Any]]:
        offset = 0
        count = 100

        while True:
            params = {
                "Count": count,
                "Offset": offset,
                "TemplateType": template_type,
            }
            templates_url = f"{POSTMARK_BASE_URL}/templates"
            data = _request_with_retry(http_client, "GET", templates_url, headers, params)
            templates = data.get("Templates") or []
            if not templates:
                break

            for summary in templates:
                t_id = str(summary.get("TemplateId"))
                alias = summary.get("Alias")

                # If specific template_ids are requested, filter by ID or Alias
                if template_ids:
                    str_ids = [str(x) for x in template_ids]
                    if t_id not in str_ids and (not alias or alias not in str_ids):
                        continue

                # Fetch full template details (including HtmlBody and TextBody)
                detail_url = f"{POSTMARK_BASE_URL}/templates/{t_id}"
                full_template = _request_with_retry(http_client, "GET", detail_url, headers)

                doc_markdown = _format_template_markdown(full_template)
                yield {
                    "id": f"template_{t_id}",
                    "name": full_template.get("Name", ""),
                    "alias": full_template.get("Alias", ""),
                    "type": "email_template",
                    "body": doc_markdown,
                }

            total_count = data.get("TotalCount", 0)
            offset += count
            if offset >= total_count:
                break

    # Declare document source attribute so cognee runs full entity extraction (document-mode)
    setattr(postmark_documents, DOCUMENT_SOURCE_ATTR, POSTMARK_SOURCE_NAME)

    @dlt.source(name=POSTMARK_SOURCE_NAME)
    def source():
        return postmark_documents

    src_instance = source()
    setattr(src_instance, DOCUMENT_SOURCE_ATTR, POSTMARK_SOURCE_NAME)
    return src_instance
