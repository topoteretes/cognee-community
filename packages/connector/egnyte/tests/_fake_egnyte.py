"""In-memory stand-in for :class:`EgnyteClient`, shared by the tests.

It models an Egnyte domain as a set of folders plus files keyed by path, and
records an event for every change the way Egnyte's Events API reports them:
one event per file action, and a single event for a folder delete or move
(not one per file inside it).
"""

import posixpath

from cognee_community_connector_egnyte.egnyte import _PAGE_SIZE, EgnyteNotFoundError


class FakeEgnyte:
    def __init__(self):
        self.folders = {"/shared"}
        self.folder_names = {"/shared": "/Shared"}
        self.files = {}  # casefolded path -> metadata dict
        self.content = {}  # group_id -> bytes
        self.events = []
        self.oldest_event_id = 1
        self.calls = []
        self._next_group = 1
        self._next_event = 100

    # -- mutations (each records the matching event) ---------------------------
    def mkdir(self, path):
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            folder = "/" + "/".join(parts[:i])
            self.folders.add(folder.casefold())
            self.folder_names.setdefault(folder.casefold(), folder)

    def put(self, path, content, *, record=True):
        """Create a file, or upload a new version of an existing one."""
        self.mkdir(posixpath.dirname(path))
        existing = self.files.get(path.casefold())
        if existing:
            group_id = existing["group_id"]
        else:
            group_id = f"group-{self._next_group}"
            self._next_group += 1
        data = content.encode("utf-8") if isinstance(content, str) else content
        self.files[path.casefold()] = {
            "name": posixpath.basename(path),
            "path": path,
            "group_id": group_id,
            "entry_id": f"entry-{self._next_event}",
            "size": len(data),
            "is_folder": False,
        }
        self.content[group_id] = data
        if record:
            self._event("create", target_path=path, is_folder=False)
        return group_id

    def delete(self, path):
        meta = self.files.pop(path.casefold())
        self.content.pop(meta["group_id"])
        self._event("delete", target_path=path, is_folder=False)

    def move(self, source, target):
        meta = self.files.pop(source.casefold())
        self.mkdir(posixpath.dirname(target))
        meta.update(path=target, name=posixpath.basename(target))
        self.files[target.casefold()] = meta
        self._event("move", source_path=source, target_path=target, is_folder=False)

    def delete_folder(self, path):
        prefix = path.casefold() + "/"
        for key in [k for k in self.files if k.startswith(prefix)]:
            self.content.pop(self.files.pop(key)["group_id"])
        self.folders = {
            f for f in self.folders if f != path.casefold() and not f.startswith(prefix)
        }
        self._event("delete", target_path=path, is_folder=True)

    def move_folder(self, source, target):
        prefix = source.casefold() + "/"
        for key in [k for k in self.files if k.startswith(prefix)]:
            meta = self.files.pop(key)
            meta["path"] = target + meta["path"][len(source) :]
            self.files[meta["path"].casefold()] = meta
        moved = [f for f in self.folders if f == source.casefold() or f.startswith(prefix)]
        for folder in moved:
            self.folders.discard(folder)
            self.folder_names.pop(folder, None)
            self.mkdir(target + folder[len(source) :])
        self.mkdir(target)
        self._event("move", source_path=source, target_path=target, is_folder=True)

    def _event(self, action, **data):
        self._next_event += 1
        self.events.append(
            {"id": self._next_event, "type": "file_system", "action": action, "data": data}
        )

    # -- EgnyteClient interface ------------------------------------------------
    def get_event_cursor(self):
        self.calls.append(("cursor",))
        return {"latest_event_id": self._next_event, "oldest_event_id": self.oldest_event_id}

    def list_events(self, start_id, folder):
        self.calls.append(("events", start_id, folder))

        def touches(event):
            paths = [event["data"].get("source_path"), event["data"].get("target_path")]
            return any(p and _is_under(p, folder) for p in paths)

        matching = [e for e in self.events if e["id"] > start_id and touches(e)]
        return matching[:_PAGE_SIZE]

    def list_folder(self, path, offset=0):
        self.calls.append(("list", path, offset))
        key = path.casefold()
        if key not in self.folders:
            raise EgnyteNotFoundError(path)
        folders = [
            {"name": posixpath.basename(f), "path": self.folder_names[f], "is_folder": True}
            for f in sorted(self.folders)
            if posixpath.dirname(f) == key and f != key
        ]
        files = [m for k, m in sorted(self.files.items()) if posixpath.dirname(k) == key]
        entries = folders + files
        page = entries[offset : offset + _PAGE_SIZE]
        return {
            "is_folder": True,
            "folders": [e for e in page if e["is_folder"]],
            "files": [dict(e) for e in page if not e["is_folder"]],
        }

    def get_metadata(self, path):
        self.calls.append(("meta", path))
        key = path.casefold()
        if key in self.files:
            return dict(self.files[key])
        if key in self.folders:
            return {"name": posixpath.basename(path), "path": path, "is_folder": True}
        raise EgnyteNotFoundError(path)

    def download(self, path):
        self.calls.append(("download", path))
        meta = self.files.get(path.casefold())
        if meta is None:
            raise EgnyteNotFoundError(path)
        return self.content[meta["group_id"]]

    # -- helpers for assertions --------------------------------------------------
    def downloads(self):
        return [call[1] for call in self.calls if call[0] == "download"]

    def calls_of(self, kind):
        return [call for call in self.calls if call[0] == kind]


def _is_under(path, folder):
    path, folder = path.casefold(), folder.rstrip("/").casefold()
    return path == folder or path.startswith(folder + "/")
