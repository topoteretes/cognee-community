"""Incremental cursor and delete rows, against the in-memory account in fake_wrike.py."""

from datetime import timedelta

import pytest
from fake_wrike import T0, FakeAccount, stamp

from cognee_community_connector_wrike.wrike import _iter_rows


def sync(account, state, folder_ids=None, full_resync=False):
    rows = list(_iter_rows(account, folder_ids, state, full_resync, now=lambda: account.clock))
    account.tick()
    return {r["id"] for r in rows if not r["_deleted"]}, {r["id"] for r in rows if r["_deleted"]}


@pytest.fixture
def account():
    return FakeAccount()


@pytest.fixture
def state(account):
    state = {}
    sync(account, state)
    return state


def test_first_run_lists_everything_and_saves_the_cursor(account):
    state = {}
    rows, deletes = sync(account, state)
    assert rows == {"folder:A", "folder:A1", "folder:B", "task:T1", "task:T2", "task:T3"}
    assert deletes == set()
    assert state["cursor"] == stamp(T0)
    assert state["roots"] == ["ROOT"]
    assert state["seen"] == sorted(rows)


def test_no_changes_means_nothing_is_synced(account, state):
    account.tick()
    assert sync(account, state) == (set(), set())


def test_only_the_changed_task_is_synced(account, state):
    account.touch("T2", title="Renamed")
    assert sync(account, state) == ({"task:T2"}, set())


def test_queries_reach_back_five_minutes_before_the_cursor(account, state):
    account.calls.clear()
    sync(account, state)
    queries = [p["updatedDate"]["start"] for _, p in account.calls if "updatedDate" in p]
    assert queries and set(queries) == {stamp(T0 - timedelta(minutes=5))}


def test_deleted_task_is_forgotten(account, state):
    account.delete_task("T1")
    assert sync(account, state) == (set(), {"task:T1"})
    assert "task:T1" not in state["seen"]


def test_deleted_folder_forgets_the_folder_and_its_tasks(account, state):
    account.delete_folder("A1")
    # The folders in scope changed, so this is a full run: what remains is
    # re-sent (harmless under merge) and the folder and its task are forgotten.
    rows, deletes = sync(account, state)
    assert deletes == {"folder:A1", "task:T2"}
    assert rows == {"folder:A", "folder:B", "task:T1", "task:T3"}


def test_folder_moved_into_scope_brings_its_tasks(account):
    # Moving a folder does not bump its tasks' updatedDate.
    state = {}
    sync(account, state, folder_ids=["A"])
    account.move_folder("B", "A")
    assert sync(account, state, folder_ids=["A"])[0] >= {"folder:B", "task:T3"}


def test_folder_moved_out_of_scope_forgets_its_tasks(account):
    state = {}
    sync(account, state, folder_ids=["A"])
    account.move_folder("A1", "B")
    assert sync(account, state, folder_ids=["A"])[1] == {"folder:A1", "task:T2"}


def test_subtask_keeps_its_parent_title_when_only_it_changes(account, state):
    account.add_task("SUB", ["ROOT"], super_task="T1")
    first = {r["id"]: r for r in _iter_rows(account, None, state, True, now=lambda: account.clock)}
    account.tick()
    account.touch("SUB", status="Completed")
    later = {r["id"]: r for r in _iter_rows(account, None, state, now=lambda: account.clock)}
    assert set(later) == {"task:SUB"}
    assert "Parent task: Task T1" in later["task:SUB"]["content"]
    assert "Parent task: Task T1" in first["task:SUB"]["content"]


def test_task_moved_out_of_scope_is_forgotten_and_comes_back_when_moved_in(account):
    state = {}
    sync(account, state, folder_ids=["A"])
    account.touch("T1", parentIds=["B"])
    assert sync(account, state, folder_ids=["A"]) == (set(), {"task:T1"})
    account.touch("T1", parentIds=["A1"])
    assert sync(account, state, folder_ids=["A"]) == ({"task:T1"}, set())


def test_changes_outside_scope_produce_no_rows_at_all(account):
    state = {}
    sync(account, state, folder_ids=["A"])
    account.touch("T3", title="Beta work")
    account.delete_task("T3")
    assert sync(account, state, folder_ids=["A"]) == (set(), set())


def test_restored_task_comes_back(account, state):
    account.delete_task("T1")
    sync(account, state)
    account.touch("T1", scope="WsTask")
    assert sync(account, state) == ({"task:T1"}, set())


def test_edit_during_a_sync_is_picked_up_next_time(account, state):
    # The cursor is the time the run started, not when it finished.
    real_get = account.get

    def get_with_concurrent_edit(path, **params):
        if path == "/folders/ROOT/tasks":
            account.tick(1)
            account.touch("T3", title="Edited while syncing")
        return real_get(path, **params)

    account.get = get_with_concurrent_edit
    sync(account, state)
    account.get = real_get
    assert "task:T3" in sync(account, state)[0]


def test_changing_folder_ids_runs_a_full_sync_and_forgets_the_old_scope(account, state):
    rows, deletes = sync(account, state, folder_ids=["B"])
    assert rows == {"folder:B", "task:T3"}
    assert deletes == {"folder:A", "folder:A1", "task:T1", "task:T2"}


def test_multi_folder_task_leaving_scope_with_a_deleted_folder(account):
    # T2 sits in A1 and B. Deleting A1 drops that parent without bumping T2's
    # updatedDate; the change in folders in scope still triggers a full run.
    account.tasks["T2"]["parentIds"].append("B")  # in both folders from the start
    state = {}
    sync(account, state, folder_ids=["A"])
    account.delete_folder("A1")
    assert sync(account, state, folder_ids=["A"])[1] == {"folder:A1", "task:T2"}


def test_full_resync_forgets_a_task_purged_from_the_recycle_bin(account, state):
    account.delete_task("T1")
    del account.tasks["T1"]  # recycle bin emptied before the next sync
    assert sync(account, state) == (set(), set())  # incremental cannot see it
    assert sync(account, state, full_resync=True) == (
        {"folder:A", "folder:A1", "folder:B", "task:T2", "task:T3"},
        {"task:T1"},
    )


def test_failed_run_leaves_state_untouched(account, state):
    before = dict(state)
    account.touch("T1", title="x")
    account.fail_on = "/comments"
    with pytest.raises(RuntimeError):
        sync(account, state)
    assert state == before
