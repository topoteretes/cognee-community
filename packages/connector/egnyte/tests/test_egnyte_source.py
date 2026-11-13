"""Unit tests for the Egnyte dlt source's sync logic.

Exercises ``_iter_rows`` directly against an in-memory fake of the Egnyte API
(no network, no credentials), with a plain dict standing in for dlt's resource
state.  Covers the initial walk, incremental syncs driven by the Events API,
deletes, renames, moves in and out of scope, folder events, an expired cursor,
and failure handling.
"""

import pytest
from _fake_egnyte import FakeEgnyte
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

from cognee_community_connector_egnyte import egnyte as eg
from cognee_community_connector_egnyte.egnyte import _EgnyteConfig

ROOT = "/Shared/Projects"


def _config(*folder_paths, max_file_size_mb=25):
    return _EgnyteConfig(
        domain="acme",
        folder_paths=tuple(folder_paths) or (ROOT,),
        max_file_size_mb=max_file_size_mb,
    )


def _sync(fake, state, config=None):
    return list(eg._iter_rows(fake, config or _config(), state))


def _live(rows):
    return {row["id"]: row for row in rows if not row["_deleted"]}


def _tombstones(rows):
    return {row["id"] for row in rows if row["_deleted"]}


def test_initial_sync_ingests_supported_files_and_saves_cursor():
    fake = FakeEgnyte()
    plan = fake.put(f"{ROOT}/plan.md", "# Plan\nShip in May.")
    notes = fake.put(f"{ROOT}/Acme/notes.txt", "Acme wants weekly updates.")
    fake.put(f"{ROOT}/logo.png", b"\x89PNG")
    fake.put("/Shared/Other/elsewhere.txt", "Not in scope.")
    state = {}

    rows = _sync(fake, state)

    live = _live(rows)
    assert set(live) == {plan, notes}
    assert live[plan]["title"] == f"{ROOT}/plan.md"
    assert live[plan]["content"] == "# Plan\nShip in May."
    assert live[plan]["url"] == f"https://acme.egnyte.com/navigate/file/{plan}"
    assert _tombstones(rows) == set()
    # The unsupported png is still remembered in state so a later folder delete
    # or move can account for it, but it was never downloaded.
    assert len(state["files"]) == 3
    assert f"{ROOT}/logo.png" not in fake.downloads()
    assert state["cursor"] == fake.events[-1]["id"]


def test_cursor_is_read_before_the_walk():
    fake = FakeEgnyte()
    fake.put(f"{ROOT}/a.txt", "A")
    original_list_folder = fake.list_folder

    def list_folder_and_change_a_file(path, offset=0):
        # A file is edited while the first sync is still walking the tree.
        listing = original_list_folder(path, offset)
        if not fake.calls_of("list")[1:]:
            fake.put(f"{ROOT}/b.txt", "B")
        return listing

    fake.list_folder = list_folder_and_change_a_file
    state = {}
    _sync(fake, state)

    # The edit landed after the cursor was read, so the next run must see it.
    rows = _sync(fake, state)
    assert [row["title"] for row in rows] == [f"{ROOT}/b.txt"]


def test_incremental_sync_only_downloads_changed_files():
    fake = FakeEgnyte()
    a = fake.put(f"{ROOT}/a.txt", "Version one.")
    fake.put(f"{ROOT}/b.txt", "Untouched.")
    state = {}
    _sync(fake, state)
    fake.calls.clear()

    fake.put(f"{ROOT}/a.txt", "Version two.")  # a new version keeps the group id
    rows = _sync(fake, state)

    assert rows == [
        {
            "id": a,
            "title": f"{ROOT}/a.txt",
            "content": "Version two.",
            "url": f"https://acme.egnyte.com/navigate/file/{a}",
            "_deleted": False,
        }
    ]
    assert fake.downloads() == [f"{ROOT}/a.txt"]
    assert fake.calls_of("list") == []  # no folder walk on an incremental run


