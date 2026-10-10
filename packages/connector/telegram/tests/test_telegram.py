"""Unit tests for the Telegram connector.

The Bot API is mocked by ``FakeBot`` (no token, no network), so these run in CI.
The fixtures mirror payloads observed on a real test bot (group, supergroup
upgrade, channel, edits, removal). Coverage:

  - message rendering (author, channel posts without ``from``, captions, replies,
    forwards, public and members-only links)
  - service messages without text are skipped
  - first sync + cursor: the next run starts at ``last_update_id + 1``
  - a failed run (state not persisted) replays the same updates
  - edits upsert the same ``chat_id:message_id`` row
  - chat selection by id and by @username
  - bot removed from a chat -> hard-delete rows for every stored message
  - group -> supergroup upgrade: new id keeps syncing, removal forgets both ids
  - paging and ``max_updates_per_run``
  - webhook conflict (409) raises a clear error
  - dlt resource is wired with merge + id PK + hard_delete column
  - a row becomes a cognee document tagged ``source="telegram"``
  - each run logs the chats it saw, with their ids
  - a real dlt merge physically removes deleted rows (what orphan_cleanup reads)
"""

import pytest

from cognee_community_connector_telegram.telegram import (
    TelegramWebhookConflictError,
    message_to_row,
    sync_updates,
    telegram_source,
)

GROUP = {"id": -100, "type": "group", "title": "Team chat"}
SUPERGROUP = {"id": -1009, "type": "supergroup", "title": "Team chat"}
CHANNEL = {"id": -1001, "type": "channel", "title": "News", "username": "news_chan"}
OTHER = {"id": -555, "type": "group", "title": "Unrelated"}
ALICE = {"id": 1, "is_bot": False, "first_name": "Alice", "username": "alice"}
BOB = {"id": 2, "is_bot": False, "first_name": "Bob"}


def msg(chat, message_id, text=None, sender=ALICE, date=1_760_000_000, **extra):
    m = {"message_id": message_id, "chat": chat, "date": date, **extra}
    if text is not None:
        m["text"] = text
    if sender is not None and chat.get("type") != "channel":
        m["from"] = sender
    return m


def removed(chat, status="left"):
    return {"chat": chat, "new_chat_member": {"status": status, "user": {"id": 99}}}


class FakeBot:
    """In-memory Bot API: a list of updates served like getUpdates does."""

    def __init__(self, updates=None, conflict=False):
        self.updates = list(updates or [])
        self.conflict = conflict
        self.calls = []

    def add(self, **update):
        next_id = (self.updates[-1]["update_id"] + 1) if self.updates else 1000
        self.updates.append({"update_id": next_id, **update})

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "getMe":
            return {"id": 99, "is_bot": True, "username": "test_bot"}
        if method == "getUpdates":
            if self.conflict:
                raise TelegramWebhookConflictError("409")
            offset = params.get("offset")
            pending = [u for u in self.updates if offset is None or u["update_id"] >= offset]
            return pending[: params.get("limit", 100)]
        raise AssertionError(method)


def run(bot, state, **kwargs):
    return list(sync_updates(bot, state, **kwargs))


# --------------------------------------------------------------------- rendering
def test_message_row_has_document_columns_and_stable_key():
    row = message_to_row(msg(GROUP, 7, "Deploy is on Friday"))
    assert row["id"] == "-100:7"
    assert row["title"] == "Team chat: message 7"
    assert "From: Alice (@alice)" in row["content"]
    assert row["content"].endswith("Deploy is on Friday")
    assert row["_deleted"] is False
    assert row["url"] is None  # private group: no public link


def test_channel_post_uses_channel_as_author_and_public_link():
    row = message_to_row(msg(CHANNEL, 3, "Release notes", sender=None))
    assert "From: News" in row["content"]
    assert row["url"] == "https://t.me/news_chan/3"


def test_caption_and_reply_are_rendered():
    reply = msg(GROUP, 5, "Where is the doc?", sender=BOB)
    row = message_to_row(msg(GROUP, 6, None, caption="here", photo=[{}], reply_to_message=reply))
    assert "[photo] here" in row["content"]
    assert "In reply to Bob: Where is the doc?" in row["content"]


def test_forwarded_message_credits_the_original_author():
    origin = {"type": "user", "date": 1_759_990_000, "sender_user": BOB}
    row = message_to_row(msg(GROUP, 10, "Ship it Friday", forward_origin=origin))
    assert "From: Alice (@alice)" in row["content"]  # who forwarded it
    assert "Forwarded from: Bob" in row["content"]  # who actually wrote it
    hidden = {"type": "hidden_user", "date": 1, "sender_user_name": "Carol"}
    row = message_to_row(msg(GROUP, 11, "x", forward_origin=hidden))
    assert "Forwarded from: Carol" in row["content"]


def test_private_supergroup_gets_members_only_link():
    private = {"id": -1004402301778, "type": "supergroup", "title": "Team"}
    assert message_to_row(msg(private, 2, "hi"))["url"] == "https://t.me/c/4402301778/2"


