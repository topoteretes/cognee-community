"""In-memory Figma REST API stand-in for the Figma connector tests.

``FakeFigma`` is mounted on a real ``requests.Session`` as a transport
adapter, so the connector's whole HTTP stack runs unchanged: dlt's bearer /
API-key auth, ``rest_api`` parent/child resolution, ``RESTClient`` and the
``response_actions`` hooks. Only the network is replaced.

It mirrors the Figma behaviour the connector relies on:

* folders (formerly "projects") nest; ``/v2/teams/:id/folders`` lists only
  top-level folders and ``/v2/folders/:id/folders`` the direct subfolders;
* ``/v2/folders/:id/files`` lists only the files directly in a folder, with
  a ``last_modified`` stamp that moves on every edit (geometry included) but
  not when a comment is added;
* renaming a file or folder does not move any file's ``last_modified`` stamp;
* a deleted file or folder answers 404 and drops out of every listing;
* comments are one flat list per file; replies point at their root through
  ``parent_id``.
"""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from requests.adapters import BaseAdapter

FIGMA_HOST = "api.figma.com"
TOKEN = "figma-test-token"


def text(characters: str, name: str | None = None, **extra) -> dict:
    """A TEXT layer."""
    return {"type": "TEXT", "name": name or characters[:20], "characters": characters, **extra}


def frame(name: str, *children: dict, node_type: str = "FRAME", **extra) -> dict:
    """A container layer (FRAME by default) holding ``children``."""
    return {"type": node_type, "name": name, "children": list(children), **extra}


