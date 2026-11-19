"""Client (retry/pagination), selection resolution, and rendering."""

import pytest
from fake_smartsheet import (
    FakeSmartsheetSession,
    _BoomSession,
    _ScriptedSession,
    attachment,
    cell,
    column,
    comment,
    discussion,
    row,
    sheet,
)

import cognee_community_connector_smartsheet.smartsheet as module
from cognee_community_connector_smartsheet.smartsheet import (
    _build_column_index,
    _build_hierarchy,
    _cell_is_text_bearing,
    _cell_text,
    _collect_folder_children,
    _collect_workspace_children,
    _Config,
    _fetch_sheet,
    _get_json,
    _is_purely_numeric,
    _iter_rows,
    _paginate_cursor,
    _paginate_offset,
    _render_cell_link,
    _resolve_sheet_ids,
    _row_qualifies,
    _sync_sheet,
)


def _config(**overrides):
    defaults = {
        "base_url": "https://api.smartsheet.com/2.0",
        "region": "com",
        "sheet_ids": None,
        "folder_ids": None,
        "workspace_ids": None,
        "all_sheets": False,
        "include_columns": None,
        "exclude_columns": None,
        "include_comments": True,
        "include_attachments": True,
        "contact_include_email": False,
        "include_all_rows": False,
        "max_rows_per_sheet": 2000,
        "max_sheets": None,
        "timeout": 30.0,
    }
    defaults.update(overrides)
    return _Config(**defaults)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(module, "_sleep", lambda seconds: None)


# ---------------------------------------------------------------------------
# 1. _get_json: retries, errors
# ---------------------------------------------------------------------------
def test_get_json_retries_5xx_then_succeeds():
    session = _ScriptedSession([(503, None), (502, None), (200, {"ok": True})])
    assert _get_json(session, "https://x/y", {}, 30) == {"ok": True}


def test_get_json_5xx_exhausted_raises():
    session = _ScriptedSession([(500, None)] * module._MAX_RETRIES)
    with pytest.raises(RuntimeError, match="failed after"):
        _get_json(session, "https://x/y", {}, 30)


def test_get_json_429_waits_then_succeeds():
    session = _ScriptedSession(
        [(429, {"errorCode": 4003, "message": "Rate limit exceeded"}), (200, {"ok": True})]
    )
    assert _get_json(session, "https://x/y", {}, 30) == {"ok": True}


def test_get_json_429_honors_retry_after_header():
    class _Resp:
        status_code = 429

        def __init__(self):
            self.headers = {"Retry-After": "5"}

        def json(self):
            return {}

    class _OnceThenOk:
        def __init__(self):
            self.calls = 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                return _Resp()
            return type("R", (), {"status_code": 200, "json": lambda self: {"ok": True}})()

    waited = []
    session = _OnceThenOk()
    import cognee_community_connector_smartsheet.smartsheet as mod

    orig_sleep = mod._sleep
    mod._sleep = lambda s: waited.append(s)
    try:
        assert _get_json(session, "https://x/y", {}, 30) == {"ok": True}
    finally:
        mod._sleep = orig_sleep
    assert waited == [5.0]


def test_get_json_429_exhausted_raises():
    session = _ScriptedSession(
        [(429, {"errorCode": 4003, "message": "Rate limit exceeded"})]
        * (module._RATE_LIMIT_MAX_RETRIES + 1)
    )
    with pytest.raises(RuntimeError, match="rate limit exceeded"):
        _get_json(session, "https://x/y", {}, 30)


def test_get_json_401_message_includes_hint_not_token():
    session = _ScriptedSession([(401, {"errorCode": 1002, "message": "Access Token is invalid"})])
    with pytest.raises(RuntimeError, match="Personal Settings"):
        _get_json(session, "https://x/y", {}, 30)


def test_get_json_403_names_url():
    url = "https://api.smartsheet.com/2.0/sheets/999"
    session = _ScriptedSession([(403, {"errorCode": 1004, "message": "Not authorized"})])
    with pytest.raises(RuntimeError, match="lacks access"):
        _get_json(session, url, {}, 30)


