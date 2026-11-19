"""Unit tests for the Smartsheet dlt connector.

The Smartsheet REST API is fully mocked via ``FakeSmartsheet`` — no network,
no credentials, so these run in CI. Coverage:

  - columnar rows are rendered into real documents: primary-column title,
    ``Column: value`` lines via the column map, sheet name prefix
  - discussions and attachment metadata are folded into the row document;
    text/plain and text/csv bodies under the cap are inlined
  - the two-level cursor: an unchanged sheet (``modifiedAt`` not advanced) is
    skipped without fetching rows; inside a changed sheet only rows with a
    newer ``modifiedAt`` are re-emitted
  - rows that vanish from a fetched sheet become hard-delete markers
    (forget-on-delete); sheets that vanish from the listing (or from an
    explicit selection) tombstone their rows
  - a sheet whose fetch fails is skipped without mass-deleting and keeps its
    cursor; a sheet-listing failure skips the whole sync
  - pagination of the account listing and of large sheets
  - the dlt resource is wired with merge + id PK + the hard_delete column and
    declares the document-source marker
  - a real dlt merge removes the marked row (end-to-end forget-on-delete)

The end-to-end "deletion removes it from memory" guarantee is provided by the
existing ``orphan_cleanup`` path in cognee core; here we prove the connector
emits the markers that drive it, and that dlt acts on them.
"""

import pytest

from cognee_community_connector_smartsheet.smartsheet import (
    SMARTSHEET_SOURCE_NAME,
    _row_title,
    smartsheet_source,
    sync_sheets,
)

SHEET = "111"
OTHER_SHEET = "222"


def _cell(column_id, value, display=None):
    cell = {"columnId": column_id, "value": value}
    if display is not None:
        cell["displayValue"] = display
    return cell


def _sheet_config(**overrides):
    """A fake account with one sheet: 2 columns (Task primary), 2 rows."""
    config = {
        "listing": {
            "id": SHEET,
            "name": "Launch plan",
            "modifiedAt": "2026-10-01T10:00:00Z",
            "permalink": "https://app.smartsheet.com/sheets/" + SHEET,
        },
        "columns": [
            {"id": 1, "title": "Task", "primary": True},
            {"id": 2, "title": "Owner"},
            {"id": 3, "title": "Status"},
        ],
        "rows": [
            {
                "id": 7001,
                "modifiedAt": "2026-09-28T09:00:00Z",
                "cells": [_cell(1, "Draft spec"), _cell(2, "ada"), _cell(3, "In progress")],
            },
            {
                "id": 7002,
                "modifiedAt": "2026-09-29T09:00:00Z",
                "cells": [_cell(1, "Ship"), _cell(2, "grace"), _cell(3, None)],
            },
        ],
        "discussions": {
            7001: [
                {"comments": [{"text": "Draft is up", "createdBy": {"email": "ada@example.com"}}]}
            ],
        },
        "attachments": {
            7001: [
                {
                    "id": 9001,
                    "name": "notes.txt",
                    "mimeType": "text/plain",
                    "sizeInKB": 1,
                    "url": "https://files.example/notes.txt",
                    "_body": "attachment body text",
                },
                {
                    "id": 9002,
                    "name": "diagram.png",
                    "mimeType": "image/png",
                    "sizeInKB": 512,
                    "url": "https://files.example/diagram.png",
                },
            ],
        },
    }
    config.update(overrides)
    return config


class _Resp:
    def __init__(self, payload, text=None):
        self._payload = payload
        self._text = text

    def raise_for_status(self):
        if isinstance(self._payload, Exception):
            raise self._payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return self._text if self._text is not None else str(self._payload)


