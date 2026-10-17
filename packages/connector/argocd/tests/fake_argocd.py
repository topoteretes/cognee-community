"""A hand-rolled stand-in for Argo CD's REST API, shaped like a ``requests`` session.

``FakeArgoSession.get(url, params=..., verify=..., timeout=...)`` is the only method the
connector calls. Application fixtures follow the swagger ``v1alpha1Application`` schema of
the Argo CD repository (assets/swagger.json): ``metadata``, ``spec`` (project, source,
destination, syncPolicy) and ``status`` (sync, health, resources, conditions, operationState,
history, summary, reconciledAt). The shape has not been checked against a live server yet.
"""

import json
from typing import Any

from requests.structures import CaseInsensitiveDict


class FakeResponse:
    def __init__(self, status_code: int = 200, body: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = CaseInsensitiveDict(headers or {})

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)  # raises ValueError for non-JSON text
        return self._body


class FakeArgoSession:
    """Serves ``{"items": [...]}`` for the list endpoint and records every call.

    ``script`` is a list of items consumed one per request before the normal answer:
    a ``FakeResponse`` is returned as is, an exception instance is raised.
    """

    def __init__(self, apps: list[dict] | None = None):
        self.apps: list[dict] = list(apps or [])
        self.script: list[Any] = []
        self.calls: list[dict] = []

    def get(self, url, params=None, verify=True, timeout=None):
        self.calls.append(
            {"url": url, "params": list(params or []), "verify": verify, "timeout": timeout}
        )
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        return FakeResponse(200, {"metadata": {"resourceVersion": "1"}, "items": self.apps})

    def close(self):
        pass


def make_app(
    name: str = "guestbook",
    *,
    namespace: str | None = "argocd",
    project: str = "default",
    repo: str = "https://github.com/argoproj/argocd-example-apps.git",
    path: str = "guestbook",
    sync: str = "Synced",
    health: str = "Healthy",
    health_message: str | None = None,
    resources: list[dict] | None = None,
    history: list[dict] | None = None,
    resource_version: str = "100",
    reconciled_at: str = "2026-10-17T10:00:00Z",
) -> dict:
    metadata: dict = {
        "name": name,
        "resourceVersion": resource_version,
        "generation": 3,
        "annotations": {"note": "do-not-render"},
        "managedFields": [{"manager": "argocd-controller"}],
        "labels": {"team": "payments"},
    }
    if namespace is not None:
        metadata["namespace"] = namespace
    status: dict = {
        "sync": {"status": sync, "revision": "abc1234"},
        "health": {"status": health},
        "reconciledAt": reconciled_at,
        "observedAt": reconciled_at,
        "operationState": {
            "phase": "Succeeded",
            "message": "successfully synced (all tasks run)",
            "operation": {"sync": {"revision": "abc1234"}},
            "syncResult": {"revision": "abc1234"},
            "startedAt": "2026-10-17T09:00:00Z",
            "finishedAt": "2026-10-17T09:00:05Z",
            "retryCount": 2,
        },
        "summary": {"images": ["gcr.io/example/guestbook:0.2"]},
        "resources": resources if resources is not None else [],
        "history": history if history is not None else [],
    }
    if health_message:
        status["health"]["message"] = health_message
    return {
        "metadata": metadata,
        "spec": {
            "project": project,
            "source": {"repoURL": repo, "path": path, "targetRevision": "HEAD"},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "default"},
            "syncPolicy": {"automated": {"prune": True, "selfHeal": True}},
        },
        "status": status,
    }


def make_resource(
    kind: str,
    name: str,
    *,
    namespace: str = "default",
    sync: str = "Synced",
    health: str | None = "Healthy",
    message: str | None = None,
) -> dict:
    resource: dict = {"group": "apps", "version": "v1", "kind": kind, "namespace": namespace}
    resource.update(name=name, status=sync)
    if health:
        resource["health"] = {"status": health}
        if message:
            resource["health"]["message"] = message
    return resource


def make_history(entry_id: int, revision: str = "abc1234", *, automated: bool = False) -> dict:
    initiated = {"automated": True} if automated else {"username": "admin"}
    return {
        "id": entry_id,
        "revision": revision,
        "deployStartedAt": f"2026-10-1{entry_id}T08:00:00Z",
        "deployedAt": f"2026-10-1{entry_id}T08:00:09Z",
        "initiatedBy": initiated,
        "source": {
            "repoURL": "https://github.com/argoproj/argocd-example-apps.git",
            "path": "guestbook",
            "targetRevision": "HEAD",
        },
    }
