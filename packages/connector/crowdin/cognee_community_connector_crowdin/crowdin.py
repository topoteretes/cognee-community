"""Crowdin connector for Cognee.

Ingests projects, terminology glossaries, source strings, and translations
from Crowdin instances into structured Markdown documents for Cognee memory.
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

CROWDIN_SOURCE_NAME = "crowdin"
CROWDIN_TABLE_NAME = "crowdin_localization"
DEFAULT_BASE_URL = "https://api.crowdin.com/api/v2"

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0


class CrowdinAPIError(Exception):
    """Base exception for Crowdin connector errors."""


class CrowdinAuthError(CrowdinAPIError):
    """Raised when Crowdin rejects credentials (401/403)."""


class CrowdinRateLimitError(CrowdinAPIError):
    """Raised when Crowdin throttles requests (429)."""


def _redact_token(text: str, token: str | None) -> str:
    """Mask credential strings in error messages to prevent leakage."""
    if not token or len(token) < 4:
        return text
    return text.replace(token, "[REDACTED_CROWDIN_TOKEN]")


class CrowdinConfig(BaseModel):
    """Configuration for Crowdin data-source connector."""

    api_token: str = Field(
        ...,
        description="Crowdin Personal Access Token (Bearer token)",
    )
    project_ids: list[int] | None = Field(
        default=None,
        description="Specific project IDs to ingest (None for all accessible)",
    )
    organization_domain: str | None = Field(
        default=None,
        description="Optional organization subdomain for Crowdin Enterprise",
    )
    include_glossaries: bool = Field(
        default=True,
        description="Whether to ingest terminology glossaries and concept definitions",
    )
    include_strings: bool = Field(
        default=True,
        description="Whether to ingest source strings, keys, and translations",
    )
    limit: int = Field(
        default=50,
        description="Pagination limit for string and term retrieval",
    )
    request_timeout: float = Field(
        default=30.0,
        description="HTTP request timeout in seconds",
    )


class CrowdinClient:
    """Synchronous HTTP client for interacting with Crowdin REST API v2."""

    def __init__(
        self,
        api_token: str,
        organization_domain: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_token = api_token
        self.organization_domain = organization_domain
        self.timeout = timeout

        if organization_domain:
            self.base_url = f"https://{organization_domain}.api.crowdin.com/api/v2"
        else:
            self.base_url = DEFAULT_BASE_URL

        self._client = client or httpx.Client(timeout=timeout)

    def _get_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_token}",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._get_headers()
        retries = 0

        while True:
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                )

                if response.status_code in (401, 403):
                    msg = (
                        f"Crowdin authentication rejected ({response.status_code}): {response.text}"
                    )
                    raise CrowdinAuthError(_redact_token(msg, self.api_token))

                if response.status_code == 429:
                    retries += 1
                    if retries > _MAX_RETRIES:
                        raise CrowdinRateLimitError("Crowdin rate limit exceeded after retries")
                    backoff = _BASE_BACKOFF * (2 ** (retries - 1)) + random.uniform(0.1, 0.5)
                    logger.warning("Crowdin rate-limited (429). Retrying in %.2fs...", backoff)
                    time.sleep(backoff)
                    continue

                if response.status_code >= 500:
                    retries += 1
                    if retries > _MAX_RETRIES:
                        response.raise_for_status()
                    backoff = _BASE_BACKOFF * (2 ** (retries - 1)) + random.uniform(0.1, 0.5)
                    logger.warning(
                        "Crowdin server error (%s). Retrying in %.2fs...",
                        response.status_code,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue

                response.raise_for_status()
                return response.json()

            except httpx.HTTPError as exc:
                if retries >= _MAX_RETRIES:
                    raise CrowdinAPIError(_redact_token(str(exc), self.api_token)) from exc
                retries += 1
                time.sleep(_BASE_BACKOFF * retries)

    def get_projects(self) -> list[dict[str, Any]]:
        """Fetch accessible Crowdin projects."""
        result = self._request("GET", "/projects")
        data = result.get("data", [])
        return [item.get("data", item) for item in data]

    def get_glossaries(self) -> list[dict[str, Any]]:
        """Fetch accessible terminology glossaries."""
        result = self._request("GET", "/glossaries")
        data = result.get("data", [])
        return [item.get("data", item) for item in data]

    def get_glossary_terms(self, glossary_id: int) -> list[dict[str, Any]]:
        """Fetch terms and definitions for a given glossary."""
        result = self._request("GET", f"/glossaries/{glossary_id}/terms")
        data = result.get("data", [])
        return [item.get("data", item) for item in data]

    def get_source_strings(self, project_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch source translation strings for a project."""
        result = self._request(
            "GET",
            f"/projects/{project_id}/strings",
            params={"limit": limit},
        )
        data = result.get("data", [])
        return [item.get("data", item) for item in data]

    def get_string_translations(
        self,
        project_id: int,
        string_id: int,
    ) -> list[dict[str, Any]]:
        """Fetch approved or pending translations for a specific string."""
        result = self._request(
            "GET",
            f"/projects/{project_id}/translations",
            params={"stringId": string_id},
        )
        data = result.get("data", [])
        return [item.get("data", item) for item in data]


