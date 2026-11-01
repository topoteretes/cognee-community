"""An in-memory Rollbar API served through ``httpx.MockTransport``, shaped like the live answers."""

import itertools
import json
from typing import Any
from urllib.parse import unquote

import httpx

PROJECT = 810999
TOKEN = "test-read-token"
# fields that live in occurrence payloads and must never reach a document
SECRETS = ("do-not-ingest-local", "session=SECRET", "ada@example.com", "card-4242")


def http_error(status: int, message: str = "") -> httpx.Response:
    return httpx.Response(status, json={"err": 1, "message": message})


def python_trace(name: str = "ZeroDivisionError", message: str = "division by zero") -> dict:
    return {
        "trace": {
            "exception": {"class": name, "message": message},
            "frames": [
                {
                    "filename": "app/views.py",
                    "lineno": 12,
                    "method": "dispatch",
                    "code": "return handler(request)",
                    "locals": {"token": "do-not-ingest-local"},
                    "args": ["card-4242"],
                },
                {
                    "filename": "app/reports/margin.py",
                    "lineno": 23,
                    "method": "margin",
                    "code": "return profit / revenue",
                    "locals": {"revenue": 0},
                },
            ],
        }
    }


class FakeRollbar:
    """One Rollbar project. Items list newest first, 100 to a page, like the live API."""

    def __init__(self, project_id: int = PROJECT, page_size: int = 100):
        self.project_id = project_id
        self.page_size = page_size
        self.items: dict[str, dict] = {}
        self.occurrences: dict[int, dict] = {}
        self.requests: list[httpx.Request] = []
        # path prefix -> answers (httpx.Response or an exception) served before the real ones
        self.queued: dict[str, list[Any]] = {}
        # path prefix -> answer served on every request
        self.failing: dict[str, Any] = {}
        # called with the page number before /items answers, to change items mid-listing
        self.before_page = None
        self.clock = itertools.count(1792002420, 60)
        self._item_ids = itertools.count(1809783100)
        self._occurrence_ids = itertools.count(470219970000)

    # ----- seeding ------------------------------------------------------
    def add_item(
        self,
        title: str,
        *,
        environment: str = "production",
        level: str = "error",
        body: dict | None = None,
        code_version: str | None = "2.4.0",
        **fields: Any,
    ) -> dict:
        item_id = str(next(self._item_ids))
        now = next(self.clock)
        item = {
            "id": item_id,
            "project_id": self.project_id,
            "counter": len(self.items) + 1,
            "project_slug": "FirstProject",
            "title": title,
            "environment": environment,
            "level": level,
            "status": "active",
            "framework": "python",
            "platform": "unknown",
            "hash": f"hash-{item_id}",
            "first_occurrence_timestamp": now,
            "last_activated_timestamp": now,
            "last_resolved_timestamp": None,
            "resolved_in_version": None,
            "assigned_user_id": None,
            "assigned_team_id": None,
            "total_occurrences": 0,
            "occurrences_count": 0,
            "unique_occurrences": None,
            "unique_person_count": 1,
            "counts": [0] * 24,
            "group_status": 1,
            "group_item_id": None,
            "last_modified_by": 236365,
            "activating_occurrence_id": None,
            "first_occurrence_id": None,
            "last_occurrence_id": None,
            "last_occurrence_timestamp": None,
        }
        item.update(fields)
        self.items[item_id] = item
        self.occur(item, body=body, code_version=code_version)
        return item

    def occur(self, item: dict, *, body: dict | None = None, code_version: str | None = None):
        """Record one more occurrence of an item, reactivating it when it was resolved."""
        occurrence_id = next(self._occurrence_ids)
        now = next(self.clock)
        data = {
            "body": body if body is not None else python_trace(),
            "environment": item["environment"],
            "level": item["level"],
            "language": "python",
            "timestamp": now,
            "uuid": f"uuid-{occurrence_id}",
            "request": {
                "url": "https://shop.example.com/",
                "headers": {"Cookie": "session=SECRET"},
            },
            "person": {"id": "42", "email": "ada@example.com"},
            "custom": {"card": "card-4242"},
            "metadata": {"customer_timestamp": now},
            "retentionDays": 30,
        }
        if code_version is not None:
            data["code_version"] = code_version
        self.occurrences[occurrence_id] = {
            "id": occurrence_id,
            "project_id": self.project_id,
            "item_id": int(item["id"]),
            "timestamp": now,
            "version": 2,
            "billable": 1,
            "data": data,
        }
        if item["status"] == "resolved" or item["activating_occurrence_id"] is None:
            item.update(
                status="active",
                activating_occurrence_id=occurrence_id,
                last_activated_timestamp=now,
                resolved_in_version=None,
                total_occurrences=0,
            )
        item["first_occurrence_id"] = item["first_occurrence_id"] or occurrence_id
        item.update(last_occurrence_id=occurrence_id, last_occurrence_timestamp=now)
        item["total_occurrences"] += 1
        item["occurrences_count"] += 1
        item["counts"] = [*item["counts"][1:], item["counts"][-1] + 1]
        return occurrence_id

    def resolve(self, item: dict, version: str | None = None) -> None:
        item.update(
            status="resolved", last_resolved_timestamp=next(self.clock), resolved_in_version=version
        )

    def delete_item_occurrences(self, item: dict) -> None:
        """Delete every occurrence of an item: the live listing then drops the item."""
        for occurrence_id in [
            key for key, value in self.occurrences.items() if value["item_id"] == int(item["id"])
        ]:
            del self.occurrences[occurrence_id]
        del self.items[item["id"]]

    # ----- serving ------------------------------------------------------
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def _scripted(self, path: str) -> Any:
        for prefix, answers in self.queued.items():
            if path.startswith(prefix) and answers:
                return answers.pop(0)
        for prefix, answer in self.failing.items():
            if path.startswith(prefix):
                return answer
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        scripted = self._scripted(path)
        if isinstance(scripted, Exception):
            raise scripted
        if scripted is not None:
            return scripted
        if request.headers.get("X-Rollbar-Access-Token") != TOKEN:
            return http_error(403, "invalid access token")
        if path == "/api/1/items":
            return self._items(request)
        if path.startswith("/api/1/instance/"):
            occurrence = self.occurrences.get(int(unquote(path.rsplit("/", 1)[1])))
            if occurrence is None:
                return http_error(404, "instance not found")
            result = json.loads(json.dumps(occurrence))
            return httpx.Response(200, json={"err": 0, "result": result})
        return http_error(404, "not found")

    def _items(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        page = int(params.get("page", "1"))
        if self.before_page is not None:
            self.before_page(page)
        statuses = params.get_list("status") or ["active", "resolved", "muted"]
        environments = params.get_list("environment")
        levels = params.get_list("level")
        selected = [
            item
            for item in sorted(self.items.values(), key=lambda item: -item["counter"])
            if item["status"] in statuses
            and (not environments or item["environment"] in environments)
            and (not levels or item["level"] in levels)
        ]
        start = (page - 1) * self.page_size
        result = {
            "items": json.loads(json.dumps(selected[start : start + self.page_size])),
            "page": page,
            "total_count": len(selected),
            "precise_count": True,
        }
        return httpx.Response(200, json={"err": 0, "result": result})

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]
