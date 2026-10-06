import os
import time
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("mixpanel_connector")

MIXPANEL_TABLE_NAME = "mixpanel_knowledge"
MIXPANEL_SOURCE_NAME = "mixpanel"
MIXPANEL_API_BASE_US = "https://mixpanel.com/api"
_MAX_RETRIES = 5

_EXTRA_HINT = 'The Mixpanel connector requires dlt and httpx: pip install "dlt[sqlalchemy]" httpx'


def mixpanel_source(
    project_id: str | int | None = None,
    service_account_username: str | None = None,
    service_account_secret: str | None = None,
    api_secret: str | None = None,
    base_url: str = MIXPANEL_API_BASE_US,
    include_schemas: bool = True,
    include_cohorts: bool = True,
    include_reports: bool = True,
    since: str | None = None,
    client: Any = None,
):
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_project_id = project_id or os.environ.get("MIXPANEL_PROJECT_ID")
    resolved_username = service_account_username or os.environ.get(
        "MIXPANEL_SERVICE_ACCOUNT_USERNAME"
    )
    resolved_secret = (
        service_account_secret
        or os.environ.get("MIXPANEL_SERVICE_ACCOUNT_SECRET")
        or api_secret
        or os.environ.get("MIXPANEL_API_SECRET")
    )

    if client is None and not resolved_secret:
        raise ValueError(
            "Mixpanel credentials required: pass service_account_secret= / api_secret= "
            "or set MIXPANEL_SERVICE_ACCOUNT_SECRET / MIXPANEL_API_SECRET."
        )

    api_client = client or MixpanelClient(
        project_id=str(resolved_project_id) if resolved_project_id else None,
        username=resolved_username,
        secret=resolved_secret,
        base_url=base_url,
    )

    @dlt.resource(name=MIXPANEL_TABLE_NAME, primary_key="id", write_disposition="replace")
    def mixpanel_knowledge():
        count = 0

        if include_schemas:
            for schema in api_client.list_event_schemas():
                updated_at = schema.get("last_modified") or schema.get("updated_at") or ""
                if since and updated_at and updated_at < since:
                    continue
                row = _format_schema_to_row(schema)
                if row:
                    count += 1
                    yield row

        if include_cohorts:
            for cohort in api_client.list_cohorts():
                updated_at = cohort.get("last_modified") or cohort.get("created") or ""
                if since and updated_at and updated_at < since:
                    continue
                row = _format_cohort_to_row(cohort)
                if row:
                    count += 1
                    yield row

        if include_reports:
            for report in api_client.list_reports():
                updated_at = report.get("last_modified") or report.get("updated_at") or ""
                if since and updated_at and updated_at < since:
                    continue
                row = _format_report_to_row(report)
                if row:
                    count += 1
                    yield row

        logger.info("Mixpanel: synced %d knowledge item(s).", count)

    @dlt.source(name=MIXPANEL_SOURCE_NAME)
    def _mixpanel():
        return mixpanel_knowledge

    source = _mixpanel()
    setattr(source, DOCUMENT_SOURCE_ATTR, MIXPANEL_SOURCE_NAME)
    return source


class MixpanelClient:
    def __init__(
        self,
        project_id: str | None = None,
        username: str | None = None,
        secret: str | None = None,
        base_url: str = MIXPANEL_API_BASE_US,
    ):
        self.project_id = project_id
        self.base_url = base_url.rstrip("/")
        self.auth = (username, secret) if username and secret else (secret, "") if secret else None

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]] | dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        query_params = dict(params or {})
        if self.project_id and "project_id" not in query_params:
            query_params["project_id"] = self.project_id

        for attempt in range(_MAX_RETRIES):
            try:
                with httpx.Client(timeout=30.0) as http:
                    response = http.request(
                        method,
                        url,
                        auth=self.auth,
                        params=query_params,
                        headers={
                            "Accept": "application/json",
                            "User-Agent": "cognee-community-connector-mixpanel",
                        },
                    )
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

    def list_event_schemas(self) -> list[dict[str, Any]]:
        res = self._request("GET", "/app/lexicon/schemas")
        if isinstance(res, dict):
            return res.get("results", []) or res.get("events", [])
        return res if isinstance(res, list) else []

    def list_cohorts(self) -> list[dict[str, Any]]:
        res = self._request("GET", "/2.0/cohorts/list")
        if isinstance(res, dict):
            return res.get("results", []) or res.get("cohorts", [])
        return res if isinstance(res, list) else []

    def list_reports(self) -> list[dict[str, Any]]:
        res = self._request("GET", "/2.0/bookmarks/list")
        if isinstance(res, dict):
            return res.get("results", []) or res.get("bookmarks", [])
        return res if isinstance(res, list) else []