def test_get_json_404_includes_error_detail():
    session = _ScriptedSession([(404, {"errorCode": 1006, "message": "Not Found", "refId": "abc"})])
    with pytest.raises(RuntimeError, match="errorCode=1006"):
        _get_json(session, "https://x/y", {}, 30)


def test_get_json_connection_error_hints_region():
    with pytest.raises(RuntimeError, match="region"):
        _get_json(_BoomSession(), "https://x/y", {}, 30)


# ---------------------------------------------------------------------------
# 2. Pagination
# ---------------------------------------------------------------------------
def test_paginate_offset_multi_page_uses_total_pages():
    session = FakeSmartsheetSession(sheets={i: sheet(i, name=f"S{i}") for i in range(1, 6)})
    items = list(_paginate_offset(session, "https://api.smartsheet.com/2.0/sheets", {}, 30))
    assert sorted(i["id"] for i in items) == [1, 2, 3, 4, 5]


def test_paginate_offset_stops_on_empty_page():
    session = FakeSmartsheetSession(sheets={})
    items = list(_paginate_offset(session, "https://api.smartsheet.com/2.0/sheets", {}, 30))
    assert items == []


def test_paginate_offset_falls_back_to_short_page_when_total_pages_missing():
    class _NoTotalPagesSession:
        def __init__(self, items):
            self._items = items
            self.calls = 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            page = int((params or {}).get("page", 1))
            page_size = int((params or {}).get("pageSize", 100))
            start = (page - 1) * page_size
            chunk = self._items[start : start + page_size]
            return type("R", (), {"status_code": 200, "json": lambda self: {"data": chunk}})()

    items = [{"id": i} for i in range(5)]
    session = _NoTotalPagesSession(items)
    result = list(_paginate_offset(session, "https://x/y", {}, 30))
    assert len(result) == 5
    assert session.calls == 1  # a single short page (< pageSize) is the stop signal


def test_paginate_cursor_follows_lastkey_until_absent():
    children = [{"id": i, "name": f"c{i}", "resourceType": "sheet"} for i in range(1, 6)]
    session = FakeSmartsheetSession(folder_children={10: children})
    items = list(
        _paginate_cursor(session, "https://api.smartsheet.com/2.0/folders/10/children", 30)
    )
    assert sorted(i["id"] for i in items) == [1, 2, 3, 4, 5]


def test_paginate_cursor_repeated_key_raises():
    class _LoopingSession:
        def get(self, url, params=None, timeout=None):
            return type(
                "R",
                (),
                {"status_code": 200, "json": lambda self: {"data": [{"id": 1}], "lastKey": "same"}},
            )()

    with pytest.raises(RuntimeError, match="repeated cursor"):
        list(_paginate_cursor(_LoopingSession(), "https://x/y", 30))


def test_paginate_cursor_page_cap_trips_on_runaway_listing():
    class _AlwaysMoreSession:
        def get(self, url, params=None, timeout=None):
            key = (params or {}).get("lastKey", "0")
            return type(
                "R",
                (),
                {
                    "status_code": 200,
                    "json": lambda self, k=key: {"data": [{"id": 1}], "lastKey": str(int(k) + 1)},
                },
            )()

    with pytest.raises(RuntimeError, match="exceeded"):
        list(_paginate_cursor(_AlwaysMoreSession(), "https://x/y", 30))


# ---------------------------------------------------------------------------
# 3. Selection resolution
# ---------------------------------------------------------------------------
def test_resolve_sheet_ids_explicit():
    session = FakeSmartsheetSession()
    ids = _resolve_sheet_ids(session, _config(sheet_ids=(5, 3)))
    assert sorted(ids) == [3, 5]


def test_resolve_sheet_ids_all_sheets():
    session = FakeSmartsheetSession(sheets={1: sheet(1, name="A"), 2: sheet(2, name="B")})
    ids = _resolve_sheet_ids(session, _config(all_sheets=True))
    assert ids == {1: "A", 2: "B"}


