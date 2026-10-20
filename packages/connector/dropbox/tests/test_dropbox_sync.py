"""Sync state machine tests: _iter_rows against an in-memory Dropbox.

No network, no LLM. Each test drives the fake Dropbox through a real-world
change (edit, move, delete, cursor reset, ...) and checks the rows the
connector yields and the state it keeps for the next run.
"""

import copy
import logging

import pytest
import requests
from dropbox import files
from dropbox.exceptions import AuthError, RateLimitError
from fake_dropbox import FakeDropbox, api_error, make_pdf

from cognee_community_connector_dropbox import dropbox as dropbox_module
from cognee_community_connector_dropbox.dropbox import (
    DropboxFolderNotFoundError,
    _DropboxConfig,
    _iter_rows,
)


def sync(fake, state, folders=("",), max_file_size_mb=25, check_active=None):
    config = _DropboxConfig(folder_paths=tuple(folders), max_file_size_mb=max_file_size_mb)
    stats: dict[str, int] = {}
    rows = list(_iter_rows(fake, config, state, stats, check_active))
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
    assert "download" not in fake.api_calls
    # A harmless no-op tombstone: see the replay test below for why it is kept.
    assert deletes == [temp]


def test_file_stored_next_to_a_failed_one_is_still_forgotten(fake):
    x = fake.put("/x.md", "stored next to a file that failed")
    y = fake.put("/y.md", "fails to download")
    fake.download_errors[y] = requests.ConnectionError("timeout")
    state: dict = {}
    upserts, _, _ = sync(fake, state)
    assert x in upserts and list(state["retry"]) == [y]  # the cursor moved, y waits by id

    fake.delete("/x.md")
    del fake.download_errors[y]
    upserts, deletes, _ = sync(fake, state)

    assert deletes == [x]
    assert set(upserts) == {y}


def test_failed_file_is_fetched_again_by_id_without_replaying_the_round(fake):
    keep = fake.put("/keep.md", "keep")
    state: dict = {}
    sync(fake, state)

    moved = fake.put("/a.md", "alpha")
    fake.move("/a.md", "/Archive/a.md")
    gone = fake.put("/gone.md", "short lived")
    flaky = fake.put("/flaky.md", "fails once")
    fake.download_errors[flaky] = requests.ConnectionError("timeout")
    sync(fake, state)  # stores moved, fails on flaky
    fake.delete("/gone.md")
    del fake.download_errors[flaky]

    upserts, deletes, stats = sync(fake, state)

    assert stats["failed"] == 0
    assert set(upserts) == {flaky}  # only the failed file, not the whole round again
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


def test_folder_missing_on_first_sync_raises_instead_of_an_empty_dataset(fake):
    state: dict = {}

    with pytest.raises(DropboxFolderNotFoundError, match="folder_paths") as error:
        sync(fake, state, ("/does-not-exist",))

    assert "does-not-exist" not in str(error.value)
    assert state == {}


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
    before = copy.deepcopy(state)

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


def test_failed_download_is_kept_by_id_and_retried(fake):
    a = fake.put("/a.md", "alpha")
    b = fake.put("/b.md", "bravo")
    state: dict = {}
    fake.download_errors[b] = requests.ConnectionError("timeout")

    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {a}
    assert stats["failed"] == 1
    assert list(state["retry"]) == [b]
    assert "cursors" in state  # the cursor moves on; b waits by id

    del fake.download_errors[b]
    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {b}
    assert stats["failed"] == 0
    assert state["retry"] == {}
    assert set(state["index"].values()) == {a, b}


def test_a_file_that_keeps_failing_is_given_up_after_three_runs(fake):
    state: dict = {}
    sync(fake, state)
    bad = fake.put("/bad.md", "never downloads")
    fake.download_errors[bad] = requests.ConnectionError("timeout")

    downloads_per_run = []
    for run in range(4):
        fake.put(f"/new{run}.md", f"new file {run}")
        fake.downloads.clear()
        _, _, stats = sync(fake, state)
        downloads_per_run.append(len(fake.downloads))
        if run < 2:
            assert stats["failed"] == 1 and state["retry"] == {bad: run + 1}
    assert stats["failed"] == 0 and state["retry"] == {}

    # The new file plus one more try of the bad one, until the third try,
    # then it is skipped as unavailable and costs nothing.
    assert downloads_per_run == [2, 2, 2, 1]