def test_no_events_means_no_work():
    fake = FakeEgnyte()
    fake.put(f"{ROOT}/a.txt", "A")
    state = {}
    _sync(fake, state)
    fake.calls.clear()

    assert _sync(fake, state) == []
    assert fake.calls_of("download") == []
    assert fake.calls_of("meta") == []


def test_deleted_file_yields_tombstone():
    fake = FakeEgnyte()
    a = fake.put(f"{ROOT}/a.txt", "A")
    b = fake.put(f"{ROOT}/b.txt", "B")
    state = {}
    _sync(fake, state)

    fake.delete(f"{ROOT}/b.txt")
    rows = _sync(fake, state)

    assert rows == [{"id": b, "_deleted": True}]
    assert set(state["files"]) == {a}


def test_rename_inside_scope_updates_in_place():
    fake = FakeEgnyte()
    a = fake.put(f"{ROOT}/draft.txt", "Contract terms.")
    state = {}
    _sync(fake, state)

    fake.move(f"{ROOT}/draft.txt", f"{ROOT}/final.txt")
    rows = _sync(fake, state)

    # Same group id, new title, and no tombstone: a rename is not a delete.
    assert _tombstones(rows) == set()
    assert _live(rows)[a]["title"] == f"{ROOT}/final.txt"
    assert state["files"] == {a: f"{ROOT}/final.txt"}


def test_move_out_of_scope_yields_tombstone():
    fake = FakeEgnyte()
    a = fake.put(f"{ROOT}/a.txt", "A")
    state = {}
    _sync(fake, state)

    fake.move(f"{ROOT}/a.txt", "/Shared/Archive/a.txt")
    rows = _sync(fake, state)

    assert rows == [{"id": a, "_deleted": True}]
    assert state["files"] == {}


def test_move_into_scope_is_ingested():
    fake = FakeEgnyte()
    fake.mkdir(ROOT)
    outside = fake.put("/Shared/Inbox/brief.txt", "New brief.")
    state = {}
    _sync(fake, state)
    assert state["files"] == {}

    fake.move("/Shared/Inbox/brief.txt", f"{ROOT}/brief.txt")
    rows = _sync(fake, state)

    assert set(_live(rows)) == {outside}


def test_folder_delete_tombstones_every_file_under_it_without_per_file_calls():
    fake = FakeEgnyte()
    keep = fake.put(f"{ROOT}/keep.txt", "Keep me.")
    gone = {fake.put(f"{ROOT}/Old/{i}.txt", f"Old file {i}.") for i in range(5)}
    state = {}
    _sync(fake, state)
    fake.calls.clear()

    fake.delete_folder(f"{ROOT}/Old")
    rows = _sync(fake, state)

    assert _tombstones(rows) == gone
    assert set(state["files"]) == {keep}
    # Egnyte reports one event for the folder; the files under it are found in
    # the state map, and the folder 404 makes per-file checks unnecessary.
    assert fake.calls_of("meta") == []


def test_folder_rename_keeps_files_and_updates_titles():
    fake = FakeEgnyte()
    a = fake.put(f"{ROOT}/Acme/a.txt", "A")
    b = fake.put(f"{ROOT}/Acme/Sub/b.txt", "B")
    state = {}
    _sync(fake, state)

    fake.move_folder(f"{ROOT}/Acme", f"{ROOT}/Acme Corp")
    rows = _sync(fake, state)

    assert _tombstones(rows) == set()
    assert set(_live(rows)) == {a, b}
    assert state["files"][a].casefold() == f"{ROOT}/Acme Corp/a.txt".casefold()


def test_folder_moved_out_of_scope_tombstones_its_files():
    fake = FakeEgnyte()
    a = fake.put(f"{ROOT}/Acme/a.txt", "A")
    state = {}
    _sync(fake, state)

    fake.move_folder(f"{ROOT}/Acme", "/Shared/Archive/Acme")
    rows = _sync(fake, state)

    assert rows == [{"id": a, "_deleted": True}]