def test_resolve_sheet_ids_dedupes_across_sources():
    session = FakeSmartsheetSession(
        folder_children={10: [{"id": 5, "name": "S5", "resourceType": "sheet"}]}
    )
    ids = _resolve_sheet_ids(session, _config(sheet_ids=(5,), folder_ids=(10,)))
    assert ids == {5: "5"}  # explicit sheet_ids' placeholder name wins (first-seen)


def test_collect_folder_children_recurses_into_subfolders():
    session = FakeSmartsheetSession(
        folder_children={
            10: [
                {"id": 1, "name": "S1", "resourceType": "sheet"},
                {"id": 20, "name": "Sub", "resourceType": "folder"},
            ],
            20: [{"id": 2, "name": "S2", "resourceType": "sheet"}],
        }
    )
    found = {}
    _collect_folder_children(
        session, _config(), 10, lambda sid, n: found.setdefault(sid, n), depth=0
    )
    assert found == {1: "S1", 2: "S2"}


def test_collect_folder_children_depth_cap_raises():
    session = FakeSmartsheetSession()
    with pytest.raises(RuntimeError, match="depth cap"):
        _collect_folder_children(
            session, _config(), 1, lambda *a: None, depth=module._MAX_FOLDER_DEPTH + 1
        )


def test_collect_workspace_children_recurses_into_folders():
    session = FakeSmartsheetSession(
        workspace_children={100: [{"id": 10, "name": "F", "resourceType": "folder"}]},
        folder_children={10: [{"id": 1, "name": "S1", "resourceType": "sheet"}]},
    )
    found = {}
    _collect_workspace_children(
        session, _config(), 100, lambda sid, n: found.setdefault(sid, n), depth=0
    )
    assert found == {1: "S1"}


def test_resolve_sheet_ids_missing_folder_raises():
    session = FakeSmartsheetSession(folder_children={})
    with pytest.raises(RuntimeError, match="Not Found"):
        _resolve_sheet_ids(session, _config(folder_ids=(999,)))


# ---------------------------------------------------------------------------
# 4. _is_purely_numeric
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["123", "  123  ", "$1,200.50", "12%", "-5", "", "   "])
def test_is_purely_numeric_true(text):
    assert _is_purely_numeric(text) is True


@pytest.mark.parametrize("text", ["In Progress", "12 apples", "Blocked on review"])
def test_is_purely_numeric_false(text):
    assert _is_purely_numeric(text) is False


# ---------------------------------------------------------------------------
# 5. Cell rendering
# ---------------------------------------------------------------------------
def _col(col_id, col_type, **kw):
    return module._ColumnInfo(
        id=col_id,
        title=kw.get("title", "Col"),
        type=col_type,
        primary=kw.get("primary", False),
        is_system=False,
        included=True,
    )


def test_cell_text_prefers_display_value_over_value():
    c = cell(1, value="raw", display_value="Pretty")
    assert _cell_text(c, _col(1, "TEXT_NUMBER"), _config()) == "Pretty"


def test_cell_text_never_renders_formula():
    c = {"columnId": 1, "formula": "=SUM(A1:A2)", "displayValue": "42"}
    assert _cell_text(c, _col(1, "TEXT_NUMBER"), _config()) == "42"


def test_cell_text_checkbox_yes_no():
    assert _cell_text(cell(1, value=True), _col(1, "CHECKBOX"), _config()) == "Yes"
    assert _cell_text(cell(1, value=False), _col(1, "CHECKBOX"), _config()) == "No"
    assert _cell_text(cell(1), _col(1, "CHECKBOX"), _config()) is None


def test_cell_text_contact_name_only_by_default():
    c = cell(
        1, object_value={"objectType": "CONTACT", "name": "Jane Doe", "email": "jane@example.com"}
    )
    assert (
        _cell_text(c, _col(1, "CONTACT_LIST"), _config(contact_include_email=False)) == "Jane Doe"
    )


