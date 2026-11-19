"""DLT source for Better Stack / Better Uptime (monitors, incidents, timelines).

Syncs uptime monitors, outage incident reports, and chronological event
timelines from Better Stack (https://betterstack.com/uptime) into Cognee's
AI-agent memory graph.

Each incident becomes a prose document that the standard cognify pipeline
processes into an entity/relationship graph, so agents can answer questions
like "Which endpoints suffered downtime last week?" or "How long did the
API gateway 502 incident last?"

Design
------
* **Auth** — Bearer token via the Better Stack Uptime API v2.  Pass
  ``api_token`` or set ``BETTER_STACK_API_TOKEN``.  A missing token raises
  a clear ``ValueError`` at call time.

* **Snapshot with forget-on-delete** — ``write_disposition="replace"`` gives
  cognee a complete view of every incident on each run.  Purged or false-alarm
  incidents simply drop out of the listing and cognee's ``orphan_cleanup``
  removes their nodes and vectors automatically.

* **Pagination** — Better Stack uses page-number pagination (``page=N``).
  Each page returns up to 50 items; we request pages until an empty response
  signals the end.

* **Retry / rate-limit** — HTTP 429 and 5xx responses are retried with
  exponential backoff up to ``_MAX_RETRIES`` times, honouring the
  ``Retry-After`` header when present.

* **Document path** — ``cognee_document_source = "better_stack"`` tags the
  source so ``resolve_dlt_sources`` sends each incident through the prose
  document → cognify path.

Limitations
-----------
* Monitor heartbeat configurations are not ingested (only incident events).
* SSL check status is not separately ingested.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Generator, List, Optional

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("better_stack_connector")
except ImportError:
    import logging
    logger = logging.getLogger("better_stack_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

BETTER_STACK_TABLE_NAME = "better_stack_incidents"
BETTER_STACK_SOURCE_NAME = "better_stack"
BETTER_STACK_API_BASE = "https://uptime.betterstack.com/api/v2"
BETTER_STACK_PAGE_SIZE = 50

_MAX_RETRIES = 5
_EXTRA_HINT = (
    "The Better Stack connector requires dlt: "
    "pip install 'dlt[sqlalchemy]>=1.9.0,<2'"
)

# Map API status values to human-readable labels
_STATUS_LABELS: Dict[str, str] = {
    "up": "Up (operational)",
    "down": "Down (outage)",
    "paused": "Paused",
    "pending": "Pending first check",
    "maintenance": "Under Maintenance",
}


def better_stack_source(
    api_token: Optional[str] = None,
    ingest_timeline: bool = True,
    client: Any = None,
):
    """Create a dlt source yielding Better Stack incidents as markdown documents.

    Args:
        api_token: Better Stack API Bearer token.
            Falls back to the ``BETTER_STACK_API_TOKEN`` environment variable.
        ingest_timeline: When ``True``, chronological timeline events are
            fetched for each incident and included in the document.
        client: Pre-built :class:`BetterStackClient` (test-injection point).
            When ``None`` one is built from *api_token*.

    Returns:
        A dlt source with ``cognee_document_source = "better_stack"`` suitable
        for ``cognee.add(...)``.

    Raises:
        ValueError: If neither *api_token* nor ``BETTER_STACK_API_TOKEN`` is
            set and *client* is ``None``.
        ImportError: If ``dlt`` is not installed.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_token = api_token or os.environ.get("BETTER_STACK_API_TOKEN")
    if client is None and not resolved_token:
        raise ValueError(
            "Better Stack API token required: pass api_token= or set BETTER_STACK_API_TOKEN."
        )

    requester: BetterStackClient = client or BetterStackClient(api_token=resolved_token)

    @dlt.resource(name=BETTER_STACK_TABLE_NAME, primary_key="id", write_disposition="replace")
    def better_stack_incidents() -> Generator[Dict[str, Any], None, None]:
        count = 0
        for incident in requester.iter_incidents():
            count += 1
            yield _incident_to_row(
                requester, incident, ingest_timeline=ingest_timeline
            )
        logger.info("Better Stack: synced %d incident(s).", count)

    @dlt.source(name=BETTER_STACK_SOURCE_NAME)
    def _better_stack_source():
        return better_stack_incidents

    source = _better_stack_source()
    setattr(source, DOCUMENT_SOURCE_ATTR, BETTER_STACK_SOURCE_NAME)
    return source


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------


