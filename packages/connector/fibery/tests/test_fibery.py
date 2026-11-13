import sys
import types
from types import SimpleNamespace

import pytest

from cognee_community_connector_fibery import fibery as fibery_module
from cognee_community_connector_fibery.fibery import (
    FiberyAPIError,
    FiberyClient,
    FiberyRateLimitError,
    _request,
    fibery_source,
    sync_entities,
)

BASE = "https://acme.fibery.io"
DATABASE = "Product/Feature"
SECRET = "doc-1"
COMMENT_SECRET = "comment-1"


def _schema():
    return {
        "fibery/types": [
            {
                "fibery/name": DATABASE,
                "fibery/fields": [
                    {
                        "fibery/name": "Product/Name",
                        "fibery/type": "fibery/text",
                        "fibery/meta": {"fibery/name?": True},
                    },
                    {
                        "fibery/name": "Product/Description",
                        "fibery/type": "Collaboration~Documents/Document",
                    },
                    {"fibery/name": "Product/Status", "fibery/type": "fibery/text"},
                    {
                        "fibery/name": "Product/Owner",
                        "fibery/type": "fibery/user",
                    },
                    {
                        "fibery/name": "comments/comments",
                        "fibery/type": "comments/comment",
                        "fibery/meta": {"fibery/collection?": True},
                    },
                ],
            },
            {
                "fibery/name": "fibery/user",
                "fibery/fields": [
                    {
                        "fibery/name": "user/name",
                        "fibery/type": "fibery/text",
                        "fibery/meta": {"fibery/name?": True},
                    }
                ],
            },
        ]
    }


def _entity(entity_id, public_id, modified, name, secret=None, status="open", comment=None):
    row = {
        "fibery/id": entity_id,
        "fibery/public-id": public_id,
        "fibery/modification-date": modified,
        "Product/Name": name,
        "Product/Status": status,
        "Product/Owner": {"fibery/id": "user-1", "user/name": "Ada"},
        "comments/comments": [],
    }
    if secret is not None:
        row["Product/Description"] = {"Collaboration~Documents/secret": secret}
    if comment is not None:
        row["comments/comments"] = [{"fibery/id": "c1", "comment/document-secret": comment}]
    return row


class Api:
    def __init__(
        self,
        entities,
        documents=None,
        *,
        schema=None,
        fail_on_where=False,
        fail_documents=False,
    ):
        self.entities = list(entities)
        self.documents_by_secret = documents or {}
        self.schema = schema
        self.queries = []
        self.document_calls = []
        self.fail_on_where = fail_on_where
        self.fail_documents = fail_documents

    def command(self, command, args=None):
        if command == "fibery.schema/query":
            return self.schema or _schema()
        if command != "fibery.entity/query":
            raise AssertionError(command)
        query = args["query"]
        self.queries.append(query)
        if self.fail_on_where and query.get("q/where"):
            raise FiberyAPIError("page failed")
        cursor = (args.get("params") or {}).get("$last-seen-id")
        rows = self.entities
        if cursor is not None:
            rows = [row for row in rows if row["fibery/id"] > cursor]
        return rows[: query["q/limit"]]

    def documents(self, secrets):
        self.document_calls.append(list(secrets))
        if self.fail_documents:
            raise FiberyAPIError("documents failed")
        return {secret: self.documents_by_secret[secret] for secret in secrets}


def _sync(api, state=None, page_size=1):
    holder = {} if state is None else state
    rows = list(sync_entities(api, BASE, [DATABASE], holder, page_size=page_size))
    return rows, holder


def test_pages_entities_in_id_order():
    api = Api(
        [
            _entity("a", "1", "2024-01-01T00:00:00.000Z", "Alpha", SECRET),
            _entity("b", "2", "2024-01-02T00:00:00.000Z", "Beta"),
        ],
        {SECRET: "alpha body"},
    )

    rows, state = _sync(api)

    assert [row["id"] for row in rows] == [f"{DATABASE}:a", f"{DATABASE}:b"]
    assert [query["q/limit"] for query in api.queries] == [2, 2]
    assert "q/where" not in api.queries[0]
    assert api.queries[1]["q/where"] == [">", ["fibery/id"], "$last-seen-id"]
    assert state["databases"][DATABASE]["known_ids"] == ["a", "b"]


def test_rich_text_and_comment_are_markdown():
    api = Api(
        [_entity("a", "1", "2024-01-01T00:00:00.000Z", "Alpha", SECRET, comment=COMMENT_SECRET)],
        {SECRET: "Decided to bill annually.", COMMENT_SECRET: "Owned by billing."},
    )

    rows, _state = _sync(api, page_size=10)

    assert rows[0]["title"] == "Alpha"
    assert rows[0]["url"] == f"{BASE}/Product/Feature/1"
    assert "Decided to bill annually." in rows[0]["content"]
    assert "Comment:\nOwned by billing." in rows[0]["content"]
    assert "Owner: Ada" in rows[0]["content"]
    assert rows[0]["_deleted"] is False