def test_cell_text_contact_with_email_when_opted_in():
    c = cell(
        1, object_value={"objectType": "CONTACT", "name": "Jane Doe", "email": "jane@example.com"}
    )
    text = _cell_text(c, _col(1, "CONTACT_LIST"), _config(contact_include_email=True))
    assert text == "Jane Doe <jane@example.com>"


def test_cell_text_multi_contact_joins_names():
    c = cell(
        1,
        object_value={
            "objectType": "MULTI_CONTACT",
            "values": [
                {"objectType": "CONTACT", "name": "A", "email": "a@x.com"},
                {"objectType": "CONTACT", "name": "B", "email": "b@x.com"},
            ],
        },
    )
    assert _cell_text(c, _col(1, "MULTI_CONTACT_LIST"), _config()) == "A, B"


def test_cell_text_multi_picklist_joins_values():
    c = cell(1, object_value={"objectType": "MULTI_PICKLIST", "values": ["Red", "Blue"]})
    assert _cell_text(c, _col(1, "MULTI_PICKLIST"), _config()) == "Red, Blue"


def test_cell_text_empty_cell_is_none():
    assert _cell_text(cell(1), _col(1, "TEXT_NUMBER"), _config()) is None


def test_cell_text_defensive_against_unknown_column_type():
    c = cell(1, display_value="something")
    assert _cell_text(c, _col(1, "SOME_FUTURE_TYPE"), _config()) == "something"


# ---------------------------------------------------------------------------
# 6. Cell link rendering
# ---------------------------------------------------------------------------
def test_render_cell_link_uses_sheet_name_when_available():
    c = cell(1, link_in_from_cell={"sheetId": 99, "sheetName": "Budget Tracker"})
    assert _render_cell_link(c) == "linked from Budget Tracker"


def test_render_cell_link_falls_back_to_sheet_id():
    c = cell(1, link_in_from_cell={"sheetId": 99})
    assert _render_cell_link(c) == "linked from sheet 99"


def test_render_cell_link_external_hyperlink():
    c = cell(1, hyperlink={"url": "https://example.com"})
    assert _render_cell_link(c) == "link: https://example.com"


def test_render_cell_link_none_when_absent():
    assert _render_cell_link(cell(1)) is None


# ---------------------------------------------------------------------------
# 7. Row inclusion rule
# ---------------------------------------------------------------------------
def test_row_qualifies_numeric_only_row_is_skipped():
    columns = {1: _col(1, "TEXT_NUMBER", primary=True)}
    r = row(1, 1, [cell(1, display_value="42")])
    assert _row_qualifies(r, columns, _config()) is False


def test_row_qualifies_numeric_with_comments_is_kept():
    columns = {1: _col(1, "TEXT_NUMBER", primary=True)}
    r = row(1, 1, [cell(1, display_value="42")], discussions=[discussion(1, [comment("hi", "A")])])
    assert _row_qualifies(r, columns, _config(include_comments=True)) is True


def test_row_qualifies_numeric_with_attachment_is_kept():
    columns = {1: _col(1, "TEXT_NUMBER", primary=True)}
    r = row(1, 1, [cell(1, display_value="42")], attachments=[attachment("f.txt")])
    assert _row_qualifies(r, columns, _config(include_attachments=True)) is True


def test_row_qualifies_text_label_with_numbers_is_kept():
    columns = {1: _col(1, "TEXT_NUMBER", primary=True)}
    r = row(1, 1, [cell(1, display_value="Server costs: $500")])
    assert _row_qualifies(r, columns, _config()) is True


def test_row_qualifies_include_all_rows_keeps_everything():
    columns = {1: _col(1, "TEXT_NUMBER", primary=True)}
    r = row(1, 1, [cell(1, display_value="42")])
    assert _row_qualifies(r, columns, _config(include_all_rows=True)) is True


