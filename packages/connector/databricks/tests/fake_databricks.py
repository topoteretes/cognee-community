"""An in-memory Databricks workspace that answers the connector's requests.

It stands in for ``_DatabricksClient`` (same ``get`` and ``pages`` methods) and
mimics what a real Free Edition workspace showed: a folder listing is one level
deep and leaves ``objects`` out when the folder is empty, editing a notebook
keeps its object_id and moves its modified_at, a missing path is a 404 with
code RESOURCE_DOES_NOT_EXIST, and an export in SOURCE format is base64 text.

Unity Catalog works the same way: a table listing carries columns, comment,
owner and view definition but no keys, which only the table's detail has; a
rename keeps the table_id; updated_at moves on metadata changes only; and the
listing answers {} for an empty schema.

Lineage is recorded per table, like the lineage API answers it: a table's
upstream entries name the tables it was built from and the notebooks that wrote
it, its downstream entries the tables it feeds and the notebooks that read it,
with no table at all for a notebook that only read it. Lineage for a table that
does not exist is a 403 PERMISSION_DENIED, as on a real workspace.
"""

import base64
import itertools

from cognee_community_connector_databricks import (
    DatabricksNotFoundError,
    DatabricksPermissionError,
)

UC = "/api/2.1/unity-catalog"

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
        # Unity Catalog: schemas by full name, tables by full name.
        self.schemas: dict[str, dict] = {}
        self.tables: dict[str, dict] = {}
        self.lineage: dict[str, dict] = {}

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

    def schema(self, full_name, comment=""):
        self.schemas[full_name] = {
            "name": full_name.split(".")[1],
            "full_name": full_name,
            "comment": comment,
        }

    def table(self, full_name, columns, comment="", primary_key=(), foreign_keys=(), view=None):
        """A table (or a view, given its SQL), with (name, type, comment) columns."""
        self.schema(
            full_name.rsplit(".", 1)[0],
            self.schemas.get(full_name.rsplit(".", 1)[0], {}).get("comment", ""),
        )
        constraints = []
        if primary_key:
            constraints.append(
                {"primary_key_constraint": {"name": "pk", "child_columns": list(primary_key)}}
            )
        for children, parent, parents in foreign_keys:
            constraints.append(
                {
                    "foreign_key_constraint": {
                        "name": "fk",
                        "child_columns": list(children),
                        "parent_table": parent,
                        "parent_columns": list(parents),
                    }
                }
            )
        self.tables[full_name] = {
            "full_name": full_name,
            "name": full_name.rsplit(".", 1)[1],
            "table_id": f"tbl-{next(self._ids)}",
            "table_type": "VIEW" if view else "MANAGED",
            "comment": comment,
            "owner": "someone@example.com",
            "columns": [
                {"name": n, "type_text": t, "comment": c, "nullable": n not in primary_key}
                for n, t, c in columns
            ],
            "view_definition": view,
            "table_constraints": constraints,
            "updated_at": next(self._clock),
            # Real listings carry statistics with values from the data.
            "properties": {"spark.sql.statistics.colStats.amount.max": "12000.00"},
        }
        return self.tables[full_name]["table_id"]

    def alter(self, full_name, **changes):
        """A metadata change, like COMMENT ON: updated_at moves."""
        self.tables[full_name].update(changes, updated_at=next(self._clock))

    def rename_table(self, old, new):
        table = self.tables.pop(old)
        table.update(full_name=new, name=new.rsplit(".", 1)[1], updated_at=next(self._clock))
        self.tables[new] = table

    def drop(self, full_name):
        del self.tables[full_name]

    def drop_schema(self, full_name):
        del self.schemas[full_name]
        for name in [t for t in self.tables if t.startswith(f"{full_name}.")]:
            del self.tables[name]

    def ran(self, notebook, reads=(), writes=(), workspace_id=None):
        """A notebook run: lineage from what it read to what it wrote.

        ``notebook`` is a workspace path, or an object_id for a notebook that is
        not in the fake (one outside the synced folders).
        """
        object_id = self.objects[notebook]["object_id"] if notebook in self.objects else notebook
        info = {
            "workspace_id": int(workspace_id or self.workspace_id),
            "notebook_id": int(object_id),
            "lineage_timestamp": "2026-10-21 16:18:04.0",
        }
        for written in writes:
            for read in reads:
                self._lineage(written, "upstreams", read, info)
                self._lineage(read, "downstreams", written, info)
        if not writes:
            for read in reads:
                self._lineage(read, "downstreams", None, info)

    def _lineage(self, table, direction, other, notebook):
        entry = {"notebookInfos": [notebook]}
        if other:
            catalog, schema, name = other.split(".")
            entry["tableInfo"] = {
                "name": name,
                "catalog_name": catalog,
                "schema_name": schema,
                "table_type": "TABLE",
                "lineage_timestamp": notebook["lineage_timestamp"],
            }
        self.lineage.setdefault(table, {}).setdefault(direction, []).append(entry)

    # -- the client interface ---------------------------------------------------
    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, params))
        target = params.get("path", "")
        for failing, error in self.failing.items():
            if target == failing or (path.endswith(failing)):
                raise error
        if path == "/api/2.0/preview/scim/v2/Me":
            return {"userName": HOME.rsplit("/", 1)[1], "active": True}
        if path == "/api/2.0/workspace/get-status" and target == "/":
            return {"object_type": "DIRECTORY", "path": "/", "object_id": 0}
        if path == "/api/2.0/lineage-tracking/table-lineage":
            name = params["table_name"]
            if name not in self.tables:
                raise DatabricksPermissionError(
                    "Databricks does not let this user read it (HTTP 403, PERMISSION_DENIED)."
                )
            return self.lineage.get(name, {})
        if path.startswith(f"{UC}/tables/"):
            name = path[len(f"{UC}/tables/") :]
            if name not in self.tables:
                raise _not_found("TABLE_DOES_NOT_EXIST")
            return self.tables[name]
        if path.startswith(f"{UC}/schemas/"):
            name = path[len(f"{UC}/schemas/") :]
            if name not in self.schemas:
                raise _not_found("SCHEMA_DOES_NOT_EXIST")
            return self.schemas[name]
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

    def pages(self, path, params, items_key):
        self.calls.append((path, params))
        for failing, error in self.failing.items():
            if path.endswith(failing) or failing in params.values():
                raise error
        if path == f"{UC}/schemas":
            catalog = params["catalog_name"]
            if not any(name.startswith(f"{catalog}.") for name in self.schemas):
                raise _not_found("CATALOG_DOES_NOT_EXIST")
            schemas = [s for n, s in sorted(self.schemas.items()) if n.startswith(f"{catalog}.")]
            return iter([*schemas, {"name": "information_schema", "comment": "auto"}])
        if path == f"{UC}/tables":
            schema = f"{params['catalog_name']}.{params['schema_name']}"
            if schema not in self.schemas:
                raise _not_found("SCHEMA_DOES_NOT_EXIST")
            # Like the real listing: no keys, which only the detail carries.
            return iter(
                [
                    {k: v for k, v in t.items() if k != "table_constraints"}
                    for n, t in sorted(self.tables.items())
                    if n.rsplit(".", 1)[0] == schema
                ]
            )
        raise AssertionError(f"unexpected listing {path}")

    def details_read(self):
        """Tables whose detail (with keys) was read, in order."""
        return [p[len(f"{UC}/tables/") :] for p, _ in self.calls if p.startswith(f"{UC}/tables/")]

    def exports(self):
        return [params["path"] for path, params in self.calls if path.endswith("/export")]

    def _status(self, path):
        obj = self.objects[path]
        status = {"object_type": obj["object_type"], "path": path, "object_id": obj["object_id"]}
        if obj["object_type"] == "NOTEBOOK":
            status.update(language=obj["language"], modified_at=obj["modified_at"])
        return status


def _not_found(code="RESOURCE_DOES_NOT_EXIST"):
    return DatabricksNotFoundError(f"Databricks says this does not exist ({code}).")
