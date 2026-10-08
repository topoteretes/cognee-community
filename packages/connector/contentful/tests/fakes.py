"""Contentful-shaped fixtures served through httpx's public transport seam."""

from collections.abc import Callable
from copy import deepcopy
from typing import Any
from urllib.parse import quote

import httpx


class FakeContentful:
    """A mutable Delivery server; no HTTP calls leave the MockTransport.

    ``initial_items`` is the current full snapshot, and ``delta`` registers a
    response for a previously returned token. ``model_passes`` can model a
    changing collection; otherwise every inventory observes ``models``.
    ``override`` handles one-off faults before the normal route handler.
    """

    def __init__(
        self,
        space: str = "space",
        environment: str = "master",
        host: str = "cdn.contentful.com",
    ):
        self.space = space
        self.environment = environment
        self.host = host
        self.initial_items: list[dict[str, Any]] = []
        self.initial_token = "t1"
        self.models: list[dict[str, Any]] = []
        self.model_passes: list[list[dict[str, Any]]] = []
        self.model_page_size = 100
        self.model_pass_count = 0
        self.live: dict[tuple[str, str], dict[str, Any] | None] = {}
        self.sync_pages: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.override: Callable[[httpx.Request], httpx.Response | None] | None = None
        self._model_snapshots: dict[int, list[dict[str, Any]]] = {}
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def url(self, endpoint: str, *, legacy: bool = False) -> str:
        base = f"https://{self.host}/spaces/{quote(self.space, safe='')}"
        if not legacy:
            base += f"/environments/{quote(self.environment, safe='')}"
        return f"{base}/{endpoint}"

    def sync_url(self, token: str, *, legacy: bool = False) -> str:
        return f"{self.url('sync', legacy=legacy)}?sync_token={quote(token, safe='')}"

    def delta(self, token: str, items: list[dict[str, Any]], next_token: str = "t2"):
        self.sync_pages[token] = {"items": items, "nextSyncUrl": self.sync_url(next_token)}

    def page(self, token: str, items: list[dict[str, Any]], next_token: str):
        self.sync_pages[token] = {"items": items, "nextPageUrl": self.sync_url(next_token)}

    def _sys(
        self,
        identifier: str,
        kind: str,
        revision: int = 1,
        updated_at: str = "2026-01-01T00:00:00Z",
    ) -> dict[str, Any]:
        return {
            "id": identifier,
            "type": kind,
            "space": {"sys": {"type": "Link", "linkType": "Space", "id": self.space}},
            "environment": {
                "sys": {"type": "Link", "linkType": "Environment", "id": self.environment}
            },
            "revision": revision,
            "createdAt": "2025-01-01T00:00:00Z",
            "updatedAt": updated_at,
        }

    def entry(
        self,
        identifier: str,
        text: str,
        *,
        content_type: str = "article",
        fields: dict[str, Any] | None = None,
        revision: int = 1,
        updated_at: str = "2026-01-01T00:00:00Z",
    ) -> dict[str, Any]:
        system = self._sys(identifier, "Entry", revision, updated_at)
        system["contentType"] = {
            "sys": {"type": "Link", "linkType": "ContentType", "id": content_type}
        }
        return {
            "sys": system,
            "fields": deepcopy(fields)
            if fields is not None
            else {"title": {"en-US": identifier}, "body": {"en-US": text}},
        }

    def asset(
        self,
        identifier: str,
        text: str,
        *,
        fields: dict[str, Any] | None = None,
        revision: int = 1,
        updated_at: str = "2026-01-01T00:00:00Z",
    ) -> dict[str, Any]:
        return {
            "sys": self._sys(identifier, "Asset", revision, updated_at),
            "fields": deepcopy(fields)
            if fields is not None
            else {
                "title": {"en-US": identifier},
                "description": {"en-US": text},
                "file": {
                    "en-US": {
                        "fileName": f"{identifier}.png",
                        "contentType": "image/png",
                        "url": f"//images.ctfassets.net/{self.space}/{identifier}/image.png",
                        "details": {"size": 1234, "image": {"width": 640, "height": 480}},
                    }
                },
            },
        }

    def model(
        self,
        identifier: str = "article",
        *,
        name: str = "Article",
        fields: list[dict[str, Any]] | None = None,
        revision: int = 1,
        updated_at: str = "2026-01-01T00:00:00Z",
    ) -> dict[str, Any]:
        return {
            "sys": self._sys(identifier, "ContentType", revision, updated_at),
            "name": name,
            "description": f"{name} content model",
            "displayField": "title",
            "fields": deepcopy(fields)
            if fields is not None
            else [
                {"id": "title", "name": "Title", "type": "Symbol", "localized": True},
                {"id": "body", "name": "Body", "type": "Text", "localized": True},
            ],
        }

    def deleted(
        self,
        identifier: str,
        *,
        kind: str = "Entry",
        deleted_at: str = "2026-02-01T00:00:00Z",
    ) -> dict[str, Any]:
        return {
            "sys": {
                "id": identifier,
                "type": f"Deleted{kind}",
                "deletedAt": deleted_at,
                "createdAt": deleted_at,
                "updatedAt": deleted_at,
            }
        }

    def _lookup(self, kind: str, identifier: str) -> dict[str, Any] | None:
        key = (kind, identifier)
        if key in self.live:
            return self.live[key]
        candidates = self.models if kind == "ContentType" else self.initial_items
        return next(
            (
                item
                for item in candidates
                if item["sys"]["id"] == identifier and item["sys"]["type"] == kind
            ),
            None,
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None:
            overridden = self.override(request)
            if overridden is not None:
                return overridden
        path = request.url.path
        if path.endswith("/sync"):
            if request.url.params.get("initial") in {"true", "True", "1"}:
                payload = self.sync_pages.get(
                    "initial",
                    {
                        "items": self.initial_items,
                        "nextSyncUrl": self.sync_url(self.initial_token),
                    },
                )
            else:
                token = request.url.params.get("sync_token")
                payload = self.sync_pages.get(
                    token, {"items": [], "nextSyncUrl": self.sync_url(token or self.initial_token)}
                )
            return httpx.Response(200, json=deepcopy(payload))
        if path.endswith("/content_types"):
            token = request.url.params.get("pageNext")
            if token:
                pass_id, offset = map(int, token.split("-"))
            else:
                self.model_pass_count += 1
                pass_id, offset = self.model_pass_count, 0
                index = min(pass_id - 1, len(self.model_passes) - 1)
                snapshot = self.model_passes[index] if self.model_passes else self.models
                self._model_snapshots[pass_id] = deepcopy(snapshot)
            snapshot = self._model_snapshots[pass_id]
            end = offset + self.model_page_size
            pages = {}
            if end < len(snapshot):
                pages["next"] = f"{self.url('content_types')}?pageNext={pass_id}-{end}"
            return httpx.Response(200, json={"items": snapshot[offset:end], "pages": pages})
        for endpoint, kind in (
            ("entries", "Entry"),
            ("assets", "Asset"),
            ("content_types", "ContentType"),
        ):
            marker = f"/{endpoint}/"
            if marker in path:
                identifier = path.rsplit(marker, 1)[1]
                item = self._lookup(kind, identifier)
                if item is not None:
                    return httpx.Response(200, json=deepcopy(item))
                return httpx.Response(404, json={"sys": {"id": "NotFound"}})
        raise AssertionError(f"Unexpected Contentful request path: {path}")