def test_row_qualifies_date_only_row_is_skipped():
    columns = {1: _col(1, "DATE", primary=True)}
    r = row(1, 1, [cell(1, display_value="2026-01-01")])
    assert _row_qualifies(r, columns, _config()) is False


def test_cell_is_text_bearing_picklist_with_value():
    assert _cell_is_text_bearing(cell(1, display_value="In Progress"), _col(1, "PICKLIST")) is True


# ---------------------------------------------------------------------------
# 8. Column index: include/exclude, system columns
# ---------------------------------------------------------------------------
def test_build_column_index_excludes_system_columns():
    columns = [
        column(1, "Created", "DATE", system_type="CREATED_DATE"),
        column(2, "Task", "TEXT_NUMBER"),
    ]
    index = _build_column_index(columns, _config())
    assert index[1].included is False
    assert index[2].included is True


def test_build_column_index_include_list_is_case_insensitive():
    columns = [column(1, "Status", "PICKLIST"), column(2, "Notes", "TEXT_NUMBER")]
    index = _build_column_index(columns, _config(include_columns=("status",)))
    assert index[1].included is True
    assert index[2].included is False


def test_build_column_index_exclude_list():
    columns = [column(1, "Status", "PICKLIST"), column(2, "Notes", "TEXT_NUMBER")]
    index = _build_column_index(columns, _config(exclude_columns=("notes",)))
    assert index[1].included is True
    assert index[2].included is False


# ---------------------------------------------------------------------------
# 9. Hierarchy
# ---------------------------------------------------------------------------
def test_build_hierarchy_parent_and_children_maps():
    rows = [row(1, 1, []), row(2, 2, [], parent_id=1), row(3, 3, [], parent_id=1)]
    parent_map, children_map = _build_hierarchy(rows)
    assert parent_map == {2: 1, 3: 1}
    assert children_map == {1: [2, 3]}


# ---------------------------------------------------------------------------
# 10. _fetch_sheet: row paging + max_rows_per_sheet guard
# ---------------------------------------------------------------------------
def test_fetch_sheet_pages_through_all_rows():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    rows = [row(i, i, [cell(1, display_value=f"Task {i}")]) for i in range(1, 6)]
    session = FakeSmartsheetSession(sheets={1: sheet(1, columns=columns, rows=rows)})
    fetched = _fetch_sheet(session, _config(), 1, ["columnType"])
    assert len(fetched["rows"]) == 5


def test_fetch_sheet_raises_when_row_count_exceeds_cap():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    rows = [row(i, i, [cell(1, display_value=f"Task {i}")]) for i in range(1, 6)]
    session = FakeSmartsheetSession(sheets={1: sheet(1, columns=columns, rows=rows)})
    with pytest.raises(RuntimeError, match="max_rows_per_sheet"):
        _fetch_sheet(session, _config(max_rows_per_sheet=2), 1, ["columnType"])


# ---------------------------------------------------------------------------
# 11. Rendering: golden content for the four example shapes + determinism
# ---------------------------------------------------------------------------
def _tracker_sheet():
    columns = [
        column(1, "Task", "TEXT_NUMBER", primary=True),
        column(2, "Status", "PICKLIST"),
        column(3, "Owner", "CONTACT_LIST"),
        column(4, "Notes", "TEXT_NUMBER"),
    ]
    rows = [
        row(
            1,
            1,
            [
                cell(1, display_value="Draft wireframes for onboarding flow"),
                cell(2, display_value="In Progress"),
                cell(
                    3,
                    object_value={
                        "objectType": "CONTACT",
                        "name": "Jane Doe",
                        "email": "jane@example.com",
                    },
                ),
                cell(4, display_value="Blocked on design review."),
            ],
            discussions=[
                discussion(10, [comment("Pushed due date.", "Jane Doe")], parent_type="ROW")
            ],
        )
    ]
    return sheet(1, name="Product Roadmap Q1", columns=columns, rows=rows)