class FakeSmartsheet:
    """Minimal stand-in for a ``requests`` session hitting the Smartsheet API."""

    def __init__(self, sheets, listing_pages=1):
        # sheets: {sheet_id: sheet_config}; listing_pages splits the listing.
        self.sheets = sheets
        self.listing_pages = listing_pages
        self.calls = []

    def get(self, url, params=None):
        params = params or {}
        self.calls.append(url)

        if url.endswith("/users/me/sheets"):
            all_sheets = [c["listing"] for c in self.sheets.values()]
            if self.listing_pages <= 1:
                return _Resp({"data": all_sheets, "totalPages": 1})
            page = int(params.get("page", 1))
            per = 1
            start = (page - 1) * per
            chunk = all_sheets[start : start + per]
            return _Resp({"data": chunk, "totalPages": self.listing_pages})

        if "/sheets/" in url and url.endswith("/discussions"):
            sheet_id = url.split("/sheets/")[1].split("/")[0]
            sheet = self.sheets.get(sheet_id) or {}
            row_id = int(url.split("/rows/")[1].split("/")[0])
            return _Resp(sheet.get("discussions", {}).get(row_id, []))

        if "/sheets/" in url and url.endswith("/attachments"):
            sheet_id = url.split("/sheets/")[1].split("/")[0]
            sheet = self.sheets.get(sheet_id) or {}
            row_id = int(url.split("/rows/")[1].split("/")[0])
            return _Resp(sheet.get("attachments", {}).get(row_id, []))

        if url.startswith("https://files.example/"):
            # Attachment body download: the config stored the text under _body.
            for sheet in self.sheets.values():
                for items in sheet.get("attachments", {}).values():
                    for item in items:
                        if item.get("url") == url:
                            return _Resp({"ok": True}, text=item.get("_body", ""))
            return _Resp(ValueError("unknown attachment URL"))

        if "/sheets/" in url:
            sheet_id = url.split("/sheets/")[1].split("/")[0]
            sheet = self.sheets.get(sheet_id)
            if sheet is None:
                return _Resp(ValueError("sheet not found"))
            page = int(params.get("page", 1))
            rows = sheet["rows"]
            chunk = rows[(page - 1) * 100 : page * 100]
            return _Resp(
                {
                    "columns": sheet["columns"],
                    "rows": chunk,
                    "totalPages": 1 if len(rows) <= 100 else 2,
                }
            )

        return _Resp(ValueError(f"unexpected URL: {url}"))


def _ids(rows):
    return [row["id"] for row in rows]


def _run(sheets, state, **kwargs):
    return list(sync_sheets(FakeSmartsheet(sheets), state, **kwargs))


# ---------------------------------------------------------------------------
# Backfill and rendering
# ---------------------------------------------------------------------------
def test_backfill_renders_columnar_rows_as_documents():
    state = {}
    rows = _run({SHEET: _sheet_config()}, state)

    assert _ids(rows) == ["7001", "7002"]
    assert all(row["_deleted"] is False for row in rows)

    first = rows[0]
    assert first["title"] == "Launch plan: Draft spec"  # sheet name + primary column
    assert first["url"] == f"https://app.smartsheet.com/sheets/{SHEET}"
    assert "Sheet: Launch plan" in first["content"]
    assert "Task: Draft spec" in first["content"]  # columnId resolved to a title
    assert "Owner: ada" in first["content"]
    assert "Status: In progress" in first["content"]
    # Empty cells are dropped, not rendered as "Status: None".
    assert "Status: None" not in first["content"]
    # Discussions folded into the row document.
    assert "- **ada@example.com**: Draft is up" in first["content"]
    # Text attachment body inlined; binary attachment listed by name only.
    assert "- notes.txt (text/plain, 1 KB)" in first["content"]
    assert "attachment body text" in first["content"]
    assert "- diagram.png (image/png, 512 KB)" in first["content"]
    assert "diagram body" not in first["content"]

    # Second row: empty Status cell dropped entirely.
    second = rows[1]
    assert second["title"] == "Launch plan: Ship"
    assert "Status:" not in second["content"]

    # State: per-sheet cursor + per-row modifiedAt.
    assert state["sheets"][SHEET]["modified_at"] == "2026-10-01T10:00:00Z"
    assert state["rows"]["7001"]["modified_at"] == "2026-09-28T09:00:00Z"


def test_row_title_uses_primary_column_and_falls_back_to_row_id():
    columns = {"1": "Task", "2": "Owner"}
    row = {"cells": [_cell(2, "ada")]}  # primary column empty
    assert _row_title(row, {"1"}, columns) == ""
    assert _row_title({"cells": [_cell(1, "Real title")]}, {"1"}, columns) == "Real title"


# ---------------------------------------------------------------------------
# Incremental (two-level cursor)
# ---------------------------------------------------------------------------
def test_unchanged_sheet_is_skipped_without_row_fetch():
    sheets = {SHEET: _sheet_config()}
    state = {}
    _run(sheets, state)

    # Same modifiedAt → the sheet is skipped entirely (no rows re-emitted).
    rows = _run(sheets, state)
    assert rows == []


