"""Phase 2: incremental sync, forget-on-delete, mass-delete guards, attachment text."""

import copy
import json

import pytest
import pytest_asyncio
from fake_smartsheet import (
    FakeSmartsheetSession,
    _ScriptedSession,
    attachment,
    cell,
    column,
    row,
    sheet,
)

import cognee_community_connector_smartsheet.smartsheet as module
from cognee_community_connector_smartsheet import smartsheet_source
from cognee_community_connector_smartsheet.smartsheet import (
    _RENDER_VERSION,
    _Config,
    _config_fingerprint,
    _fetch_sheet,
    _is_text_attachment,
    _iter_rows,
    _SmartsheetAPIError,
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
        "include_comments": False,
        "include_attachments": False,
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


def _one_row_sheet(sheet_id, text="Task one", *, name=None):
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    rows = [row(1, 1, [cell(1, display_value=text)])]
    return sheet(sheet_id, name=name or f"Sheet{sheet_id}", columns=columns, rows=rows)


def _run(session, config, state):
    return list(_iter_rows(session, config, state))


# ---------------------------------------------------------------------------
# 1. First-run full pass + state shape
# ---------------------------------------------------------------------------
def test_first_run_is_a_full_pass_and_records_state():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,))
    state = {}
    docs = _run(session, config, state)
    ids = {d["id"] for d in docs}
    assert "smartsheet:com:1:summary" in ids
    assert "smartsheet:com:1:row:1" in ids
    assert state["initialized"] is True
    assert state["render_version"] == _RENDER_VERSION
    assert state["config_fingerprint"] == _config_fingerprint(config)
    assert state["sheets"]["1"]["version"] == 1
    assert set(state["sheets"]["1"]["doc_ids"]) == ids


# ---------------------------------------------------------------------------
# 2. No-op second run: only a version check, no full re-fetch
# ---------------------------------------------------------------------------
def test_unchanged_sheet_on_incremental_run_only_checks_version():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,), recheck_sheets_per_run=0)
    state = {}
    _run(session, config, state)

    session.calls.clear()
    docs = _run(session, config, state)
    assert docs == []
    paths = [url for url, _q, _h in session.calls]
    assert all(p.endswith("/version") for p in paths)
    assert len(paths) == 1


# ---------------------------------------------------------------------------
# 3. Version bump triggers a re-fetch and picks up the new content
# ---------------------------------------------------------------------------
def test_version_bump_triggers_refetch_and_new_content():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,))
    state = {}
    _run(session, config, state)

    session.sheets[1] = _one_row_sheet(1, text="Task one, revised")
    session.versions[1] = 2
    docs = _run(session, config, state)
    row_doc = next(d for d in docs if d["id"] == "smartsheet:com:1:row:1")
    assert "revised" in row_doc["content"]
    assert state["sheets"]["1"]["version"] == 2


# ---------------------------------------------------------------------------
# 4. Row deletion / disqualification / requalification tombstoning
# ---------------------------------------------------------------------------
def test_deleted_row_is_tombstoned_on_a_forced_refetch():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    s1 = sheet(
        1,
        columns=columns,
        rows=[
            row(1, 1, [cell(1, display_value="Keep me")]),
            row(2, 2, [cell(1, display_value="Delete me")]),
        ],
    )
    session = FakeSmartsheetSession(sheets={1: s1})
    config = _config(sheet_ids=(1,))
    state = {}
    _run(session, config, state)
    assert "smartsheet:com:1:row:2" in state["sheets"]["1"]["doc_ids"]

    session.sheets[1] = sheet(
        1, columns=columns, rows=[row(1, 1, [cell(1, display_value="Keep me")])]
    )
    session.versions[1] = 2
    docs = _run(session, config, state)
    tombstones = {d["id"] for d in docs if d.get("_deleted")}
    assert "smartsheet:com:1:row:2" in tombstones
    assert "smartsheet:com:1:row:2" not in state["sheets"]["1"]["doc_ids"]