def test_expired_cursor_falls_back_to_full_sync():
    fake = FakeEgnyte()
    a = fake.put(f"{ROOT}/a.txt", "A")
    b = fake.put(f"{ROOT}/b.txt", "B")
    state = {}
    _sync(fake, state)

    fake.delete(f"{ROOT}/b.txt")
    c = fake.put(f"{ROOT}/c.txt", "C")
    # Egnyte has dropped every event up to and past the saved cursor.
    fake.oldest_event_id = fake.events[-1]["id"]
    fake.calls.clear()
    rows = _sync(fake, state)

    assert fake.calls_of("events") == []
    assert set(_live(rows)) == {a, c}
    assert _tombstones(rows) == {b}
    assert set(state["files"]) == {a, c}


def test_events_after_the_cursor_snapshot_are_left_for_the_next_run():
    fake = FakeEgnyte()
    fake.put(f"{ROOT}/a.txt", "A")
    state = {}
    _sync(fake, state)

    fake.put(f"{ROOT}/b.txt", "B")
    original_cursor = fake.get_event_cursor

    def cursor_then_another_change():
        snapshot = original_cursor()
        fake.put(f"{ROOT}/c.txt", "C")  # lands after the snapshot was taken
        return snapshot

    fake.get_event_cursor = cursor_then_another_change
    rows = _sync(fake, state)
    assert [row["title"] for row in rows] == [f"{ROOT}/b.txt"]

    fake.get_event_cursor = original_cursor
    rows = _sync(fake, state)
    assert [row["title"] for row in rows] == [f"{ROOT}/c.txt"]


def test_api_failure_leaves_state_untouched():
    fake = FakeEgnyte()
    fake.put(f"{ROOT}/a.txt", "A")
    state = {}
    _sync(fake, state)
    saved = {"cursor": state["cursor"], "files": dict(state["files"])}

    fake.put(f"{ROOT}/b.txt", "B")

    def broken_download(path):
        raise eg.EgnyteQuotaExceededError("daily quota used up")

    fake.download = broken_download
    with pytest.raises(eg.EgnyteQuotaExceededError):
        _sync(fake, state)

    # A failed run must not advance the cursor, or the change would be lost.
    assert state == saved


def test_missing_root_folder_fails_instead_of_forgetting_everything():
    fake = FakeEgnyte()
    fake.put(f"{ROOT}/a.txt", "A")
    state = {}
    _sync(fake, state)

    # A stale cursor forces a full walk; a typo'd root must not look like an
    # empty folder, or every file synced from the real root would be forgotten.
    fake.oldest_event_id = fake.events[-1]["id"] + 1
    saved = {"cursor": state["cursor"], "files": dict(state["files"])}
    with pytest.raises(eg.EgnyteError, match="was not found"):
        _sync(fake, state, _config("/Shared/Projcts"))
    assert state == saved


def test_file_or_subfolder_deleted_mid_walk_is_skipped_not_fatal():
    fake = FakeEgnyte()
    keep = fake.put(f"{ROOT}/keep.txt", "Keep me.")
    fake.put(f"{ROOT}/vanishing.txt", "Gone before download.")
    fake.put(f"{ROOT}/Temp/t.txt", "Gone before listing.")
    original_list_folder = fake.list_folder

    def list_folder_then_delete(path, offset=0):
        listing = original_list_folder(path, offset)
        if path == ROOT:
            # Both disappear right after the root listing returned them.
            fake.delete(f"{ROOT}/vanishing.txt")
            fake.delete_folder(f"{ROOT}/Temp")
        return listing

    fake.list_folder = list_folder_then_delete
    state = {}
    rows = _sync(fake, state)

    assert set(_live(rows)) == {keep}
    assert "cursor" in state  # the run completed


