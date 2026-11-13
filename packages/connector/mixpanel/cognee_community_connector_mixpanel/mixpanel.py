"""DLT source for Mixpanel workspace knowledge.

The connector turns durable analytics artifacts into normal cognee documents:
event schemas, saved cohorts, and saved reports.  It also supports a bounded
event export for teams that want recent product activity in the same dataset.
Every run is a full snapshot, so an artifact deleted from Mixpanel naturally
falls out of staging and cognee's orphan cleanup can forget it.
"""

import json
import os
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any, Protocol

from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

MIXPANEL_SOURCE_NAME = "mixpanel"
MIXPANEL_TABLE_NAME = "mixpanel_documents"
_EXTRA_HINT = 'Install the Mixpanel connector extra: pip install "cognee-community-connector-mixpanel".'


class MixpanelClient(Protocol):
    """Small client surface; keeping it injectable makes the source testable."""

    def event_schemas(self) -> Iterable[dict[str, Any]]: ...

    def cohorts(self) -> Iterable[dict[str, Any]]: ...

    def saved_reports(self) -> Iterable[dict[str, Any]]: ...

    def events(self, start: date, end: date) -> Iterable[dict[str, Any]]: ...


def mixpanel_source(
    service_account_username: str | None = None,
    service_account_password: str | None = None,
    project_id: str | int | None = None,
    *,
    include_events: bool = False,
    event_start: date | None = None,
    event_end: date | None = None,
    client: MixpanelClient | None = None,
):
    """Return a document-mode dlt source for a Mixpanel project.

    Credentials default to ``MIXPANEL_SERVICE_ACCOUNT_USERNAME``,
    ``MIXPANEL_SERVICE_ACCOUNT_PASSWORD``, and ``MIXPANEL_PROJECT_ID``. Events
    are opt-in and require a bounded date window; all other workspace artifacts
    are snapshotted on every run.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if client is None:
        username = service_account_username or os.getenv("MIXPANEL_SERVICE_ACCOUNT_USERNAME")
        password = service_account_password or os.getenv("MIXPANEL_SERVICE_ACCOUNT_PASSWORD")
        resolved_project_id = project_id or os.getenv("MIXPANEL_PROJECT_ID")
        if not all((username, password, resolved_project_id)):
            raise ValueError(
                "Mixpanel service-account credentials required: pass service_account_username, "
                "service_account_password, and project_id (or set MIXPANEL_* environment variables)."
            )
        client = HttpMixpanelClient(str(username), str(password), str(resolved_project_id))

    if include_events:
        event_end = event_end or date.today()
        event_start = event_start or event_end - timedelta(days=1)
        if event_start > event_end:
            raise ValueError("event_start must be on or before event_end.")

    @dlt.resource(name=MIXPANEL_TABLE_NAME, primary_key="id", write_disposition="replace")
    def mixpanel_documents():
        yield from _artifact_rows(client)
        if include_events:
            assert event_start is not None and event_end is not None
            yield from _event_rows(client.events(event_start, event_end))

    @dlt.source(name=MIXPANEL_SOURCE_NAME)
    def _mixpanel():
        return mixpanel_documents

    source = _mixpanel()
    setattr(source, DOCUMENT_SOURCE_ATTR, MIXPANEL_SOURCE_NAME)
    return source


def _artifact_rows(client: MixpanelClient) -> Iterable[dict[str, str]]:
    for kind, artifacts in (
        ("event schema", client.event_schemas()),
        ("cohort", client.cohorts()),
        ("saved report", client.saved_reports()),
    ):
        for artifact in artifacts:
            yield _artifact_row(kind, artifact)


def _artifact_row(kind: str, artifact: dict[str, Any]) -> dict[str, str]:
    artifact_id = str(artifact.get("id") or artifact.get("name") or json.dumps(artifact, sort_keys=True))
    title = str(artifact.get("name") or artifact.get("title") or artifact_id)
    return {
        "id": f"{kind}:{artifact_id}",
        "title": f"Mixpanel {kind}: {title}",
        "content": json.dumps(artifact, sort_keys=True, default=str, indent=2),
    }


def _event_rows(events: Iterable[dict[str, Any]]) -> Iterable[dict[str, str]]:
    for event in events:
        properties = event.get("properties") or {}
        event_id = str(properties.get("$insert_id") or event.get("id") or json.dumps(event, sort_keys=True))
        name = str(event.get("event") or properties.get("event") or "event")
        yield {
            "id": f"event:{event_id}",
            "title": f"Mixpanel event: {name}",
            "content": json.dumps(event, sort_keys=True, default=str, indent=2),
        }


class HttpMixpanelClient:
    """HTTP implementation using service-account basic authentication.

    The API surface varies by Mixpanel plan and residency. Workspace endpoints
    are therefore configurable through ``MIXPANEL_API_BASE_URL``; the public US
    endpoint is the default. The class intentionally exposes the same small
    protocol used by tests so applications can supply their own client too.
    """

    def __init__(self, username: str, password: str, project_id: str):
        self.username = username
        self.password = password
        self.project_id = project_id
        self.base_url = os.getenv("MIXPANEL_API_BASE_URL", "https://mixpanel.com").rstrip("/")

    def _get(self, path: str, **params: str) -> Any:
        try:
            import requests
        except ImportError as exc:
            raise ImportError('Install requests to use the default HTTP client: pip install requests') from exc
        response = requests.get(
            f"{self.base_url}{path}", params=params, auth=(self.username, self.password), timeout=60
        )
        response.raise_for_status()
        return response.json()

    def event_schemas(self) -> Iterable[dict[str, Any]]:
        payload = self._get(f"/api/app/projects/{self.project_id}/schemas")
        return _items(payload)

    def cohorts(self) -> Iterable[dict[str, Any]]:
        payload = self._get("/api/2.0/cohorts/list", project_id=self.project_id)
        return _items(payload)

    def saved_reports(self) -> Iterable[dict[str, Any]]:
        payload = self._get(f"/api/app/projects/{self.project_id}/reports")
        return _items(payload)

    def events(self, start: date, end: date) -> Iterable[dict[str, Any]]:
        payload = self._get("/api/2.0/export", from_date=start.isoformat(), to_date=end.isoformat())
        return _items(payload)


def _items(payload: Any) -> list[dict[str, Any]]:
    """Normalize common Mixpanel list envelopes without hiding malformed data."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []
