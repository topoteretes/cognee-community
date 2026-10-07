"""Unit tests for the Telegram Bot API dlt connector.

Two layers, all runnable in CI with no bot token and no network:

* DB-free tests for update→row mapping, chat scoping, retry behavior, and the
  generic document DataItem tagging (``source="telegram"``) that routes
  messages through normal cognify.
* dlt-pipeline tests (fake Bot API, temp sqlite destination) covering the
  acceptance criteria: backfill, the ``update_id`` incremental cursor across
  runs, and edit upserts under merge.
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest

# The row → document-DataItem mapping is generic and owned by the ingestion
# layer (any document source uses it), not the connector.
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_telegram.telegram import (
    TELEGRAM_SOURCE_NAME,
    TelegramClient,
    _retry_delay,
    _update_to_row,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _message(message_id, text, chat_id=-1001, update_id=1, **overrides):
    message = {
        "message_id": message_id,
        "date": 1700000000,
        "chat": {"id": chat_id, "title": "team"},
        "from": {"username": "alice"},
        "text": text,
    }
    message.update(overrides)
    return {"update_id": update_id, "message": message}


class FakeTelegramClient(TelegramClient):
    """Stand-in for the Bot API backed by canned updates (no network)."""

    def __init__(self, updates):
        super().__init__(token="test-token")
        self._updates = list(updates)
        self.calls = []

    def get_updates(self, offset=None, limit=100, timeout=30):
        self.calls.append({"offset": offset, "limit": limit})
        pending = [u for u in self._updates if offset is None or u["update_id"] >= offset]
        return pending[:limit]


# ---------------------------------------------------------------------------
# Update → row (DB-free)
# ---------------------------------------------------------------------------


def test_update_to_row_flattens_message():
    row = _update_to_row(_message(7, "hello", update_id=10), scope=None)

    assert row["id"] == "-1001:7"
    assert row["update_id"] == 10
    assert row["chat_id"] == -1001
    assert row["chat_title"] == "team"
    assert row["sender"] == "alice"
    assert row["content"] == "hello"
    assert row["date"].startswith("2023-11-14")
    assert row["_deleted"] is False


def test_update_to_row_handles_channel_and_edits():
    channel = {
        "update_id": 3,
        "channel_post": {
            "message_id": 9,
            "date": 1700000000,
            "chat": {"id": -1002, "title": "news"},
            "text": "broadcast",
        },
    }
    row = _update_to_row(channel, scope=None)
    assert row["id"] == "-1002:9"
    assert row["sender"] == "news"  # channels have no sender — fall back to chat title

    edited = {
        "update_id": 4,
        "edited_message": {
            "message_id": 7,
            "date": 1700000000,
            "chat": {"id": -1001, "title": "team"},
            "from": {"username": "alice"},
            "text": "hello (edited)",
        },
    }
    row = _update_to_row(edited, scope=None)
    # Same stable id as the original → merge rewrites the row instead of duplicating it.
    assert row["id"] == "-1001:7"
    assert row["content"] == "hello (edited)"


def test_update_to_row_uses_caption_and_skips_empty():
    captioned = _message(5, None, update_id=2)
    captioned["message"]["caption"] = "a photo"
    assert _update_to_row(captioned, scope=None)["content"] == "a photo"

    service = {
        "update_id": 3,
        "message": {"message_id": 6, "date": 1700000000, "chat": {"id": -1001}, "text": "   "},
    }
    assert _update_to_row(service, scope=None) is None
    assert _update_to_row({"update_id": 4}, scope=None) is None  # unknown update kind


def test_update_to_row_applies_chat_scope():
    update = _message(1, "hi", chat_id=-1001, update_id=1)
    assert _update_to_row(update, scope={-1001}) is not None
    assert _update_to_row(update, scope={-1002}) is None


# ---------------------------------------------------------------------------
# Retry behavior (DB-free)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_retry_delay_prefers_retry_after():
    assert _retry_delay(7.5, 3) == 7.5
    assert _retry_delay(None, 2) == 4.0


def test_request_retries_429_then_succeeds(monkeypatch):
    from cognee_community_connector_telegram.telegram import _request

    calls = []
    responses = [
        _FakeResponse(429, {"parameters": {"retry_after": 3}}),
        _FakeResponse(200, {"ok": True, "result": []}),
    ]

    def http_get(url, params):
        calls.append(params)
        return responses.pop(0)

    delays = []
    monkeypatch.setattr("time.sleep", delays.append)

    assert _request(http_get, "https://x/botT/getUpdates", {}) == {"ok": True, "result": []}
    assert delays == [3.0]  # honored the API's retry_after, not backoff


def test_request_raises_after_persistent_failure(monkeypatch):
    from cognee_community_connector_telegram.telegram import _request

    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="after 5 attempts"):
        _request(lambda url, params: _FakeResponse(500), "https://x", {})


def test_request_raises_immediately_on_auth_error():
    from cognee_community_connector_telegram.telegram import _request

    with pytest.raises(RuntimeError, match="HTTP 401"):
        _request(lambda url, params: _FakeResponse(401), "https://x", {})


# ---------------------------------------------------------------------------
# Source wiring (DB-free) / row → DataItem
# ---------------------------------------------------------------------------


def test_telegram_source_requires_token():
    from cognee_community_connector_telegram.telegram import telegram_source

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        telegram_source()


def test_telegram_source_requires_dlt(monkeypatch):
    import builtins

    from cognee_community_connector_telegram.telegram import telegram_source

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dlt":
            raise ImportError("no dlt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="connector-telegram"):
        telegram_source(bot_token="x")


def test_telegram_source_wiring():
    pytest.importorskip("dlt")

    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_telegram.telegram import telegram_source

    resource = telegram_source(client=FakeTelegramClient([]))

    assert resource.name == "telegram_messages"
    schema = resource.compute_table_schema()
    write_disposition = schema.get("write_disposition")
    if isinstance(write_disposition, dict):  # dlt may normalize to a config dict
        write_disposition = write_disposition.get("disposition")
    assert write_disposition == "merge"

    columns = schema["columns"]
    assert columns["id"].get("primary_key") is True
    assert columns["_deleted"].get("hard_delete") is True

    # The document marker routes rows through normal cognify (not "dlt").
    assert TELEGRAM_SOURCE_NAME == "telegram"
    assert document_source_tag(resource) == "telegram"


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        table_name="telegram_messages",
        row_data={"id": "-1001:7", "content": "hello"},
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "-1001:7")

    item = _build_document_data_item(row, data_id, "telegram")

    assert item.system_metadata["source"] == "telegram"
    assert item.system_metadata["external_id"] == "-1001:7"
    assert item.data_id == data_id
    assert "hello" in item.data


# ---------------------------------------------------------------------------
# dlt pipeline: backfill + incremental cursor + edit upsert (needs dlt)
# ---------------------------------------------------------------------------


def _run_sync(dlt, tmp_path, updates, **source_kwargs):
    """Run telegram_source through a dlt pipeline into a temp sqlite destination."""
    from cognee_community_connector_telegram.telegram import telegram_source

    db_path = (tmp_path / "telegram.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="telegram_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="telegram_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    # Same pipeline name + dir on every run so resource_state (the update_id
    # cursor) persists across syncs.
    pipeline.run(telegram_source(client=FakeTelegramClient(updates), **source_kwargs))
    return pipeline


def _read_messages(pipeline):
    """Return {id: row-dict} for the telegram_messages table."""
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            "SELECT id, chat_title, sender, content FROM telegram_messages"
        ) as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"chat_title": row[1], "sender": row[2], "content": row[3]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_backfill_loads_messages(dlt_mod, tmp_path):
    updates = [_message(1, "first", update_id=1), _message(2, "second", update_id=2)]

    rows = _read_messages(_run_sync(dlt_mod, tmp_path, updates))

    assert set(rows) == {"-1001:1", "-1001:2"}
    assert rows["-1001:1"]["content"] == "first"


def test_second_run_only_syncs_new_updates(dlt_mod, tmp_path):
    first = [_message(1, "first", update_id=1)]
    _run_sync(dlt_mod, tmp_path, first)

    # The bot has since seen update 2; run 2 must pick up only what changed —
    # update 1 is not re-yielded (the cursor advanced past it).
    both = [*first, _message(2, "second", update_id=2)]
    rows = _read_messages(_run_sync(dlt_mod, tmp_path, both))

    assert set(rows) == {"-1001:1", "-1001:2"}


def test_edit_rewrites_row_instead_of_duplicating(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, [_message(1, "v1", update_id=1)])

    edited = {
        "update_id": 2,
        "edited_message": {
            "message_id": 1,
            "date": 1700000000,
            "chat": {"id": -1001, "title": "team"},
            "from": {"username": "alice"},
            "text": "v2",
        },
    }
    rows = _read_messages(_run_sync(dlt_mod, tmp_path, [_message(1, "v1", update_id=1), edited]))

    assert set(rows) == {"-1001:1"}
    assert rows["-1001:1"]["content"] == "v2"


def test_chat_scope_filters_sync(dlt_mod, tmp_path):
    updates = [
        _message(1, "kept", chat_id=-1001, update_id=1),
        _message(2, "dropped", chat_id=-1002, update_id=2),
    ]
    rows = _read_messages(_run_sync(dlt_mod, tmp_path, updates, chat_ids=[-1001]))

    assert set(rows) == {"-1001:1"}
