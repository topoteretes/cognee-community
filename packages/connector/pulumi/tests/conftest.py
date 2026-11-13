"""In-memory stand-in for the Pulumi Cloud REST API, shared by the test modules.

Answers follow the shapes in Pulumi's OpenAPI spec: the stack listing pages with
``continuationToken`` and carries no tags, a stack's own endpoint carries its tags
and ``config``, and the update history pages newest first. Updates carry ``config``
and ``environment`` with planted secrets, which must never reach a document.
"""

import re

import pytest

from cognee_community_connector_pulumi.pulumi import (
    PulumiAPIError,
    PulumiAuthError,
    PulumiRateLimitedError,
)

ORG = "acme"
NOW = 1_793_000_000  # 2026-10-26, as a Unix timestamp
SECRET_CONFIG = "config-secret-0f1e2d"
SECRET_ENV = "env-secret-9a8b7c"
USER = {"name": "Priya Shah", "githubLogin": "priya", "avatarUrl": "https://example.test/a.png"}


class FakePulumi:
    """Implements ``get`` like Pulumi Cloud. ``page_size`` keeps listing pages small."""

    def __init__(self, page_size=2):
        self.page_size = page_size
        self.stacks: dict[str, dict] = {}
        self.tags: dict[str, dict] = {}
        self.updates: dict[str, list[dict]] = {}
        self.errors: dict[str, int] = {}
        self.rate_limited: set[str] = set()
        self.bad_token = False
        self.calls: list[tuple[str, dict]] = []
        self.clock = NOW - 3600

    def add_stack(self, project, name, *, org=ORG, tags=None, resources=0):
        stack_id = f"id-{project}-{name}" if org == ORG else f"id-{org}-{project}-{name}"
        self.stacks[stack_id] = {
            "id": stack_id,
            "orgName": org,
            "projectName": project,
            "stackName": name,
            "resourceCount": resources,
            "links": {"self": f"https://app.pulumi.com/{org}/{project}/{name}"},
        }
        self.tags[stack_id] = {"pulumi:project": project, **(tags or {})}
        self.updates[stack_id] = []
        return stack_id

    def run(
        self,
        stack_id,
        kind="update",
        result="succeeded",
        *,
        message="",
        changes=None,
        start=None,
        commit=None,
        resources=None,
        checkpoint=True,
    ):
        """Record an update the way the CLI would, and move the stack's lastUpdate.

        As live: lastUpdate is 0 while an update runs and its start time once it has
        finished, and an update that saved no checkpoint leaves it alone.
        """
        self.clock += 60
        version = len(self.updates[stack_id]) + 1
        start = start if start is not None else self.clock
        update = {
            "version": version,
            "latestVersion": version,
            "updateID": f"u-{stack_id}-{version}",
            "requestedBy": USER,
            "requestedByToken": "pul-requested-by-token-secret",
            "policyPacks": [
                {
                    "name": "p",
                    "displayName": "P",
                    "version": 1,
                    "versionTag": "1",
                    "config": {"key": SECRET_CONFIG},
                }
            ],
            "info": {
                "kind": kind,
                "result": result,
                "message": message,
                "startTime": start,
                "endTime": start + 30 if result in ("succeeded", "failed") else 0,
                "version": version,
                "resourceChanges": changes if changes is not None else {"create": 1},
                "resourceCount": resources if resources is not None else 1,
                "config": {"db:password": {"string": SECRET_CONFIG, "secret": False}},
                "environment": {"AWS_SECRET_ACCESS_KEY": SECRET_ENV},
            },
        }
        if commit:
            update["githubCommitInfo"] = commit
        self.updates[stack_id].append(update)
        stack = self.stacks[stack_id]
        if checkpoint:
            stack["lastUpdate"] = start if result in ("succeeded", "failed") else 0
            if resources is not None:
                stack["resourceCount"] = resources
        return update

    def finish(self, stack_id, version, result="succeeded", checkpoint=True):
        info = self.updates[stack_id][version - 1]["info"]
        info["result"] = result
        info["endTime"] = info["startTime"] + 30
        if checkpoint:
            self.stacks[stack_id]["lastUpdate"] = info["startTime"]

    def remove(self, stack_id):
        self.stacks.pop(stack_id)

    def _fail(self, path):
        if self.bad_token:
            raise PulumiAuthError("Pulumi rejected the access token (HTTP 401)")
        if any(key in path for key in self.rate_limited):
            raise PulumiRateLimitedError("Pulumi rate limit exceeded (HTTP 429)")
        for key, status in self.errors.items():
            if key in path:
                if status == 401:
                    raise PulumiAuthError("Pulumi rejected the access token (HTTP 401)")
                raise PulumiAPIError(f"Pulumi request failed: HTTP {status}", status=status)

    def _by_path(self, org, project, name):
        for stack_id, stack in self.stacks.items():
            if (stack["orgName"], stack["projectName"], stack["stackName"]) == (org, project, name):
                return stack_id
        raise PulumiAPIError("Pulumi request failed: HTTP 404", status=404)

    def _tag_matches(self, stack_id, params):
        if "tagName" not in params:
            return True
        tags = self.tags[stack_id]
        if params["tagName"] not in tags:
            return False
        return "tagValue" not in params or tags[params["tagName"]] == params["tagValue"]

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        self._fail(path)
        if path == "/api/user/stacks":
            listed = [
                s
                for s in self.stacks.values()
                if s["orgName"] == params.get("organization", s["orgName"])
                and s["projectName"] == params.get("project", s["projectName"])
                and self._tag_matches(s["id"], params)
            ]
            listed.sort(key=lambda s: s["id"])
            start = int(params.get("continuationToken") or 0)
            page = [dict(s) for s in listed[start : start + self.page_size]]
            body = {"stacks": page}
            if start + self.page_size < len(listed):
                body["continuationToken"] = str(start + self.page_size)
            return body
        match = re.fullmatch(r"/api/stacks/([^/]+)/([^/]+)/([^/]+)(/updates(?:/(\d+))?)?", path)
        if not match:
            raise AssertionError(f"unexpected path {path}")
        org, project, name, history, version = match.groups()
        stack_id = self._by_path(org, project, name)
        if history is None:
            return {
                "id": stack_id,
                "orgName": org,
                "projectName": project,
                "stackName": name,
                "tags": dict(self.tags[stack_id]),
                "config": {"secretsProvider": "service", "environment": SECRET_CONFIG},
                "version": len(self.updates[stack_id]),
            }
        updates = self.updates[stack_id]
        if version is not None:
            if not 1 <= int(version) <= len(updates):
                raise PulumiAPIError("Pulumi request failed: HTTP 404", status=404)
            return dict(updates[int(version) - 1])
        assert params.get("output-type"), "the paginated format is asked for"
        newest = sorted(updates, key=lambda u: u["version"], reverse=True)
        size = int(params["pageSize"])
        page = int(params["page"])
        chunk = newest[(page - 1) * size : page * size]
        return {"updates": [dict(u) for u in chunk], "itemsPerPage": size, "total": len(newest)}

    def paths(self, prefix=""):
        return [p for p, _ in self.calls if p.startswith(prefix)]


@pytest.fixture
def pulumi():
    return FakePulumi()