def test_service_message_without_text_is_skipped():
    assert message_to_row(msg(GROUP, 8, None, new_chat_members=[BOB])) is None


def test_row_is_deterministic():
    m = msg(GROUP, 9, "same")
    assert message_to_row(m) == message_to_row(dict(m))


# --------------------------------------------------------------------- cursor
def test_first_sync_then_cursor_only_returns_new_updates():
    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, "one"))
    bot.add(message=msg(GROUP, 2, "two"))
    state = {}
    assert [r["id"] for r in run(bot, state)] == ["-100:1", "-100:2"]
    assert state["last_update_id"] == 1001

    bot.add(message=msg(GROUP, 3, "three"))
    assert [r["id"] for r in run(bot, state)] == ["-100:3"]
    assert bot.calls[-1][1]["offset"] == 1002
    assert run(bot, state) == []  # nothing new: no rows, cursor unchanged
    assert state["last_update_id"] == 1002


def test_failed_run_replays_same_updates():
    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, "one"))
    persisted = {}
    run(bot, dict(persisted))  # load failed: dlt does not commit the state copy
    assert [r["id"] for r in run(bot, persisted)] == ["-100:1"]


def test_edit_upserts_same_row_and_last_edit_wins():
    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, "draft"))
    bot.add(edited_message=msg(GROUP, 1, "final", edit_date=1_760_000_100))
    rows = run(bot, {})
    assert len(rows) == 1
    assert rows[0]["content"].endswith("final")
    assert "Edited:" in rows[0]["content"]


def test_edit_of_old_channel_post_upserts():
    bot = FakeBot()
    state = {}
    bot.add(channel_post=msg(CHANNEL, 4, "v1", sender=None))
    run(bot, state)
    bot.add(edited_channel_post=msg(CHANNEL, 4, "v2", sender=None, edit_date=1_760_200_000))
    rows = run(bot, state)
    assert [r["id"] for r in rows] == ["-1001:4"] and rows[0]["content"].endswith("v2")


# --------------------------------------------------------------------- selection
def test_selection_by_id_and_username():
    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, "keep"))
    bot.add(channel_post=msg(CHANNEL, 2, "keep too", sender=None))
    bot.add(message=msg(OTHER, 3, "skip"))
    rows = run(bot, {}, chats=[-100, "@News_Chan"])
    assert sorted(r["id"] for r in rows) == ["-1001:2", "-100:1"]


def test_chats_seen_are_logged_with_ids_to_help_pick_a_selection(monkeypatch):
    from cognee_community_connector_telegram import telegram as module

    lines = []

    class RecordingLogger:
        def info(self, fmt, *args):
            lines.append(fmt % args)

        warning = info

    monkeypatch.setattr(module, "logger", RecordingLogger())
    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, "keep"))
    bot.add(message=msg(OTHER, 2, "not selected, still listed"))
    run(bot, {}, chats=[-100])
    seen = next(line for line in lines if "chats seen" in line)
    assert "Team chat (-100)" in seen and "Unrelated (-555)" in seen


# --------------------------------------------------------------------- deletion
def test_bot_removed_from_chat_forgets_its_messages():
    bot = FakeBot()
    state = {}
    bot.add(message=msg(GROUP, 1, "a"))
    bot.add(message=msg(GROUP, 2, "b"))
    bot.add(message=msg(OTHER, 1, "other chat"))
    run(bot, state)
    bot.add(my_chat_member=removed(GROUP, "kicked"))
    rows = run(bot, state)
    assert sorted(r["id"] for r in rows) == ["-100:1", "-100:2"]
    assert all(r["_deleted"] for r in rows)
    assert "-100" not in state["known"] and "-555" in state["known"]


def test_message_and_removal_in_same_batch_ends_deleted():
    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, "a"))
    bot.add(my_chat_member=removed(GROUP))
    rows = run(bot, {})
    assert rows == [{"id": "-100:1", "chat_id": -100, "message_id": 1, "_deleted": True}]


def test_removal_from_unselected_chat_is_ignored():
    bot = FakeBot()
    state = {}
    bot.add(message=msg(GROUP, 1, "a"))
    run(bot, state, chats=[-100])
    bot.add(my_chat_member=removed(OTHER))
    assert run(bot, state, chats=[-100]) == []


# --------------------------------------------------------------------- migration
def test_group_upgrade_keeps_syncing_and_removal_forgets_both_ids():
    bot = FakeBot()
    state = {}
    bot.add(message=msg(GROUP, 5, "before upgrade"))
    bot.add(message=msg(GROUP, 6, None, migrate_to_chat_id=-1009))
    bot.add(message=msg(SUPERGROUP, 1, "after upgrade", migrate_from_chat_id=-100))
    rows = run(bot, state, chats=[-100])  # selected by the OLD id
    assert sorted(r["id"] for r in rows) == ["-1009:1", "-100:5"]

    bot.add(my_chat_member=removed(SUPERGROUP))
    gone = run(bot, state, chats=[-100])
    assert sorted(r["id"] for r in gone) == ["-1009:1", "-100:5"]
    assert all(r["_deleted"] for r in gone)