def _build_project_overview_markdown(project: dict[str, Any]) -> str:
    """Format project metadata into structured Markdown."""
    name = project.get("name", "Untitled Project")
    identifier = project.get("identifier", "unknown")
    proj_id = project.get("id", "n/a")
    source_lang = project.get("sourceLanguageId", "en")
    target_langs = ", ".join(project.get("targetLanguageIds", [])) or "none configured"
    desc = project.get("description", "No description provided.")
    web_url = project.get("webUrl", f"https://crowdin.com/project/{identifier}")

    lines = [
        f"# Crowdin Project: {name} (ID: {proj_id})",
        f"\n**Identifier**: `{identifier}`",
        f"**Source Language**: `{source_lang}`",
        f"**Target Languages**: {target_langs}",
        f"**Web Dashboard**: {web_url}\n",
        "## Project Description",
        f"{desc}\n",
    ]
    return "\n".join(lines) + "\n"


def _build_glossary_markdown(
    glossary: dict[str, Any],
    terms: list[dict[str, Any]],
) -> str:
    """Format glossary terminology into structured Markdown."""
    g_id = glossary.get("id", "unknown")
    name = glossary.get("name", f"Glossary {g_id}")
    terms_count = len(terms)

    lines = [
        f"# Terminology Glossary: {name} (ID: {g_id})",
        f"\n**Total Terms Recorded**: {terms_count}\n",
        "## Approved Terms & Conceptual Definitions",
    ]

    for t in terms:
        t_id = t.get("id", "n/a")
        text = t.get("text", "unspecified")
        lang = t.get("languageId", "all")
        pos = t.get("partOfSpeech", "general")
        status = t.get("status", "approved")
        definition = t.get("description", "No definition provided.")

        lines.append(f"### Term: `{text}` ({lang.upper()})")
        lines.append(f"- **Term ID**: {t_id}")
        lines.append(f"- **Part of Speech**: {pos}")
        lines.append(f"- **Status**: {status}")
        lines.append(f"- **Definition / Context**: {definition}\n")

    return "\n".join(lines) + "\n"


def _build_strings_markdown(
    project_name: str,
    project_id: int,
    strings_data: list[dict[str, Any]],
) -> str:
    """Format source strings and translations into structured Markdown."""
    lines = [
        f"# Localization Strings & Translations: {project_name} (ID: {project_id})",
        f"\n**Total Strings Tracked**: {len(strings_data)}\n",
        "## String Catalog",
    ]

    for s_entry in strings_data:
        s_id = s_entry.get("id", "n/a")
        identifier = s_entry.get("identifier", f"string_{s_id}")
        text = s_entry.get("text", "")
        context = s_entry.get("context", "General UI context")
        translations = s_entry.get("translations", [])

        lines.append(f"### Key: `{identifier}` (ID: {s_id})")
        lines.append(f'- **Source Text**: "{text}"')
        lines.append(f"- **Developer Context**: {context}")

        if translations:
            lines.append("- **Translations**:")
            for tr in translations:
                tr_lang = tr.get("languageId", "unknown")
                tr_text = tr.get("text", "")
                rating = tr.get("rating", 0)
                lines.append(f'  - `[{tr_lang.upper()}]`: "{tr_text}" (rating: {rating})')
        else:
            lines.append("- **Translations**: None available")

        lines.append("")

    return "\n".join(lines) + "\n"