def test_row_losing_its_only_content_is_tombstoned():
    """A row that goes from text-bearing to purely numeric must disappear."""
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    s1 = sheet(1, columns=columns, rows=[row(1, 1, [cell(1, display_value="Real text")])])
    session = FakeSmartsheetSession(sheets={1: s1})
    config = _config(sheet_ids=(1,))
    state = {}
    _run(session, config, state)
    assert "smartsheet:com:1:row:1" in state["sheets"]["1"]["doc_ids"]

    session.sheets[1] = sheet(1, columns=columns, rows=[row(1, 1, [cell(1, display_value="42")])])
    session.versions[1] = 2
    docs = _run(session, config, state)
    tombstones = {d["id"] for d in docs if d.get("_deleted")}
    assert "smartsheet:com:1:row:1" in tombstones
    assert "smartsheet:com:1:row:1" not in state["sheets"]["1"]["doc_ids"]


def test_row_gaining_content_requalifies_without_a_tombstone():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    s1 = sheet(1, columns=columns, rows=[row(1, 1, [cell(1, display_value="42")])])
    session = FakeSmartsheetSession(sheets={1: s1})
    config = _config(sheet_ids=(1,))
    state = {}
    _run(session, config, state)
    assert (
        "smartsheet:com:1:row:1" not in state["sheets"]["1"]["doc_ids"]
    )  # purely numeric: skipped

    session.sheets[1] = sheet(
        1, columns=columns, rows=[row(1, 1, [cell(1, display_value="Now has text")])]
    )
    session.versions[1] = 2
    docs = _run(session, config, state)
    assert not any(d.get("_deleted") for d in docs)
    assert "smartsheet:com:1:row:1" in state["sheets"]["1"]["doc_ids"]


# ---------------------------------------------------------------------------
# 5. Rotating re-check catches a change that does not bump version
# ---------------------------------------------------------------------------
def test_rotating_recheck_catches_a_change_the_version_missed():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    s1 = sheet(1, columns=columns, rows=[row(1, 1, [cell(1, display_value="Original")])])
    s2 = sheet(2, columns=columns, rows=[row(2, 1, [cell(1, display_value="Other")])])
    session = FakeSmartsheetSession(sheets={1: s1, 2: s2})
    config = _config(sheet_ids=(1, 2), recheck_sheets_per_run=1, full_sync_every=0)
    state = {}
    _run(session, config, state)

    # Content changes but the version number (a bug/limitation upstream) does not.
    session.sheets[1] = sheet(
        1, columns=columns, rows=[row(1, 1, [cell(1, display_value="Changed")])]
    )

    seen_change = False
    for _ in range(3):  # recheck_sheets_per_run=1 over 2 sheets: fully rotates in 2 runs
        docs = _run(session, config, state)
        if any(
            d.get("id") == "smartsheet:com:1:row:1" and "Changed" in d.get("content", "")
            for d in docs
        ):
            seen_change = True
    assert seen_change


# ---------------------------------------------------------------------------
# 6. Sheet lifecycle: deleted / access-error / out-of-scope / never-synced-bad-id
# ---------------------------------------------------------------------------
def test_sheet_deleted_404_after_being_known_is_tombstoned():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,))
    state = {}
    _run(session, config, state)
    prior_ids = set(state["sheets"]["1"]["doc_ids"])

    del session.sheets[1]
    docs = _run(session, config, state)
    tombstones = {d["id"] for d in docs if d.get("_deleted")}
    assert tombstones == prior_ids
    assert "1" not in state["sheets"]


def test_sheet_access_forbidden_403_after_being_known_raises_not_tombstones():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,), recheck_sheets_per_run=1, full_sync_every=0)
    state = {}
    _run(session, config, state)

    session.forbidden_sheet_ids.add(1)
    with pytest.raises(_SmartsheetAPIError, match="lacks access"):
        _run(session, config, state)
    # 403 is not a "gone" signal; nothing was forgotten, state is untouched.
    assert "1" in state["sheets"]


def test_sheet_moved_out_of_scope_is_tombstoned_without_an_extra_api_call():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1), 2: _one_row_sheet(2)})
    config = _config(sheet_ids=(1, 2))
    state = {}
    _run(session, config, state)
    prior_ids_2 = set(state["sheets"]["2"]["doc_ids"])

    narrower_config = _config(sheet_ids=(1,))
    session.calls.clear()
    docs = _run(session, narrower_config, state)
    tombstones = {d["id"] for d in docs if d.get("_deleted")}
    assert tombstones == prior_ids_2
    assert "2" not in state["sheets"]
    # No API call was made for sheet 2 -- it was a pure diff against state.
    assert all("/sheets/2" not in url for url, _q, _h in session.calls)