def test_render_row_text_heavy_task_tracker():
    s = _tracker_sheet()
    session = FakeSmartsheetSession(sheets={1: s})
    stats = {}
    docs = _sync_sheet(session, _config(), 1, stats)
    row_docs = [d for d in docs if ":row:" in d["id"]]
    assert len(row_docs) == 1
    content = row_docs[0]["content"]
    assert "# [Row] Draft wireframes for onboarding flow" in content
    assert "Sheet: Product Roadmap Q1" in content
    assert "Parent: none (top level)" in content
    assert "Status: In Progress" in content
    assert "Owner: Jane Doe" in content
    assert "jane@example.com" not in content  # email off by default
    assert "Comments:" in content
    assert "Jane Doe: Pushed due date." in content


def test_render_row_parent_with_children():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    rows = [
        row(1, 1, [cell(1, display_value="Phase 1 - Discovery")]),
        row(2, 2, [cell(1, display_value="Draft wireframes")], parent_id=1),
        row(3, 3, [cell(1, display_value="Stakeholder signoff")], parent_id=1),
    ]
    s = sheet(1, name="Roadmap", columns=columns, rows=rows)
    session = FakeSmartsheetSession(sheets={1: s})
    docs = _sync_sheet(session, _config(include_comments=False, include_attachments=False), 1, {})
    parent_doc = next(d for d in docs if d["id"].endswith(":row:1"))
    assert "Children: 2 subrow(s): Draft wireframes, Stakeholder signoff" in parent_doc["content"]
    child_doc = next(d for d in docs if d["id"].endswith(":row:2"))
    assert "Parent: Phase 1 - Discovery" in child_doc["content"]


def test_render_row_with_a_text_label_and_a_numeric_cell_is_kept():
    columns = [
        column(1, "Category", "TEXT_NUMBER", primary=True),
        column(2, "Budgeted", "TEXT_NUMBER"),
    ]
    rows = [row(1, 1, [cell(1, display_value="Paid Ads"), cell(2, display_value="$45,000")])]
    s = sheet(1, name="Budget Tracker", columns=columns, rows=rows)
    session = FakeSmartsheetSession(sheets={1: s})
    docs = _sync_sheet(session, _config(include_comments=False, include_attachments=False), 1, {})
    row_docs = [d for d in docs if ":row:" in d["id"]]
    assert len(row_docs) == 1  # "Paid Ads" is real text -> the row qualifies
    assert "Budgeted: $45,000" in row_docs[0]["content"]


def test_render_row_purely_numeric_primary_is_skipped():
    columns = [column(1, "Amount", "TEXT_NUMBER", primary=True)]
    rows = [row(1, 1, [cell(1, display_value="45000")])]
    s = sheet(1, name="Budget", columns=columns, rows=rows)
    session = FakeSmartsheetSession(sheets={1: s})
    docs = _sync_sheet(session, _config(include_comments=False, include_attachments=False), 1, {})
    assert [d for d in docs if ":row:" in d["id"]] == []


def test_render_row_cross_sheet_link_and_hyperlink():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True), column(2, "Ref", "TEXT_NUMBER")]
    rows = [
        row(
            1,
            1,
            [
                cell(1, display_value="Review budget"),
                cell(
                    2,
                    display_value="Linked value",
                    link_in_from_cell={"sheetId": 7, "sheetName": "Budget Tracker 2026"},
                ),
            ],
        )
    ]
    s = sheet(1, name="Tasks", columns=columns, rows=rows)
    session = FakeSmartsheetSession(sheets={1: s})
    docs = _sync_sheet(session, _config(include_comments=False, include_attachments=False), 1, {})
    row_doc = next(d for d in docs if ":row:" in d["id"])
    assert "Ref: Linked value (linked from Budget Tracker 2026)" in row_doc["content"]


