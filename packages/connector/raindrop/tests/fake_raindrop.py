"""In-memory stand-in for the Raindrop.io REST API, served through httpx.MockTransport."""

import httpx

from cognee_community_connector_raindrop.raindrop import API_BASE_URL


def bookmark(raindrop_id, title, collection_id=1, **fields):
    return {
        "_id": raindrop_id,
        "title": title,
        "link": f"https://example.com/{raindrop_id}",
        "type": "article",
        "created": "2026-01-01T00:00:00.000Z",
        "lastUpdate": "2026-02-02T00:00:00.000Z",
        # Live API shape: the id is in collectionId, collection.$id is unset.
        "collectionId": collection_id,
        "collection": {},
        "removed": False,
        "tags": [],
        "excerpt": "",
        "note": "",
        "highlights": [],
        **fields,
    }


class FakeRaindrop:
    """Holds collections + bookmarks and answers the endpoints the connector calls."""

    def __init__(self):
        self.collections = [{"_id": 1, "title": "Reading"}]
        self.children = []
        self.bookmarks = {}
        self.trash = {}
        self.requests = []
        # Status codes to answer (in order) before serving requests normally.
        self.failures = []

    def put(self, item):
        self.bookmarks[item["_id"]] = item

    def trash_bookmark(self, raindrop_id):
        self.trash[raindrop_id] = self.bookmarks.pop(raindrop_id)

    def client(self):
        return httpx.Client(base_url=API_BASE_URL, transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        self.requests.append(request)
        if self.failures:
            status, headers = self.failures.pop(0)
            return httpx.Response(status, headers=headers, json={"result": False})

        path = request.url.path.removeprefix("/rest/v1")
        if path == "/collections":
            return httpx.Response(200, json={"result": True, "items": self.collections})
        if path == "/collections/childrens":
            return httpx.Response(200, json={"result": True, "items": self.children})
        if path.startswith("/raindrops/"):
            return self._raindrops(int(path.rsplit("/", 1)[1]), request.url.params)
        return httpx.Response(404, json={"result": False})

    def _raindrops(self, collection_id, params):
        if collection_id == -99:
            items = list(self.trash.values())
        elif collection_id == 0:
            items = list(self.bookmarks.values())
        else:
            scope = {collection_id}
            if params.get("nested") == "true":
                scope |= {c["_id"] for c in self.children if self._descends(c, collection_id)}
            items = [b for b in self.bookmarks.values() if b["collectionId"] in scope]

        per_page = int(params.get("perpage", 25))
        page = int(params.get("page", 0))
        window = items[page * per_page : (page + 1) * per_page]
        return httpx.Response(200, json={"result": True, "items": window, "count": len(items)})

    def _descends(self, collection, ancestor_id):
        by_id = {c["_id"]: c for c in self.collections + self.children}
        parent = (collection.get("parent") or {}).get("$id")
        while parent is not None:
            if parent == ancestor_id:
                return True
            parent = (by_id.get(parent, {}).get("parent") or {}).get("$id")
        return False
