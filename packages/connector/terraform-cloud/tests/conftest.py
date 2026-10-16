"""In-memory stand-in for the HCP Terraform API (JSON:API), shared by the test modules."""

import pytest

from cognee_community_connector_terraform_cloud.terraform_cloud import TerraformCloudAPIError

ORG = "acme"
_OPERATION_FLAGS = {
    "plan_only": {"plan-only": True},
    "destroy": {"is-destroy": True},
    "refresh_only": {"refresh-only": True},
    "save_plan": {"save-plan": True},
    "plan_and_apply": {},
}


class FakeTerraformCloud:
    """Implements ``get`` and ``download`` like HCP Terraform, runs newest first."""

    def __init__(self):
        self.workspaces: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.operations: dict[str, str] = {}
        self.logs: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.downloads: list[str] = []
        self.errors: dict[str, int] = {}
        self.unordered = False

    def add_workspace(self, workspace_id, name, *, tags=(), **attributes):
        self.workspaces[workspace_id] = {
            "id": workspace_id,
            "type": "workspaces",
            "attributes": {
                "name": name,
                "tag-names": list(tags),
                "terraform-version": "1.9.4",
                "execution-mode": "remote",
                "resource-count": 10,
                "locked": False,
                **attributes,
            },
        }

    def add_run(
        self,
        workspace_id,
        run_id,
        created,
        *,
        status="applied",
        operation="plan_and_apply",
        message="Triggered via UI",
        plan_log=None,
        apply_log=None,
        commit=None,
    ):
        attributes = {
            "status": status,
            "created-at": created,
            "message": message,
            "source": "tfe-ui",
            "trigger-reason": "manual",
            **_OPERATION_FLAGS[operation],
        }
        self.operations[run_id] = operation
        plan_status = "running" if status in ("planning", "pending") else "finished"
        if status == "errored":
            plan_status = "errored" if apply_log is None else "finished"
        plan = {
            "id": f"plan-{run_id}",
            "type": "plans",
            "attributes": {
                "status": plan_status,
                "resource-additions": 2,
                "resource-changes": 1,
                "resource-destructions": 0,
                "log-read-url": f"https://archivist.test/plan-{run_id}",
            },
        }
        # An errored run failed in the apply only when an apply log is given.
        apply_status = "unreachable"
        if status == "applied":
            apply_status = "finished"
        elif status == "errored" and apply_log is not None:
            apply_status = "errored"
        apply = {
            "id": f"apply-{run_id}",
            "type": "applies",
            "attributes": {
                "status": apply_status,
                "resource-additions": 2,
                "resource-changes": 1,
                "resource-destructions": 0,
                "log-read-url": f"https://archivist.test/apply-{run_id}",
            },
        }
        self.logs[plan["attributes"]["log-read-url"]] = plan_log or ""
        self.logs[apply["attributes"]["log-read-url"]] = apply_log or ""
        relationships = {
            "workspace": {"data": {"id": workspace_id, "type": "workspaces"}},
            "plan": {"data": {"id": plan["id"], "type": "plans"}},
            "apply": {"data": {"id": apply["id"], "type": "applies"}},
        }
        included = [plan, apply]
        if commit:
            version = {
                "id": f"cv-{run_id}",
                "type": "configuration-versions",
                "relationships": {
                    "ingress-attributes": {
                        "data": {"id": f"ia-{run_id}", "type": "ingress-attributes"}
                    }
                },
            }
            ingress = {"id": f"ia-{run_id}", "type": "ingress-attributes", "attributes": commit}
            relationships["configuration-version"] = {
                "data": {"id": version["id"], "type": "configuration-versions"}
            }
            included += [version, ingress]
        self.runs[run_id] = {
            "data": {
                "id": run_id,
                "type": "runs",
                "attributes": attributes,
                "relationships": relationships,
            },
            "included": included,
        }

    def _page(self, items, params, included=None):
        size, number = int(params["page[size]"]), int(params["page[number]"])
        chunk = items[(number - 1) * size : number * size]
        more = number * size < len(items)
        document = {"data": chunk, "links": {"next": f"page {number + 1}" if more else None}}
        if included is not None:
            document["included"] = included
        return document

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        for key, status in self.errors.items():
            if key in path:
                raise TerraformCloudAPIError(f"HTTP {status}", status=status)
        if path == f"/organizations/{ORG}":
            return {"data": {"id": ORG, "type": "organizations"}}
        if path.startswith("/organizations/") and path.count("/") == 2:
            raise TerraformCloudAPIError("HTTP 404", status=404)
        if path == f"/organizations/{ORG}/workspaces":
            items = list(self.workspaces.values())
            if "search[tags]" in params:
                tags = set(params["search[tags]"].split(","))
                items = [w for w in items if tags <= set(w["attributes"]["tag-names"])]
            return self._page(items, params)
        if path == f"/organizations/{ORG}/runs":
            operations = set(params["filter[operation]"].split(","))
            names = params.get("filter[workspace_names]")
            runs = [
                doc
                for run_id, doc in self.runs.items()
                if self.operations[run_id] in operations
                and (
                    names is None
                    or self.workspaces.get(
                        doc["data"]["relationships"]["workspace"]["data"]["id"], {}
                    )
                    .get("attributes", {})
                    .get("name")
                    in names.split(",")
                )
            ]
            runs.sort(
                key=lambda d: d["data"]["attributes"]["created-at"], reverse=not self.unordered
            )
            page = self._page([d["data"] for d in runs], params)
            ids = {r["id"] for r in page["data"]}
            page["included"] = [i for d in runs if d["data"]["id"] in ids for i in d["included"]]
            return page
        if path.startswith("/runs/"):
            run_id = path.split("/")[2]
            if run_id not in self.runs:
                raise TerraformCloudAPIError("HTTP 404", status=404)
            return self.runs[run_id]
        raise AssertionError(f"unexpected path {path}")

    def download(self, url):
        self.downloads.append(url)
        return self.logs[url]

    def run_list_calls(self):
        return [p for path, p in self.calls if path.endswith("/runs")]


@pytest.fixture
def tfc():
    return FakeTerraformCloud()
