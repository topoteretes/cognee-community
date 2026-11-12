"""An in-memory Smartsheet API for tests.

Mimics the ``requests.Session`` surface this connector calls: ``.get(url,
params=..., timeout=...)``. Two distinct pagination contracts are modeled,
matching what the connector actually uses:

* **Legacy page/pageSize** for List Sheets and the discussions-listing
  endpoint: ``{"data": [...], "totalPages": N}``.
* **Cursor lastKey/maxItems** for the ``/workspaces/{id}/children`` and
  ``/folders/{id}/children`` endpoints: ``{"data": [...], "lastKey": "..."}``
  (omitted once exhausted).

Get Sheet (``GET /sheets/{id}``) returns the sheet object directly (not
wrapped in ``data``), with its own legacy ``page``/``pageSize`` row-paging.
"""

import re
from urllib.parse import urlsplit


class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, content=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.content = content if content is not None else b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"fake HTTP error {self.status_code}")


def _error(error_code, message, ref_id="ref-1"):
    return {"errorCode": error_code, "message": message, "refId": ref_id}


class FakeSmartsheetSession:
    """In-memory stand-in for a ``requests.Session`` hitting the Smartsheet API.

    ``sheets``: {sheet_id: full sheet dict, as Get Sheet would return it
    (id, name, permalink, columns, rows, totalRowCount, ...)}.
    ``folder_children`` / ``workspace_children``: {id: [child entry, ...]},
    each entry shaped like ``{"id": ..., "name": ..., "resourceType": "sheet"|"folder"}``.
    ``sheet_discussions``: {sheet_id: [discussion dict, ...]} for the
    sheet-level discussions-listing endpoint.
    """

    LIST_PAGE_SIZE = 2  # small, to exercise pagination in tests
    ROW_PAGE_SIZE = 2
    CHILDREN_PAGE_SIZE = 2

    def __init__(
        self,
        sheets=None,
        folder_children=None,
        workspace_children=None,
        sheet_discussions=None,
        versions=None,
        forbidden_sheet_ids=None,
        attachment_content=None,
    ):
        self.sheets = sheets or {}
        self.folder_children = folder_children or {}
        self.workspace_children = workspace_children or {}
        self.sheet_discussions = sheet_discussions or {}
        # {sheet_id: version}; a sheet present in `sheets` with no entry here
        # defaults to version 1.
        self.versions = versions or {}
        # Sheet ids that exist (so List Sheets/children could still name
        # them) but every direct fetch 403s -- "token lost access", distinct
        # from "doesn't exist" (404).
        self.forbidden_sheet_ids = set(forbidden_sheet_ids or ())
        # {attachment_id: bytes} content served from the fake's pre-signed
        # download URL (never from the authenticated API host).
        self.attachment_content = attachment_content or {}
        self.calls: list[tuple[str, dict, dict]] = []
        self.download_calls: list[tuple[str, dict]] = []
        self.script: list[tuple[int, dict | None]] = []

    def get(self, url, params=None, timeout=None, headers=None):
        query = dict(params or {})
        if self.script:
            status, payload = self.script.pop(0)
            self.calls.append((url, query, dict(headers or {})))
            return _FakeResponse(status, payload)

        path = urlsplit(url).path
        if not path.startswith("/2.0/"):
            # A download URL from an attachment's `url` field, not a
            # Smartsheet API path -- served separately so tests can assert
            # no Authorization header ever reaches it.
            self.download_calls.append((url, dict(headers or {})))
            return self._download(url)

        self.calls.append((url, query, dict(headers or {})))

        m = re.fullmatch(r"/2\.0/sheets/(\d+)/version", path)
        if m:
            return self._get_sheet_version(int(m.group(1)))

        m = re.fullmatch(r"/2\.0/sheets/(\d+)/attachments/(\d+)", path)
        if m:
            return self._get_attachment(int(m.group(1)), int(m.group(2)))

        m = re.fullmatch(r"/2\.0/sheets/(\d+)", path)
        if m:
            return self._get_sheet(int(m.group(1)), query)

        if path == "/2.0/sheets":
            items = [{"id": sid, "name": s.get("name", str(sid))} for sid, s in self.sheets.items()]
            return self._page_offset(items, query, self.LIST_PAGE_SIZE)

        m = re.fullmatch(r"/2\.0/sheets/(\d+)/discussions", path)
        if m:
            items = self.sheet_discussions.get(int(m.group(1)), [])
            return self._page_offset(items, query, self.LIST_PAGE_SIZE)

        m = re.fullmatch(r"/2\.0/folders/(\d+)/children", path)
        if m:
            items = self.folder_children.get(int(m.group(1)))
            if items is None:
                return _FakeResponse(404, _error(1006, "Not Found"))
            return self._page_cursor(items, query)

        m = re.fullmatch(r"/2\.0/workspaces/(\d+)/children", path)
        if m:
            items = self.workspace_children.get(int(m.group(1)))
            if items is None:
                return _FakeResponse(404, _error(1006, "Not Found"))
            return self._page_cursor(items, query)

        return _FakeResponse(404, _error(1006, "Not Found"))

    def _get_sheet_version(self, sheet_id):
        if sheet_id in self.forbidden_sheet_ids:
            return _FakeResponse(403, _error(1004, "You are not authorized to perform this action"))
        if sheet_id not in self.sheets:
            return _FakeResponse(404, _error(1006, "Not Found"))
        return _FakeResponse(200, {"version": self.versions.get(sheet_id, 1)})

    def _get_attachment(self, sheet_id, attachment_id):
        if attachment_id not in self.attachment_content:
            return _FakeResponse(404, _error(1006, "Not Found"))
        url = f"https://download.fake.smartsheet.test/attachments/{attachment_id}"
        return _FakeResponse(200, {"id": attachment_id, "url": url, "urlExpiresInMillis": 120000})

    def _download(self, url):
        m = re.fullmatch(r"/attachments/(\d+)", urlsplit(url).path)
        if not m:
            return _FakeResponse(404, _error(1006, "Not Found"))
        content = self.attachment_content.get(int(m.group(1)))
        if content is None:
            return _FakeResponse(404, _error(1006, "Not Found"))
        return _FakeResponse(200, content=content)

    def _get_sheet(self, sheet_id, query):
        if sheet_id in self.forbidden_sheet_ids:
            return _FakeResponse(403, _error(1004, "You are not authorized to perform this action"))
        sheet = self.sheets.get(sheet_id)
        if sheet is None:
            return _FakeResponse(404, _error(1006, "Not Found"))
        all_rows = sheet.get("rows") or []
        page = int(query.get("page", 1))
        start = (page - 1) * self.ROW_PAGE_SIZE
        page_rows = all_rows[start : start + self.ROW_PAGE_SIZE]
        total_pages = max(1, -(-len(all_rows) // self.ROW_PAGE_SIZE))
        payload = dict(sheet)
        payload["rows"] = page_rows
        payload["totalPages"] = total_pages
        payload["totalRowCount"] = len(all_rows)
        return _FakeResponse(200, payload)

    def _page_offset(self, items, query, page_size):
        page = int(query.get("page", 1))
        start = (page - 1) * page_size
        chunk = items[start : start + page_size]
        total_pages = max(1, -(-len(items) // page_size))
        return _FakeResponse(200, {"data": chunk, "totalPages": total_pages})

    def _page_cursor(self, items, query):
        last_key = query.get("lastKey")
        start = int(last_key) if last_key else 0
        chunk = items[start : start + self.CHILDREN_PAGE_SIZE]
        payload = {"data": chunk}
        next_start = start + len(chunk)
        if next_start < len(items):
            payload["lastKey"] = str(next_start)
        return _FakeResponse(200, payload)


class _ScriptedSession:
    """Returns a scripted sequence of responses to successive GET calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls += 1
        status, payload = self._responses.pop(0)
        return _FakeResponse(status, payload)


class _BoomSession:
    def get(self, url, params=None, timeout=None, headers=None):
        raise RuntimeError("network boom")


def sheet(
    sheet_id,
    name="Sheet",
    permalink=None,
    columns=None,
    rows=None,
    total_row_count=None,
):
    return {
        "id": sheet_id,
        "name": name,
        "permalink": permalink or f"https://app.smartsheet.com/sheets/{sheet_id}",
        "columns": columns or [],
        "rows": rows or [],
        "totalRowCount": total_row_count if total_row_count is not None else len(rows or []),
    }


def column(col_id, title, col_type, *, primary=False, system_type=None, options=None):
    col = {"id": col_id, "title": title, "type": col_type, "primary": primary}
    if system_type:
        col["systemColumnType"] = system_type
    if options:
        col["options"] = options
    return col


def row(
    row_id, row_number, cells, *, parent_id=None, discussions=None, attachments=None, permalink=None
):
    data = {"id": row_id, "rowNumber": row_number, "cells": cells}
    if parent_id is not None:
        data["parentId"] = parent_id
    if discussions is not None:
        data["discussions"] = discussions
    if attachments is not None:
        data["attachments"] = attachments
    if permalink is not None:
        data["permalink"] = permalink
    return data


def cell(
    column_id,
    *,
    value=None,
    display_value=None,
    object_value=None,
    hyperlink=None,
    link_in_from_cell=None,
):
    data = {"columnId": column_id}
    if value is not None:
        data["value"] = value
    if display_value is not None:
        data["displayValue"] = display_value
    if object_value is not None:
        data["objectValue"] = object_value
    if hyperlink is not None:
        data["hyperlink"] = hyperlink
    if link_in_from_cell is not None:
        data["linkInFromCell"] = link_in_from_cell
    return data


def discussion(discussion_id, comments, *, parent_type="ROW", row_id=None):
    data = {"id": discussion_id, "parentType": parent_type, "comments": comments}
    if row_id is not None:
        data["rowId"] = row_id
    return data


def comment(text, author_name, *, author_email=None, created_at="2026-10-01T10:00:00Z"):
    return {
        "text": text,
        "createdBy": {"name": author_name, "email": author_email or f"{author_name}@example.com"},
        "createdAt": created_at,
    }


def attachment(
    name, attachment_type="FILE", *, mime_type=None, size_in_kb=None, attachment_id=None
):
    data = {"name": name, "attachmentType": attachment_type}
    if mime_type is not None:
        data["mimeType"] = mime_type
    if size_in_kb is not None:
        data["sizeInKb"] = size_in_kb
    if attachment_id is not None:
        data["id"] = attachment_id
    return data
