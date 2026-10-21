"""In-memory stand-in for a Sanity dataset, with the client methods the sync uses.

Documents are stored the way the Content Lake stores them: drafts live next to
the published document under a ``drafts.`` id, system documents under ``_.``.
The query methods only return what the ``published`` perspective would.
Every write gets a new ``_rev``; ``_updatedAt`` has second precision, like the
real API, so documents written in the same second share it.
"""

import itertools

_rev = itertools.count(1)


def block(text, key="b", style="normal", **extra):
    return {
        "_type": "block",
        "_key": key,
        "style": style,
        "markDefs": extra.pop("markDefs", []),
        "children": [
            {"_type": "span", "_key": key + "s", "text": text, "marks": extra.pop("marks", [])}
        ],
        **extra,
    }


def ref(doc_id):
    return {"_type": "reference", "_ref": doc_id}


class FakeSanity:
    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail: dict[str, Exception] = {}
        self.schema: dict | None = None
        self.now = "2026-10-19T10:00:00Z"

    # -- writes ----------------------------------------------------------------
    def put(self, doc_id, doc_type, **fields):
        self.docs[doc_id] = {
            "_id": doc_id,
            "_type": doc_type,
            "_rev": f"rev{next(_rev)}",
            "_updatedAt": self.now,
            **fields,
        }
        return doc_id

    def patch(self, doc_id, **fields):
        self.docs[doc_id].update(fields, _rev=f"rev{next(_rev)}", _updatedAt=self.now)

    def delete(self, doc_id):
        self.docs.pop(doc_id, None)

    def deploy_schema(self, types):
        self.schema = {
            "_id": "_.schemas.default",
            "_type": "system.schema",
            "_rev": f"rev{next(_rev)}",
            "schema": types,
        }

    # -- what the published perspective sees -----------------------------------
    def _published(self):
        return {
            k: v for k, v in self.docs.items() if not k.startswith(("drafts.", "versions.", "_."))
        }

    def _check(self, name):
        self.calls.append((name,))
        if name in self.fail:
            raise self.fail[name]

    # -- the SanityClient surface the sync uses ---------------------------------
    def list_revisions(self, scope):
        self._check("list_revisions")
        return {
            k: {"rev": v["_rev"], "type": v["_type"]}
            for k, v in self._published().items()
            if v["_type"] in scope.document_types
        }

    def get_documents(self, ids):
        self._check("get_documents")
        published = self._published()
        self.calls[-1] = ("get_documents", tuple(sorted(ids)))
        return [dict(published[i]) for i in sorted(ids) if i in published]

    def get_labels(self, ids):
        self._check("get_labels")
        published = self._published()
        out = {}
        for i in ids:
            if i in published:
                d = published[i]
                label = next((d[f] for f in ("title", "name") if d.get(f)), i)
                out[i] = {"rev": d["_rev"], "type": d["_type"], "label": label}
        return out

    def get_schema(self):
        self._check("get_schema")
        return self.schema
