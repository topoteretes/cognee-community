"""Incremental cursor and delete rows, against an in-memory Wrike account.

The fake reproduces what the real API did when tested (see the README):
edits, moves, deletes and restores bump ``updatedDate``; deleted tasks leave
``/tasks`` and are found in the Recycle Bin only with ``descendants=true``; a
task that loses one of two folders to a folder delete keeps its ``updatedDate``.
"""

from datetime import UTC, datetime, timedelta

import pytest

from cognee_community_connector_wrike.wrike import _iter_rows

T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeAccount:
    def __init__(self):
        self.clock = T0 - timedelta(days=1)  # the account exists before the first sync
        self.calls = []
        self.fail_on = None
        self.folders = {}
        self.tasks = {}
        self._folder("ROOT", "Root", "WsRoot", None)
        self._folder("RB", "Recycle Bin", "RbRoot", None)
        self._folder("A", "Alpha", "WsFolder", "ROOT")
        self._folder("A1", "Alpha child", "WsFolder", "A")
        self._folder("B", "Beta", "WsFolder", "ROOT")
        self.add_task("T1", ["A"])
        self.add_task("T2", ["A1"])
        self.add_task("T3", ["B"])
        self.clock = T0

    # -- edits, as a Wrike user would make them -------------------------------
    def tick(self, minutes=60):
        self.clock += timedelta(minutes=minutes)

    def _folder(self, fid, title, scope, parent):
        self.folders[fid] = {
            "id": fid,
            "title": title,
            "scope": scope,
            "childIds": [],
            "updatedDate": stamp(self.clock),
        }
        if parent:
            self.folders[parent]["childIds"].append(fid)

    def add_task(self, tid, parents):
        self.tasks[tid] = {
            "id": tid,
            "title": f"Task {tid}",
            "parentIds": list(parents),
            "superTaskIds": [],
            "scope": "WsTask",
            "status": "Active",
            "updatedDate": stamp(self.clock),
        }

    def touch(self, tid, **changes):
        self.tasks[tid].update(changes, updatedDate=stamp(self.clock))

    def delete_task(self, tid):
        self.touch(tid, scope="RbTask")

    def delete_folder(self, fid):
        folder = self.folders[fid]
        folder.update(scope="RbFolder", updatedDate=stamp(self.clock))
        for task in self.tasks.values():
            if fid in task["parentIds"]:
                if len(task["parentIds"]) == 1:
                    self.touch(task["id"], scope="RbTask")
                else:  # tested: the task loses the parent and keeps its updatedDate
                    task["parentIds"].remove(fid)

    # -- the API ---------------------------------------------------------------
    def _subtree(self, fid):
        out = [fid]
        for child in self.folders[fid]["childIds"]:
            if self.folders[child]["scope"] == "WsFolder":
                out += self._subtree(child)
        return out

    @staticmethod
    def _since(items, params):
        start = (params.get("updatedDate") or {}).get("start")
        return [i for i in items if start is None or i["updatedDate"] >= start]

    def get(self, path, **params):
        self.calls.append((path, params))
        if self.fail_on and self.fail_on in path:
            raise RuntimeError("Wrike went away")
        if path == "/folders":
            return self._since(list(self.folders.values()), params)
        if path in ("/workflows", "/spaces", "/contacts", "/customfields"):
            return []
        if path == "/tasks":
            live = [t for t in self.tasks.values() if t["scope"] == "WsTask"]
            return self._since(live, params)
        if path.startswith("/folders/") and path.endswith("/tasks"):
            fid = path.split("/")[2]
            if fid == "RB":
                found = [t for t in self.tasks.values() if t["scope"] == "RbTask"]
            else:
                scope = set(self._subtree(fid))
                found = [
                    t
                    for t in self.tasks.values()
                    if t["scope"] == "WsTask" and scope & set(t["parentIds"])
                ]
            return self._since(found, params)
        if path.startswith("/folders/"):
            return [self.folders[fid] for fid in path.split("/")[2].split(",")]
        if path.endswith("/comments"):
            return []
        raise AssertionError(path)


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
    assert sync(account, state) == (set(), {"folder:A1", "task:T2"})


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


def test_multi_folder_task_leaving_scope_needs_full_resync(account):
    # T2 sits in A1 and B. Deleting A1 drops that parent without bumping
    # updatedDate, so an incremental run cannot see T2 leave scope A.
    account.tasks["T2"]["parentIds"].append("B")  # in both folders from the start
    state = {}
    sync(account, state, folder_ids=["A"])
    account.delete_folder("A1")
    assert sync(account, state, folder_ids=["A"]) == (set(), {"folder:A1"})
    assert sync(account, state, folder_ids=["A"], full_resync=True)[1] == {"task:T2"}


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
