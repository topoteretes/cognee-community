import os
import time
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("pendo_connector")

PENDO_TABLE_NAME = "pendo_content"
PENDO_SOURCE_NAME = "pendo"
PENDO_API_BASE_US = "https://app.pendo.io/api/v1"
_MAX_RETRIES = 5

_EXTRA_HINT = 'The Pendo connector requires dlt and httpx: pip install "dlt[sqlalchemy]" httpx'


def pendo_source(
    integration_key: str | None = None,
    base_url: str = PENDO_API_BASE_US,
    include_guides: bool = True,
    include_feedback: bool = True,
    include_nps: bool = True,
    since: str | None = None,
    client: Any = None,
):
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_key = (
        integration_key
        or os.environ.get("PENDO_INTEGRATION_KEY")
        or os.environ.get("PENDO_API_KEY")
    )
    if client is None and not resolved_key:
        raise ValueError(
            "Pendo integration key required: pass integration_key= or set PENDO_INTEGRATION_KEY."
        )

    api_client = client or PendoClient(integration_key=resolved_key, base_url=base_url)

    @dlt.resource(name=PENDO_TABLE_NAME, primary_key="id", write_disposition="replace")
    def pendo_content():
        count = 0

        if include_guides:
            for guide in api_client.list_guides():
                updated_at = guide.get("lastUpdatedAt") or guide.get("createdAt") or ""
                if since and updated_at and updated_at < since:
                    continue
                row = _format_guide_to_row(guide)
                if row:
                    count += 1
                    yield row

        if include_feedback:
            for feedback in api_client.list_feedback():
                updated_at = feedback.get("lastUpdatedAt") or feedback.get("createdAt") or ""
                if since and updated_at and updated_at < since:
                    continue
                row = _format_feedback_to_row(feedback)
                if row:
                    count += 1
                    yield row

        if include_nps:
            for nps in api_client.list_nps_responses():
                submitted_at = nps.get("createdAt") or nps.get("submittedAt") or ""
                if since and submitted_at and submitted_at < since:
                    continue
                row = _format_nps_to_row(nps)
                if row:
                    count += 1
                    yield row

        logger.info("Pendo: synced %d knowledge item(s).", count)

    @dlt.source(name=PENDO_SOURCE_NAME)
    def _pendo():
        return pendo_content

    source = _pendo()
    setattr(source, DOCUMENT_SOURCE_ATTR, PENDO_SOURCE_NAME)
    return source


class PendoClient:
    def __init__(self, integration_key: str, base_url: str = PENDO_API_BASE_US):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "x-pendo-integration-key": integration_key,
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-pendo",
        }

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(_MAX_RETRIES):
            try:
                with httpx.Client(timeout=30.0) as http:
                    response = http.request(method, url, headers=self.headers, params=params)
                    if response.status_code == 429:
                        retry_after = float(response.headers.get("Retry-After", 2**attempt))
                        time.sleep(retry_after)
                        continue
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403, 404) or attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
        return []

    def list_guides(self) -> list[dict[str, Any]]:
        res = self._request("GET", "/guide")
        return res if isinstance(res, list) else res.get("results", [])

    def list_feedback(self) -> list[dict[str, Any]]:
        res = self._request("GET", "/feedback")
        return res if isinstance(res, list) else res.get("results", [])

    def list_nps_responses(self) -> list[dict[str, Any]]:
        res = self._request("GET", "/nps")
        return res if isinstance(res, list) else res.get("results", [])


def _format_guide_to_row(guide: dict[str, Any]) -> dict[str, Any] | None:
    guide_id = guide.get("id")
    if not guide_id:
        return None

    name = guide.get("name", f"Guide {guide_id}")
    state = guide.get("state", "unknown")
    segment = guide.get("segment", {}).get("name", "All Users")
    updated_at = guide.get("lastUpdatedAt") or guide.get("createdAt") or ""

    steps_text = []
    for idx, step in enumerate(guide.get("steps", []), 1):
        step_title = step.get("name") or f"Step {idx}"
        step_content = step.get("content") or step.get("text") or ""
        if step_content:
            steps_text.append(f"#### {step_title}\n{step_content}")

    steps_block = "\n\n".join(steps_text) if steps_text else "No specific step content provided."

    text = f"""# Pendo Product Guide: {name}
- **Guide ID:** {guide_id}
- **Status:** {state}
- **Target Audience Segment:** {segment}
- **Last Updated:** {updated_at}

### Guide Steps and Walkthrough Content
{steps_block}""".strip()

    return {
        "id": f"pendo_guide_{guide_id}",
        "title": f"Pendo Guide: {name}",
        "text": text,
        "url": f"https://app.pendo.io/guides/{guide_id}",
        "resource_type": "guide",
        "last_updated": updated_at,
    }


def _format_feedback_to_row(feedback: dict[str, Any]) -> dict[str, Any] | None:
    feedback_id = feedback.get("id")
    if not feedback_id:
        return None

    title = feedback.get("title", f"Feedback {feedback_id}")
    description = feedback.get("description") or feedback.get("content") or ""
    status = feedback.get("status", "open")
    priority = feedback.get("priority", "none")
    visitor_id = feedback.get("visitorId") or feedback.get("user", {}).get("id") or "anonymous"
    account_id = feedback.get("accountId") or "unknown"
    updated_at = feedback.get("lastUpdatedAt") or feedback.get("createdAt") or ""

    text = f"""# Pendo Customer Feedback: {title}
- **Feedback ID:** {feedback_id}
- **Status:** {status}
- **Priority:** {priority}
- **Visitor ID:** {visitor_id}
- **Account ID:** {account_id}
- **Last Updated:** {updated_at}

### Feedback Details and Customer Comments
{description}""".strip()

    return {
        "id": f"pendo_feedback_{feedback_id}",
        "title": f"Pendo Feedback: {title}",
        "text": text,
        "url": f"https://app.pendo.io/feedback/{feedback_id}",
        "resource_type": "feedback",
        "last_updated": updated_at,
    }


def _format_nps_to_row(nps: dict[str, Any]) -> dict[str, Any] | None:
    nps_id = nps.get("id") or nps.get("responseId")
    comment = nps.get("comment") or nps.get("feedback") or ""
    if not nps_id or not comment.strip():
        return None

    score = nps.get("score", "N/A")
    visitor_id = nps.get("visitorId", "anonymous")
    account_id = nps.get("accountId", "unknown")
    submitted_at = nps.get("createdAt") or nps.get("submittedAt") or ""

    sentiment = (
        "Promoter (9-10)"
        if isinstance(score, int) and score >= 9
        else (
            "Passive (7-8)"
            if isinstance(score, int) and score >= 7
            else ("Detractor (0-6)" if isinstance(score, int) else "Standard")
        )
    )

    text = f"""# Pendo NPS Survey Response ({nps_id})
- **Score:** {score} / 10 ({sentiment})
- **Visitor ID:** {visitor_id}
- **Account ID:** {account_id}
- **Submitted At:** {submitted_at}

### Qualitative Customer Comment
"{comment.strip()}" """.strip()

    clean_id = str(nps_id).removeprefix("pendo_nps_").removeprefix("nps_")

    return {
        "id": f"pendo_nps_{clean_id}",
        "title": f"Pendo NPS Response - {visitor_id} (Score: {score})",
        "text": text,
        "url": "https://app.pendo.io/nps",
        "resource_type": "nps",
        "last_updated": submitted_at,
    }
