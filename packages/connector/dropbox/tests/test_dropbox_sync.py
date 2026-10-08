"""Sync state machine tests: _iter_rows against an in-memory Dropbox.

No network, no LLM. Each test drives the fake Dropbox through a real-world
change (edit, move, delete, cursor reset, ...) and checks the rows the
connector yields and the state it keeps for the next run.
"""

import pytest
from dropbox import files
from fake_dropbox import FakeDropbox, api_error, make_pdf

from cognee_community_connector_dropbox.dropbox import _DropboxConfig, _iter_rows


def sync(fake, state, folders=("",), max_file_size_mb=25):
    config = _DropboxConfig(folder_paths=tuple(folders), max_file_size_mb=max_file_size_mb)
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, config, state, stats))
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    deletes = sorted(row["id"] for row in rows if row["_deleted"])
    return upserts, deletes, stats


@pytest.fixture
def fake():
    return FakeDropbox()


# ---------------------------------------------------------------------------
# First sync and steady state
# ---------------------------------------------------------------------------
def test_first_sync_yields_every_supported_file_and_saves_state(fake):
    a = fake.put("/Notes/a.md", "# Alpha notes")
    b = fake.put("/Notes/deep/b.txt", "bravo text")
    c = fake.put("/data.csv", "x,y\n1,2")
    state: dict = {}

    upserts, deletes, stats = sync(fake, state)

    assert set(upserts) == {a, b, c}
    assert deletes == []
    assert state["index"] == {"/notes/a.md": a, "/notes/deep/b.txt": b, "/data.csv": c}
    assert set(state["cursors"]) == {""}
    assert stats["deleted"] == 0 and stats["failed"] == 0


def test_row_shape_and_content_carry_path_and_folder(fake):
    a = fake.put("/Notes/Plans/a.md", "Ship the connector.")

    upserts, _, _ = sync(fake, {})

    assert upserts[a] == {
        "id": a,
        "title": "a.md",
        "content": ("Dropbox file: /Notes/Plans/a.md\nFolder: /Notes/Plans\n\nShip the connector."),
        "url": None,
        "_deleted": False,
    }


def test_resync_without_changes_yields_nothing_but_saves_a_fresh_cursor(fake):
    fake.put("/a.md", "alpha")
    state: dict = {}
    sync(fake, state)
    first_cursor = state["cursors"][""]
    fake.put("/Other/empty-folder-marker.png", b"")  # an unrelated, unsupported change

    upserts, deletes, _ = sync(fake, state)

    assert upserts == {} and deletes == []
    assert state["cursors"][""] != first_cursor


def test_pagination_reads_every_page(fake):
    fake.page_size = 2
    ids = {fake.put(f"/f{i}.txt", f"file {i}") for i in range(5)}
    state: dict = {}

    upserts, _, _ = sync(fake, state)
    assert set(upserts) == ids

    more = {fake.put(f"/g{i}.txt", f"file {i}") for i in range(5)}
    upserts, _, _ = sync(fake, state)
    assert set(upserts) == more


# ---------------------------------------------------------------------------
# Edits, moves and deletes
# ---------------------------------------------------------------------------
def test_edit_reyields_the_same_id_with_new_content(fake):
    a = fake.put("/a.md", "old")
    state: dict = {}
    sync(fake, state)

    assert fake.put("/a.md", "new") == a
    upserts, deletes, _ = sync(fake, state)

    assert list(upserts) == [a]
    assert upserts[a]["content"].endswith("new")
    assert deletes == []


def test_move_is_an_update_not_a_delete(fake):
    a = fake.put("/Inbox/a.md", "alpha")
    state: dict = {}
    sync(fake, state)

    fake.move("/Inbox/a.md", "/Archive/2026/renamed.md")
    upserts, deletes, _ = sync(fake, state)

    assert deletes == []
    assert upserts[a]["title"] == "renamed.md"
    assert "Folder: /Archive/2026" in upserts[a]["content"]
    assert state["index"] == {"/archive/2026/renamed.md": a}


def test_move_between_two_synced_folders_is_an_update(fake):
    a = fake.put("/One/a.md", "alpha")
    fake.put("/Two/keep.md", "keep")
    state: dict = {}
    folders = ("/one", "/two")
    sync(fake, state, folders)

    fake.move("/One/a.md", "/Two/a.md")
    upserts, deletes, _ = sync(fake, state, folders)

    assert deletes == []
    assert list(upserts) == [a]


def test_move_out_of_synced_folder_forgets_the_file(fake):
    a = fake.put("/Synced/a.md", "alpha")
    state: dict = {}
    sync(fake, state, ("/synced",))

    fake.move("/Synced/a.md", "/Elsewhere/a.md")
    upserts, deletes, _ = sync(fake, state, ("/synced",))

    assert upserts == {}
    assert deletes == [a]