def test_render_comments_capped_and_sorted_oldest_first():
    comments = [
        comment(f"c{i}", "A", created_at=f"2026-10-{i:02d}T00:00:00Z") for i in range(1, 25)
    ]
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    rows = [
        row(1, 1, [cell(1, display_value="Important task")], discussions=[discussion(1, comments)])
    ]
    s = sheet(1, name="Tasks", columns=columns, rows=rows)
    session = FakeSmartsheetSession(sheets={1: s})
    docs = _sync_sheet(session, _config(include_attachments=False), 1, {})
    row_doc = next(d for d in docs if ":row:" in d["id"])
    assert row_doc["content"].index("c1") < row_doc["content"].index("c2")
    assert "(+4 more)" in row_doc["content"]


def test_render_attachments_metadata_only_no_url():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    rows = [
        row(
            1,
            1,
            [cell(1, display_value="Upload the report")],
            attachments=[attachment("report.pdf", mime_type="application/pdf", size_in_kb=512)],
        )
    ]
    s = sheet(1, name="Tasks", columns=columns, rows=rows)
    session = FakeSmartsheetSession(sheets={1: s})
    docs = _sync_sheet(session, _config(include_comments=False), 1, {})
    row_doc = next(d for d in docs if ":row:" in d["id"])
    assert "report.pdf" in row_doc["content"]
    assert "FILE" in row_doc["content"]
    assert "application/pdf" in row_doc["content"]
    assert "512 KB" in row_doc["content"]
    assert "http" not in row_doc["content"]


def test_rendering_is_deterministic_regardless_of_input_order():
    columns = [column(2, "B", "TEXT_NUMBER"), column(1, "A", "TEXT_NUMBER", primary=True)]
    rows = [row(1, 1, [cell(2, display_value="bee"), cell(1, display_value="Ay")])]
    s1 = sheet(1, name="S", columns=columns, rows=rows)
    s2 = sheet(
        1,
        name="S",
        columns=list(reversed(columns)),
        rows=[row(1, 1, [cell(1, display_value="Ay"), cell(2, display_value="bee")])],
    )
    docs1 = _sync_sheet(
        FakeSmartsheetSession(sheets={1: s1}),
        _config(include_comments=False, include_attachments=False),
        1,
        {},
    )
    docs2 = _sync_sheet(
        FakeSmartsheetSession(sheets={1: s2}),
        _config(include_comments=False, include_attachments=False),
        1,
        {},
    )
    row1 = next(d for d in docs1 if ":row:" in d["id"])
    row2 = next(d for d in docs2 if ":row:" in d["id"])
    assert row1["content"] == row2["content"]


def test_volatile_fields_never_affect_rendered_content():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    rows = [row(1, 1, [cell(1, display_value="Important task")])]
    s1 = sheet(1, name="S", columns=columns, rows=rows)
    s1["modifiedAt"] = "2026-01-01T00:00:00Z"
    s1["version"] = 1
    s2 = sheet(1, name="S", columns=columns, rows=rows)
    s2["modifiedAt"] = "2026-10-01T12:00:00Z"
    s2["version"] = 99
    docs1 = _sync_sheet(
        FakeSmartsheetSession(sheets={1: s1}),
        _config(include_comments=False, include_attachments=False),
        1,
        {},
    )
    docs2 = _sync_sheet(
        FakeSmartsheetSession(sheets={1: s2}),
        _config(include_comments=False, include_attachments=False),
        1,
        {},
    )
    assert docs1 == docs2


# ---------------------------------------------------------------------------
# 12. Sheet summary
# ---------------------------------------------------------------------------
def test_sheet_summary_content_and_id():
    columns = [
        column(1, "Task", "TEXT_NUMBER", primary=True),
        column(2, "Status", "PICKLIST", options=["Open", "In Progress", "Done"]),
    ]
    rows = [row(1, 1, [cell(1, display_value="Top-level task")])]
    s = sheet(1, name="My Sheet", columns=columns, rows=rows)
    session = FakeSmartsheetSession(sheets={1: s})
    docs = _sync_sheet(session, _config(include_comments=False, include_attachments=False), 1, {})
    summary = next(d for d in docs if d["id"].endswith(":summary"))
    assert summary["id"] == "smartsheet:com:1:summary"
    assert "# Smartsheet sheet: My Sheet" in summary["content"]
    assert "- Task (TEXT_NUMBER)" in summary["content"]
    assert "- Status (PICKLIST)" in summary["content"]
    assert "options: Open, In Progress, Done" in summary["content"]
    assert "Top-level rows:" in summary["content"]
    assert "Top-level task" in summary["content"]


