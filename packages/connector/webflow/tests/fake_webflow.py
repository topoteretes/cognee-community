"""An in-memory Webflow Data API v2, served through ``httpx.MockTransport``.

It follows the documented behaviour the connector relies on: bearer auth (401),
offset pagination with ``limit`` capped at 100 and a ``pagination.total``, the
split between staged (``/items``) and live (``/items/live``) items, the
``{message, code}`` error body, and 429 with ``Retry-After``. Tests drive it
like a site editor: add, edit, unpublish, archive and delete items and pages.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

TOKEN = "test-token"


class FakeWebflow:
    def __init__(self) -> None:
        self.sites: list[dict] = []
        self.collections: dict[str, dict] = {}
        self.items: dict[str, list[dict]] = {}
        self.pages: dict[str, list[dict]] = {}
        self.dom: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []
        # Called before each request, to change data while a listing is read.
        self.on_request: Callable[[str, dict], None] | None = None
        # path prefix -> responses to give before answering normally.
        self._failures: dict[str, list[httpx.Response]] = {}
        self._clock = datetime(2026, 1, 1, tzinfo=UTC)
        self._counter = 0

    # -- editor actions ------------------------------------------------------
    def add_site(self, name="Acme", short="acme", site_id=None) -> str:
        site_id = site_id or self._id()
        self.sites.append({"id": site_id, "displayName": name, "shortName": short})
        return site_id

    def add_collection(self, site_id, slug, name=None, singular=None, fields=None) -> str:
        collection_id = self._id()
        self.collections[collection_id] = {
            "id": collection_id,
            "siteId": site_id,
            "slug": slug,
            "displayName": name or slug.title(),
            "singularName": singular or slug.rstrip("s").title(),
            "createdOn": self._now(),
            "lastUpdated": self._now(),
            "fields": [
                {"id": self._id(), "isRequired": False, "isEditable": True, "helpText": None, **f}
                for f in (fields or [])
            ],
        }
        self.items[collection_id] = []
        return collection_id

    def add_item(self, collection_id, name, *, draft=False, archived=False, **fields) -> str:
        item_id = self._id()
        now = self._now()
        self.items[collection_id].append(
            {
                "id": item_id,
                "cmsLocaleId": "locale1",
                "createdOn": now,
                "lastUpdated": now,
                "lastPublished": None if draft else now,
                "isArchived": archived,
                "isDraft": draft,
                "fieldData": {"name": name, "slug": name.lower().replace(" ", "-"), **fields},
            }
        )
        return item_id

    def edit_item(self, collection_id, item_id, **fields) -> None:
        item = self._item(collection_id, item_id)
        item["fieldData"].update(fields)
        item["lastUpdated"] = self._now()

    def touch_item(self, collection_id, item_id) -> None:
        """Bump ``lastUpdated`` without changing any field (a publish, a no-op save)."""
        self._item(collection_id, item_id)["lastUpdated"] = self._now()

    def publish_item(self, collection_id, item_id) -> None:
        item = self._item(collection_id, item_id)
        item.update(isDraft=False, lastPublished=self._now(), lastUpdated=self._now())

    def unpublish_item(self, collection_id, item_id) -> None:
        item = self._item(collection_id, item_id)
        item.update(isDraft=True, lastPublished=None, lastUpdated=self._now())

    def archive_item(self, collection_id, item_id) -> None:
        self._item(collection_id, item_id).update(isArchived=True, lastUpdated=self._now())

    def delete_item(self, collection_id, item_id) -> None:
        self.items[collection_id] = [i for i in self.items[collection_id] if i["id"] != item_id]

    def delete_collection(self, collection_id) -> None:
        del self.collections[collection_id]
        del self.items[collection_id]

    def add_page(self, site_id, title, slug, nodes=(), *, draft=False, collection_id=None) -> str:
        page_id = self._id()
        self.pages.setdefault(site_id, []).append(
            {
                "id": page_id,
                "siteId": site_id,
                "title": title,
                "slug": slug,
                "collectionId": collection_id,
                "createdOn": self._now(),
                "lastUpdated": self._now(),
                "archived": False,
                "draft": draft,
                "isBranch": False,
                "publishedPath": f"/{slug}",
                "seo": {"title": title, "description": f"About {slug}"},
            }
        )
        self.dom[page_id] = list(nodes)
        return page_id

    def edit_page(self, site_id, page_id, nodes) -> None:
        page = next(p for p in self.pages[site_id] if p["id"] == page_id)
        page["lastUpdated"] = self._now()
        self.dom[page_id] = list(nodes)

    def delete_page(self, site_id, page_id) -> None:
        self.pages[site_id] = [p for p in self.pages[site_id] if p["id"] != page_id]
        self.dom.pop(page_id, None)

    # -- failure injection ---------------------------------------------------
    def fail(self, path_prefix: str, status: int, *, times=1, code="internal_error", headers=None):
        response = httpx.Response(
            status,
            headers=headers or {},
            json={"message": "injected", "code": code, "externalReference": None, "details": []},
        )
        self._failures.setdefault(path_prefix, []).extend([response] * times)

    # -- transport -----------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def requests_to(self, fragment: str) -> list[tuple[str, dict]]:
        return [call for call in self.calls if fragment in call[0]]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v2")
        params = dict(request.url.params)
        self.calls.append((path, params))
        if self.on_request:
            self.on_request(path, params)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return _error(401, "not_authorized", "Request not authorized")
        for prefix, queue in self._failures.items():
            if path.startswith(prefix) and queue:
                return queue.pop(0)
        try:
            return self._route(path, params)
        except KeyError:
            return _error(404, "resource_not_found", "Requested resource not found")

    def _route(self, path: str, params: dict) -> httpx.Response:
        parts = path.strip("/").split("/")
        if parts == ["sites"]:
            return httpx.Response(200, json={"sites": self.sites})
        if len(parts) == 2 and parts[0] == "sites":
            return httpx.Response(200, json=self._site(parts[1]))
        if len(parts) == 3 and parts[0] == "sites" and parts[2] == "collections":
            rows = [
                _public(c)
                for c in self.collections.values()
                if c["siteId"] == parts[1] and self._site(parts[1])
            ]
            return httpx.Response(200, json={"collections": rows})
        if len(parts) == 3 and parts[0] == "sites" and parts[2] == "pages":
            self._site(parts[1])
            return self._paged("pages", self.pages.get(parts[1], []), params)
        if len(parts) == 2 and parts[0] == "collections":
            return httpx.Response(200, json=self.collections[parts[1]])
        if parts[0] == "collections" and parts[2] == "items":
            rows = self.items[parts[1]]
            if parts[3:] == ["live"]:
                rows = [i for i in rows if not i["isDraft"] and not i["isArchived"]]
            elif parts[3:]:
                raise KeyError(path)
            return self._paged("items", rows, params)
        if len(parts) == 3 and parts[0] == "pages" and parts[2] == "dom":
            nodes = self.dom[parts[1]]
            return self._paged("nodes", nodes, params, extra={"pageId": parts[1]})
        raise KeyError(path)

    def _paged(self, key, rows, params, extra=None) -> httpx.Response:
        limit = int(params.get("limit", 100))
        offset = int(params.get("offset", 0))
        if limit > 100:
            return _error(400, "bad_request", "limit must be at most 100")
        body = {
            key: rows[offset : offset + limit],
            "pagination": {"limit": limit, "offset": offset, "total": len(rows)},
            **(extra or {}),
        }
        return httpx.Response(
            200, content=json.dumps(body), headers={"content-type": "application/json"}
        )

    # -- helpers -------------------------------------------------------------
    def _site(self, site_id) -> dict:
        for site in self.sites:
            if site["id"] == site_id:
                return site
        raise KeyError(site_id)

    def _item(self, collection_id, item_id) -> dict:
        return next(i for i in self.items[collection_id] if i["id"] == item_id)

    def _id(self) -> str:
        self._counter += 1
        return f"{self._counter:024x}"

    def _now(self) -> str:
        self._clock += timedelta(minutes=1)
        return self._clock.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _public(collection: dict) -> dict:
    keys = ("id", "displayName", "singularName", "slug", "createdOn", "lastUpdated")
    return {key: collection[key] for key in keys}


def _error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"message": message, "code": code, "externalReference": None, "details": []}
    )