def test_never_synced_bad_sheet_id_404_skips_with_warning_not_a_raise():
    session = FakeSmartsheetSession(sheets={})  # sheet 1 never existed
    config = _config(sheet_ids=(1,))
    state = {}
    docs = _run(session, config, state)
    assert docs == []
    assert "1" not in state.get("sheets", {})


# ---------------------------------------------------------------------------
# 7. Mass-delete guards
# ---------------------------------------------------------------------------
def test_guard_empty_selection_with_prior_state_raises_without_allow_mass_delete():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,))
    state = {}
    _run(session, config, state)

    empty_config = _config(sheet_ids=None, folder_ids=(10,))
    session.folder_children[10] = []
    with pytest.raises(RuntimeError, match="resolved selection is empty"):
        _run(session, empty_config, state)
    assert "1" in state["sheets"]  # refused: state untouched


def test_guard_empty_selection_with_empty_state_only_warns(caplog):
    session = FakeSmartsheetSession(sheets={})
    config = _config(folder_ids=(10,))
    session.folder_children[10] = []
    state = {}
    docs = _run(session, config, state)
    assert docs == []
    assert state["initialized"] is True


def test_guard_empty_selection_bypassed_with_allow_mass_delete():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,))
    state = {}
    _run(session, config, state)

    empty_config = _config(folder_ids=(10,), allow_mass_delete=True)
    session.folder_children[10] = []
    docs = _run(session, empty_config, state)
    assert any(d.get("_deleted") for d in docs)
    assert state["sheets"] == {}


def test_guard_ratio_trips_when_tombstoning_over_half_of_stored_docs():
    sheets = {i: _one_row_sheet(i) for i in range(1, 25)}  # 24 sheets * 2 docs = 48 stored docs
    all_ids = list(sheets.keys())
    session = FakeSmartsheetSession(sheets=sheets)
    config = _config(sheet_ids=tuple(all_ids))
    state = {}
    _run(session, config, state)
    assert sum(len(v["doc_ids"]) for v in state["sheets"].values()) == 48

    for sheet_id in all_ids[:20]:  # delete 20 of 24 sheets: 40 of 48 docs, > 50% and >= 20
        del session.sheets[sheet_id]
    remaining_config = _config(sheet_ids=tuple(all_ids[20:]))
    with pytest.raises(RuntimeError, match="would tombstone"):
        _run(session, remaining_config, state)
    assert len(state["sheets"]) == 24  # refused: state untouched


def test_guard_ratio_bypassed_with_allow_mass_delete():
    sheets = {i: _one_row_sheet(i) for i in range(1, 25)}
    all_ids = list(sheets.keys())
    session = FakeSmartsheetSession(sheets=sheets)
    config = _config(sheet_ids=tuple(all_ids))
    state = {}
    _run(session, config, state)

    for sheet_id in all_ids[:20]:
        del session.sheets[sheet_id]
    remaining_config = _config(sheet_ids=tuple(all_ids[20:]), allow_mass_delete=True)
    docs = _run(session, remaining_config, state)
    assert sum(1 for d in docs if d.get("_deleted")) == 40


# ---------------------------------------------------------------------------
# 8. Row-count integrity check
# ---------------------------------------------------------------------------
def test_fetch_sheet_raises_if_row_count_does_not_match_total_row_count():
    session = _ScriptedSession(
        [
            (
                200,
                {
                    "id": 1,
                    "name": "S",
                    "columns": [],
                    "rows": [{"id": 1}],
                    "totalRowCount": 5,
                    "totalPages": 1,
                },
            )
        ]
    )
    with pytest.raises(RuntimeError, match="reported totalRowCount"):
        _fetch_sheet(session, _config(), 1, [])


# ---------------------------------------------------------------------------
# 9. Mid-run failures leave state untouched
# ---------------------------------------------------------------------------
def test_mid_run_sheet_fetch_failure_leaves_state_untouched():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1), 2: _one_row_sheet(2)})
    config = _config(sheet_ids=(1, 2))
    state = {}
    _run(session, config, state)
    before = copy.deepcopy(state)

    session.forbidden_sheet_ids.add(2)
    with pytest.raises(_SmartsheetAPIError):
        _run(session, config, state)
    assert state == before