def test_sheet_summary_includes_sheet_level_comments():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    rows = [row(1, 1, [cell(1, display_value="A task")])]
    s = sheet(1, name="S", columns=columns, rows=rows)
    session = FakeSmartsheetSession(
        sheets={1: s},
        sheet_discussions={
            1: [discussion(99, [comment("Great sheet!", "Boss")], parent_type="SHEET")]
        },
    )
    docs = _sync_sheet(session, _config(include_attachments=False), 1, {})
    summary = next(d for d in docs if d["id"].endswith(":summary"))
    assert "Sheet comments:" in summary["content"]
    assert "Boss: Great sheet!" in summary["content"]


def test_sheet_level_discussions_excludes_row_level_ones():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    rows = [row(1, 1, [cell(1, display_value="A task")])]
    s = sheet(1, name="S", columns=columns, rows=rows)
    session = FakeSmartsheetSession(
        sheets={1: s},
        sheet_discussions={
            1: [
                discussion(99, [comment("Sheet-level!", "Boss")], parent_type="SHEET"),
                discussion(
                    100, [comment("Row-level, skip me", "Dev")], parent_type="ROW", row_id=1
                ),
            ]
        },
    )
    docs = _sync_sheet(session, _config(include_attachments=False), 1, {})
    summary = next(d for d in docs if d["id"].endswith(":summary"))
    assert "Sheet-level!" in summary["content"]
    assert "Row-level, skip me" not in summary["content"]


# ---------------------------------------------------------------------------
# 13. _iter_rows orchestration
# ---------------------------------------------------------------------------
def test_iter_rows_yields_summary_and_row_docs_across_sheets():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    s1 = sheet(1, name="S1", columns=columns, rows=[row(1, 1, [cell(1, display_value="Task one")])])
    s2 = sheet(2, name="S2", columns=columns, rows=[row(2, 1, [cell(1, display_value="Task two")])])
    session = FakeSmartsheetSession(sheets={1: s1, 2: s2})
    config = _config(sheet_ids=(1, 2), include_comments=False, include_attachments=False)
    docs = list(_iter_rows(session, config, {}))
    ids = {d["id"] for d in docs}
    assert "smartsheet:com:1:summary" in ids
    assert "smartsheet:com:2:summary" in ids
    assert "smartsheet:com:1:row:1" in ids
    assert "smartsheet:com:2:row:2" in ids


def test_iter_rows_respects_max_sheets():
    session = FakeSmartsheetSession(sheets={1: sheet(1), 2: sheet(2)})
    config = _config(sheet_ids=(1, 2), max_sheets=1)
    with pytest.raises(RuntimeError, match="max_sheets"):
        list(_iter_rows(session, config, {}))


def test_iter_rows_builds_everything_before_yielding_on_failure():
    """A hard failure partway through (second sheet) must mean nothing was yielded at
    all. 404 is special-cased for forget-on-delete (see test_smartsheet_forget.py);
    403 (access forbidden, not "gone") is not, and must still propagate.
    """
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    s1 = sheet(1, name="S1", columns=columns, rows=[row(1, 1, [cell(1, display_value="Task one")])])
    s2 = sheet(2, name="S2", columns=columns, rows=[])
    session = FakeSmartsheetSession(sheets={1: s1, 2: s2}, forbidden_sheet_ids={2})
    config = _config(sheet_ids=(1, 2))
    with pytest.raises(RuntimeError, match="lacks access"):
        list(_iter_rows(session, config, {}))
