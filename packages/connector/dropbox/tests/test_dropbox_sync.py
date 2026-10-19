"""Sync state machine tests: _iter_rows against an in-memory Dropbox.

No network, no LLM. Each test drives the fake Dropbox through a real-world
change (edit, move, delete, cursor reset, ...) and checks the rows the
connector yields and the state it keeps for the next run.
"""

import copy
import logging

import pytest
from dropbox import files
from dropbox.exceptions import AuthError, RateLimitError
from fake_dropbox import FakeDropbox, api_error, make_pdf

from cognee_community_connector_dropbox import dropbox as dropbox_module
from cognee_community_connector_dropbox.dropbox import _DropboxConfig, _iter_rows


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
    assert fake.downloads == []
    # A harmless no-op tombstone: see the replay test below for why it is kept.
    assert deletes == [temp]


def test_file_stored_next_to_a_failed_one_is_still_forgotten(fake):
    x = fake.put("/x.md", "stored next to a file that failed")
    y = fake.put("/y.md", "fails to download")
    fake.download_errors[y] = ConnectionError("timeout")
    state: dict = {}
    upserts, _, _ = sync(fake, state)
    assert x in upserts and state["retry"] == [y]  # the cursor moved, y waits by id

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
    fake.download_errors[flaky] = ConnectionError("timeout")
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
    fake.download_errors[b] = ConnectionError("timeout")

    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {a}
    assert stats["failed"] == 1
    assert state["retry"] == [b]
    assert "cursors" in state  # the cursor moves on; b waits by id

    del fake.download_errors[b]
    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {b}
    assert stats["failed"] == 0
    assert state["retry"] == []
    assert set(state["index"].values()) == {a, b}


def test_a_file_that_keeps_failing_does_not_grow_later_runs(fake):
    state: dict = {}
    sync(fake, state)
    bad = fake.put("/bad.md", "never downloads")
    fake.download_errors[bad] = ConnectionError("timeout")

    downloads_per_run = []
    for run in range(4):
        fake.put(f"/new{run}.md", f"new file {run}")
        fake.downloads.clear()
        _, _, stats = sync(fake, state)
        downloads_per_run.append(len(fake.downloads))
        assert stats["failed"] == 1
        assert state["retry"] == [bad]

    # The new file plus one more try of the bad one, every run.
    assert downloads_per_run == [2, 2, 2, 2]


def test_retried_file_deleted_in_the_meantime_is_dropped(fake):
    a = fake.put("/a.md", "alpha")
    fake.download_errors[a] = ConnectionError("timeout")
    state: dict = {}
    sync(fake, state)
    assert state["retry"] == [a]

    fake.delete("/a.md")
    upserts, deletes, _ = sync(fake, state)

    assert upserts == {}
    assert deletes == [a]
    assert state["retry"] == []


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
    assert state["retry"] == [b, c]

    fake.downloads.clear()
    upserts, _, stats = sync(fake, state)

    assert set(upserts) == {b, c}
    assert fake.downloads == [b, c]
    assert state["retry"] == []


def test_rate_limit_that_never_ends_gives_up_after_a_few_short_waits(fake, waits):
    a = fake.put("/a.md", "alpha")
    fake.download_errors[a] = RateLimitError("request-id", None, 1)
    state: dict = {}

    upserts, _, stats = sync(fake, state)

    assert upserts == {}
    assert waits == [1, 1, 1]
    assert stats["failed_rate_limit"] == 1
    assert state["retry"] == [a]


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
    assert state["retry"] == [b, c]

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
    fake.download_errors[offline] = ConnectionError("timeout for /Secret Plans/offline.md")
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