def test_deleted_file_is_tombstoned_by_id_from_the_index(fake):
    a = fake.put("/a.md", "alpha")
    b = fake.put("/b.md", "bravo")
    state: dict = {}
    sync(fake, state)

    fake.delete("/b.md")
    upserts, deletes, stats = sync(fake, state)

    assert upserts == {}
    assert deletes == [b]
    assert stats["deleted"] == 1
    assert state["index"] == {"/a.md": a}


def test_deleted_folder_forgets_everything_under_it_but_not_lookalike_siblings(fake):
    a = fake.put("/Notes/a.md", "alpha")
    b = fake.put("/Notes/deep/b.md", "bravo")
    keep = fake.put("/Notes2/keep.md", "keep")
    state: dict = {}
    sync(fake, state)

    fake.delete("/Notes")
    upserts, deletes, _ = sync(fake, state)

    assert upserts == {}
    assert deletes == sorted([a, b])
    assert state["index"] == {"/notes2/keep.md": keep}


def test_file_added_and_deleted_in_the_same_round_is_never_downloaded(fake):
    state: dict = {}
    sync(fake, state)

    temp = fake.put("/temp.md", "short lived")
    fake.delete("/temp.md")
    upserts, deletes, _ = sync(fake, state)

    assert upserts == {}
    assert fake.downloads == []
    # A harmless no-op tombstone: see the replay test below for why it is kept.
    assert deletes == [temp]


def test_file_stored_by_a_failed_run_is_still_forgotten_on_replay(fake):
    x = fake.put("/x.md", "stored during a run that later failed")
    y = fake.put("/y.md", "fails to download")
    fake.download_errors[y] = ConnectionError("timeout")
    state: dict = {}
    upserts, _, _ = sync(fake, state)
    assert x in upserts and "cursors" not in state  # x reached cognee, the cursor did not move

    fake.delete("/x.md")
    del fake.download_errors[y]
    upserts, deletes, _ = sync(fake, state)

    assert deletes == [x]
    assert set(upserts) == {y}


def test_incremental_round_that_failed_replays_to_the_same_result(fake):
    keep = fake.put("/keep.md", "keep")
    state: dict = {}
    sync(fake, state)

    moved = fake.put("/a.md", "alpha")
    fake.move("/a.md", "/Archive/a.md")
    gone = fake.put("/gone.md", "short lived")
    flaky = fake.put("/flaky.md", "fails once")
    fake.download_errors[flaky] = ConnectionError("timeout")
    sync(fake, state)  # stores moved, fails on flaky
    fake.delete("/gone.md")
    del fake.download_errors[flaky]

    upserts, deletes, stats = sync(fake, state)

    assert stats["failed"] == 0
    assert set(upserts) == {moved, flaky}  # the held-back round replays
    assert deletes == [gone]
    assert state["index"] == {"/keep.md": keep, "/archive/a.md": moved, "/flaky.md": flaky}


def test_path_reused_by_a_new_file_forgets_the_old_id(fake):
    old = fake.put("/a.md", "old file")
    state: dict = {}
    sync(fake, state)

    fake.delete("/a.md")
    new = fake.put("/a.md", "brand new file")
    upserts, deletes, _ = sync(fake, state)

    assert new != old
    assert deletes == [old]
    assert list(upserts) == [new]


# ---------------------------------------------------------------------------
# Cursor reset, missing folders and errors
# ---------------------------------------------------------------------------
def test_reset_cursor_relists_and_reconciles_like_a_snapshot(fake):
    a = fake.put("/a.md", "alpha")
    b = fake.put("/b.md", "bravo")
    state: dict = {}
    sync(fake, state)

    fake.delete("/b.md")
    c = fake.put("/c.md", "charlie")
    fake.reset_cursors.add(state["cursors"][""])
    upserts, deletes, _ = sync(fake, state)

    assert deletes == [b]
    assert set(upserts) == {a, c}  # a full listing re-yields every file
    assert state["index"] == {"/a.md": a, "/c.md": c}


def test_synced_folder_that_disappears_forgets_its_files(fake):
    a = fake.put("/Project/a.md", "alpha")
    state: dict = {}
    sync(fake, state, ("/project",))

    fake.delete("/Project")
    upserts, deletes, _ = sync(fake, state, ("/project",))

    assert upserts == {}
    assert deletes == [a]
    assert state["cursors"] == {}


def test_folder_missing_on_first_sync_yields_nothing(fake):
    state: dict = {}

    upserts, deletes, _ = sync(fake, state, ("/does-not-exist",))

    assert upserts == {} and deletes == []


