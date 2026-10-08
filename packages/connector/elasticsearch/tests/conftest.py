"""An Elasticsearch API double; storage tests use real dlt and SQLite."""

import os
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from elasticsearch import Elasticsearch


@pytest.fixture
def live():
    """Share a real, API-key scoped disposable index across opt-in test modules."""
    url = os.environ.get("ES_TEST_URL")
    password = os.environ.get("ES_TEST_PASSWORD")
    if not url or not password:
        pytest.skip("Set ES_TEST_URL and ES_TEST_PASSWORD for a disposable server.")
    admin = Elasticsearch(url, basic_auth=("elastic", password), request_timeout=60)
    index = f"cognee-connector-test-{uuid4().hex}"
    admin.indices.create(
        index=index,
        settings={"number_of_shards": 2, "number_of_replicas": 0},
        mappings={"properties": {"updated_at": {"type": "date"}, "published": {"type": "boolean"}}},
    )
    key = admin.security.create_api_key(
        name=index,
        role_descriptors={
            "reader": {
                "cluster": [],
                "indices": [{"names": [index], "privileges": ["read", "view_index_metadata"]}],
            }
        },
    )
    reader = Elasticsearch(url, api_key=key["encoded"], request_timeout=60)
    try:
        yield admin, reader, index, key["encoded"]
    finally:
        reader.close()
        admin.indices.delete(index=index)
        admin.security.invalidate_api_key(ids=[key["id"]])
        admin.close()


class FakeElasticsearch:
    def __init__(self, documents=()):
        self.documents = list(documents)
        self.requests = []
        self.closed = []
        self.counter = 0
        self.fail_at = None
        self.partial_at = None
        self.omit_delta = False
        self.index_uuids = {"articles": "uuid-articles"}
        self.indices = SimpleNamespace(get_settings=self.get_settings)

    def get_settings(self, **kwargs):
        for item in self.documents:
            self.index_uuids.setdefault(item["index"], f"uuid-{item['index']}")
        return {
            name: {"settings": {"index": {"uuid": uuid}}} for name, uuid in self.index_uuids.items()
        }

    def open_point_in_time(self, **kwargs):
        self.snapshot = deepcopy(self.documents)
        self.counter += 1
        self.pit_id = f"pit-{self.counter}"
        return {"id": self.pit_id, "_shards": {"total": 2, "successful": 2, "failed": 0}}

    def close_point_in_time(self, *, id):
        self.closed.append(id)
        assert id == self.pit_id
        return {"succeeded": True}

    def search(self, **kwargs):
        assert "from" not in kwargs
        assert "index" not in kwargs
        assert kwargs["pit"]["id"] == self.pit_id
        self.requests.append(deepcopy(kwargs))
        if self.fail_at == len(self.requests):
            raise ConnectionError("simulated connection failure")
        self.counter += 1
        self.pit_id = f"pit-{self.counter}"
        hits = []
        for position, document in enumerate(self.snapshot):
            if not self.matches(document, kwargs["query"]):
                continue
            hit = {
                "_index": document["index"],
                "_id": document["id"],
                "_seq_no": document.get("seq", 1),
                "_primary_term": 1,
                "sort": [document["date"], position + 4294967296],
            }
            field = kwargs["docvalue_fields"][0]["field"]
            hit["fields"] = {field: [document["date"]] if document["date"] is not None else []}
            if kwargs["source"] is not False:
                if self.omit_delta:
                    continue
                source = deepcopy(document["source"])
                if isinstance(kwargs["source"], list):
                    source = {
                        key: value for key, value in source.items() if key in kwargs["source"]
                    }
                hit["_source"] = source
            hits.append(hit)

        def numeric_sort(values):
            return (int(values[0]) if values[0] is not None else float("inf"), values[1])

        hits.sort(key=lambda hit: numeric_sort(hit["sort"]))
        after = kwargs.get("search_after")
        if after is not None:
            hits = [hit for hit in hits if numeric_sort(hit["sort"]) > numeric_sort(after)]
        return {
            "pit_id": self.pit_id,
            "timed_out": False,
            "_shards": {
                "total": 2,
                "successful": 2,
                "failed": int(self.partial_at == len(self.requests)),
            },
            "hits": {"hits": hits[: kwargs["size"]]},
        }

    @classmethod
    def matches(cls, document, query):
        if "match_all" in query:
            return True
        if "bool" in query:
            return all(cls.matches(document, part) for part in query["bool"]["filter"])
        if "ids" in query:
            return document["id"] in query["ids"]["values"]
        if "term" in query:
            key, value = next(iter(query["term"].items()))
            actual = document["index"] if key == "_index" else document["source"].get(key)
            return actual == value
        if "range" in query:
            bounds = next(iter(query["range"].values()))
            return document["date"] >= bounds["gte"]
        raise AssertionError(f"Unhandled test query: {query}")


def document(id="1", *, index="articles", date=1000, seq=1, body="Hello", published=True):
    return {
        "id": id,
        "index": index,
        "date": date,
        "seq": seq,
        "source": {
            "title": "Guide",
            "body": body,
            "updated_at": date,
            "published": published,
            "secret": "not selected",
        },
    }
