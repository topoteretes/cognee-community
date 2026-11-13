"""In-memory stand-in for the Coda API, shared by the test modules.

Like the live API, a table listing carries only references: ``updatedAt`` and
``displayColumn`` come from the table's own endpoint.
"""

import itertools
import re

import pytest

from cognee_community_connector_coda.coda import (
    CodaAPIError,
    CodaAuthError,
    CodaRateLimitedError,
)

DOC = "AbCDeFGH"


class FakeCoda:
    """Implements ``get`` and ``export_markdown`` like Coda, with ``nextPageToken`` paging."""

    def __init__(self, page_size=25):
        self.page_size = page_size
        self.docs: dict[str, dict] = {}
        self.pages: dict[str, list[dict]] = {}
        self.tables: dict[str, list[dict]] = {}
        self.columns: dict[str, list[dict]] = {}
        self.rows: dict[str, list[dict]] = {}
        self.markdown: dict[str, str] = {}
        self.exports: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.bad_token = False
        self.errors: dict[str, int] = {}
        self.export_errors: dict[str, int] = {}
        self.rate_limited: set[str] = set()
        self.drop_items = False
        self._clock = itertools.count(1)
        self.add_doc(DOC, "Launch plan")

    def _stamp(self) -> str:
        return f"2026-10-10T10:00:{next(self._clock):02d}.000Z"

    def add_doc(self, doc_id, name, *, workspace="ws-1", folder="fl-1"):
        self.docs[doc_id] = {
            "id": doc_id,
            "name": name,
            "workspaceId": workspace,
            "folderId": folder,
            "browserLink": f"https://coda.io/d/_d{doc_id}",
        }
        self.pages.setdefault(doc_id, [])
        self.tables.setdefault(doc_id, [])

    def add_page(self, page_id, name, markdown, doc_id=DOC, **fields):
        page = {
            "id": page_id,
            "name": name,
            "contentType": "canvas",
            "isEffectivelyHidden": False,
            "browserLink": f"https://coda.io/d/_d{doc_id}/_su{page_id}",
            "updatedAt": self._stamp(),
            **fields,
        }
        self.pages[doc_id].append(page)
        self.markdown[page_id] = markdown
        return page

    def add_table(self, table_id, name, columns, rows, doc_id=DOC, table_type="table"):
        table = {
            "id": table_id,
            "name": name,
            "tableType": table_type,
            "displayColumn": {"id": columns[0]["id"]},
            "browserLink": f"https://coda.io/d/_d{doc_id}#{table_id}",
            "updatedAt": self._stamp(),
        }
        self.tables[doc_id].append(table)
        self.columns[table_id] = columns
        self.rows[table_id] = rows
        return table

    def touch(self, item):
        item["updatedAt"] = self._stamp()

    def _list(self, items, params):
        start = int(params.get("pageToken") or 0)
        chunk = items[start : start + self.page_size]
        more = start + self.page_size < len(items)
        if self.drop_items:
            return {"nextPageToken": None}
        return {"items": chunk, "nextPageToken": str(start + self.page_size) if more else None}

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if self.bad_token:
            raise CodaAuthError("Coda rejected the API token (HTTP 401)")
        if any(key in path for key in self.rate_limited):
            raise CodaRateLimitedError("Coda rate limit exceeded (HTTP 429)")
        for key, status in self.errors.items():
            if key in path:
                raise CodaAPIError(f"Coda request failed: HTTP {status}", status=status)
        if path == "/docs":
            docs = [
                d
                for d in self.docs.values()
                if params.get("workspaceId", d["workspaceId"]) == d["workspaceId"]
                and params.get("folderId", d["folderId"]) == d["folderId"]
            ]
            return self._list(docs, params)
        pattern = r"/docs/([\w-]+)(?:/(pages|tables)(?:/([\w-]+)(?:/(columns|rows))?)?)?"
        match = re.fullmatch(pattern, path)
        if not match or match.group(1) not in self.docs:
            raise CodaAPIError("Coda request failed: HTTP 404", status=404)
        doc_id, kind, table_id, part = match.groups()
        if kind is None:
            return self.docs[doc_id]
        if kind == "pages":
            return self._list(self.pages[doc_id], params)
        if table_id is None:
            types = params.get("tableTypes", "table,view,database").split(",")
            tables = [t for t in self.tables[doc_id] if t["tableType"] in types]
            return self._list([self._reference(t) for t in tables], params)
        if part is None:
            table = next((t for t in self.tables[doc_id] if t["id"] == table_id), None)
            if table is None:
                raise CodaAPIError("Coda request failed: HTTP 404", status=404)
            return dict(table)
        if part == "columns":
            return self._list(self.columns[table_id], params)
        return self._list(self.rows[table_id], params)

    @staticmethod
    def _reference(table):
        """The fields the live API puts in a table listing."""
        keys = ("id", "type", "tableType", "href", "browserLink", "name", "parent")
        return {key: table[key] for key in keys if key in table}

    def export_markdown(self, doc_id, page_id):
        if self.bad_token:
            raise CodaAuthError("Coda rejected the API token (HTTP 401)")
        if page_id in self.export_errors:
            status = self.export_errors[page_id]
            raise CodaAPIError(f"Coda request failed: HTTP {status}", status=status)
        if page_id not in self.markdown:
            raise CodaAPIError("Coda request failed: HTTP 404", status=404)
        self.exports.append(page_id)
        return self.markdown[page_id]


def columns(*names):
    return [{"id": f"c-{name.lower()}", "name": name} for name in names]


def row(row_id, index, **values):
    return {
        "id": row_id,
        "index": index,
        "name": next(iter(values.values()), ""),
        "values": {f"c-{key.lower()}": value for key, value in values.items()},
    }


@pytest.fixture
def coda():
    return FakeCoda()