# --------------------------------------------------------------------- paging / errors
def test_pages_through_more_than_100_updates():
    bot = FakeBot()
    for i in range(1, 251):
        bot.add(message=msg(GROUP, i, f"m{i}"))
    rows = run(bot, {})
    assert len(rows) == 250
    assert sum(1 for c in bot.calls if c[0] == "getUpdates") == 3


def test_max_updates_per_run_fetches_one_page():
    bot = FakeBot()
    for i in range(1, 151):
        bot.add(message=msg(GROUP, i, f"m{i}"))
    state = {}
    assert len(run(bot, state, max_updates=100)) == 100
    assert len(run(bot, state, max_updates=100)) == 50


def test_webhook_conflict_raises_clear_error():
    with pytest.raises(TelegramWebhookConflictError):
        run(FakeBot(conflict=True), {})


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        telegram_source()


# --------------------------------------------------------------------- dlt wiring
def test_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    source = telegram_source(client=FakeBot())
    resource = source.resources["telegram_messages"]
    assert resource.write_disposition == "merge"
    assert resource.compute_table_schema()["columns"]["_deleted"]["hard_delete"] is True
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "telegram"


def test_row_becomes_a_telegram_document_in_cognee():
    # The row -> document mapping is cognee's (the same check the Notion tests do).
    from types import SimpleNamespace
    from uuid import NAMESPACE_OID, uuid5

    from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

    row = message_to_row(msg(CHANNEL, 3, "Release notes", sender=None))
    data_id = uuid5(NAMESPACE_OID, row["id"])
    item = _build_document_data_item(
        SimpleNamespace(row_data=row, table_name="telegram_messages"), data_id, "telegram"
    )
    # source="telegram" (not "dlt") routes the message through normal cognify.
    assert item.system_metadata["source"] == "telegram"
    assert item.system_metadata["external_id"] == "-1001:3"
    assert item.system_metadata["url"] == "https://t.me/news_chan/3"
    assert item.data.startswith("# News: message 3")
    assert item.data.endswith("Release notes")


def test_e2e_dlt_merge_upserts_edits_and_removes_deleted(tmp_path):
    """Offline end to end through a real dlt merge into SQLite: an edit replaces
    the row, and removal from a chat physically deletes its rows."""
    dlt = pytest.importorskip("dlt")
    db_path = tmp_path / "telegram.db"
    pipelines_dir = str(tmp_path / "pipelines")
    bot = FakeBot()

    def sync():
        pipeline = dlt.pipeline(
            pipeline_name="telegram_e2e",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
            dataset_name="telegram_e2e",
            pipelines_dir=pipelines_dir,
        )
        pipeline.run(telegram_source(client=bot))
        with pipeline.sql_client() as client:
            return client.execute_sql("SELECT id, content FROM telegram_messages ORDER BY id")

    bot.add(message=msg(GROUP, 1, "hello"))
    bot.add(message=msg(OTHER, 1, "elsewhere"))
    assert [r[0] for r in sync()] == ["-100:1", "-555:1"]

    bot.add(edited_message=msg(GROUP, 1, "hello, edited", edit_date=1_760_000_500))
    rows = sync()
    assert len(rows) == 2 and rows[0][1].endswith("hello, edited")

    bot.add(my_chat_member=removed(GROUP))
    assert [r[0] for r in sync()] == ["-555:1"]


def test_upgrade_seen_only_from_new_supergroup_side_still_links_ids():
    bot = FakeBot()
    state = {}
    bot.add(message=msg(GROUP, 5, "before upgrade"))
    run(bot, state, chats=[-100])
    # this run misses the old group's migrate_to message; only the new side arrives
    bot.add(message=msg(SUPERGROUP, 1, None, migrate_from_chat_id=-100))
    bot.add(message=msg(SUPERGROUP, 2, "after upgrade"))
    assert [r["id"] for r in run(bot, state, chats=[-100])] == ["-1009:2"]
    bot.add(my_chat_member=removed(SUPERGROUP))
    gone = run(bot, state, chats=[-100])
    assert sorted(r["id"] for r in gone) == ["-1009:2", "-100:5"]


def test_known_ids_are_not_duplicated_across_edits_and_runs():
    bot = FakeBot()
    state = {}
    bot.add(message=msg(GROUP, 1, "a"))
    run(bot, state)
    bot.add(edited_message=msg(GROUP, 1, "a2", edit_date=1_760_000_100))
    bot.add(message=msg(GROUP, 2, "b"))
    run(bot, state)
    assert state["known"]["-100"] == [1, 2]


def test_pipeline_scope_is_set_when_cognee_supports_it():
    from cognee_community_connector_telegram.telegram import PIPELINE_SCOPE_ATTR

    source = telegram_source(token="12345:secret", client=FakeBot())
    if PIPELINE_SCOPE_ATTR is None:
        pytest.skip("cognee < 1.6 has no PIPELINE_SCOPE_ATTR")
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "telegram:12345"