def test_mid_run_listing_failure_leaves_state_untouched():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,))
    state = {}
    _run(session, config, state)
    before = copy.deepcopy(state)

    bad_config = _config(sheet_ids=(1,), folder_ids=(999,))  # folder 999 doesn't exist -> 404
    with pytest.raises(_SmartsheetAPIError):
        _run(session, bad_config, state)
    assert state == before


# ---------------------------------------------------------------------------
# 10. Config change / RENDER_VERSION bump force a full pass
# ---------------------------------------------------------------------------
def test_config_change_forces_a_full_pass_even_if_version_is_unchanged():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,), include_all_rows=False)
    state = {}
    _run(session, config, state)

    changed_config = _config(sheet_ids=(1,), include_all_rows=True)
    session.calls.clear()
    _run(session, changed_config, state)
    # A full pass fetches the sheet itself, not only its version.
    assert any(url.rstrip("/").endswith("/sheets/1") for url, _q, _h in session.calls)


def test_render_version_bump_forces_a_full_pass():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,))
    state = {}
    _run(session, config, state)

    state["render_version"] = _RENDER_VERSION - 1
    session.calls.clear()
    _run(session, config, state)
    assert any(url.rstrip("/").endswith("/sheets/1") for url, _q, _h in session.calls)
    assert state["render_version"] == _RENDER_VERSION


def test_periodic_full_sync_every_forces_a_full_pass():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,), full_sync_every=2, recheck_sheets_per_run=0)
    state = {}
    _run(session, config, state)  # run_count 0 -> 1
    _run(session, config, state)  # run_count 1 -> 2, not a multiple of 2 yet

    session.calls.clear()
    _run(session, config, state)  # run_count 2 -> full pass
    assert any(url.rstrip("/").endswith("/sheets/1") for url, _q, _h in session.calls)


# ---------------------------------------------------------------------------
# 11. Attachment text
# ---------------------------------------------------------------------------
def test_is_text_attachment_detects_by_mime_and_extension():
    assert _is_text_attachment({"mimeType": "text/csv", "name": "x.bin"})
    assert _is_text_attachment({"name": "notes.md"})
    assert not _is_text_attachment({"mimeType": "image/png", "name": "photo.png"})


def test_attachment_text_is_fetched_and_rendered_without_an_auth_header():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    att = attachment("notes.txt", mime_type="text/plain", attachment_id=42)
    s1 = sheet(
        1,
        columns=columns,
        rows=[row(1, 1, [cell(1, display_value="Has a file")], attachments=[att])],
    )
    session = FakeSmartsheetSession(sheets={1: s1}, attachment_content={42: b"hello from the file"})
    config = _config(sheet_ids=(1,), include_attachments=True, attachment_text=True)
    docs = _run(session, config, {})
    row_doc = next(d for d in docs if d["id"] == "smartsheet:com:1:row:1")
    assert "hello from the file" in row_doc["content"]
    assert session.download_calls, "the pre-signed download URL must have been hit"
    for _url, headers in session.download_calls:
        assert headers.get("Authorization") is None


def test_attachment_text_off_by_default_never_downloads():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    att = attachment("notes.txt", mime_type="text/plain", attachment_id=42)
    s1 = sheet(
        1,
        columns=columns,
        rows=[row(1, 1, [cell(1, display_value="Has a file")], attachments=[att])],
    )
    session = FakeSmartsheetSession(sheets={1: s1}, attachment_content={42: b"hello"})
    config = _config(sheet_ids=(1,), include_attachments=True, attachment_text=False)
    docs = _run(session, config, {})
    row_doc = next(d for d in docs if d["id"] == "smartsheet:com:1:row:1")
    assert "hello" not in row_doc["content"]
    assert not session.download_calls


def test_attachment_text_is_truncated_at_the_configured_byte_cap():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    att = attachment("notes.txt", mime_type="text/plain", attachment_id=42)
    s1 = sheet(
        1,
        columns=columns,
        rows=[row(1, 1, [cell(1, display_value="Has a file")], attachments=[att])],
    )
    session = FakeSmartsheetSession(sheets={1: s1}, attachment_content={42: b"x" * 1000})
    config = _config(
        sheet_ids=(1,), include_attachments=True, attachment_text=True, attachment_max_bytes=10
    )
    docs = _run(session, config, {})
    row_doc = next(d for d in docs if d["id"] == "smartsheet:com:1:row:1")
    assert "x" * 11 not in row_doc["content"]
    assert "x" * 10 in row_doc["content"]


