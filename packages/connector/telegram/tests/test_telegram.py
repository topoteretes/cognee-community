"""Unit tests for the Telegram dlt connector.

Two layers, all runnable in CI with no live Telegram:

* DB-free tests for config normalization, chat filtering, message rendering
  (text/caption, urls, reply/forward metadata), and the pure ``sync_updates``
  state machine (ingest, the ``update_id`` cursor, edit re-emission by content
  hash, forget-on-removal tombstones) with a plain dict standing in for dlt's
  resource state and hand-built Bot API update payloads.
* dlt-pipeline tests (temp sqlite destination, fake ``get_updates`` client)
  covering the acceptance criteria end to end: first sync loads messages,
  edits upsert on ``merge``, a removed chat's messages drop out via the
  ``_deleted`` hard-delete marker.
"""

import json

import pytest

from cognee_community_connector_telegram.telegram import (
    TELEGRAM_SOURCE_NAME,
    _chat_matches,
    _message_url,
    _normalize_chats,
    _render_message,
    sync_updates,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_GROUP = {"id": -1001234567890, "title": "Team Chat", "type": "supergroup"}
_CHANNEL = {"id": -1009876543210, "title": "News", "type": "channel", "username": "team_news"}


def _message(
    update_id: int,
    text: str,
    *,
    message_id: int = 1,
    chat: dict | None = None,
    key: str = "message",
    **extra,
) -> dict:
    payload = {
        "message_id": message_id,
        "chat": chat or dict(_GROUP),
        "from": {"id": 7, "first_name": "Alice", "last_name": "Smith"},
        "date": 1756500000,
        "text": text,
        **extra,
    }
    return {"update_id": update_id, key: payload}


def _removal(update_id: int, chat: dict, status: str = "kicked") -> dict:
    return {
        "update_id": update_id,
        "my_chat_member": {"chat": chat, "new_chat_member": {"status": status}},
    }


def _run(state: dict, updates: list[dict], chats=None) -> list[dict]:
    return list(sync_updates(_normalize_chats(chats), state, updates))


def _live_rows(rows) -> dict[str, dict]:
    return {row["id"]: row for row in rows if not row.get("_deleted")}


def _deleted_ids(rows) -> set[str]:
    return {row["id"] for row in rows if row.get("_deleted")}


# ---------------------------------------------------------------------------
# Config normalization + chat filtering
# ---------------------------------------------------------------------------


def test_normalize_chats_splits_ids_and_usernames():
    config = _normalize_chats([-1001234567890, "-42", "@Team_News", "other"])
    assert config.chat_ids == {-1001234567890, -42}
    assert config.chat_usernames == {"team_news", "other"}


def test_empty_chats_means_unfiltered():
    config = _normalize_chats(None)
    assert config.unfiltered
    assert _chat_matches({"id": 123}, config)


def test_chat_filter_by_id():
    config = _normalize_chats([_GROUP["id"]])
    assert _chat_matches(_GROUP, config)
    assert not _chat_matches(_CHANNEL, config)


def test_chat_filter_by_username_case_insensitive():
    config = _normalize_chats(["@Team_NEWS"])
    assert _chat_matches(_CHANNEL, config)
    assert not _chat_matches(_GROUP, config)


# ---------------------------------------------------------------------------
# Message rendering
# ---------------------------------------------------------------------------


def test_text_message_becomes_document_row():
    row = _render_message(_message(1, "Ship the connector today.")["message"])
    assert row["id"] == f"{_GROUP['id']}/1"
    assert row["title"] == "Ship the connector today."
    assert row["content"].startswith("Ship the connector today.")
    assert "- chat: Team Chat" in row["content"]
    assert "- sender: Alice Smith" in row["content"]
    metadata = json.loads(row["metadata"])
    assert metadata["chat_id"] == _GROUP["id"]
    assert metadata["sender"] == "Alice Smith"
    assert metadata["date"] == "2025-08-29T20:40:00Z"
    assert row["_deleted"] is False


def test_caption_is_ingested_with_media_note():
    update = _message(1, "", photo=[{"file_id": "x"}])
    update["message"]["text"] = None
    update["message"]["caption"] = "Our launch photo"
    row = _render_message(update["message"])
    assert row["title"] == "Our launch photo"
    assert "attached media: photo" in row["content"]
    assert json.loads(row["metadata"])["media"] == "photo"


def test_message_without_text_or_caption_is_skipped():
    update = _message(1, "", photo=[{"file_id": "x"}])
    update["message"]["text"] = ""
    assert _render_message(update["message"]) is None


def test_reply_and_forward_land_in_metadata_and_content():
    update = _message(
        1,
        "Agreed.",
        reply_to_message={"message_id": 41},
        forward_origin={"type": "user", "sender_user": {"first_name": "Bob"}},
    )
    row = _render_message(update["message"])
    metadata = json.loads(row["metadata"])
    assert metadata["reply_to_message_id"] == 41
    assert metadata["forwarded_from"] == "Bob"
    assert "- reply to message 41" in row["content"]
    assert "- forwarded from: Bob" in row["content"]


def test_long_first_line_is_truncated_into_title():
    row = _render_message(_message(1, "x" * 200)["message"])
    assert len(row["title"]) == 80
    assert row["title"].endswith("...")


def test_public_chat_url_uses_username():
    assert _message_url(_CHANNEL, 5) == "https://t.me/team_news/5"


def test_private_supergroup_url_uses_internal_id():
    assert _message_url(_GROUP, 5) == "https://t.me/c/1234567890/5"


def test_basic_group_gets_deterministic_identifier():
    assert _message_url({"id": -424242}, 5) == "telegram://chat/-424242/message/5"


# ---------------------------------------------------------------------------
# Ingest path (pure sync_updates)
# ---------------------------------------------------------------------------


def test_first_sync_ingests_group_and_channel_messages():
    state: dict = {}
    rows = _live_rows(
        _run(
            state,
            [
                _message(10, "hello group", message_id=1),
                _message(11, "channel post", message_id=2, chat=dict(_CHANNEL), key="channel_post"),
            ],
        )
    )
    assert set(rows) == {f"{_GROUP['id']}/1", f"{_CHANNEL['id']}/2"}


def test_chat_filter_drops_other_chats():
    state: dict = {}
    rows = _live_rows(
        _run(
            state,
            [
                _message(10, "keep me", message_id=1, chat=dict(_CHANNEL), key="channel_post"),
                _message(11, "drop me", message_id=2),
            ],
            chats=["@team_news"],
        )
    )
    assert set(rows) == {f"{_CHANNEL['id']}/1"}


def test_service_updates_without_message_are_ignored():
    state: dict = {}
    assert _run(state, [{"update_id": 10, "poll": {"id": "p"}}]) == []
    assert state["offset"] == 11


# ---------------------------------------------------------------------------
# Incremental cursor (update_id offset)
# ---------------------------------------------------------------------------


def test_offset_advances_past_highest_update_id():
    state: dict = {}
    _run(state, [_message(10, "a", message_id=1), _message(12, "b", message_id=2)])
    assert state["offset"] == 13


def test_second_run_with_no_updates_emits_nothing_and_keeps_offset():
    state: dict = {}
    _run(state, [_message(10, "a")])
    assert _run(state, []) == []
    assert state["offset"] == 11


def test_replayed_update_is_not_re_emitted():
    # A crash between fetch and load can replay the same update on the next
    # run; the fingerprint map must swallow the duplicate.
    state: dict = {}
    update = _message(10, "same message")
    _run(state, [update])
    assert _run(state, [update]) == []


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------


def test_edited_message_re_emits_row():
    state: dict = {}
    _run(state, [_message(10, "v1", message_id=1)])

    edited = _message(11, "v2", message_id=1, key="edited_message", edit_date=1756500100)
    rows = _live_rows(_run(state, [edited]))
    assert set(rows) == {f"{_GROUP['id']}/1"}
    assert rows[f"{_GROUP['id']}/1"]["content"].startswith("v2")


def test_cosmetic_edit_with_identical_content_is_not_re_emitted():
    state: dict = {}
    _run(state, [_message(10, "same", message_id=1)])
    # e.g. an inline-keyboard change: Telegram sends edited_message, text intact.
    assert _run(state, [_message(11, "same", message_id=1, key="edited_message")]) == []
    assert state["offset"] == 12  # the cursor still advances


def test_edit_of_message_never_seen_still_ingests():
    # The bot may have joined after the original message; the edit is the
    # first (and only) sighting — it must ingest, not KeyError.
    state: dict = {}
    rows = _live_rows(_run(state, [_message(10, "late edit", message_id=9, key="edited_message")]))
    assert set(rows) == {f"{_GROUP['id']}/9"}


# ---------------------------------------------------------------------------
# Forget-on-removal (the reliably detectable deletion signal)
# ---------------------------------------------------------------------------


def test_bot_removed_from_chat_tombstones_its_messages():
    state: dict = {}
    _run(
        state,
        [
            _message(10, "a", message_id=1),
            _message(11, "b", message_id=2),
            _message(12, "elsewhere", message_id=3, chat=dict(_CHANNEL), key="channel_post"),
        ],
    )

    rows = _run(state, [_removal(13, dict(_GROUP), status="kicked")])
    assert _deleted_ids(rows) == {f"{_GROUP['id']}/1", f"{_GROUP['id']}/2"}
    assert _live_rows(rows) == {}
    # The other chat's state is untouched; the removed chat's is gone.
    assert str(_CHANNEL["id"]) in state["chats"]
    assert str(_GROUP["id"]) not in state["chats"]


def test_leaving_a_chat_tombstones_like_a_kick():
    state: dict = {}
    _run(state, [_message(10, "a", message_id=1)])
    rows = _run(state, [_removal(11, dict(_GROUP), status="left")])
    assert _deleted_ids(rows) == {f"{_GROUP['id']}/1"}


def test_joining_a_chat_does_not_tombstone():
    state: dict = {}
    _run(state, [_message(10, "a", message_id=1)])
    assert _run(state, [_removal(11, dict(_GROUP), status="administrator")]) == []
    assert str(_GROUP["id"]) in state["chats"]


def test_removal_from_unknown_chat_is_a_no_op():
    state: dict = {}
    assert _run(state, [_removal(10, {"id": -555})]) == []
    assert state["offset"] == 11


# ---------------------------------------------------------------------------
# Factory validation
# ---------------------------------------------------------------------------


def test_source_requires_token(monkeypatch):
    pytest.importorskip("dlt")
    from cognee_community_connector_telegram.telegram import telegram_source

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Bot token is required"):
        telegram_source()


def test_source_declares_document_marker():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_telegram.telegram import telegram_source

    source = telegram_source(client=_FakeClient([]))
    assert TELEGRAM_SOURCE_NAME == "telegram"
    assert document_source_tag(source) == "telegram"


# ---------------------------------------------------------------------------
# dlt pipeline: incremental sync + forget-on-removal end to end (needs dlt)
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stands in for the Bot API: serves queued updates past the offset."""

    def __init__(self, updates: list[dict]):
        self.updates = list(updates)

    def get_updates(self, offset: int) -> list[dict]:
        return [u for u in self.updates if u["update_id"] >= offset][:100]


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_pipeline(dlt, tmp_path, client):
    """Run telegram_source through a dlt pipeline into a temp sqlite destination."""
    from cognee_community_connector_telegram.telegram import telegram_source

    db_path = (tmp_path / "telegram.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="telegram_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="telegram_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(telegram_source(client=client))
    return pipeline


def _read_messages(pipeline):
    """Return {id: row-dict} for the telegram_messages table (positional read —
    dlt's sqlalchemy cursor exposes a SQLAlchemy Result without ``description``)."""
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM telegram_messages") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


def test_pipeline_first_sync_loads_messages(dlt_mod, tmp_path):
    client = _FakeClient(
        [
            _message(10, "hello group", message_id=1),
            _message(11, "channel post", message_id=2, chat=dict(_CHANNEL), key="channel_post"),
        ]
    )
    pipeline = _run_pipeline(dlt_mod, tmp_path, client)

    rows = _read_messages(pipeline)
    assert set(rows) == {f"{_GROUP['id']}/1", f"{_CHANNEL['id']}/2"}
    assert "- sender: Alice Smith" in rows[f"{_GROUP['id']}/1"]["content"]


def test_pipeline_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    client = _FakeClient([_message(10, "v1", message_id=1)])
    _run_pipeline(dlt_mod, tmp_path, client)

    client.updates.append(_message(11, "v2", message_id=1, key="edited_message"))
    pipeline = _run_pipeline(dlt_mod, tmp_path, client)

    rows = _read_messages(pipeline)
    assert rows[f"{_GROUP['id']}/1"]["content"].startswith("v2")
    assert "v1" not in rows[f"{_GROUP['id']}/1"]["content"]


def test_pipeline_removed_chat_messages_drop_out_on_resync(dlt_mod, tmp_path):
    client = _FakeClient(
        [
            _message(10, "goes away", message_id=1),
            _message(11, "stays", message_id=2, chat=dict(_CHANNEL), key="channel_post"),
        ]
    )
    _run_pipeline(dlt_mod, tmp_path, client)

    client.updates.append(_removal(12, dict(_GROUP)))
    pipeline = _run_pipeline(dlt_mod, tmp_path, client)

    rows = _read_messages(pipeline)
    # The _deleted hard-delete marker removed the rows on merge; cognee's
    # orphan_cleanup forgets them downstream.
    assert f"{_GROUP['id']}/1" not in rows
    assert f"{_CHANNEL['id']}/2" in rows
