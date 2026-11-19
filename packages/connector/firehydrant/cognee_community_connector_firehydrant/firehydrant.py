"""FireHydrant Incident Management Data-Source Connector for Cognee.

Ingests incidents, timelines, retrospective analysis, and runbooks from FireHydrant REST API v1
into Cognee knowledge graph and vector memory.
Resolves issue: cognee#6107.
"""

import os
import time
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Any, Optional, Iterator, Union, Set
from datetime import datetime, timezone

# Optional dlt import for Cognee ecosystem compatibility
try:
    import dlt
    _has_dlt = True
except ImportError:
    dlt = None
    _has_dlt = False

def _optional_source(func):
    if _has_dlt and hasattr(dlt, "source"):
        return dlt.source(func)
    return func

def _optional_resource(*args, **kwargs):
    def decorator(func):
        if _has_dlt and hasattr(dlt, "resource"):
            return dlt.resource(*args, **kwargs)(func)
        return func
    return decorator


class FireHydrantClient:
    """Client for FireHydrant REST API v1."""

    def __init__(
        self,
        api_token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 30,
        user_agent: Optional[str] = None,
    ):
        self.api_token = api_token or os.getenv("FIREHYDRANT_API_KEY") or os.getenv("FIREHYDRANT_API_TOKEN")
        self.base_url = (base_url or os.getenv("FIREHYDRANT_BASE_URL") or "https://api.firehydrant.io/v1").rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent or "FireHydrantCogneeConnector/1.0 (https://github.com/topoteretes/cognee-community)"

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _http_get(self, path: str, params: Optional[Dict[str, Any]] = None, max_retries: int = 3) -> Dict[str, Any]:
        """Performs an authorized GET request with rate-limit retry support (300 req/min budget)."""
        clean_path = path.lstrip("/")
        url = f"{self.base_url}/{clean_path}"
        if params:
            query_string = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query_string:
                url = f"{url}?{query_string}"

        req = urllib.request.Request(url, headers=self._get_headers(), method="GET")

        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = resp.read()
                    if not data:
                        return {}
                    return json.loads(data.decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    if attempt < max_retries - 1:
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                    raise RuntimeError("FireHydrant rate limit exceeded (HTTP 429)") from e
                elif e.code == 401:
                    raise PermissionError(f"FireHydrant authentication failed (HTTP 401): {e.reason}") from e
                elif e.code == 404:
                    raise ValueError(f"FireHydrant resource not found (HTTP 404) at {url}") from e
                elif e.code >= 500 and attempt < max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                else:
                    raise RuntimeError(f"HTTP error {e.code}: {e.reason}") from e
            except urllib.error.URLError as e:
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise ConnectionError(f"Failed to connect to FireHydrant at {url}: {e.reason}") from e

        return {}

    def get_incidents(
        self,
        updated_after: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
        archived: bool = False
    ) -> List[Dict[str, Any]]:
        """Fetch incidents from FireHydrant."""
        params: Dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "archived": str(archived).lower()
        }
        if updated_after:
            params["updated_after"] = updated_after

        res = self._http_get("incidents", params=params)
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("data", res.get("incidents", []))
        return []

    def get_incident_notes(self, incident_id: str) -> List[Dict[str, Any]]:
        """Fetch timeline notes recorded for a specific incident."""
        res = self._http_get(f"incidents/{incident_id}/notes")
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("data", res.get("notes", []))
        return []

    def get_runbooks(self) -> List[Dict[str, Any]]:
        """Fetch incident response runbooks."""
        res = self._http_get("runbooks")
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("data", res.get("runbooks", []))
        return []

    def fetch_incidents_bundle(
        self,
        updated_after: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Fetches incidents and enriches them with timeline notes."""
        raw_incidents = self.get_incidents(updated_after=updated_after, per_page=min(limit, 100))
        enriched: List[Dict[str, Any]] = []

        for inc in raw_incidents:
            inc_id = inc.get("id")
            notes: List[Dict[str, Any]] = []
            if inc_id:
                try:
                    notes = self.get_incident_notes(str(inc_id))
                except Exception:
                    notes = []
            inc_copy = dict(inc)
            inc_copy["notes"] = notes
            enriched.append(inc_copy)

        return enriched


def extract_incident_node_sets(incident: Dict[str, Any]) -> Dict[str, List[str]]:
    """Builds node sets for Cognee spatial knowledge graph traversal."""
    inc_id = str(incident.get("id", "unknown"))
    name = incident.get("name", "unnamed_incident")
    severity = incident.get("severity") or (incident.get("severity_condition") or {}).get("name") or "UNKNOWN"
    priority = incident.get("priority") or "P3"
    milestone = incident.get("current_milestone", "open")

    services: Set[str] = set()
    for svc in incident.get("services", []):
        if isinstance(svc, dict) and "name" in svc:
            services.add(svc["name"])
        elif isinstance(svc, str):
            services.add(svc)

    environments: Set[str] = set()
    for env in incident.get("environments", []):
        if isinstance(env, dict) and "name" in env:
            environments.add(env["name"])
        elif isinstance(env, str):
            environments.add(env)

    return {
        "incident": [inc_id, name],
        "severity": [str(severity)],
        "priority": [str(priority)],
        "milestone": [str(milestone)],
        "service": sorted(list(services)),
        "environment": sorted(list(environments)),
    }


def format_incident_to_markdown(incident: Dict[str, Any]) -> str:
    """Formats a FireHydrant incident, its impacted scope, and timeline notes into Markdown."""
    inc_id = incident.get("id", "N/A")
    name = incident.get("name", "Untitled Incident")
    summary = incident.get("summary") or "No summary provided."
    description = incident.get("description") or "No detailed description provided."
    severity = incident.get("severity") or (incident.get("severity_condition") or {}).get("name") or "UNKNOWN"
    priority = incident.get("priority") or "P3"
    milestone = incident.get("current_milestone", "open")
    created = incident.get("created_at", "N/A")
    updated = incident.get("updated_at", "N/A")

    services = [s.get("name") if isinstance(s, dict) else s for s in incident.get("services", [])]
    environments = [e.get("name") if isinstance(e, dict) else e for e in incident.get("environments", [])]

    lines = [
        f"# FireHydrant Incident: {name} (`{inc_id}`)",
        f"- **Severity**: `{severity}` | **Priority**: `{priority}`",
        f"- **Current Milestone**: `{milestone}`",
        f"- **Impacted Services**: {', '.join(services) if services else 'None specified'}",
        f"- **Environments**: {', '.join(environments) if environments else 'None specified'}",
        f"- **Created At**: {created} | **Updated At**: {updated}",
        "",
        "## Incident Summary",
        summary.strip(),
        "",
        "## Incident Description",
        description.strip(),
        "",
    ]

    notes = incident.get("notes", [])
    if notes:
        lines.append("## Timeline Updates & Notes")
        for n in notes:
            n_body = n.get("body", "").strip()
            n_author = (n.get("created_by") or {}).get("name", "Operator")
            n_time = n.get("created_at", "N/A")
            lines.append(f"### Update by {n_author} ({n_time})")
            lines.append(n_body)
            lines.append("")

    # Retrospective if present
    retro = incident.get("retrospective") or incident.get("post_mortem")
    if retro and isinstance(retro, dict):
        lines.append("## Retrospective & Post-Mortem Report")
        retro_summary = retro.get("summary", "No retrospective summary.")
        lines.append(retro_summary)
        lines.append("")

    return "\n".join(lines)


@_optional_source
def firehydrant_data(
    api_token: Optional[str] = None,
    base_url: Optional[str] = None,
    updated_after: Optional[str] = None,
    limit: int = 50,
    client: Optional[FireHydrantClient] = None,
) -> Iterator[Dict[str, Any]]:
    """Yields structured Cognee memory documents from FireHydrant incidents.

    Compatible with Cognee vector memory and spatial knowledge graph embeddings.
    """
    fh_client = client or FireHydrantClient(
        api_token=api_token,
        base_url=base_url,
    )

    incidents = fh_client.fetch_incidents_bundle(
        updated_after=updated_after,
        limit=limit,
    )

    for inc in incidents:
        inc_id = str(inc.get("id", "unknown"))
        text = format_incident_to_markdown(inc)
        node_sets = extract_incident_node_sets(inc)

        yield {
            "id": f"firehydrant_incident_{inc_id}",
            "text": text,
            "type": "firehydrant_incident",
            "metadata": {
                "incident_id": inc_id,
                "name": inc.get("name"),
                "severity": inc.get("severity"),
                "priority": inc.get("priority"),
                "milestone": inc.get("current_milestone"),
                "created_at": inc.get("created_at"),
                "updated_at": inc.get("updated_at"),
                "node_sets": node_sets,
            }
        }
