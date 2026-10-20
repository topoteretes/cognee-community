"""Unit tests for the Telegram connector.

The Bot API is mocked by ``FakeBot`` (see fake_telegram.py; no token, no network),
so these run in CI. Coverage:

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
  - each run logs the chats it saw, with their ids (private chats without names)
  - each bot gets its own table and sync state (``resource_name``)
  - ``check_active`` runs before every Bot API call
  - a real dlt merge physically removes deleted rows (what orphan_cleanup reads)
"""

import pytest
from fake_telegram import BOB, CHANNEL, GROUP, OTHER, SUPERGROUP, FakeBot, msg, removed

from cognee_community_connector_telegram.telegram import (
    TelegramWebhookConflictError,
    message_to_row,
    sync_updates,
    telegram_source,
)


def run(bot, state, **kwargs):
    return list(sync_updates(bot, state, **kwargs))


# --------------------------------------------------------------------- rendering
def test_message_row_has_document_columns_and_stable_key():
    row = message_to_row(msg(GROUP, 7, "Deploy is on Friday"))
    assert row["id"] == "-100:7"
    assert row["title"] == "Deploy is on Friday"  # the first line says more than "message 7"
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
    assert rows == [{"id": "-100:1", "_deleted": True}]


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
def test_one_getupdates_call_per_run_and_a_backlog_catches_up_over_runs():
    bot = FakeBot()
    for i in range(1, 251):
        bot.add(message=msg(GROUP, i, f"m{i}"))
    state = {}
    sizes = [len(run(bot, state)) for _ in range(4)]
    assert sizes == [100, 100, 50, 0]
    assert len(bot.get_updates_calls()) == 4, "never a second getUpdates in one run"


def test_max_updates_per_run_limits_the_page():
    bot = FakeBot()
    for i in range(1, 31):
        bot.add(message=msg(GROUP, i, f"m{i}"))
    state = {}
    assert len(run(bot, state, max_updates=20)) == 20
    assert bot.get_updates_calls()[0]["limit"] == 20
    assert len(run(bot, state, max_updates=20)) == 10


def test_webhook_conflict_raises_clear_error():
    with pytest.raises(TelegramWebhookConflictError):
        run(FakeBot(conflict=True), {})


def test_missing_token_raises_and_the_env_var_is_not_read(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:from-env")
    with pytest.raises(ValueError, match="token="):
        telegram_source()


# --------------------------------------------------------------------- dlt wiring
def test_resource_is_configured_for_merge_and_hard_delete():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    source = telegram_source(client=FakeBot())
    resource = source.resources["telegram_default"]
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
    assert item.data.startswith("# Release notes")
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
            return client.execute_sql("SELECT id, content FROM telegram_default ORDER BY id")

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


def test_each_bot_gets_its_own_table_and_sync_state():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR

    source = telegram_source(token="12345:secret", client=FakeBot())
    assert set(source.resources) == {"telegram_12345"}
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "telegram:12345"
    named = telegram_source(token="12345:secret", resource_name="team_bot", client=FakeBot())
    assert set(named.resources) == {"team_bot"}
    # A chosen name is the scope, so two bots given one name share one state (and
    # the second is refused) instead of silently writing into one table.
    assert getattr(named, PIPELINE_SCOPE_ATTR) == "telegram:team_bot"


def test_check_active_runs_before_every_request():
    """A connection revoked mid-run stops the sync before the next Bot API call,
    not only between emitted rows (every call happens before the first row)."""

    class AccessRevokedError(Exception):
        pass

    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, "a"))

    def revoked_after_first_request():
        if bot.calls:
            raise AccessRevokedError

    source = telegram_source(client=bot, check_active=revoked_after_first_request)
    with pytest.raises(Exception) as caught:
        list(source.resources["telegram_default"])
    causes, error = [], caught.value
    while error is not None:
        causes.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(e, AccessRevokedError) for e in causes)
    assert [method for method, _ in bot.calls] == ["getMe"]  # getUpdates never ran


def test_private_chat_names_stay_out_of_the_logs(monkeypatch):
    from cognee_community_connector_telegram import telegram as module

    lines = []

    class RecordingLogger:
        def info(self, fmt, *args):
            lines.append(fmt % args)

        warning = info

    monkeypatch.setattr(module, "logger", RecordingLogger())
    private = {"id": 42, "type": "private", "first_name": "Carol", "last_name": "Diaz"}
    bot = FakeBot()
    bot.add(message=msg(private, 1, "hi bot"))
    bot.add(my_chat_member=removed(private, "kicked"))
    run(bot, {})
    assert lines and not any("Carol" in line or "Diaz" in line for line in lines)
    assert any("private chat (42)" in line for line in lines)