def test_entity_without_rich_text_skips_document_reads():
    api = Api([_entity("a", "1", "2024-01-01T00:00:00.000Z", "Alpha", status="shipped")])

    rows, _state = _sync(api, page_size=10)

    assert api.document_calls == []
    assert "Status: shipped" in rows[0]["content"]
    assert rows[0]["title"] == "Alpha"


def test_second_run_emits_a_tombstone_only_after_a_complete_read():
    first = Api(
        [
            _entity("a", "1", "2024-01-01T00:00:00.000Z", "Alpha", SECRET),
            _entity("b", "2", "2024-01-01T00:00:00.000Z", "Beta"),
        ],
        {SECRET: "alpha"},
    )
    _rows, state = _sync(first, page_size=10)

    second = Api(
        [_entity("a", "1", "2024-01-01T00:00:00.000Z", "Alpha", SECRET)],
        {SECRET: "alpha"},
    )
    rows, state = _sync(second, state, page_size=10)

    assert rows == [{"id": f"{DATABASE}:b", "_deleted": True}]
    assert second.document_calls == []
    assert state["databases"][DATABASE]["known_ids"] == ["a"]


def test_later_database_failure_emits_nothing():
    state = {
        "databases": {DATABASE: {"known_ids": ["a"], "last_modified": "2024-01-01T00:00:00.000Z"}}
    }
    original = {
        "databases": {DATABASE: {"known_ids": ["a"], "last_modified": "2024-01-01T00:00:00.000Z"}}
    }
    api = Api(
        [_entity("b", "2", "2024-02-01T00:00:00.000Z", "Beta", SECRET)],
        {SECRET: "body"},
    )
    rows = []
    with pytest.raises(FiberyAPIError):
        for row in sync_entities(api, BASE, [DATABASE, "CRM/Customer"], state, page_size=10):
            rows.append(row)

    assert rows == []
    assert state == original


def test_comment_field_is_read_by_its_secret():
    schema = _schema()
    schema["fibery/types"][0]["fibery/fields"][-1] = {
        "fibery/name": "Product/Discussion",
        "fibery/type": "comments/comment",
        "fibery/meta": {"fibery/collection?": True},
    }
    entity = _entity("a", "1", "2024-01-01T00:00:00.000Z", "Alpha")
    entity["Product/Discussion"] = [
        {
            "fibery/id": "c2",
            "comment/document-secret": COMMENT_SECRET,
            "fibery/creation-date": "2024-01-02T00:00:00.000Z",
        },
        {
            "fibery/id": "c1",
            "comment/document-secret": "comment-0",
            "fibery/creation-date": "2024-01-01T00:00:00.000Z",
        },
    ]
    api = Api(
        [entity],
        {COMMENT_SECRET: "Later note.", "comment-0": "Earlier note."},
        schema=schema,
    )

    rows, _state = _sync(api, page_size=10)

    selected = api.queries[0]["q/select"]
    assert any(isinstance(item, dict) and "Product/Discussion" in item for item in selected)
    assert api.document_calls == [["comment-0", COMMENT_SECRET]]
    assert rows[0]["content"].index("Earlier note.") < rows[0]["content"].index("Later note.")


def test_failed_page_emits_nothing_and_keeps_state():
    state = {
        "databases": {
            DATABASE: {"known_ids": ["a", "b"], "last_modified": "2024-01-01T00:00:00.000Z"}
        }
    }
    original = {
        "databases": {
            DATABASE: {"known_ids": ["a", "b"], "last_modified": "2024-01-01T00:00:00.000Z"}
        }
    }
    api = Api(
        [
            _entity("a", "1", "2024-01-01T00:00:00.000Z", "Alpha"),
            _entity("b", "2", "2024-01-02T00:00:00.000Z", "Beta"),
        ],
        fail_on_where=True,
    )

    with pytest.raises(FiberyAPIError):
        _sync(api, state, page_size=1)

    assert state == original


def test_failed_document_read_emits_no_tombstone():
    state = {"databases": {DATABASE: {"known_ids": ["gone"], "last_modified": ""}}}
    api = Api(
        [_entity("a", "1", "2024-02-01T00:00:00.000Z", "Alpha", SECRET)],
        fail_documents=True,
    )

    with pytest.raises(FiberyAPIError):
        _sync(api, state, page_size=10)

    assert state == {"databases": {DATABASE: {"known_ids": ["gone"], "last_modified": ""}}}


def test_retry_after_too_long_does_not_sleep():
    slept = []

    def call():
        return SimpleNamespace(status_code=429, headers={"Retry-After": "120"})

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(fibery_module.time, "sleep", slept.append)
    try:
        with pytest.raises(FiberyRateLimitError):
            _request(call)
    finally:
        monkeypatch.undo()
    assert slept == []


