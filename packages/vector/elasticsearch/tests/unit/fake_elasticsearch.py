"""In-memory stand-in for AsyncElasticsearch covering exactly the calls the adapter makes.

It enforces the Elasticsearch rules the adapter depends on (index naming, dense
vector dimensions, kNN bounds, bulk item errors, cosine score convention) so
the offline suite fails for the same reasons a real cluster would.
"""

import copy
import fnmatch
import math
import re
from types import SimpleNamespace

from cognee_community_vector_adapter_elasticsearch.elasticsearch_adapter import (
    REMOVE_TAGS_SCRIPT,
    ElasticsearchAdapter,
)
from elasticsearch import BadRequestError, NotFoundError

SCORE_SCRIPT = "cosineSimilarity(params.query_vector, 'vector') + 1.0"
_INVALID_INDEX = re.compile(r'[\\/*?"<>|,#: A-Z]')


def _api_error(cls, error_type, reason, status):
    return cls(
        message=error_type,
        meta=SimpleNamespace(status=status),
        body={"error": {"type": error_type, "reason": reason}, "status": status},
    )


def _cosine(a, b):
    if len(a) != len(b):
        raise _api_error(
            BadRequestError, "illegal_argument_exception", "vector dimension mismatch", 400
        )
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        raise _api_error(
            BadRequestError, "illegal_argument_exception", "zero vector with cosine", 400
        )
    return sum(x * y for x, y in zip(a, b, strict=True)) / (norm_a * norm_b)


def _field_values(doc, field):
    value = doc.get(field)
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _matches(doc, query):
    if query is None or "match_all" in query:
        return True
    if "bool" in query:
        clauses = list(query["bool"].get("filter", [])) + list(query["bool"].get("must", []))
        return all(_matches(doc, clause) for clause in clauses)
    if "term" in query:
        ((field, value),) = query["term"].items()
        return value in _field_values(doc, field)
    if "terms" in query:
        ((field, values),) = query["terms"].items()
        return any(value in _field_values(doc, field) for value in values)
    if "ids" in query:
        return doc["_id"] in set(query["ids"]["values"])
    raise NotImplementedError(f"fake does not support query {query!r}")


def _project(source, includes):
    if includes is None:
        return copy.deepcopy(source)
    return {key: copy.deepcopy(source[key]) for key in includes if key in source}


def _merge(target, updates):
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class _FakeIndices:
    def __init__(self, store):
        self._store = store

    async def exists(self, index):
        return index in self._store

    async def create(self, index, mappings=None, settings=None):
        if _INVALID_INDEX.search(index) or index[0] in "-_+":
            raise _api_error(
                BadRequestError, "invalid_index_name_exception", f"bad index name {index}", 400
            )
        if index in self._store:
            raise _api_error(
                BadRequestError, "resource_already_exists_exception", f"{index} exists", 400
            )
        dims = mappings["properties"]["vector"]["dims"]
        self._store[index] = {"mappings": copy.deepcopy(mappings), "dims": dims, "docs": {}}
        return {"acknowledged": True}

    async def delete(self, index, ignore_unavailable=False):
        if index not in self._store:
            if ignore_unavailable:
                return {"acknowledged": True}
            raise _api_error(NotFoundError, "index_not_found_exception", index, 404)
        del self._store[index]
        return {"acknowledged": True}

    async def get(self, index):
        if any(char in index for char in "*?"):
            return {
                name: {"mappings": copy.deepcopy(entry["mappings"]), "settings": {}}
                for name, entry in self._store.items()
                if fnmatch.fnmatchcase(name, index)
            }
        if index not in self._store:
            raise _api_error(NotFoundError, "index_not_found_exception", index, 404)
        return {index: {"mappings": copy.deepcopy(self._store[index]["mappings"]), "settings": {}}}