def test_retried_file_deleted_in_the_meantime_is_dropped(fake):
    a = fake.put("/a.md", "alpha")
    fake.download_errors[a] = requests.ConnectionError("timeout")
    state: dict = {}
    sync(fake, state)
    assert list(state["retry"]) == [a]

    fake.delete("/a.md")
    upserts, deletes, _ = sync(fake, state)

    assert upserts == {}
    assert deletes == [a]
    assert state["retry"] == {}


# ---------------------------------------------------------------------------
# Rate limits and an expired token
# ---------------------------------------------------------------------------
@pytest.fixture
def waits(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(dropbox_module, "_sleep", slept.append)
    return slept


def test_short_rate_limit_waits_and_retries_the_same_request(fake, waits):
    a = fake.put("/a.md", "alpha")
    fake.download_errors_once[a] = RateLimitError("request-id", None, 2)
    checks: list[int] = []

    upserts, _, stats = sync(fake, {}, check_active=lambda: checks.append(1))

    assert set(upserts) == {a}
    assert waits == [2]
    assert stats["failed_rate_limit"] == 0
    # Asked before the listing, the first download and the retried download.
    assert len(checks) == 3


def test_long_rate_limit_stops_cleanly_and_resumes_next_run(fake, waits):
    a = fake.put("/a.md", "alpha")
    b = fake.put("/b.md", "bravo")
    c = fake.put("/c.md", "charlie")
    fake.download_errors_once[b] = RateLimitError("request-id", None, 3600)
    state: dict = {}

    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {a}
    assert waits == []  # an hour is never slept through
    assert stats["failed_rate_limit"] == 2
    assert list(state["retry"]) == [b, c]

    fake.downloads.clear()
    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {b, c}
    assert fake.downloads == [b, c]
    assert state["retry"] == {}


def test_rate_limit_that_never_ends_gives_up_after_a_few_short_waits(fake, waits):
    a = fake.put("/a.md", "alpha")
    fake.download_errors[a] = RateLimitError("request-id", None, 1)
    state: dict = {}

    upserts, _, stats = sync(fake, state)

    assert upserts == {}
    assert waits == [1, 1, 1]
    assert stats["failed_rate_limit"] == 1
    assert list(state["retry"]) == [a]


def test_expired_token_mid_run_stops_keeps_progress_and_resumes(fake):
    a = fake.put("/a.md", "alpha")
    b = fake.put("/b.md", "bravo")
    c = fake.put("/c.md", "charlie")
    fake.download_errors_once[b] = AuthError("request-id", "expired_access_token")
    state: dict = {}

    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {a}
    assert fake.downloads == [a, b], "no request after the 401"
    assert stats["failed_auth"] == 2
    assert list(state["retry"]) == [b, c]

    upserts, _, _ = sync(fake, state)

    assert set(upserts) == {b, c}


def test_bad_token_before_anything_was_stored_raises(fake):
    a = fake.put("/a.md", "alpha")
    fake.download_errors[a] = AuthError("request-id", "invalid_access_token")
    state: dict = {}

    with pytest.raises(AuthError):
        sync(fake, state)

    assert state == {}


def test_repeated_cursor_raises_instead_of_looping(fake):
    fake.put("/a.md", "alpha")
    state: dict = {}
    sync(fake, state)
    stuck = files.ListFolderResult(entries=[], cursor=state["cursors"][""], has_more=True)
    fake.files_list_folder_continue = lambda _cursor: stuck

    with pytest.raises(RuntimeError, match="same cursor"):
        sync(fake, state)


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


# ---------------------------------------------------------------------------
# Authorization checkpoint and privacy
# ---------------------------------------------------------------------------
class _RevokedError(Exception):
    pass


@pytest.mark.parametrize(
    "allowed",
    [
        ["list_folder"],  # blocks the next page of the first listing
        ["list_folder", "list_folder_continue", "list_folder_continue", "download"],
    ],
)
def test_check_active_runs_before_every_api_call_not_only_between_rows(fake, allowed):
    fake.page_size = 2
    for i in range(5):
        fake.put(f"/f{i}.md", f"file {i}")
    state: dict = {}

    def check_active():
        # The connection is revoked once these calls have been made.
        if len(fake.api_calls) >= len(allowed):
            raise _RevokedError

    with pytest.raises(_RevokedError):
        sync(fake, state, check_active=check_active)

    assert fake.api_calls == allowed, "no request after the revocation"
    assert state == {}


def test_check_active_also_guards_incremental_rounds(fake):
    fake.put("/a.md", "alpha")
    state: dict = {}
    sync(fake, state)
    before = copy.deepcopy(state)
    fake.put("/b.md", "bravo")
    fake.api_calls.clear()

    def check_active():
        raise _RevokedError

    with pytest.raises(_RevokedError):
        sync(fake, state, check_active=check_active)

    assert fake.api_calls == [], "list_folder/continue must not run once revoked"
    assert state == before


def test_check_active_is_not_swallowed_by_the_download_error_handling(fake):
    fake.put("/a.md", "alpha")

    def check_active():
        if fake.api_calls == ["list_folder"]:  # revoked right before the download
            raise _RevokedError

    with pytest.raises(_RevokedError):
        sync(fake, {}, check_active=check_active)

    assert "download" not in fake.api_calls


def test_no_file_names_paths_or_ids_reach_the_logs(fake, caplog):
    caplog.set_level(logging.DEBUG)
    folder = ("/secret plans",)
    budget = fake.put("/Secret Plans/budget-2027.md", "numbers")
    photo = fake.put("/Secret Plans/photo.png", b"\x89PNG")
    broken = fake.put("/Secret Plans/broken.pdf", b"not a pdf")
    offline = fake.put("/Secret Plans/offline.md", "later")
    fake.download_errors[offline] = requests.ConnectionError("timeout for /Secret Plans/offline.md")
    blocked = fake.put("/Secret Plans/blocked.md", "no")
    fake.download_errors[blocked] = api_error(
        files.DownloadError.path(files.LookupError.restricted_content)
    )
    state: dict = {}

    # Every path that logs: skips, a failed download, a reset cursor, a vanished folder.
    sync(fake, state, folder)
    del fake.download_errors[offline]
    sync(fake, state, folder)
    fake.reset_cursors.add(state["cursors"]["/secret plans"])
    sync(fake, state, folder)
    fake.delete("/Secret Plans")
    sync(fake, state, folder)

    assert caplog.text, "the paths above log"
    names = ("budget-2027.md", "photo.png", "broken.pdf", "offline.md", "blocked.md")
    for secret in ("Secret Plans", "secret plans", *names):
        assert secret not in caplog.text
    for file_id in (budget, photo, broken, offline, blocked):
        assert file_id not in caplog.text


# ---------------------------------------------------------------------------
# Review round 3: the cases found by running the connector through cognee
# ---------------------------------------------------------------------------
def test_file_written_over_a_path_without_a_delete_forgets_the_old_id(fake):
    old = fake.put("/a.md", "old file")
    state: dict = {}
    sync(fake, state)

    fake.vanish("/a.md")  # the delta holds only the new file
    new = fake.put("/a.md", "brand new file")
    upserts, deletes, _ = sync(fake, state)

    assert deletes == [old]
    assert list(upserts) == [new]
    assert state["index"] == {"/a.md": new}


def test_folder_created_where_a_file_was_forgets_the_file(fake):
    old = fake.put("/plans", "a file called plans")
    state: dict = {}
    sync(fake, state)

    fake.vanish("/plans")
    inner = fake.put("/Plans/q1.md", "now a folder")
    upserts, deletes, _ = sync(fake, state)

    assert deletes == [old]
    assert list(upserts) == [inner]


@pytest.mark.parametrize(
    "change",
    [
        lambda fake: fake.put("/a.md", "   \n  "),  # emptied
        lambda fake: fake.put("/a.md", "x" * 2 * 1024 * 1024),  # grew past the limit
    ],
)
def test_stored_file_that_is_now_skipped_is_forgotten(fake, change):
    a = fake.put("/a.md", "alpha")
    state: dict = {}
    sync(fake, state, max_file_size_mb=1)

    change(fake)
    upserts, deletes, _ = sync(fake, state, max_file_size_mb=1)

    assert upserts == {}
    assert deletes == [a], "the old text must not stay in memory"


def test_new_file_that_is_skipped_emits_no_tombstone(fake):
    fake.put("/photo.png", b"\x89PNG")

    _, deletes, stats = sync(fake, {})

    assert deletes == [] and stats["deleted"] == 0


def test_exported_paper_doc_is_held_to_the_size_limit(fake):
    info = files.ExportInfo(export_as="markdown", export_options=["markdown", "html"])
    fake.put(
        "/Big plan.paper", "# Plan", is_downloadable=False, export_info=info, size=3 * 1024 * 1024
    )

    upserts, _, stats = sync(fake, {}, max_file_size_mb=1)

    assert upserts == {}
    assert stats["skipped_too_large"] == 1
    assert fake.exports == [], "never read into memory"


def test_utf16_text_is_decoded_and_binary_text_is_skipped(fake):
    notepad = fake.put("/notepad.txt", "hello from notepad".encode("utf-16"))
    fake.put("/binary.csv", b"a,b\x00\x01\x02")

    upserts, _, stats = sync(fake, {})

    assert "hello from notepad" in upserts[notepad]["content"]
    assert "\x00" not in upserts[notepad]["content"]
    assert len(upserts) == 1 and stats["skipped_unparseable"] == 1


def test_change_without_a_path_is_skipped_not_fatal(fake):
    a = fake.put("/a.md", "alpha")
    state: dict = {}
    sync(fake, state)
    fake.log.append(files.DeletedMetadata(name="unmounted"))  # path_lower is None

    upserts, deletes, _ = sync(fake, state)

    assert upserts == {} and deletes == []
    assert state["index"] == {"/a.md": a}


@pytest.mark.parametrize("error", [PermissionError, TimeoutError, ConnectionError])
def test_os_errors_from_check_active_stop_the_run_and_are_not_counted(fake, error):
    fake.put("/a.md", "alpha")

    def check_active():
        if fake.api_calls == ["list_folder"]:  # revoked right before the download
            raise error("revoked")

    stats: dict = {}
    with pytest.raises(error):
        list(_iter_rows(fake, _DropboxConfig(("",), 25), {}, stats, check_active))

    assert stats["failed"] == 0
    assert "download" not in fake.api_calls


def test_unsupported_file_is_skipped_not_retried(fake):
    a = fake.put("/locked.md", "no")
    fake.download_errors[a] = api_error(files.DownloadError.unsupported_file)
    state: dict = {}

    upserts, _, stats = sync(fake, state)

    assert upserts == {}
    assert stats["skipped_unsupported_type"] == 1 and stats["failed"] == 0
    assert state["retry"] == {}


def test_retried_file_that_vanished_without_a_delete_is_dropped(fake):
    a = fake.put("/a.md", "alpha")
    fake.download_errors[a] = requests.ConnectionError("timeout")
    state: dict = {}
    sync(fake, state)
    assert state["retry"] == {a: 1}

    fake.vanish("/a.md")  # still in the index, but get_metadata says not_found
    upserts, deletes, stats = sync(fake, state)

    assert upserts == {} and deletes == []
    assert stats["failed"] == 0 and state["retry"] == {}


def test_rate_limit_waits_share_one_budget_per_run(fake, waits):
    ids = [fake.put(f"/f{i:02}.md", f"file {i}") for i in range(20)]
    for file_id in ids:
        fake.download_errors_once[file_id] = RateLimitError("request-id", None, 60)
    state: dict = {}

    upserts, _, stats = sync(fake, state)

    assert waits == [60, 60], "two minutes for the whole run, not per call"
    assert set(upserts) == set(ids[:2])
    assert stats["failed_rate_limit"] == 18
    assert list(state["retry"]) == ids[2:]


def test_every_download_response_is_closed(fake):
    fake.put("/a.md", "alpha")
    info = files.ExportInfo(export_as="markdown", export_options=["markdown"])
    fake.put("/Plan.paper", "# Plan", is_downloadable=False, export_info=info)

    sync(fake, {})

    assert len(fake.responses) == 2
    assert all(response.closed for response in fake.responses)