def test_short_429_is_retried():
    responses = iter(
        [
            SimpleNamespace(status_code=429, headers={"Retry-After": "0"}),
            SimpleNamespace(
                status_code=200,
                headers={},
                json=lambda: {"success": True, "result": []},
            ),
        ]
    )
    slept = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(fibery_module.time, "sleep", slept.append)
    try:
        response = _request(lambda: next(responses))
    finally:
        monkeypatch.undo()

    assert response.status_code == 200
    assert slept == [0.0]


def test_auth_failure_is_not_retried():
    calls = []

    def call():
        calls.append(1)
        return SimpleNamespace(status_code=401, headers={})

    with pytest.raises(FiberyAPIError):
        _request(call)
    assert calls == [1]


def test_account_and_database_names_are_checked():
    with pytest.raises(ValueError):
        _account_or_database("acme.fibery.io", [DATABASE])
    with pytest.raises(ValueError):
        _account_or_database("acme", ["Feature"])


def _account_or_database(account, databases):
    from cognee_community_connector_fibery.fibery import _account_name, _database_names

    _account_name(account)
    _database_names(databases)


def test_client_reads_commands_and_markdown_documents():
    http = _Http(
        [
            _response(200, {"success": True, "result": {"ok": True}}),
            _response(200, [{"secret": SECRET, "content": "Decided to bill annually."}]),
        ]
    )
    client = FiberyClient("secret-token", BASE, http=http)

    assert client.command("fibery.schema/query") == {"ok": True}
    assert client.documents([SECRET]) == {SECRET: "Decided to bill annually."}
    assert http.posts[0][0] == f"{BASE}/api/commands"
    assert http.posts[0][2]["Authorization"] == "Token secret-token"
    assert http.posts[1][0] == f"{BASE}/api/documents/commands?format=md"
    assert http.posts[1][1]["command"] == "get-documents"


def test_failed_command_raises():
    client = FiberyClient("secret-token", BASE, http=_Http([_response(200, {"success": False})]))
    with pytest.raises(FiberyAPIError):
        client.command("fibery.schema/query")


def test_source_is_a_document_source():
    pytest.importorskip("dlt")
    document_source_tag = _document_source_tag()
    source = fibery_source("Acme", [DATABASE], client=Api([]))
    assert document_source_tag(source) == "fibery"
    assert source.cognee_pipeline_scope == "fibery:acme"


def test_replace_disposition_is_rejected(tmp_path):
    dlt = pytest.importorskip("dlt")
    _document_source_tag()
    database = tmp_path / "fibery.db"
    pipeline = dlt.pipeline(
        pipeline_name="fibery_disposition",
        destination=dlt.destinations.sqlalchemy(credentials=f"sqlite:///{database.as_posix()}"),
        dataset_name="docs",
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    api = Api([])
    source = fibery_source("acme", [DATABASE], client=api)

    with pytest.raises(Exception, match="merge"):
        pipeline.run(source, write_disposition="replace")

    assert api.queries == []


def _response(status, payload, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, json=lambda: payload)


class _Http:
    def __init__(self, responses):
        self._responses = list(responses)
        self.posts = []

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json, headers))
        return self._responses.pop(0)

    def close(self):
        return None


def _document_source_tag():
    try:
        from cognee.tasks.ingestion.dlt_utils import document_source_tag
    except ImportError:
        for name in list(sys.modules):
            if name == "cognee" or name.startswith("cognee."):
                del sys.modules[name]
        _install_dlt_utils()
        from cognee.tasks.ingestion.dlt_utils import document_source_tag
    return document_source_tag


def _install_dlt_utils():
    modules = {
        "cognee": types.ModuleType("cognee"),
        "cognee.tasks": types.ModuleType("cognee.tasks"),
        "cognee.tasks.ingestion": types.ModuleType("cognee.tasks.ingestion"),
        "cognee.tasks.ingestion.dlt_utils": types.ModuleType("cognee.tasks.ingestion.dlt_utils"),
    }
    for name, module in modules.items():
        module.__path__ = []
        sys.modules[name] = module
    dlt_utils = modules["cognee.tasks.ingestion.dlt_utils"]
    dlt_utils.DOCUMENT_SOURCE_ATTR = "cognee_document_source"
    dlt_utils.DOCUMENT_SYNC_VERSION = 2
    dlt_utils.PIPELINE_SCOPE_ATTR = "cognee_pipeline_scope"

    def document_source_tag(item):
        tag = getattr(item, dlt_utils.DOCUMENT_SOURCE_ATTR, None)
        return tag if isinstance(tag, str) and tag else None

    dlt_utils.document_source_tag = document_source_tag
