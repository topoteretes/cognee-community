"""An in-memory Wrike account that behaves like the real API did when tested.

Edits, moves, deletes and restores bump ``updatedDate``; deleted tasks leave
``/tasks`` and are found in the Recycle Bin only with ``descendants=true``; a
task that loses one of two folders to a folder delete keeps its ``updatedDate``.
"""

from datetime import UTC, datetime, timedelta

T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
# Left out of task listings unless asked for with ``fields`` (as tested; the
# title is always there).
OPTIONAL_TASK_FIELDS = {"description", "parentIds", "superTaskIds"}


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeAccount:
    def __init__(self, start=T0):
        # Edits are stamped on this clock. Tests that call _iter_rows directly
        # pass it as ``now``; end-to-end tests go through wrike_source, which
        # uses the real clock, so they start the account at the real time.
        self.clock = start - timedelta(days=1)  # the account exists before the first sync
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
        self.clock = start

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

    def add_task(self, tid, parents, description="", super_task=None):
        self.tasks[tid] = {
            "id": tid,
            "title": f"Task {tid}",
            "description": description,
            "parentIds": list(parents),
            "superTaskIds": [super_task] if super_task else [],
            "scope": "WsTask",
            "status": "Active",
            "updatedDate": stamp(self.clock),
        }

    def touch(self, tid, **changes):
        self.tasks[tid].update(changes, updatedDate=stamp(self.clock))

    def delete_task(self, tid):
        self.touch(tid, scope="RbTask")

    def rename_folder(self, fid, title):
        # Only the folder's own updatedDate moves, not its subfolders' or tasks' (tested).
        self.folders[fid].update(title=title, updatedDate=stamp(self.clock))

    def add_folder_parent(self, fid, parent):
        self.folders[parent]["childIds"].append(fid)
        self.folders[fid]["updatedDate"] = stamp(self.clock)

    def move_folder(self, fid, new_parent):
        for folder in self.folders.values():
            if fid in folder["childIds"]:
                folder["childIds"].remove(fid)
        self.folders[new_parent]["childIds"].append(fid)
        self.folders[fid]["updatedDate"] = stamp(self.clock)  # its tasks keep theirs

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
        if path.startswith("/folders/") and path.endswith("/tasks"):
            scope = set(self._subtree(path.split("/")[2]))
            found = [
                t
                for t in self.tasks.values()
                if t["scope"] == "WsTask" and scope & set(t["parentIds"])
            ]
            hidden = OPTIONAL_TASK_FIELDS - set(params.get("fields", []))
            return [
                {k: v for k, v in t.items() if k not in hidden} for t in self._since(found, params)
            ]
        if path.startswith("/folders/"):
            return [self.folders[fid] for fid in path.split("/")[2].split(",")]
        if path.endswith("/comments"):
            return []
        if path.startswith("/tasks/"):
            return [self.tasks[tid] for tid in path.split("/")[2].split(",")]
        raise AssertionError(path)
