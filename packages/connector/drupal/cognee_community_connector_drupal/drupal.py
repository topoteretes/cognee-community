"""Drupal CMS content and documentation data-source connector for Cognee.

Ingests articles, basic pages, policies, and knowledge base nodes from Drupal core JSON:API
into Cognee memory for natural language retrieval and enterprise search.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Generator, List, Optional, Tuple
import dlt
import httpx
from bs4 import BeautifulSoup

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

logger = logging.getLogger(__name__)

DEFAULT_DRUPAL_BASE_URL = "http://localhost:8080"


def _html_to_clean_text(html_str: Optional[str]) -> str:
    """Strip HTML markup from Drupal node body into clean readable text."""
    if not html_str:
        return ""
    import re
    soup = BeautifulSoup(html_str, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+([.,!?;:])", r"\1", text)


class DrupalClient:
    """Client for querying Drupal core JSON:API endpoints."""

    def __init__(
        self,
        base_url: str = DEFAULT_DRUPAL_BASE_URL,
        auth: Optional[Tuple[str, str]] = None,
        http_client: Optional[Any] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {
            "User-Agent": "CogneeCommunityDrupalConnector/0.1.0",
            "Accept": "application/vnd.api+json",
        }
        self._http_client = http_client or httpx.Client(
            timeout=timeout,
            headers=headers,
            auth=auth,
        )

    def get_nodes(
        self,
        bundle: str = "article",
        limit: int = 50,
        changed_since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve content nodes for a specific bundle type (e.g. article, page)."""
        url = f"{self.base_url}/jsonapi/node/{bundle}"
        params: Dict[str, Any] = {"page[limit]": limit, "sort": "-changed"}
        if changed_since:
            params["filter[changed][operator]"] = ">="
            params["filter[changed][value]"] = changed_since

        try:
            resp = self._http_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
        except Exception as exc:
            logger.error("Failed to fetch Drupal nodes for bundle '%s': %s", bundle, exc)
            raise


def _node_to_document(node: Dict[str, Any], bundle: str, base_url: str) -> Optional[Dict[str, Any]]:
    """Convert a Drupal JSON:API node into a structured Markdown document."""
    node_id = node.get("id")  # Drupal JSON:API uses UUID for id
    attrs = node.get("attributes", {})
    title = attrs.get("title")
    if not node_id or not title:
        return None

    created = attrs.get("created", "Unknown")
    changed = attrs.get("changed", created)
    path_info = attrs.get("path", {})
    path_alias = path_info.get("alias") or f"/node/{attrs.get('drupal_internal__nid', node_id)}"
    url = f"{base_url}{path_alias}"

    # Extract body & summary
    body_data = attrs.get("body", {})
    if isinstance(body_data, dict):
        raw_body = body_data.get("value") or body_data.get("processed") or ""
        summary = body_data.get("summary") or ""
    elif isinstance(body_data, str):
        raw_body = body_data
        summary = ""
    else:
        raw_body = ""
        summary = ""

    clean_body = _html_to_clean_text(raw_body)
    clean_summary = _html_to_clean_text(summary)

    content = f"""# Drupal {bundle.capitalize()}: {title}
**UUID:** `{node_id}`
**URL:** [{title}]({url})
**Content Type (Bundle):** `{bundle}`
**Last Changed:** {changed} | **Created:** {created}
{f'**Summary:** {clean_summary}' if clean_summary else ''}

## Document Body:
{clean_body if clean_body else '*(No content body provided)*'}

---
*Ingested from Drupal CMS at `{base_url}` via Cognee community connector.*"""

    return {
        "id": f"drupal_{bundle}_{node_id}",
        "url": url,
        "title": f"[{bundle.capitalize()}] {title}",
        "content": content,
    }


@dlt.source(name="drupal_source")
def drupal_source(
    base_url: str = DEFAULT_DRUPAL_BASE_URL,
    bundles: Optional[List[str]] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    limit: int = 50,
    client: Optional[DrupalClient] = None,
) -> Any:
    """DLT data-source for ingesting Drupal CMS content into Cognee."""
    auth = (username, password) if username and password else None
    drupal_client = client or DrupalClient(base_url=base_url, auth=auth)

    @dlt.resource(
        name="drupal_content",
        write_disposition="replace",
        primary_key="id",
    )
    def drupal_content() -> Generator[Dict[str, Any], None, None]:
        target_bundles = bundles or ["article", "page"]

        for b_name in target_bundles:
            nodes = drupal_client.get_nodes(bundle=b_name, limit=limit)
            for node in nodes:
                doc = _node_to_document(node, bundle=b_name, base_url=base_url)
                if doc:
                    yield doc

    resource = drupal_content()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "drupal")
    return resource