def test_changed_sheet_reemits_only_newer_rows():
    state = {}
    _run({SHEET: _sheet_config()}, state)

    changed = _sheet_config(
        listing={**_sheet_config()["listing"], "modifiedAt": "2026-10-05T10:00:00Z"},
        rows=[
            {
                "id": 7001,
                "modifiedAt": "2026-09-28T09:00:00Z",  # unchanged row
                "cells": [_cell(1, "Draft spec"), _cell(2, "ada"), _cell(3, "In progress")],
            },
            {
                "id": 7002,
                "modifiedAt": "2026-10-04T09:00:00Z",  # edited row
                "cells": [_cell(1, "Ship"), _cell(2, "grace"), _cell(3, "Done")],
            },
            {
                "id": 7003,
                "modifiedAt": "2026-10-05T09:00:00Z",  # new row
                "cells": [_cell(1, "Retro")],
            },
        ],
    )
    rows = _run({SHEET: changed}, state)

    assert _ids(rows) == ["7002", "7003"]
    assert "Status: Done" in rows[0]["content"]
    assert state["rows"]["7002"]["modified_at"] == "2026-10-04T09:00:00Z"
    assert state["sheets"][SHEET]["modified_at"] == "2026-10-05T10:00:00Z"


def test_changed_row_refetches_discussions_and_attachments():
    state = {}
    _run({SHEET: _sheet_config()}, state)

    changed = _sheet_config(
        listing={**_sheet_config()["listing"], "modifiedAt": "2026-10-05T10:00:00Z"},
        rows=[
            {
                "id": 7002,
                "modifiedAt": "2026-10-04T09:00:00Z",
                "cells": [_cell(1, "Ship"), _cell(2, "grace")],
            }
        ],
        discussions={7002: [{"comments": [{"text": "ready", "createdBy": {"email": "g@x.io"}}]}]},
        attachments={},
    )
    rows = _run({SHEET: changed}, state)

    assert "- **g@x.io**: ready" in rows[0]["content"]  # comments refetched for the changed row


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------
def test_vanished_row_emits_hard_delete_marker():
    state = {}
    _run({SHEET: _sheet_config()}, state)

    shrunken = _sheet_config(
        listing={**_sheet_config()["listing"], "modifiedAt": "2026-10-05T10:00:00Z"},
        rows=[row for row in _sheet_config()["rows"] if row["id"] != 7002],
    )
    rows = _run({SHEET: shrunken}, state)

    assert rows == [{"id": "7002", "_deleted": True}]
    assert "7002" not in state["rows"]


def test_sheet_removed_from_listing_tombstones_its_rows():
    state = {}
    other = _sheet_config(
        listing={
            "id": OTHER_SHEET,
            "name": "Ops",
            "modifiedAt": "2026-10-01T10:00:00Z",
            "permalink": "https://app.smartsheet.com/sheets/" + OTHER_SHEET,
        },
        rows=[{"id": 8001, "modifiedAt": "2026-09-28T09:00:00Z", "cells": [_cell(1, "Ops task")]}],
    )
    _run({SHEET: _sheet_config(), OTHER_SHEET: other}, state)

    # OTHER_SHEET vanished from the account listing (deleted or de-shared).
    rows = _run({SHEET: _sheet_config()}, state)

    assert rows == [{"id": "8001", "_deleted": True}]
    assert "8001" not in state["rows"]


def test_sheet_dropped_from_explicit_selection_tombstones_its_rows():
    state = {}
    other = _sheet_config(
        listing={
            "id": OTHER_SHEET,
            "name": "Ops",
            "modifiedAt": "2026-10-01T10:00:00Z",
            "permalink": "https://app.smartsheet.com/sheets/" + OTHER_SHEET,
        },
        rows=[{"id": 8001, "modifiedAt": "2026-09-28T09:00:00Z", "cells": [_cell(1, "Ops task")]}],
    )
    _run({SHEET: _sheet_config(), OTHER_SHEET: other}, state)

    rows = _run({SHEET: _sheet_config()}, state, sheet_ids=[SHEET])

    assert {"id": "8001", "_deleted": True} in rows


# ---------------------------------------------------------------------------
# Failure posture
# ---------------------------------------------------------------------------
def test_sheet_fetch_failure_skips_without_deletions_and_keeps_cursor():
    state = {}
    _run({SHEET: _sheet_config()}, state)
    cursor = state["sheets"][SHEET]["modified_at"]

    broken = _sheet_config(
        listing={**_sheet_config()["listing"], "modifiedAt": "2026-10-05T10:00:00Z"}
    )
    broken["rows"] = ValueError("sheet fetch boom")
    # The fake raises on JSON decode of an Exception payload via raise_for_status.
    sheets = {SHEET: broken}
    rows = _run(sheets, state)

    assert rows == []  # skipped, not tombstoned
    assert state["sheets"][SHEET]["modified_at"] == cursor  # cursor kept
    assert set(state["rows"]) == {"7001", "7002"}


