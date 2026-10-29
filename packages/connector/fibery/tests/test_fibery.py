from cognee_community_connector_fibery.fibery import (
    _request,
    _query_entities,
    _selectors,
    fibery_source,
    sync_entities,
)


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = RuntimeError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self.payload


class FiberyClient:
    def __init__(self, entities, documents=None):
        self.entities = entities
        self.documents = documents or {}
        self.calls = []

    def post(self, url, json=None):
        self.calls.append((url, json))
        command = json["command"]
        if command == "fibery.schema/query":
            return Response({"success": True, "result": {"fibery/types": [
                {
                    "fibery/name": "Product/Feature",
                    "fibery/fields": [
                        {"fibery/name": "fibery/id", "fibery/type": "fibery/uuid"},
                        {"fibery/name": "fibery/public-id", "fibery/type": "fibery/text"},
                        {"fibery/name": "fibery/modification-date", "fibery/type": "fibery/date-time"},
                        {"fibery/name": "Product/Name", "fibery/type": "fibery/text"},
                        {
                            "fibery/name": "Product/Description",
                            "fibery/type": "Collaboration~Documents/Document",
                        },
                        {
                            "fibery/name": "comments/comments",
                            "fibery/type": "comments/comment",
                            "fibery/meta": {"fibery/collection?": True},
                        },
                    ],
                }
            ]}})
        if command == "fibery.entity/query":
            query = json["args"]["query"]
            cursor = json.get("args", {}).get("params", {}).get("$last-seen-id")
            rows = [row for row in self.entities if not cursor or row["fibery/id"] > cursor]
            return Response({"success": True, "result": rows})
        if command == "get-documents":
            return Response(
                [
                    {"secret": item["secret"], "content": self.documents[item["secret"]]}
                    for item in json["args"]
                ]
            )
        raise AssertionError(command)


def _entity(entity_id, modified, description_secret, comments=None):
    return {
        "fibery/id": entity_id,
        "fibery/public-id": entity_id[-1],
        "fibery/modification-date": modified,
        "Product/Name": f"Feature {entity_id}",
        "Product/Description": {"Collaboration~Documents/secret": description_secret},
        "comments/comments": comments or [],
    }


def test_keyset_pagination_uses_id_cursor(monkeypatch):
    import cognee_community_connector_fibery.fibery as fibery

    monkeypatch.setattr(fibery, "_PAGE_SIZE", 2)
    calls = []

    class Client:
        def post(self, url, json):
            query = json["args"]["query"]
            calls.append(query)
            if "q/where" not in query:
                return Response(
                    {"success": True, "result": [{"fibery/id": "a"}, {"fibery/id": "b"}, {"fibery/id": "c"}]}
                )
            return Response({"success": True, "result": [{"fibery/id": "c"}]})

    rows = _query_entities(Client(), "https://x.fibery.io", "Product/Feature", [])
    assert [row["fibery/id"] for row in rows] == ["a", "b", "c"]
    assert calls[1]["q/where"] == [">", ["fibery/id"], "$last-seen-id"]
    assert calls[1]["q/order-by"] == [[ ["fibery/id"], "q/asc" ]]


def test_schema_selects_document_secrets_and_comment_relation():
    selectors = _selectors({
        "fibery/fields": [
            {"fibery/name": "Product/Name", "fibery/type": "fibery/text"},
            {"fibery/name": "Product/Description", "fibery/type": "Collaboration~Documents/Document"},
            {
                "fibery/name": "comments/comments",
                "fibery/type": "comments/comment",
                "fibery/meta": {"fibery/collection?": True},
            },
        ]
    })
    assert {"Product/Description": ["Collaboration~Documents/secret"]} in selectors
    assert any(isinstance(item, dict) and "comments/comments" in item for item in selectors)


def test_sync_emits_documents_and_deletions_after_complete_read():
    comment = {"fibery/id": "comment-1", "comment/document-secret": "comment-doc"}
    client = FiberyClient(
        [_entity("id-2", "2025-02-01", "doc-2", [comment]), _entity("id-3", "2025-03-01", "doc-3")],
        {"doc-2": "second body", "doc-3": "third body", "comment-doc": "review note"},
    )
    state = {"databases": {"Product/Feature": {"known_ids": ["id-1", "id-2"], "last_modified": "2025-01-01"}}}

    rows = list(sync_entities(client, "https://acme.fibery.io", ["Product/Feature"], state))

    assert [row["id"] for row in rows] == ["Product/Feature:id-2", "Product/Feature:id-3", "Product/Feature:id-1"]
    assert "second body" in rows[0]["content"]
    assert "review note" in rows[0]["content"]
    assert rows[-1] == {"id": "Product/Feature:id-1", "_deleted": True}
    assert state["databases"]["Product/Feature"]["known_ids"] == ["id-2", "id-3"]
    assert state["databases"]["Product/Feature"]["last_modified"] == "2025-03-01"


def test_unchanged_known_entity_skips_document_fetch():
    client = FiberyClient([_entity("id-1", "2025-01-01", "doc-1")], {"doc-1": "ignored"})
    state = {"databases": {"Product/Feature": {"known_ids": ["id-1"], "last_modified": "2025-01-01"}}}

    assert list(sync_entities(client, "https://acme.fibery.io", ["Product/Feature"], state)) == []
    assert not any("documents/commands" in url for url, _ in client.calls)


def test_failed_listing_does_not_emit_tombstones_or_advance_state():
    class BrokenClient(FiberyClient):
        def post(self, url, json=None):
            if json["command"] == "fibery.entity/query":
                raise RuntimeError("request failed")
            return super().post(url, json)

    client = BrokenClient([], {})
    state = {"databases": {"Product/Feature": {"known_ids": ["id-1"], "last_modified": "2025-01-01"}}}
    try:
        list(sync_entities(client, "https://acme.fibery.io", ["Product/Feature"], state))
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed listing should abort the sync")
    assert state["databases"]["Product/Feature"] == {"known_ids": ["id-1"], "last_modified": "2025-01-01"}


def test_source_requires_token_when_client_is_not_injected(monkeypatch):
    monkeypatch.delenv("FIBERY_API_TOKEN", raising=False)
    try:
        fibery_source("acme", ["Product/Feature"])
    except ValueError as exc:
        assert "token" in str(exc).lower()
    else:
        raise AssertionError("missing token should be rejected")


def test_rate_limit_retries_using_retry_after(monkeypatch):
    sleeps = []

    class RateLimitedClient:
        attempts = 0

        def get(self, url):
            self.attempts += 1
            if self.attempts == 1:
                return Response({}, status=429, headers={"Retry-After": "2"})
            return Response({"ok": True})

    monkeypatch.setattr("cognee_community_connector_fibery.fibery.time.sleep", sleeps.append)
    assert _request(RateLimitedClient(), "GET", "https://acme.fibery.io/api/test") == {"ok": True}
    assert sleeps == [2.0]
