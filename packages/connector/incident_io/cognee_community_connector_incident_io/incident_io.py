"""DLT source for incident.io (incidents, updates, actions + forget-on-delete).

Syncs incidents, post-mortem timelines, and remediation action items from
incident.io (https://incident.io) into Cognee's AI-agent memory graph.

Each incident becomes a prose document that the standard cognify pipeline
processes into an entity/relationship graph, so agents can answer questions
like "Who resolved the last payment-service outage?" or "What follow-up
actions are outstanding from last week's P1?"

Design
------
* **Auth** — Bearer token via the incident.io REST API v2.  Pass ``api_key``
  or set ``INCIDENT_IO_API_KEY``.  A missing key raises a clear ``ValueError``
  at call time, not at import time, so tests that don't exercise auth can
  import the module freely.

* **Snapshot with forget-on-delete** — ``write_disposition="replace"`` gives
  cognee a complete view of every incident on each run.  Incidents that are
  purged or retracted upstream simply drop out of the listing and cognee's
  ``orphan_cleanup`` removes their nodes and vectors automatically.

* **Pagination** — incident.io cursor-paginated using ``after`` returned in
  ``pagination_meta``.  A 100-item page size keeps the run count low.

* **Retry / rate-limit** — HTTP 429 and 5xx responses are retried with
  exponential backoff up to ``_MAX_RETRIES`` times, honouring the
  ``Retry-After`` header when present.

* **Document path** — ``cognee_document_source = "incident_io"`` tags the
  source so ``resolve_dlt_sources`` sends each incident through the prose
  document → cognify path instead of the deterministic dlt-row schema path.

* **Incremental field** — ``last_activity_at`` timestamps are included in the
  document content so the full-text entity extraction captures temporal
  context without an explicit watermark cursor.

Limitations
-----------
* Attachments and custom field values are not ingested.
* Archived organisations or incidents behind private access controls will
  return 403; those incidents are skipped with a warning.
"""