def test_unparseable_file_is_skipped_but_tracked():
    fake = FakeEgnyte()
    broken = fake.put(f"{ROOT}/broken.pdf", b"not really a pdf")
    good = fake.put(f"{ROOT}/good.txt", "Fine.")
    state = {}

    rows = _sync(fake, state)

    assert set(_live(rows)) == {good}
    assert broken in state["files"]


def test_files_over_the_size_limit_are_not_downloaded():
    fake = FakeEgnyte()
    fake.put(f"{ROOT}/huge.txt", "x" * (2 * 1024 * 1024))
    small = fake.put(f"{ROOT}/small.txt", "small")
    state = {}

    rows = _sync(fake, state, _config(ROOT, max_file_size_mb=1))

    assert set(_live(rows)) == {small}
    assert fake.downloads() == [f"{ROOT}/small.txt"]


def test_large_folders_and_event_bursts_are_paginated():
    fake = FakeEgnyte()
    ids = {fake.put(f"{ROOT}/file-{i:03}.txt", f"File {i}.") for i in range(eg._PAGE_SIZE + 30)}
    state = {}

    rows = _sync(fake, state)
    assert set(_live(rows)) == ids
    assert [call[2] for call in fake.calls_of("list")] == [0, eg._PAGE_SIZE]

    for i in range(eg._PAGE_SIZE + 5):
        fake.put(f"{ROOT}/file-{i:03}.txt", f"File {i}, edited.")
    fake.calls.clear()
    rows = _sync(fake, state)

    assert len(_live(rows)) == eg._PAGE_SIZE + 5
    assert len(fake.calls_of("events")) == 2


def test_multiple_roots_and_case_insensitive_paths():
    fake = FakeEgnyte()
    a = fake.put(f"{ROOT}/a.txt", "A")
    b = fake.put("/Shared/Legal/b.txt", "B")
    state = {}

    rows = _sync(fake, state, _config(ROOT, "/shared/legal"))
    assert set(_live(rows)) == {a, b}

    fake.delete("/Shared/Legal/b.txt")
    rows = _sync(fake, state, _config(ROOT, "/shared/legal"))
    assert rows == [{"id": b, "_deleted": True}]


def test_folder_paths_are_normalized():
    assert eg._resolve_folder_paths("Shared/Projects/, /Shared/Legal") == (
        "/Shared/Projects",
        "/Shared/Legal",
    )
    with pytest.raises(ValueError):
        eg._resolve_folder_paths("  ,  ")


def test_resource_opts_into_document_mode_and_pipeline_scope():
    resource = eg.egnyte_source(
        domain="acme", folder_paths=[ROOT], resource_name="legal_files", client=FakeEgnyte()
    )

    assert resource.name == "legal_files"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "egnyte"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "legal_files"


def test_older_cognee_without_scoped_cleanup_is_refused(monkeypatch):
    monkeypatch.delattr(eg.dlt_utils, "DOCUMENT_SYNC_VERSION")

    with pytest.raises(RuntimeError, match="Upgrade Cognee"):
        eg.egnyte_source(domain="acme", folder_paths=[ROOT], client=FakeEgnyte())


def test_missing_credentials_fail_when_the_source_is_built(monkeypatch):
    for name in ("EGNYTE_ACCESS_TOKEN", "EGNYTE_API_KEY", "EGNYTE_USERNAME", "EGNYTE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="EGNYTE_ACCESS_TOKEN"):
        eg.egnyte_source(domain="acme", folder_paths=[ROOT])


def test_revoked_access_stops_extraction_before_any_request():
    fake = FakeEgnyte()
    fake.put(f"{ROOT}/a.txt", "A")

    def check_active():
        raise PermissionError("access revoked")

    resource = eg.egnyte_source(
        domain="acme", folder_paths=[ROOT], client=fake, check_active=check_active
    )
    with pytest.raises(Exception, match="access revoked"):
        list(resource)
    assert fake.calls == []