@dlt.source(name=CROWDIN_SOURCE_NAME)
def crowdin_source(
    config: CrowdinConfig | None = None,
    api_token: str | None = None,
    project_ids: list[int] | None = None,
    organization_domain: str | None = None,
    include_glossaries: bool = True,
    include_strings: bool = True,
    client: CrowdinClient | None = None,
) -> Iterable[Any]:
    """DLT Source for Crowdin localization, glossaries, and string translations."""
    if config:
        cfg = config
    else:
        if not api_token and not client:
            raise ValueError("Either 'config', 'api_token', or 'client' must be provided.")
        cfg = CrowdinConfig(
            api_token=api_token or (client.api_token if client else ""),
            project_ids=project_ids,
            organization_domain=organization_domain,
            include_glossaries=include_glossaries,
            include_strings=include_strings,
        )

    k_client = client or CrowdinClient(
        api_token=cfg.api_token,
        organization_domain=cfg.organization_domain,
        timeout=cfg.request_timeout,
    )

    @dlt.resource(name=CROWDIN_TABLE_NAME, write_disposition="replace")
    def crowdin_resource() -> Iterable[dict[str, Any]]:
        projects = k_client.get_projects()

        if cfg.project_ids:
            projects = [p for p in projects if p.get("id") in cfg.project_ids]

        # 1. Ingest Project Overviews
        for p in projects:
            p_id = p.get("id")
            p_name = p.get("name", f"Project-{p_id}")
            if p_id is not None:
                p_md = _build_project_overview_markdown(p)
                p_doc_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"crowdin:project_overview:{p_id}"))
                yield {
                    "id": p_doc_id,
                    "text": p_md,
                    "metadata": {
                        "source": CROWDIN_SOURCE_NAME,
                        "report_type": "project_overview",
                        "project_id": p_id,
                        "project_name": p_name,
                    },
                }

        # 2. Ingest Glossaries & Approved Terms
        if cfg.include_glossaries:
            glossaries = k_client.get_glossaries()
            for g in glossaries:
                g_id = g.get("id")
                if g_id is not None:
                    terms = k_client.get_glossary_terms(g_id)
                    g_md = _build_glossary_markdown(g, terms)
                    g_doc_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"crowdin:glossary:{g_id}"))
                    yield {
                        "id": g_doc_id,
                        "text": g_md,
                        "metadata": {
                            "source": CROWDIN_SOURCE_NAME,
                            "report_type": "glossary",
                            "glossary_id": g_id,
                            "total_terms": len(terms),
                        },
                    }

        # 3. Ingest Source Strings & Available Translations
        if cfg.include_strings:
            for p in projects:
                p_id = p.get("id")
                p_name = p.get("name", f"Project-{p_id}")
                if p_id is not None:
                    strings = k_client.get_source_strings(p_id, limit=cfg.limit)
                    strings_with_trans: list[dict[str, Any]] = []
                    for s in strings:
                        s_id = s.get("id")
                        trans = k_client.get_string_translations(p_id, s_id) if s_id else []
                        s_copy = dict(s)
                        s_copy["translations"] = trans
                        strings_with_trans.append(s_copy)

                    if strings_with_trans:
                        s_md = _build_strings_markdown(p_name, p_id, strings_with_trans)
                        s_doc_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"crowdin:strings:{p_id}"))
                        yield {
                            "id": s_doc_id,
                            "text": s_md,
                            "metadata": {
                                "source": CROWDIN_SOURCE_NAME,
                                "report_type": "strings_and_translations",
                                "project_id": p_id,
                                "project_name": p_name,
                                "total_strings": len(strings_with_trans),
                            },
                        }

    @dlt.source(name=CROWDIN_SOURCE_NAME)
    def _source() -> Any:
        return crowdin_resource

    source_inst = _source()
    # Cognee invariant: Must set DOCUMENT_SOURCE_ATTR to enable cognify()
    setattr(source_inst, DOCUMENT_SOURCE_ATTR, CROWDIN_SOURCE_NAME)
    return source_inst


async def crowdin_connector(
    config: CrowdinConfig,
    client: CrowdinClient | None = None,
) -> list[dict[str, Any]]:
    """Convenience async wrapper returning ingested documents directly for cognee.add()."""
    source = crowdin_source(config=config, client=client)
    documents: list[dict[str, Any]] = []

    for resource_func in source.selected_resources.values():
        for item in resource_func():
            documents.append(item)

    return documents