class FakeFigma(BaseAdapter):
    """Serves the handful of Figma endpoints the connector reads."""

    def __init__(self):
        super().__init__()
        self.teams: dict[str, dict] = {}
        self.folders: dict[str, dict] = {}
        self.files: dict[str, dict] = {}
        self.comments: dict[str, list[dict]] = {}
        self.requests: list[str] = []
        # (path substring, status, headers): served once instead of the real
        # response, to simulate throttling or outages.
        self.failures: list[tuple[str, int, dict]] = []
        self._clock = datetime(2026, 1, 1, tzinfo=UTC)
        self._next_id = 100

    # -- fixture builders --------------------------------------------------

    def session(self) -> requests.Session:
        """A plain session routed to this fake (no retries)."""
        session = requests.Session()
        session.mount(f"https://{FIGMA_HOST}/", self)
        return session

    def add_team(self, team_id: str, name: str = "") -> None:
        self.teams[team_id] = {"id": team_id, "name": name or team_id}

    def add_folder(self, team_id: str, folder_id: str, name: str, parent: str | None = None):
        self.folders[folder_id] = {
            "id": folder_id,
            "name": name,
            "parent_folder_id": parent,
            "team_id": team_id,
        }

    def remove_folder(self, folder_id: str) -> None:
        self.folders.pop(folder_id)
        for key in [key for key, file in self.files.items() if file["folder_id"] == folder_id]:
            self.delete_file(key)

    def add_file(self, folder_id: str | None, key: str, name: str, pages: dict, editor="figma"):
        """Add a file; ``pages`` maps a page name to its top-level layers."""
        self.files[key] = {
            "key": key,
            "name": name,
            "folder_id": folder_id,
            "editorType": editor,
            "version": "1",
            "last_modified": self._tick(),
            "pages": pages,
        }
        self.comments.setdefault(key, [])

    def edit(self, key: str, pages: dict | None = None, name: str | None = None) -> None:
        """Change a file's layers and/or name: bumps version and last_modified."""
        file = self.files[key]
        if pages is not None:
            file["pages"] = pages
        if name is not None:
            file["name"] = name
        file["version"] = str(int(file["version"]) + 1)
        file["last_modified"] = self._tick()

    def rename(self, key: str, name: str) -> None:
        """Rename a file without moving its ``last_modified`` stamp."""
        self.files[key]["name"] = name

    def rename_folder(self, folder_id: str, name: str) -> None:
        self.folders[folder_id]["name"] = name

    def touch(self, key: str) -> None:
        """A geometry-only edit (moving a layer): stamp moves, text does not."""
        self.edit(key)

    def delete_file(self, key: str) -> None:
        self.files.pop(key)
        self.comments.pop(key, None)

    def comment(self, key, message, author="Ada", parent=None, node_id=None) -> str:
        """Post a comment (or a reply when ``parent`` is set); returns its id."""
        self._next_id += 1
        comment_id = str(self._next_id)
        roots = [c for c in self.comments[key] if not c.get("parent_id")]
        self.comments[key].append(
            {
                "id": comment_id,
                "file_key": key,
                "parent_id": parent or "",
                "user": {"id": f"user-{author}", "handle": author, "img_url": ""},
                "created_at": self._tick(),
                "resolved_at": None,
                "message": message,
                "order_id": None if parent else str(len(roots) + 1),
                "client_meta": {"node_id": node_id, "node_offset": {"x": 0, "y": 0}}
                if node_id
                else {"x": 10, "y": 20},
                "reactions": [],
            }
        )
        return comment_id

    def resolve(self, key: str, comment_id: str) -> None:
        self._find_comment(key, comment_id)["resolved_at"] = self._tick()

    def delete_comment(self, key: str, comment_id: str) -> None:
        self.comments[key] = [
            c for c in self.comments[key] if comment_id not in (c["id"], c.get("parent_id"))
        ]

    def fail_once(self, path_fragment: str, status: int, headers: dict | None = None) -> None:
        self.failures.append((path_fragment, status, headers or {}))

    def file_requests(self) -> list[str]:
        """Node-tree fetches (``GET /v1/files/:key``) seen so far."""
        return [
            path for path in self.requests if path.startswith("/v1/files/") and path.count("/") == 3
        ]

    # -- transport ---------------------------------------------------------

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        auth_ok = request.headers.get("Authorization") == f"Bearer {TOKEN}" or (
            request.headers.get("X-Figma-Token") == TOKEN
        )
        if not auth_ok:
            return self._json(request, 403, {"status": 403, "err": "Invalid token"})

        path = unquote(url.path)
        self.requests.append(path)
        for index, (fragment, status, headers) in enumerate(self.failures):
            if fragment in path:
                del self.failures[index]
                return self._json(request, status, {"status": status, "err": "fail"}, headers)

        parts = path.strip("/").split("/")
        match parts:
            case ["v1", "me"]:
                return self._json(request, 200, {"id": "user-1", "handle": "Ada", "email": "a@x"})
            case ["v2", "teams", team_id, "folders"]:
                return self._team_folders(request, team_id)
            case ["v2", "folders", folder_id, "folders"]:
                return self._subfolders(request, folder_id)
            case ["v2", "folders", folder_id, "files"]:
                return self._folder_files(request, folder_id)
            case ["v1", "files", key, "meta"]:
                return self._file_meta(request, key)
            case ["v1", "files", key, "comments"]:
                assert query.get("as_md") == "true", "comments should be read as markdown"
                return self._file_comments(request, key)
            case ["v1", "files", key]:
                assert "geometry" not in query, "vector geometry must not be requested"
                return self._file(request, key)
        return self._not_found(request)

    def close(self):
        pass

    # -- endpoints ---------------------------------------------------------

    def _team_folders(self, request, team_id):
        team = self.teams.get(team_id)
        if team is None:
            return self._not_found(request)
        folders = [
            self._folder_view(f)
            for f in self.folders.values()
            if f["team_id"] == team_id and f["parent_folder_id"] is None
        ]
        return self._json(request, 200, {"name": team["name"], "folders": folders})

    def _subfolders(self, request, folder_id):
        parent = self.folders.get(folder_id)
        if parent is None:
            return self._not_found(request)
        folders = [
            self._folder_view(f)
            for f in self.folders.values()
            if f["parent_folder_id"] == folder_id
        ]
        return self._json(request, 200, {"name": parent["name"], "folders": folders})

    def _folder_files(self, request, folder_id):
        folder = self.folders.get(folder_id)
        if folder is None:
            return self._not_found(request)
        files = [
            {
                "key": f["key"],
                "name": f["name"],
                "thumbnail_url": f"https://thumb/{f['key']}",
                "last_modified": f["last_modified"],
            }
            for f in self.files.values()
            if f["folder_id"] == folder_id
        ]
        return self._json(request, 200, {"name": folder["name"], "files": files})

    def _file_meta(self, request, key):
        file = self.files.get(key)
        if file is None:
            return self._not_found(request)
        folder = self.folders.get(file["folder_id"] or "") or {}
        meta = {
            "name": file["name"],
            "folder_name": folder.get("name"),
            "last_touched_at": file["last_modified"],
            "editorType": file["editorType"],
        }
        return self._json(request, 200, {"file": meta})

    def _file(self, request, key):
        file = self.files.get(key)
        if file is None:
            return self._not_found(request)
        counter = iter(range(1, 1_000_000))
        pages = [
            {
                "id": f"{index}:0",
                "type": "CANVAS",
                "name": page_name,
                "children": [_with_ids(node, counter) for node in nodes],
            }
            for index, (page_name, nodes) in enumerate(file["pages"].items())
        ]
        body = {
            "name": file["name"],
            "lastModified": file["last_modified"],
            "editorType": file["editorType"],
            "version": file["version"],
            "document": {"id": "0:0", "type": "DOCUMENT", "name": "Document", "children": pages},
        }
        return self._json(request, 200, body)

    def _file_comments(self, request, key):
        if key not in self.files:
            return self._not_found(request)
        return self._json(request, 200, {"comments": [dict(c) for c in self.comments[key]]})

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _folder_view(folder: dict) -> dict:
        return {key: folder[key] for key in ("id", "name", "parent_folder_id")}

    def _find_comment(self, key: str, comment_id: str) -> dict:
        return next(c for c in self.comments[key] if c["id"] == comment_id)

    def _tick(self) -> str:
        self._clock += timedelta(minutes=1)
        return self._clock.isoformat().replace("+00:00", "Z")

    def _not_found(self, request):
        return self._json(request, 404, {"status": 404, "err": "Not found"})

    @staticmethod
    def _json(request, status, payload, headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
        response.headers.update(headers or {})
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response


def _with_ids(node: dict, counter) -> dict:
    """Copy a layer tree, giving every node a stable-per-render ``n:m`` id."""
    copied = {key: value for key, value in node.items() if key != "children"}
    copied.setdefault("id", f"1:{next(counter)}")
    if "children" in node:
        copied["children"] = [_with_ids(child, counter) for child in node["children"]]
    return copied