def _format_schema_to_row(schema: dict[str, Any]) -> dict[str, Any] | None:
    event_name = schema.get("name") or schema.get("event")
    if not event_name:
        return None

    clean_name = str(event_name).strip().replace(" ", "_").lower()
    description = schema.get("description", "No description provided.")
    tags = ", ".join(schema.get("tags", [])) or "None"
    status = schema.get("status", "active")
    updated_at = schema.get("last_modified") or schema.get("updated_at") or ""

    props_lines = []
    for prop in schema.get("properties", []):
        p_name = prop.get("name", "unnamed")
        p_type = prop.get("type", "string")
        p_desc = prop.get("description", "")
        desc_str = f" - {p_desc}" if p_desc else ""
        props_lines.append(f"  - `{p_name}` ({p_type}){desc_str}")

    props_block = "\n".join(props_lines) if props_lines else "  - No custom properties defined."

    text = f"""# Mixpanel Lexicon Event: {event_name}
- **Event Name:** {event_name}
- **Status:** {status}
- **Tags:** {tags}
- **Last Modified:** {updated_at}

### Description
{description}

### Event Properties & Data Dictionary
{props_block}""".strip()

    return {
        "id": f"mixpanel_schema_{clean_name}",
        "title": f"Mixpanel Event Schema: {event_name}",
        "text": text,
        "url": f"https://mixpanel.com/report/events/{clean_name}",
        "resource_type": "event_schema",
        "last_updated": updated_at,
    }


def _format_cohort_to_row(cohort: dict[str, Any]) -> dict[str, Any] | None:
    cohort_id = cohort.get("id")
    if not cohort_id:
        return None

    name = cohort.get("name", f"Cohort {cohort_id}")
    description = cohort.get("description", "No description provided.")
    count = cohort.get("count", 0)
    is_visible = cohort.get("is_visible", True)
    updated_at = cohort.get("last_modified") or cohort.get("created") or ""

    text = f"""# Mixpanel User Cohort: {name}
- **Cohort ID:** {cohort_id}
- **Member Count:** {count}
- **Visible in UI:** {is_visible}
- **Last Modified:** {updated_at}

### Cohort Definition & Target Criteria
{description}""".strip()

    clean_id = str(cohort_id).removeprefix("mixpanel_cohort_").removeprefix("cohort_")

    return {
        "id": f"mixpanel_cohort_{clean_id}",
        "title": f"Mixpanel Cohort: {name}",
        "text": text,
        "url": f"https://mixpanel.com/report/cohorts/{cohort_id}",
        "resource_type": "cohort",
        "last_updated": updated_at,
    }


def _format_report_to_row(report: dict[str, Any]) -> dict[str, Any] | None:
    report_id = report.get("id") or report.get("bookmark_id")
    if not report_id:
        return None

    name = report.get("name") or report.get("title", f"Report {report_id}")
    description = report.get("description", "Saved Mixpanel analysis bookmark.")
    report_type = report.get("type") or report.get("report_type", "insight")
    creator = report.get("creator_name") or report.get("creator_email", "unknown")
    updated_at = report.get("last_modified") or report.get("updated_at") or ""

    text = f"""# Mixpanel Saved Report: {name}
- **Report ID:** {report_id}
- **Report Type:** {report_type}
- **Created By:** {creator}
- **Last Modified:** {updated_at}

### Analysis Goal & Query Description
{description}""".strip()

    clean_id = (
        str(report_id).removeprefix("mixpanel_report_").removeprefix("report_").removeprefix("rep_")
    )

    return {
        "id": f"mixpanel_report_{clean_id}",
        "title": f"Mixpanel Report: {name}",
        "text": text,
        "url": f"https://mixpanel.com/report/{report_id}",
        "resource_type": "report",
        "last_updated": updated_at,
    }