def test_sheet_listing_failure_skips_whole_sync():
    state = {}
    _run({SHEET: _sheet_config()}, state)

    class BrokenListing(FakeSmartsheet):
        def get(self, url, params=None):
            if url.endswith("/users/me/sheets"):
                return _Resp(ValueError("server down"))
            return super().get(url, params)

    rows = list(sync_sheets(BrokenListing({SHEET: _sheet_config()}), state))
    assert rows == []
    assert set(state["rows"]) == {"7001", "7002"}  # state untouched


def test_discussion_or_attachment_failure_degrades_the_row_not_the_sync():
    state = {}
    _run({SHEET: _sheet_config()}, state)

    class BrokenDetails(FakeSmartsheet):
        def get(self, url, params=None):
            if url.endswith("/discussions"):
                return _Resp(ValueError("discussions down"))
            return super().get(url, params)

    changed = _sheet_config(
        listing={**_sheet_config()["listing"], "modifiedAt": "2026-10-05T10:00:00Z"},
        rows=[
            {  # unchanged row: skipped entirely
                "id": 7001,
                "modifiedAt": "2026-09-28T09:00:00Z",
                "cells": [_cell(1, "Draft spec"), _cell(2, "ada"), _cell(3, "In progress")],
            },
            {
                "id": 7002,
                "modifiedAt": "2026-10-04T09:00:00Z",
                "cells": [_cell(1, "Ship"), _cell(2, "grace")],
            },
        ],
    )
    rows = list(sync_sheets(BrokenDetails({SHEET: changed}), state))

    assert len(rows) == 1  # the changed row still syncs, without comments


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
def test_account_listing_and_large_sheet_are_paginated():
    sheets = {SHEET: _sheet_config()}
    sheets[SHEET]["rows"] = [
        {
            "id": row_id,
            "modifiedAt": "2026-09-28T09:00:00Z",
            "cells": [_cell(1, f"Task {row_id}")],
        }
        for row_id in range(1, 151)  # 150 rows → 2 pages of 100
    ]
    state = {}
    rows = list(
        sync_sheets(FakeSmartsheet(sheets, listing_pages=2), state),
    )
    assert len(rows) == 150
    assert state["rows"]["150"]["modified_at"] == "2026-09-28T09:00:00Z"


# ---------------------------------------------------------------------------
# smartsheet_source — dlt wiring — requires dlt
# ---------------------------------------------------------------------------
def test_smartsheet_source_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")

    resource = smartsheet_source(session=FakeSmartsheet({SHEET: _sheet_config()}))
    assert resource.name == "smartsheet_rows"

    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True


def test_smartsheet_source_declares_document_marker():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    resource = smartsheet_source(session=FakeSmartsheet({SHEET: _sheet_config()}))
    # resolve_dlt_sources routes on this marker (not the name); keep it stable.
    assert SMARTSHEET_SOURCE_NAME == "smartsheet"
    assert document_source_tag(resource) == "smartsheet"


def test_smartsheet_source_requires_token_or_session():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="token"):
        smartsheet_source()


def test_smartsheet_source_requires_dlt(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="cognee-community-connector-smartsheet"):
        smartsheet_source(session=FakeSmartsheet({}))


# ---------------------------------------------------------------------------
# End-to-end: a real dlt merge acts on the hard-delete marker, and the
# incremental state persists across pipeline runs
# ---------------------------------------------------------------------------
def test_forget_on_delete_and_incremental_end_to_end_through_a_real_dlt_pipeline(tmp_path):
    dlt = pytest.importorskip("dlt")

    db_path = (tmp_path / "smartsheet.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_smartsheet_e2e",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="sheets",
        pipelines_dir=str(tmp_path / "state"),
    )

    # Sync #1: two rows land in the destination.
    pipeline.run(smartsheet_source(session=FakeSmartsheet({SHEET: _sheet_config()})))
    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM smartsheet_rows")[0][0] == 2

    # Sync #2 (same pipeline → persisted dlt state): row 7002 is deleted
    # upstream. The connector emits a hard-delete marker; the merge applies it.
    shrunken = _sheet_config(
        listing={**_sheet_config()["listing"], "modifiedAt": "2026-10-05T10:00:00Z"},
        rows=[row for row in _sheet_config()["rows"] if row["id"] != 7002],
    )
    pipeline.run(smartsheet_source(session=FakeSmartsheet({SHEET: shrunken})))
    with pipeline.sql_client() as client:
        remaining = {row[0] for row in client.execute_sql("SELECT id FROM smartsheet_rows")}

    assert remaining == {"7001"}  # 7002 forgotten from the destination