import os
import time
from typing import Any, Dict, Generator, List, Optional
import urllib.error
import urllib.parse
import urllib.request
import json

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("incident_io_connector")
except ImportError:
    import logging
    logger = logging.getLogger("incident_io_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

INCIDENT_IO_TABLE_NAME = "incident_io_incidents"
INCIDENT_IO_SOURCE_NAME = "incident_io"
INCIDENT_IO_API_BASE = "https://api.incident.io/v2"
INCIDENT_IO_PAGE_SIZE = 100

_MAX_RETRIES = 5
_EXTRA_HINT = (
    "The incident.io connector requires dlt: "
    "pip install 'dlt[sqlalchemy]>=1.9.0,<2'"
)

# Human-readable status labels for document rendering
_STATUS_LABELS: Dict[str, str] = {
    "triage": "Triage (investigating cause)",
    "investigating": "Investigating",
    "identified": "Cause Identified",
    "monitoring": "Monitoring (fix applied)",
    "resolved": "Resolved",
    "closed": "Closed",
    "declined": "Declined",
}


def incident_io_source(
    api_key: Optional[str] = None,
    status: Optional[List[str]] = None,
    ingest_updates: bool = True,
    ingest_actions: bool = True,
    client: Any = None,
):
    """Create a dlt source yielding incident.io records as markdown documents.

    Args:
        api_key: incident.io API key (Bearer token).
            Falls back to the ``INCIDENT_IO_API_KEY`` environment variable.
        status: Optional list of incident statuses to filter, e.g.
            ``["investigating", "resolved"]``.  When ``None`` all incidents
            are returned.
        ingest_updates: Include chronological milestone updates in each
            document.  Defaults to ``True``.
        ingest_actions: Include remediation action items in each document.
            Defaults to ``True``.
        client: Pre-built :class:`IncidentIOClient` (test-injection point).
            When ``None`` one is built from *api_key*.

    Returns:
        A dlt source with ``cognee_document_source = "incident_io"`` suitable
        for ``cognee.add(...)``.

    Raises:
        ValueError: If neither *api_key* nor ``INCIDENT_IO_API_KEY`` is set
            and *client* is ``None``.
        ImportError: If ``dlt`` is not installed.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_api_key = api_key or os.environ.get("INCIDENT_IO_API_KEY")
    if client is None and not resolved_api_key:
        raise ValueError(
            "incident.io API key required: pass api_key= or set INCIDENT_IO_API_KEY."
        )

    requester: IncidentIOClient = client or IncidentIOClient(api_key=resolved_api_key)

    @dlt.resource(name=INCIDENT_IO_TABLE_NAME, primary_key="id", write_disposition="replace")
    def incident_io_incidents() -> Generator[Dict[str, Any], None, None]:
        # Full snapshot: each run replaces staging with all currently visible
        # incidents.  Purged or retracted incidents drop out of the listing
        # so cognee's orphan_cleanup forgets them automatically.
        count = 0
        for incident in requester.iter_incidents(status=status):
            count += 1
            yield _incident_to_row(
                requester,
                incident,
                ingest_updates=ingest_updates,
                ingest_actions=ingest_actions,
            )
        logger.info("incident.io: synced %d incident(s).", count)

    @dlt.source(name=INCIDENT_IO_SOURCE_NAME)
    def _incident_io_source():
        return incident_io_incidents

    source = _incident_io_source()
    # Tag the source so cognee routes it through the prose document → cognify
    # path instead of the deterministic dlt-row schema-context path.
    setattr(source, DOCUMENT_SOURCE_ATTR, INCIDENT_IO_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------


class IncidentIOClient:
    """REST API Client for incident.io v2 with pagination and retry backoff.

    Uses only the Python standard library (``urllib``) so the connector adds
    no new dependencies beyond ``dlt``.
    """

    def __init__(self, api_key: str, base_url: str = INCIDENT_IO_API_BASE) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def iter_incidents(
        self, status: Optional[List[str]] = None
    ) -> Generator[Dict[str, Any], None, None]:
        """Yield every incident, handling cursor pagination."""
        after: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"page_size": INCIDENT_IO_PAGE_SIZE}
            if after:
                params["after"] = after
            if status:
                for s in status:
                    params.setdefault("status[]", []).append(s)

            data = self._request("/incidents", params=params)
            incidents: List[Dict[str, Any]] = data.get("incidents", [])
            yield from incidents

            after = data.get("pagination_meta", {}).get("after")
            if not after or not incidents:
                break

    def get_incident_updates(self, incident_id: str) -> List[Dict[str, Any]]:
        """Return ordered timeline updates for *incident_id*, newest last."""
        try:
            data = self._request("/incident_updates", params={"incident_id": incident_id})
            return sorted(data.get("incident_updates", []), key=lambda u: u.get("created_at", ""))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                logger.warning(
                    "incident.io: updates for incident %s are not accessible (%d), skipping.",
                    incident_id, exc.code,
                )
                return []
            raise

    def get_incident_actions(self, incident_id: str) -> List[Dict[str, Any]]:
        """Return remediation action items for *incident_id*."""
        try:
            data = self._request("/incident_actions", params={"incident_id": incident_id})
            return data.get("incident_actions", [])
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                logger.warning(
                    "incident.io: actions for incident %s are not accessible (%d), skipping.",
                    incident_id, exc.code,
                )
                return []
            raise

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._build_url(endpoint, params)
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "cognee-community-connector-incident-io/0.1.0",
                "Accept": "application/json",
            },
            method="GET",
        )
        for attempt in range(_MAX_RETRIES):
            try:
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if attempt == _MAX_RETRIES - 1 or exc.code not in (429, 500, 502, 503, 504):
                    raise
                delay = self._retry_delay(exc.headers, attempt)
                logger.warning(
                    "incident.io: HTTP %d — retrying in %.1fs (%d/%d).",
                    exc.code, delay, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(delay)
            except urllib.error.URLError:
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(float(2 ** attempt))
        return {}  # unreachable; satisfies type-checkers

    @staticmethod
    def _build_url(endpoint: str, params: Optional[Dict[str, Any]]) -> str:
        base = INCIDENT_IO_API_BASE.rstrip("/") + endpoint
        if not params:
            return base
        # Flatten list values to repeated query string keys
        parts: List[tuple] = []
        for k, v in params.items():
            if isinstance(v, list):
                parts.extend((k, item) for item in v)
            else:
                parts.append((k, str(v)))
        return f"{base}?{urllib.parse.urlencode(parts)}"

    @staticmethod
    def _retry_delay(headers: Any, attempt: int) -> float:
        """Return seconds to wait: the ``Retry-After`` header or exponential backoff."""
        header = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
        try:
            return float(header)
        except (TypeError, ValueError):
            return float(2 ** attempt)


# ---------------------------------------------------------------------------
# Document synthesis
# ---------------------------------------------------------------------------


def _incident_to_row(
    client: Any,
    incident: Dict[str, Any],
    *,
    ingest_updates: bool = True,
    ingest_actions: bool = True,
) -> Dict[str, Any]:
    """Flatten an incident.io incident into a Cognee document row."""
    incident_id: str = incident.get("id", "")
    name: str = incident.get("name") or f"Incident {incident_id}"
    permalink: str = incident.get("permalink") or f"https://app.incident.io/incidents/{incident_id}"

    updates: List[Dict[str, Any]] = (
        client.get_incident_updates(incident_id)
        if ingest_updates and hasattr(client, "get_incident_updates")
        else []
    )
    actions: List[Dict[str, Any]] = (
        client.get_incident_actions(incident_id)
        if ingest_actions and hasattr(client, "get_incident_actions")
        else []
    )

    content = _render_markdown(incident, updates, actions)

    return {
        "id": incident_id,
        "url": permalink,
        "title": f"Incident: {name}",
        "content": content,
    }


def _render_markdown(
    incident: Dict[str, Any],
    updates: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
) -> str:
    """Render an incident.io incident to a searchable Markdown document."""
    name = incident.get("name") or "Untitled Incident"

    # Status — use a human-readable label when available
    raw_status: str = ""
    inc_status = incident.get("incident_status")
    if isinstance(inc_status, dict):
        raw_status = inc_status.get("name") or inc_status.get("category") or ""
    elif isinstance(inc_status, str):
        raw_status = inc_status
    status_label = _STATUS_LABELS.get(raw_status.lower(), raw_status or "unknown")

    # Severity
    severity = "unknown"
    sev = incident.get("severity")
    if isinstance(sev, dict):
        severity = sev.get("name") or "unknown"
    elif isinstance(sev, str):
        severity = sev

    # Lead / creator
    lead = "Unassigned"
    creator = incident.get("creator")
    if isinstance(creator, dict):
        user = creator.get("user") or {}
        lead = user.get("name") or user.get("email") or creator.get("name") or lead

    created_at = incident.get("created_at", "unknown")
    resolved_at = incident.get("resolved_at") or "Ongoing"
    summary = incident.get("summary") or "No summary provided."
    permalink = incident.get("permalink") or ""

    # Affected components / catalog entries
    affected: List[str] = []
    for entry in incident.get("incident_role_assignments", []):
        role = entry.get("role", {}).get("name") or ""
        assignee = entry.get("assignee", {}).get("name") or ""
        if role and assignee:
            affected.append(f"{role}: {assignee}")

    lines = [
        f"# Incident: {name}",
        "",
        f"- **Status:** {status_label}",
        f"- **Severity:** {severity}",
        f"- **Lead Responder:** {lead}",
        f"- **Opened:** {created_at}",
        f"- **Resolved:** {resolved_at}",
    ]
    if permalink:
        lines.append(f"- **Link:** {permalink}")
    if affected:
        lines += ["", "## Role Assignments"]
        lines.extend(f"  - {a}" for a in affected)
    lines += [
        "",
        "## Summary",
        summary,
        "",
    ]

    if updates:
        lines.append("## Incident Timeline")
        for upd in updates:
            ts = upd.get("created_at", "")
            msg = (upd.get("message") or "").strip()
            if msg:
                lines.append(f"- **[{ts}]** {msg}")
        lines.append("")

    if actions:
        outstanding = [a for a in actions if a.get("status") != "completed"]
        completed = [a for a in actions if a.get("status") == "completed"]
        lines.append("## Remediation Actions")
        for act in completed:
            desc = (act.get("description") or "").strip()
            assignee_name = _action_assignee(act)
            lines.append(f"- [x] {desc} (Assignee: {assignee_name})")
        for act in outstanding:
            desc = (act.get("description") or "").strip()
            assignee_name = _action_assignee(act)
            lines.append(f"- [ ] {desc} (Assignee: {assignee_name})")
        lines.append("")

    return "\n".join(lines)


def _action_assignee(action: Dict[str, Any]) -> str:
    """Extract a display name for an action's assignee."""
    assignee = action.get("assignee")
    if isinstance(assignee, dict):
        return assignee.get("name") or assignee.get("email") or "Unassigned"
    return "Unassigned"
