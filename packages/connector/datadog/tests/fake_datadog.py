"""In-memory stand-in for the Datadog REST API, mounted as a requests transport.

The client under test sends real HTTP requests through dlt's RESTClient and its
paginators; this adapter answers them with responses shaped like Datadog's.
"""

import json
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import BaseAdapter

from cognee_community_connector_datadog.datadog import DatadogClient

API_KEY = "SECRET-dd-api-key-123"
APP_KEY = "SECRET-dd-app-key-456"


class FakeDatadog(BaseAdapter):
    def __init__(self, site: str = "datadoghq.com"):
        super().__init__()
        self.host = f"api.{site}"
        self.monitors: dict[str, dict] = {}
        self.dashboards: dict[str, dict] = {}
        # trashed dashboards: left out of the list, restorable
        self.trash: dict[str, dict] = {}
        self.notebooks: dict[str, dict] = {}
        self.slos: dict[str, dict] = {}
        self.incidents: dict[str, dict] = {}
        # still in datadog but left out of the list, like an item skipped by a page shift
        self.hidden: set[str] = set()
        # "list:<kind>" or "get:<kind>" -> the status to answer with instead
        self.statuses: dict[str, int] = {}
        # "list:<kind>" or "get:<kind>" -> x-ratelimit-remaining to report
        self.remaining: dict[str, int] = {}
        self.calls: list[str] = []
        self._clock = 0

    # -- seeding -----------------------------------------------------------
    def stamp(self) -> str:
        self._clock += 1
        return f"2026-10-01T10:{self._clock // 60:02d}:{self._clock % 60:02d}.000000+00:00"

    def add_monitor(self, monitor_id: int, name: str, message: str = "", **fields) -> dict:
        self.monitors[str(monitor_id)] = {
            "id": monitor_id,
            "name": name,
            "type": "metric alert",
            "query": "avg(last_5m):avg:system.cpu.user{*} > 90",
            "message": message,
            "tags": ["team:payments"],
            "priority": 2,
            "options": {"thresholds": {"critical": 90.0, "warning": 80.0}},
            "overall_state": "OK",
            "created": "2026-10-01T09:00:00.000000+00:00",
            "modified": self.stamp(),
            **fields,
        }
        return self.monitors[str(monitor_id)]

    def add_dashboard(self, dashboard_id: str, title: str, widgets: list | None = None) -> dict:
        self.dashboards[dashboard_id] = {
            "id": dashboard_id,
            "title": title,
            "description": f"{title} overview",
            "layout_type": "ordered",
            "url": f"/dashboard/{dashboard_id}/{title.lower().replace(' ', '-')}",
            "author_handle": "ada@example.com",
            "created_at": "2026-10-01T09:00:00.000000+00:00",
            "modified_at": self.stamp(),
            "widgets": widgets
            if widgets is not None
            else [{"id": 1, "definition": {"type": "timeseries", "title": "p99 latency"}}],
        }
        return self.dashboards[dashboard_id]

    def add_notebook(self, notebook_id: int, name: str, text: str, kind: str = "postmortem"):
        self.notebooks[str(notebook_id)] = {
            "id": notebook_id,
            "type": "notebooks",
            "attributes": {
                "name": name,
                "status": "published",
                "metadata": {"type": kind, "is_template": False},
                "created": "2026-10-01T09:00:00.000000+00:00",
                "modified": self.stamp(),
                "cells": [
                    {
                        "id": "c1",
                        "type": "notebook_cells",
                        "attributes": {"definition": {"type": "markdown", "text": text}},
                    }
                ],
            },
        }
        return self.notebooks[str(notebook_id)]

    def add_slo(self, slo_id: str, name: str, monitor_ids: list[int] | None = None) -> dict:
        self.slos[slo_id] = {
            "id": slo_id,
            "name": name,
            "description": f"{name} for checkout",
            "type": "monitor" if monitor_ids else "metric",
            "monitor_ids": monitor_ids or [],
            "thresholds": [{"timeframe": "30d", "target": 99.9, "target_display": "99.9"}],
            "tags": ["team:payments"],
            "created_at": 1790000000,
            "modified_at": 1790000000 + self._clock,
        }
        return self.slos[slo_id]

    def add_incident(self, incident_id: str, public_id: int, title: str, **fields) -> dict:
        self.incidents[incident_id] = {
            "id": incident_id,
            "type": "incidents",
            "attributes": {
                "public_id": public_id,
                "title": title,
                "customer_impacted": True,
                "customer_impact_scope": "Checkout failed for EU users",
                "created": "2026-10-02T08:00:00.000000+00:00",
                "declared": "2026-10-02T08:01:00.000000+00:00",
                "modified": self.stamp(),
                "resolved": None,
                "is_test": False,
                "fields": {
                    "severity": {"type": "dropdown", "value": "SEV-2"},
                    "state": {"type": "dropdown", "value": "resolved"},
                    "summary": {"type": "textbox", "value": "Card payments timed out."},
                    "root_cause": {"type": "textbox", "value": "A bad deploy of payments-api."},
                    "services": {"type": "autocomplete", "value": ["payments-api"]},
                    "teams": {"type": "autocomplete", "value": None},
                    **fields,
                },
            },
        }
        return self.incidents[incident_id]

    def trash_dashboard(self, dashboard_id: str) -> None:
        self.trash[dashboard_id] = self.dashboards.pop(dashboard_id)

    def restore_dashboard(self, dashboard_id: str) -> None:
        # restoring does not move modified_at
        self.dashboards[dashboard_id] = self.trash.pop(dashboard_id)

    # -- transport ---------------------------------------------------------
    def close(self) -> None:
        pass

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        self.calls.append(f"{request.method} {url.path}" + (f"?{url.query}" if url.query else ""))
        if url.netloc != self.host:
            return self._answer(request, 403, {"errors": ["Forbidden"]}, None)
        if request.headers.get("DD-API-KEY") != API_KEY:
            return self._answer(request, 403, {"errors": ["Forbidden"]}, None)
        if request.headers.get("DD-APPLICATION-KEY") != APP_KEY:
            return self._answer(request, 403, {"errors": ["Forbidden"]}, None)
        key, payload = self._route(url.path, query)
        status = self.statuses.get(key, 200 if payload is not None else 404)
        body = payload if status == 200 else {"errors": [f"HTTP {status}"]}
        return self._answer(request, status, body, key)

    def _answer(self, request, status: int, body, key: str | None) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        response.headers["Content-Type"] = "application/json"
        if key is not None:
            response.headers["x-ratelimit-remaining"] = str(self.remaining.get(key, 100))
        response.request = request
        response.url = request.url
        response.reason = "OK" if status == 200 else "Error"
        return response

    def _route(self, path: str, query: dict) -> tuple[str, object]:
        parts = path.strip("/").split("/")
        if parts[:3] == ["api", "v1", "monitor"]:
            if len(parts) == 4:
                return "get:monitors", self.monitors.get(parts[3])
            return "list:monitors", self._page_number(self._listed(self.monitors), query)
        if parts[:3] == ["api", "v1", "dashboard"]:
            if len(parts) == 4:
                return "get:dashboards", self.dashboards.get(parts[3])
            summaries = [
                {k: v for k, v in d.items() if k != "widgets"}
                for d in self._listed(self.dashboards)
            ]
            items = self._offset(summaries, query, "start", "count")
            return "list:dashboards", {"dashboards": items}
        if parts[:3] == ["api", "v1", "notebooks"]:
            if len(parts) == 4:
                notebook = self.notebooks.get(parts[3])
                return "get:notebooks", {"data": notebook} if notebook else None
            listed = [
                n
                for n in self._listed(self.notebooks)
                if not n["attributes"]["metadata"]["is_template"]
            ]
            stripped = [
                {**n, "attributes": {k: v for k, v in n["attributes"].items() if k != "cells"}}
                for n in listed
            ]
            items = self._offset(stripped, query, "start", "count")
            meta = {"page": {"total_count": len(listed), "total_filtered_count": len(listed)}}
            return "list:notebooks", {"data": items, "meta": meta}
        if parts[:3] == ["api", "v1", "slo"]:
            if len(parts) == 4:
                slo = self.slos.get(parts[3])
                return "get:slos", {"data": slo} if slo else None
            items = self._offset(self._listed(self.slos), query, "offset", "limit")
            return "list:slos", {"data": items, "errors": None}
        if parts[:3] == ["api", "v2", "incidents"]:
            if len(parts) == 4:
                incident = self.incidents.get(parts[3])
                return "get:incidents", {"data": incident} if incident else None
            items = self._offset(self._listed(self.incidents), query, "page[offset]", "page[size]")
            offset = int(query.get("page[offset]", 0))
            meta = {"pagination": {"offset": offset, "next_offset": offset + len(items)}}
            return "list:incidents", {"data": items, "meta": meta}
        return "unknown", None

    def _listed(self, items: dict[str, dict]) -> list[dict]:
        return [item for key, item in sorted(items.items()) if key not in self.hidden]

    @staticmethod
    def _offset(items: list, query: dict, offset_param: str, limit_param: str) -> list:
        offset = int(query.get(offset_param, 0))
        return items[offset : offset + int(query.get(limit_param, 100))]

    @staticmethod
    def _page_number(items: list, query: dict) -> list:
        if "page" not in query:
            return items
        size = int(query.get("page_size", 100))
        page = int(query["page"])
        return items[page * size : (page + 1) * size]


def client_for(fake: FakeDatadog, site: str = "datadoghq.com") -> DatadogClient:
    session = requests.Session()
    session.mount("https://", fake)
    return DatadogClient(API_KEY, APP_KEY, site, session=session)