@pytest.mark.parametrize(
    "error",
    [
        api_error(files.ListFolderContinueError.other),
        ConnectionError("network down"),
    ],
)
def test_listing_errors_raise_and_never_delete(fake, error):
    fake.put("/a.md", "alpha")
    state: dict = {}
    sync(fake, state)
    before = {"cursors": dict(state["cursors"]), "index": dict(state["index"])}

    fake.delete("/a.md")
    fake.continue_errors.append(error)
    with pytest.raises(type(error)):
        sync(fake, state)

    assert state == before


def test_path_error_other_than_not_found_raises(fake):
    fake.put("/Project/a.md", "alpha")
    state: dict = {}
    sync(fake, state, ("/project",))

    fake.continue_errors.append(
        api_error(files.ListFolderContinueError.path(files.LookupError.restricted_content))
    )
    fake.list_errors.append(
        api_error(files.ListFolderError.path(files.LookupError.restricted_content))
    )
    with pytest.raises(Exception, match="restricted_content"):
        sync(fake, state, ("/project",))


def test_failed_download_keeps_state_so_the_file_is_retried(fake):
    a = fake.put("/a.md", "alpha")
    b = fake.put("/b.md", "bravo")
    state: dict = {}
    fake.download_errors[b] = ConnectionError("timeout")

    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {a}
    assert stats["failed"] == 1
    assert "cursors" not in state  # the cursor is held back, so the round replays

    del fake.download_errors[b]
    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {a, b}
    assert stats["failed"] == 0
    assert set(state["index"].values()) == {a, b}


def test_restricted_file_is_skipped_not_retried_forever(fake):
    a = fake.put("/a.md", "alpha")
    blocked = fake.put("/blocked.md", "cannot download")
    fake.download_errors[blocked] = api_error(
        files.DownloadError.path(files.LookupError.restricted_content)
    )
    state: dict = {}

    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {a}
    assert stats["failed"] == 0
    assert stats["skipped_unavailable"] == 1
    assert "cursors" in state


# ---------------------------------------------------------------------------
# Content types and skips
# ---------------------------------------------------------------------------
def test_pdf_text_is_extracted(fake):
    report = fake.put("/report.pdf", make_pdf("Quarterly report Zephyrcorp"))

    upserts, _, _ = sync(fake, {})

    assert "Quarterly report Zephyrcorp" in upserts[report]["content"]


def test_paper_doc_is_exported_as_markdown(fake):
    paper = fake.put(
        "/Plans.paper",
        "# Roadmap\n\n- launch",
        is_downloadable=False,
        export_info=files.ExportInfo(export_as="markdown", export_options=["markdown", "html"]),
    )

    upserts, _, _ = sync(fake, {})

    assert fake.exports == [(paper, "markdown")]
    assert "# Roadmap" in upserts[paper]["content"]


def test_unsupported_large_empty_and_corrupt_files_are_skipped_and_counted(fake):
    good = fake.put("/good.md", "fine")
    fake.put("/photo.png", b"\x89PNG")
    fake.put("/huge.txt", "x", size=2 * 1024 * 1024)
    fake.put("/empty.md", "   \n")
    fake.put("/broken.pdf", b"not a pdf at all")
    fake.put(
        "/Sheet.gsheet",
        "a,b",
        is_downloadable=False,
        export_info=files.ExportInfo(export_as="xlsx"),
    )

    upserts, _, stats = sync(fake, {}, max_file_size_mb=1)

    assert set(upserts) == {good}
    assert stats["scanned"] == 6
    assert stats["skipped"] == 5
    assert stats["skipped_unsupported_type"] == 2
    assert stats["skipped_too_large"] == 1
    assert stats["skipped_empty_content"] == 1
    assert stats["skipped_unparseable"] == 1
    assert stats["failed"] == 0


# ---------------------------------------------------------------------------
# Folder configuration
# ---------------------------------------------------------------------------
def test_only_configured_folders_are_synced(fake):
    a = fake.put("/Work/a.md", "alpha")
    fake.put("/Personal/b.md", "bravo")

    upserts, _, _ = sync(fake, {}, ("/work",))

    assert set(upserts) == {a}


def test_dropping_a_folder_from_the_config_forgets_its_files(fake):
    a = fake.put("/Work/a.md", "alpha")
    b = fake.put("/Personal/b.md", "bravo")
    state: dict = {}
    sync(fake, state, ("/personal", "/work"))

    upserts, deletes, _ = sync(fake, state, ("/work",))

    assert upserts == {}
    assert deletes == [b]
    assert state["index"] == {"/work/a.md": a}


def test_widening_to_a_parent_folder_keeps_existing_files(fake):
    a = fake.put("/Work/Team/a.md", "alpha")
    b = fake.put("/Work/b.md", "bravo")
    state: dict = {}
    sync(fake, state, ("/work/team",))

    upserts, deletes, _ = sync(fake, state, ("/work",))

    assert deletes == []
    assert set(upserts) == {a, b}