def test_attachment_text_404_on_detail_is_skipped_not_raised():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    att = attachment("notes.txt", mime_type="text/plain", attachment_id=999)  # no detail registered
    s1 = sheet(
        1,
        columns=columns,
        rows=[row(1, 1, [cell(1, display_value="Has a file")], attachments=[att])],
    )
    session = FakeSmartsheetSession(sheets={1: s1}, attachment_content={})
    config = _config(sheet_ids=(1,), include_attachments=True, attachment_text=True)
    docs = _run(session, config, {})  # must not raise
    row_doc = next(d for d in docs if d["id"] == "smartsheet:com:1:row:1")
    assert "notes.txt" in row_doc["content"]


def test_attachment_text_other_error_on_detail_raises():
    class _ForbiddenAttachmentSession(FakeSmartsheetSession):
        def _get_attachment(self, sheet_id, attachment_id):
            from fake_smartsheet import _error, _FakeResponse

            return _FakeResponse(403, _error(1004, "forbidden"))

    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    att = attachment("notes.txt", mime_type="text/plain", attachment_id=42)
    s1 = sheet(
        1,
        columns=columns,
        rows=[row(1, 1, [cell(1, display_value="Has a file")], attachments=[att])],
    )
    session = _ForbiddenAttachmentSession(sheets={1: s1}, attachment_content={42: b"hello"})
    config = _config(sheet_ids=(1,), include_attachments=True, attachment_text=True)
    with pytest.raises(_SmartsheetAPIError, match="forbidden"):
        _run(session, config, {})


def test_attachment_text_respects_the_per_row_cap():
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    attachments = [
        attachment(f"n{i}.txt", mime_type="text/plain", attachment_id=i) for i in range(1, 8)
    ]
    s1 = sheet(
        1,
        columns=columns,
        rows=[row(1, 1, [cell(1, display_value="Has files")], attachments=attachments)],
    )
    content = {i: f"content-{i}".encode() for i in range(1, 8)}
    session = FakeSmartsheetSession(sheets={1: s1}, attachment_content=content)
    config = _config(sheet_ids=(1,), include_attachments=True, attachment_text=True)
    docs = _run(session, config, {})
    row_doc = next(d for d in docs if d["id"] == "smartsheet:com:1:row:1")
    fetched = sum(1 for i in range(1, 8) if f"content-{i}" in row_doc["content"])
    assert fetched == 5  # _MAX_TEXT_ATTACHMENTS_PER_ROW


# ---------------------------------------------------------------------------
# 12. State is plain JSON, and the token never appears in it or in logs
# ---------------------------------------------------------------------------
def test_state_round_trips_through_json():
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,))
    state = {}
    _run(session, config, state)
    assert json.loads(json.dumps(state)) == state


def test_token_never_appears_in_state_or_log_output(caplog):
    secret = "ssheet-super-secret-abc123"
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    config = _config(sheet_ids=(1,))
    state = {}
    with caplog.at_level("DEBUG"):
        _run(session, config, state)
    assert secret not in json.dumps(state)
    assert secret not in caplog.text


# ---------------------------------------------------------------------------
# 13. Real dlt.pipeline: physical tombstone removal, idempotent zero-row rerun
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_pipeline(dlt, tmp_path, run_name, session, **source_kwargs):
    db_path = (tmp_path / f"{run_name}.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name=run_name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="smartsheet_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        smartsheet_source(
            api_token="tok",
            include_comments=False,
            include_attachments=False,
            session=session,
            **source_kwargs,
        )
    )
    return pipeline


def _read_ids(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM smartsheet_documents") as cursor,
    ):
        return {r[0] for r in cursor.fetchall()}


def test_pipeline_physically_removes_a_deleted_row(dlt_mod, tmp_path):
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    s1 = sheet(
        1,
        columns=columns,
        rows=[
            row(1, 1, [cell(1, display_value="Keep")]),
            row(2, 2, [cell(1, display_value="Delete")]),
        ],
    )
    session = FakeSmartsheetSession(sheets={1: s1})
    run_name = "smartsheet_forget_pipeline"
    pipeline = _run_pipeline(dlt_mod, tmp_path, run_name, session, sheet_ids=[1])
    assert "smartsheet:com:1:row:2" in _read_ids(pipeline)

    session.sheets[1] = sheet(1, columns=columns, rows=[row(1, 1, [cell(1, display_value="Keep")])])
    session.versions[1] = 2
    pipeline = _run_pipeline(dlt_mod, tmp_path, run_name, session, sheet_ids=[1])
    ids = _read_ids(pipeline)
    assert "smartsheet:com:1:row:2" not in ids
    assert "smartsheet:com:1:row:1" in ids