class FakeAsyncElasticsearch:
    def __init__(self):
        self.store = {}
        self.indices = _FakeIndices(self.store)
        self.closed = False
        self.calls = []

    async def close(self):
        self.closed = True

    def _index(self, index):
        if index not in self.store:
            raise _api_error(NotFoundError, "index_not_found_exception", index, 404)
        return self.store[index]

    @staticmethod
    def _docs(entry):
        return [{"_id": doc_id, **source} for doc_id, source in entry["docs"].items()]

    async def count(self, index, query=None):
        self.calls.append("count")
        entry = self._index(index)
        return {"count": sum(1 for doc in self._docs(entry) if _matches(doc, query))}

    async def search(self, index, knn=None, query=None, size=None, source_includes=None):
        self.calls.append("search")
        return self._search(index, knn, query, size, source_includes)

    def _search(self, index, knn, query, size, source_includes):
        entry = self._index(index)
        docs = self._docs(entry)
        if knn is not None:
            k = knn["k"]
            candidates = knn["num_candidates"]
            if not 1 <= k <= 10_000 or candidates < k or candidates > 10_000:
                raise _api_error(BadRequestError, "illegal_argument_exception", "knn bounds", 400)
            if len(knn["query_vector"]) != entry["dims"]:
                raise _api_error(BadRequestError, "illegal_argument_exception", "dims", 400)
            scored = [
                (doc, (1.0 + _cosine(knn["query_vector"], doc["vector"])) / 2.0)
                for doc in docs
                if _matches(doc, knn.get("filter"))
            ]
            limit = min(k, size if size is not None else 10)
        elif query is not None and "script_score" in query:
            script = query["script_score"]["script"]
            assert script["source"] == SCORE_SCRIPT, script["source"]
            query_vector = script["params"]["query_vector"]
            scored = [
                (doc, _cosine(query_vector, doc["vector"]) + 1.0)
                for doc in docs
                if _matches(doc, query["script_score"]["query"])
            ]
            limit = size if size is not None else 10
        else:
            scored = [(doc, 1.0) for doc in docs if _matches(doc, query)]
            limit = size if size is not None else 10
        scored.sort(key=lambda item: item[1], reverse=True)
        hits = [
            {
                "_index": index,
                "_id": doc["_id"],
                "_score": score,
                "_source": _project({k: v for k, v in doc.items() if k != "_id"}, source_includes),
            }
            for doc, score in scored[:limit]
        ]
        return {"hits": {"total": {"value": len(scored)}, "hits": hits}}

    async def msearch(self, searches):
        self.calls.append("msearch")
        responses = []
        for header, body in zip(searches[::2], searches[1::2], strict=True):
            try:
                responses.append(
                    self._search(
                        header["index"],
                        body.get("knn"),
                        body.get("query"),
                        body.get("size"),
                        body.get("_source"),
                    )
                )
            except NotFoundError as error:
                responses.append({"error": error.body["error"], "status": 404})
        return {"responses": responses}

    async def mget(self, index, ids, source_includes=None):
        self.calls.append("mget")
        entry = self._index(index)
        docs = []
        for doc_id in ids:
            source = entry["docs"].get(doc_id)
            if source is None:
                docs.append({"_index": index, "_id": doc_id, "found": False})
            else:
                docs.append(
                    {
                        "_index": index,
                        "_id": doc_id,
                        "found": True,
                        "_source": _project(source, source_includes),
                    }
                )
        return {"docs": docs}

    async def bulk(self, operations, refresh=None):
        self.calls.append("bulk")
        items = []
        errors = False
        pending = iter(operations)
        for action in pending:
            ((operation, meta),) = action.items()
            doc_id = meta["_id"]
            entry = self.store.get(meta["_index"])
            if entry is None:
                next(pending, None) if operation in ("index", "update") else None
                items.append(
                    {
                        operation: {
                            "_id": doc_id,
                            "status": 404,
                            "error": {
                                "type": "index_not_found_exception",
                                "reason": meta["_index"],
                            },
                        }
                    }
                )
                errors = True
                continue
            if operation == "index":
                source = next(pending)
                if len(source.get("vector", [])) != entry["dims"]:
                    items.append(
                        {
                            "index": {
                                "_id": doc_id,
                                "status": 400,
                                "error": {
                                    "type": "document_parsing_exception",
                                    "reason": "vector dims mismatch",
                                },
                            }
                        }
                    )
                    errors = True
                    continue
                created = doc_id not in entry["docs"]
                entry["docs"][doc_id] = copy.deepcopy(source)
                items.append({"index": {"_id": doc_id, "status": 201 if created else 200}})
            elif operation == "update":
                body = next(pending)
                if doc_id not in entry["docs"]:
                    items.append(
                        {
                            "update": {
                                "_id": doc_id,
                                "status": 404,
                                "error": {
                                    "type": "document_missing_exception",
                                    "reason": f"[{doc_id}]: document missing",
                                },
                            }
                        }
                    )
                    errors = True
                    continue
                _merge(entry["docs"][doc_id], body["doc"])
                items.append({"update": {"_id": doc_id, "status": 200}})
            elif operation == "delete":
                existed = entry["docs"].pop(doc_id, None) is not None
                items.append({"delete": {"_id": doc_id, "status": 200 if existed else 404}})
            else:
                raise NotImplementedError(operation)
        return {"took": 0, "errors": errors, "items": items}

    async def delete_by_query(self, index, query=None, refresh=None, conflicts=None):
        self.calls.append("delete_by_query")
        entry = self._index(index)
        doomed = [doc["_id"] for doc in self._docs(entry) if _matches(doc, query)]
        for doc_id in doomed:
            del entry["docs"][doc_id]
        return {"deleted": len(doomed)}

    async def update_by_query(self, index, query=None, script=None, refresh=None, conflicts=None):
        self.calls.append("update_by_query")
        entry = self._index(index)
        assert script["source"] == REMOVE_TAGS_SCRIPT, "fake only understands the detag script"
        tags = set(script["params"]["tags"])
        updated = deleted = 0
        for doc in self._docs(entry):
            if not _matches(doc, query):
                continue
            source = entry["docs"][doc["_id"]]
            source["belongs_to_set"] = [
                t for t in source.get("belongs_to_set") or [] if t not in tags
            ]
            payload = source.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("belongs_to_set"), list):
                payload["belongs_to_set"] = [t for t in payload["belongs_to_set"] if t not in tags]
            if not source["belongs_to_set"]:
                del entry["docs"][doc["_id"]]
                deleted += 1
            else:
                updated += 1
        return {"updated": updated, "deleted": deleted}


def make_adapters(client, datasets, embedding_engine_factory, **kwargs):
    """One adapter per dataset name, all sharing *client*."""
    return [
        ElasticsearchAdapter(
            url="http://fake:9200",
            api_key=None,
            embedding_engine=embedding_engine_factory(),
            database_name=dataset,
            client=client,
            **kwargs,
        )
        for dataset in datasets
    ]
