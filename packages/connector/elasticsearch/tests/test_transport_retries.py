"""Exercise the official client's real retry loop without network state."""

import json
from collections import deque
from typing import ClassVar

import pytest
from elastic_transport import (
    ApiResponseMeta,
    BaseNode,
    ConnectionError,
    ConnectionTimeout,
    HttpHeaders,
)
from elastic_transport._node._base import NodeApiResponse
from elasticsearch import ApiError, Elasticsearch
from test_elasticsearch import sync


@pytest.fixture
def scripted_node():
    class ScriptedNode(BaseNode):
        failures: ClassVar[deque] = deque()
        attempts = 0
        closed_pits = 0

        def perform_request(self, method, target, body=None, headers=None, request_timeout=None):
            status = 200
            if method == "GET" and "_settings" in target:
                result = {"articles": {"settings": {"index": {"uuid": "uuid-articles"}}}}
            elif method == "POST" and "_pit" in target:
                result = {"id": "pit-1", "_shards": {"total": 1, "successful": 1, "failed": 0}}
            elif method == "DELETE" and "_pit" in target:
                type(self).closed_pits += 1
                result = {"succeeded": True, "num_freed": 1}
            elif "_search" in target:
                type(self).attempts += 1
                if self.failures:
                    failure = self.failures.popleft()
                    if isinstance(failure, Exception):
                        raise failure
                    status = failure
                    result = {
                        "error": {"type": "unavailable", "reason": "Injected retry"},
                        "status": status,
                    }
                else:
                    request = json.loads(body)
                    hit = {
                        "_index": "articles",
                        "_id": "1",
                        "_seq_no": 1,
                        "_primary_term": 1,
                        "sort": [1000, 1],
                        "fields": {"updated_at": [1000]},
                    }
                    if request.get("_source") is not False:
                        hit["_source"] = {"title": "Retry test", "body": "Recovered"}
                    result = {
                        "timed_out": False,
                        "_shards": {"total": 1, "successful": 1, "failed": 0},
                        "hits": {"hits": [] if "search_after" in request else [hit]},
                    }
            else:
                raise AssertionError(f"Unexpected API route: {method} {target}")
            meta = ApiResponseMeta(
                status=status,
                http_version="1.1",
                headers=HttpHeaders(
                    {"content-type": "application/json", "x-elastic-product": "Elasticsearch"}
                ),
                duration=0.0,
                node=self.config,
            )
            return NodeApiResponse(meta, json.dumps(result).encode())

        def close(self):
            pass

    return ScriptedNode


@pytest.mark.parametrize("failure", [429, 503, "timeout", "connection"])
def test_official_client_retries_transient_failures(scripted_node, failure):
    if failure == "timeout":
        failure = ConnectionTimeout("Injected timeout")
    elif failure == "connection":
        failure = ConnectionError("Injected disconnect")
    scripted_node.failures = deque([failure])
    client = Elasticsearch(
        "http://scripted.invalid:9200",
        node_class=scripted_node,
        max_retries=3,
        retry_on_timeout=True,
    )
    try:
        state = {}
        rows = sync(client, state)
        assert len(rows) == 1 and "Recovered" in rows[0]["content"]
        assert scripted_node.attempts == 5  # 4 real requests plus one retry.
        assert scripted_node.closed_pits == 1
    finally:
        client.close()


@pytest.mark.parametrize("failure", [429, 503, "timeout", "connection"])
def test_retry_exhaustion_preserves_state_and_closes_pit(scripted_node, failure):
    if failure == "timeout":
        failure = ConnectionTimeout("Injected timeout")
    elif failure == "connection":
        failure = ConnectionError("Injected disconnect")
    scripted_node.failures = deque([failure] * 4)
    client = Elasticsearch(
        "http://scripted.invalid:9200",
        node_class=scripted_node,
        max_retries=3,
        retry_on_timeout=True,
    )
    try:
        state = {}
        with pytest.raises((ApiError, ConnectionError, ConnectionTimeout)):
            sync(client, state)
        assert state == {}
        assert scripted_node.attempts == 4
        assert scripted_node.closed_pits == 1
    finally:
        client.close()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_http_errors_are_not_retried_or_interpreted_as_deletion(scripted_node, status):
    scripted_node.failures = deque([status])
    client = Elasticsearch(
        "http://scripted.invalid:9200",
        node_class=scripted_node,
        max_retries=3,
        retry_on_timeout=True,
    )
    try:
        state = {}
        with pytest.raises(ApiError) as failure:
            sync(client, state)
        assert failure.value.status_code == status
        assert state == {} and scripted_node.attempts == 1
        assert scripted_node.closed_pits == 1
    finally:
        client.close()