class BetterStackClient:
    """REST API client for Better Stack Uptime API v2.

    Uses only the Python standard library (``urllib``) so the connector
    adds no new dependencies beyond ``dlt``.
    """

    def __init__(self, api_token: str, base_url: str = BETTER_STACK_API_BASE) -> None:
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def iter_incidents(self) -> Generator[Dict[str, Any], None, None]:
        """Yield every incident, handling page-number pagination."""
        page = 1
        while True:
            data = self._request("/incidents", params={"page": str(page), "per_page": str(BETTER_STACK_PAGE_SIZE)})
            items: List[Dict[str, Any]] = data.get("data", [])
            if not items:
                break
            yield from items
            # Better Stack returns a ``pagination`` object with ``next`` URL
            # when more pages exist.
            pagination = data.get("pagination", {})
            if not pagination.get("next"):
                break
            page += 1

    def get_incident_timeline(self, incident_id: str) -> List[Dict[str, Any]]:
        """Return timeline events for *incident_id*, sorted oldest-first."""
        try:
            data = self._request(f"/incidents/{incident_id}/timeline")
            events: List[Dict[str, Any]] = data.get("data", [])
            return sorted(
                events,
                key=lambda e: (e.get("attributes") or {}).get("occurred_at", ""),
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                logger.warning(
                    "Better Stack: timeline for incident %s not accessible (%d).",
                    incident_id, exc.code,
                )
                return []
            raise

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, endpoint: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "User-Agent": "cognee-community-connector-better-stack/0.1.0",
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
                    "Better Stack: HTTP %d — retrying in %.1fs (%d/%d).",
                    exc.code, delay, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(delay)
            except urllib.error.URLError:
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(float(2 ** attempt))
        return {}

    @staticmethod
    def _retry_delay(headers: Any, attempt: int) -> float:
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
    item: Dict[str, Any],
    *,
    ingest_timeline: bool = True,
) -> Dict[str, Any]:
    """Flatten a Better Stack incident item into a Cognee document row."""
    incident_id: str = item.get("id", "")
    attributes: Dict[str, Any] = item.get("attributes") or {}
    name: str = attributes.get("name") or f"Incident {incident_id}"
    url: str = (
        attributes.get("url")
        or f"https://uptime.betterstack.com/incidents/{incident_id}"
    )

    timeline: List[Dict[str, Any]] = (
        client.get_incident_timeline(incident_id)
        if ingest_timeline and hasattr(client, "get_incident_timeline")
        else []
    )

    content = _render_markdown(attributes, timeline)

    return {
        "id": incident_id,
        "url": url,
        "title": f"Better Stack Outage: {name}",
        "content": content,
    }


def _render_markdown(
    attributes: Dict[str, Any],
    timeline: List[Dict[str, Any]],
) -> str:
    """Render a Better Stack outage record to a searchable Markdown document."""
    name = attributes.get("name") or "Outage"
    cause = (attributes.get("cause") or "Root cause not specified.").strip()
    status = attributes.get("status") or "unknown"
    status_label = _STATUS_LABELS.get(status.lower(), status)
    started_at = attributes.get("started_at") or "unknown"
    resolved_at = attributes.get("resolved_at") or "Ongoing"
    response_content = (attributes.get("response_content") or "").strip()

    # Affected monitor / URL from the incident attributes
    affected_url = attributes.get("affected_url") or ""

    lines = [
        f"# Better Stack Incident: {name}",
        "",
        f"- **Status:** {status_label}",
        f"- **Started At:** {started_at}",
        f"- **Resolved At:** {resolved_at}",
    ]
    if affected_url:
        lines.append(f"- **Affected URL:** {affected_url}")

    lines += [
        "",
        "## Root Cause",
        cause,
        "",
    ]

    if response_content:
        lines += [
            "## Response Content",
            f"```\n{response_content}\n```",
            "",
        ]

    if timeline:
        lines.append("## Chronological Timeline")
        for event in timeline:
            ev_attrs: Dict[str, Any] = event.get("attributes") or {}
            occurred_at = ev_attrs.get("occurred_at") or ""
            action = (ev_attrs.get("action") or "").title()
            comment = (ev_attrs.get("comment") or "").strip()
            detail = f" — {comment}" if comment else ""
            lines.append(f"- **[{occurred_at}]** {action}{detail}")
        lines.append("")

    return "\n".join(lines)
