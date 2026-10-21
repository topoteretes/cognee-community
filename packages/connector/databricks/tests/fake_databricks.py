"""An in-memory Databricks workspace that answers the connector's requests.

It stands in for ``_DatabricksClient`` (same ``get`` and ``pages`` methods) and
mimics what a real Free Edition workspace showed: a folder listing is one level
deep and leaves ``objects`` out when the folder is empty, editing a notebook
keeps its object_id and moves its modified_at, a missing path is a 404 with
code RESOURCE_DOES_NOT_EXIST, and an export in SOURCE format is base64 text.
"""

import base64
import itertools

from cognee_community_connector_databricks import DatabricksNotFoundError

HOME = "/Users/someone@example.com"


def source(*cells, comment="#"):
    """A SOURCE export with these cells, in a language's comment syntax."""
    body = f"\n\n{comment} COMMAND ----------\n\n".join(cells)
    return f"{comment} Databricks notebook source\n{body}\n"


class FakeWorkspace:
    def __init__(self, workspace_id="1111"):
        self.workspace_id = workspace_id
        self.objects: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.failing: dict[str, Exception] = {}
        self._ids = itertools.count(1000)
        self._clock = itertools.count(1_000_000)
        self.folder(HOME)

    # -- building a workspace -------------------------------------------------
    def folder(self, path, kind="DIRECTORY"):
        parent = path.rsplit("/", 1)[0]
        if parent and parent not in self.objects and parent != "/Users":
            self.folder(parent)
        self.objects.setdefault(path, {"object_type": kind, "object_id": next(self._ids)})
        return self.objects[path]["object_id"]

    def notebook(self, path, text, language="PYTHON"):
        self.folder(path.rsplit("/", 1)[0])
        self.objects[path] = {
            "object_type": "NOTEBOOK",
            "object_id": next(self._ids),
            "language": language,
            "modified_at": next(self._clock),
            "source": text,
        }
        return str(self.objects[path]["object_id"])

    def other(self, path, kind):
        self.folder(path.rsplit("/", 1)[0])
        self.objects[path] = {"object_type": kind, "object_id": next(self._ids)}

    def edit(self, path, text):
        self.objects[path].update(source=text, modified_at=next(self._clock))

    def move(self, old, new):
        """A move or rename in the UI: same object, new path."""
        self.folder(new.rsplit("/", 1)[0])
        self.objects[new] = self.objects.pop(old)

    def delete(self, path):
        for key in [k for k in self.objects if k == path or k.startswith(f"{path}/")]:
            del self.objects[key]

    # -- the client interface ---------------------------------------------------
    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, params))
        target = params.get("path", "")
        for failing, error in self.failing.items():
            if target == failing or (path.endswith(failing)):
                raise error
        if path == "/api/2.0/workspace/list":
            if target not in self.objects or self.objects[target]["object_type"] == "NOTEBOOK":
                raise _not_found()
            children = [
                self._status(p) for p in sorted(self.objects) if p.rsplit("/", 1)[0] == target
            ]
            # A real empty folder answers {}.
            return {"objects": children} if children else {}
        if path == "/api/2.0/workspace/get-status":
            if target not in self.objects:
                raise _not_found()
            return self._status(target)
        if path == "/api/2.0/workspace/export":
            assert params.get("format") == "SOURCE", "only SOURCE exports carry no outputs"
            obj = self.objects.get(target)
            if not obj or obj["object_type"] != "NOTEBOOK":
                raise _not_found()
            return {"content": base64.b64encode(obj["source"].encode()).decode(), "file_type": "py"}
        raise AssertionError(f"unexpected request {path}")

    def exports(self):
        return [params["path"] for path, params in self.calls if path.endswith("/export")]

    def _status(self, path):
        obj = self.objects[path]
        status = {"object_type": obj["object_type"], "path": path, "object_id": obj["object_id"]}
        if obj["object_type"] == "NOTEBOOK":
            status.update(language=obj["language"], modified_at=obj["modified_at"])
        return status


def _not_found():
    return DatabricksNotFoundError("Databricks says this does not exist (RESOURCE_DOES_NOT_EXIST).")