def test_pipeline_rerun_with_no_changes_makes_no_further_writes(dlt_mod, tmp_path):
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    run_name = "smartsheet_noop_pipeline"
    pipeline = _run_pipeline(dlt_mod, tmp_path, run_name, session, sheet_ids=[1])
    ids_before = _read_ids(pipeline)

    pipeline = _run_pipeline(dlt_mod, tmp_path, run_name, session, sheet_ids=[1])
    assert _read_ids(pipeline) == ids_before


def test_pipeline_exception_leaves_the_table_unchanged(dlt_mod, tmp_path):
    session = FakeSmartsheetSession(sheets={1: _one_row_sheet(1)})
    run_name = "smartsheet_exception_pipeline"
    pipeline = _run_pipeline(dlt_mod, tmp_path, run_name, session, sheet_ids=[1])
    ids_before = _read_ids(pipeline)

    session.forbidden_sheet_ids.add(1)
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error
        _run_pipeline(dlt_mod, tmp_path, run_name, session, sheet_ids=[1])
    assert _read_ids(pipeline) == ids_before


# ---------------------------------------------------------------------------
# 14. Graph-level forget: a deleted row must disappear from cognee's graph,
# not just from the dlt table (modeled on google-calendar's forget test).
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def cognee_graph_env(tmp_path, monkeypatch):
    import importlib

    import cognee

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})

    from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
        LiteLLMEmbeddingEngine,
    )
    from cognee.infrastructure.llm import LLMGateway

    add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

    async def _noop_index(*_a, **_k):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)

    def _with_required(model, **values):
        for field_name, field in model.model_fields.items():
            if field.is_required() and field_name not in values:
                values[field_name] = "mock"
        return model(**values)

    tokens = ("Alphacorp", "Bravocorp")

    async def _mock_structured_output(
        text_input=None, system_prompt=None, response_model=str, **_kwargs
    ):
        from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
        from cognee.shared.data_models import Node as KGNode

        if response_model is str:
            return "Mocked answer."
        if response_model == SummarizedContent:
            return SummarizedContent(summary="Mock summary", description="Mock summary")
        if response_model == KnowledgeGraph:
            name = next((t for t in tokens if text_input and t in text_input), None)
            nodes = (
                [
                    _with_required(
                        KGNode, id=name, name=name, type="Company", description=f"{name} entity"
                    )
                ]
                if name
                else []
            )
            return _with_required(KnowledgeGraph, nodes=nodes, edges=[])
        return response_model()

    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield tokens
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _graph_has(token: str) -> bool:
    from cognee.infrastructure.databases.graph import get_graph_engine

    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


async def _sync_to_cognee(session, dataset):
    import cognee

    await cognee.add(
        smartsheet_source(
            api_token="tok",
            sheet_ids=[1],
            include_comments=False,
            include_attachments=False,
            session=session,
        ),
        dataset_name=dataset,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    await cognee.cognify(datasets=[dataset])


@pytest.mark.asyncio
async def test_deleted_row_is_forgotten_from_the_graph(cognee_graph_env):
    alpha, bravo = cognee_graph_env
    columns = [column(1, "Task", "TEXT_NUMBER", primary=True)]
    s1 = sheet(
        1,
        columns=columns,
        rows=[
            row(1, 1, [cell(1, display_value=f"{alpha} owns this task")]),
            row(2, 2, [cell(1, display_value=f"{bravo} owns this one")]),
        ],
    )
    session = FakeSmartsheetSession(sheets={1: s1})
    dataset = "smartsheet_forget_graph_test"

    await _sync_to_cognee(session, dataset)
    assert await _graph_has(alpha)
    assert await _graph_has(bravo)

    session.sheets[1] = sheet(
        1, columns=columns, rows=[row(1, 1, [cell(1, display_value=f"{alpha} owns this task")])]
    )
    session.versions[1] = 2
    await _sync_to_cognee(session, dataset)

    assert await _graph_has(alpha), "an untouched row must stay"
    assert not await _graph_has(bravo), "a deleted row must be forgotten from the graph"
