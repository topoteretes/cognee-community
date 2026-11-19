"""Airtable HTTP fixtures shared by connector and real-storage tests.

Only the external HTTP interface is replaced. Pagination and failures are exposed
through requests.Response, so the connector executes its actual HTTP handling.
"""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

import requests


def response(payload: Any, status: int = 200, *, headers: dict | None = None):
    result = requests.Response()
    result.status_code = status
    result.url = "https://api.airtable.com/test"
    result._content = json.dumps(payload).encode()
    result.headers.update(headers or {})
    return result


def table(table_id: str = "tblOne", name: str = "Customers") -> dict:
    return {
        "id": table_id,
        "name": name,
        "primaryFieldId": "fldText",
        "description": "Customers and their supported facts.",
        "fields": [
            {"id": "fldText", "name": "Notes", "type": "multilineText"},
            {
                "id": "fldModified",
                "name": "Last modified time",
                "type": "lastModifiedTime",
                "options": {
                    "isValid": True,
                    "referencedFieldIds": None,
                    "result": {
                        "type": "dateTime",
                        "options": {
                            "dateFormat": {"name": "iso", "format": "YYYY-MM-DD"},
                            "timeFormat": {"name": "24hour", "format": "HH:mm"},
                            "timeZone": "utc",
                        },
                    },
                },
            },
        ],
        "views": [{"id": "viwAll", "name": "Grid view", "type": "grid"}],
    }


def record(
    record_id: str = "recOne",
    text: str = "Cedar ships violet teapots.",
    timestamp: str | None = "2026-10-01T10:00:00.000Z",
    *,
    fields: dict | None = None,
) -> dict:
    values = {"fldText": text}
    if timestamp is not None:
        values["fldModified"] = timestamp
    values.update(fields or {})
    return {
        "id": record_id,
        "createdTime": "2026-09-01T10:00:00.000Z",
        "fields": values,
    }


def comment(comment_id: str = "comOne", text: str = "Delivery uses amber crates.") -> dict:
    return {
        "id": comment_id,
        "text": text,
        "createdTime": "2026-10-01T10:01:00.000Z",
        "lastUpdatedTime": None,
        "author": {"id": "usrOne", "name": "Reader", "email": "reader@example.test"},
    }


class FakeAirtableSession:
    """Mutable Airtable inventory with actual paginated HTTP response objects."""

    def __init__(
        self,
        *,
        schema: list[dict] | None = None,
        records: dict[str, list[dict]] | None = None,
        comments: dict[tuple[str, str], list[dict]] | None = None,
        page_size: int = 100,
        failures: dict[str, list[Any]] | None = None,
    ):
        self.schema = copy.deepcopy([table()] if schema is None else schema)
        self.records = copy.deepcopy({"tblOne": [record()]} if records is None else records)
        self.comments = copy.deepcopy(comments or {})
        self.page_size = page_size
        self.failures = defaultdict(list, copy.deepcopy(failures or {}))
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.closed = False

    def get(self, url: str, *, params=None, timeout=None, **kwargs):
        path = urlsplit(url).path
        params = params or {}
        self.calls.append(
            {
                "url": url,
                "path": path,
                "params": copy.deepcopy(params),
                "timeout": timeout,
                "headers": dict(self.headers) | kwargs.get("headers", {}),
            }
        )
        if self.failures[path]:
            failure = self.failures[path].pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        parts = path.strip("/").split("/")
        if parts[:3] == ["v0", "meta", "bases"] and parts[-1] == "tables":
            return response({"tables": self.schema})
        if len(parts) == 3 and parts[0] == "v0":
            return self._page("records", self.records.get(parts[2], []), params)
        if len(parts) == 5 and parts[0] == "v0" and parts[-1] == "comments":
            return self._page("comments", self.comments.get((parts[2], parts[3]), []), params)
        raise AssertionError(f"Unexpected HTTP request: {path}")

    def _page(self, key: str, items: list, params: dict):
        start = int(params.get("offset", "0"))
        payload = {key: copy.deepcopy(items[start : start + self.page_size])}
        if start + self.page_size < len(items):
            payload["offset"] = str(start + self.page_size)
        elif key == "comments":
            payload["offset"] = None
        return response(payload)

    def close(self):
        self.closed = True


class ScriptedSession:
    """Finite ordered responses for transport and malformed-response tests."""

    def __init__(self, replies: list[Any]):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs, "headers": dict(self.headers)})
        if not self.replies:
            raise AssertionError("Connector made an unexpected extra HTTP request")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        pass
