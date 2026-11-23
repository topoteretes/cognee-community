"""No update is ever lost, and errors stay safe.

FakeBot confirms (drops) updates below the offset like the real Bot API, so these
tests can see an update disappear for good. They cover a load that fails, a host
revoking access between rows, a failure in the middle of a run, the update_id
restart after a quiet week, long flood waits, 409s, the token never reaching an
error, the state of the connector's first version, and a state owned by another bot.
"""

import http.client
import io
import json
import urllib.error

import dlt
import pytest
from dlt.destinations.exceptions import DatabaseUndefinedRelation
from fake_telegram import GROUP, OTHER, FakeBot, msg, removed

from cognee_community_connector_telegram import telegram as module
from cognee_community_connector_telegram.telegram import (
    LEGACY_RESOURCE_NAME,
    TelegramAPIError,
    TelegramAuthError,
    TelegramBotClient,
    TelegramConflictError,
    TelegramRateLimitError,
    TelegramWebhookConflictError,
    _chat_and_aliases,
    sync_updates,
    telegram_source,
)

TOKEN = "111:secret-token-value"
DAY = 24 * 3600


@pytest.fixture(autouse=True)
def no_active_pipeline():
    """Iterating a resource outside a run uses the active pipeline's state, so make
    sure a pipeline left active by an earlier test cannot leak its cursor in."""
    from dlt.common.configuration.container import Container
    from dlt.common.pipeline import PipelineContext

    Container()[PipelineContext].deactivate()
    yield
    Container()[PipelineContext].deactivate()


def _pipeline(tmp_path, name="telegram_delivery"):
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'tg.db'}"),
        dataset_name="tg",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _ids(pipeline, table):
    with pipeline.sql_client() as client:
        try:
            return [r[0] for r in client.execute_sql(f"SELECT id FROM {table} ORDER BY id")]
        except DatabaseUndefinedRelation:  # nothing loaded into this table yet
            return []


# --------------------------------------------------------------------- no loss
def test_a_failed_load_loses_nothing_even_with_a_backlog():
    bot = FakeBot()
    for i in range(1, 151):
        bot.add(message=msg(GROUP, i, f"m{i}"))
    persisted: dict = {}

    list(sync_updates(bot, dict(persisted)))  # the load fails: dlt keeps the old state

    delivered = [r["id"] for r in sync_updates(bot, persisted)]
    delivered += [r["id"] for r in sync_updates(bot, persisted)]
    assert len(delivered) == 150, "all 150 updates still arrive after the failed run"


def test_check_active_revoked_between_rows_loses_nothing(tmp_path):
    bot = FakeBot()
    for i in range(1, 251):
        bot.add(message=msg(GROUP, i, f"m{i}"))
    pipeline = _pipeline(tmp_path)
    checks = {"n": 0}

    def revoke_after_a_few_checks():
        checks["n"] += 1
        if checks["n"] > 5:  # past both API calls, between rows
            raise PermissionError("access revoked")

    with pytest.raises(Exception, match="access revoked"):
        pipeline.run(
            telegram_source(token=TOKEN, client=bot, check_active=revoke_after_a_few_checks)
        )
    assert _ids(pipeline, "telegram_111") == []

    for _ in range(3):
        pipeline.run(telegram_source(token=TOKEN, client=bot))
    assert len(_ids(pipeline, "telegram_111")) == 250, "every message arrives in later runs"


def test_check_active_runs_between_rows(tmp_path):
    bot = FakeBot()
    for i in range(1, 4):
        bot.add(message=msg(GROUP, i, f"m{i}"))
    checks = []
    source = telegram_source(token=TOKEN, client=bot, check_active=lambda: checks.append(1))
    rows = list(source.resources["telegram_111"])
    # 2 before the two API calls, then guarded_rows: before and after taking each
    # row, and twice at the end. Without the per-row guard this would be 2.
    assert len(rows) == 3
    assert len(checks) == 2 + 2 * 3 + 2


def test_a_failure_in_the_middle_of_a_run_leaves_cursor_and_rows_untouched(monkeypatch):
    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, "one"))
    state: dict = {}
    list(sync_updates(bot, state))
    before = json.loads(json.dumps(state))
    bot.add(message=msg(GROUP, 2, "two"))
    bot.add(my_chat_member=removed(OTHER))
    bot.add(message=msg(GROUP, 3, "three"))

    original = module.message_to_row

    def explode_on_third(message):
        if message.get("message_id") == 3:
            raise RuntimeError("bad payload")
        return original(message)

    monkeypatch.setattr(module, "message_to_row", explode_on_third)
    with pytest.raises(RuntimeError):
        list(sync_updates(bot, state))
    assert state == before, "the cursor and known messages did not move"


def test_a_run_stopped_after_its_first_row_saves_no_state():
    bot = FakeBot()
    for i in (1, 2, 3):
        bot.add(message=msg(GROUP, i, f"m{i}"))
    state: dict = {}
    rows = sync_updates(bot, state)
    next(rows)
    rows.close()  # what happens when check_active raises between rows
    assert state == {}, "the cursor only moves once every row is out"


# --------------------------------------------------------------------- quiet week
def test_after_a_quiet_week_the_cursor_is_not_sent_so_restarted_ids_survive():
    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, "old"))
    state: dict = {}
    list(sync_updates(bot, state, now=1_000_000))
    assert state["last_update_id"] == 1000

    # A week later the old, never-confirmed update has expired (Telegram keeps
    # updates about 24h), and update_id restarted below the cursor.
    bot.updates.clear()
    bot.add_with_id(42, message=msg(GROUP, 2, "after the quiet week"))
    rows = list(sync_updates(bot, state, now=1_000_000 + 8 * DAY))

    assert [r["id"] for r in rows] == ["-100:2"]
    assert bot.get_updates_calls()[-1]["offset"] is None, "no offset, so nothing is confirmed"
    assert state["last_update_id"] == 42


def test_within_six_days_the_offset_is_used():
    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, "old"))
    state: dict = {}
    list(sync_updates(bot, state, now=1_000_000))
    list(sync_updates(bot, state, now=1_000_000 + 5 * DAY))
    assert bot.get_updates_calls()[-1]["offset"] == 1001


# --------------------------------------------------------------------- rate limits
def _http_error(status, payload, url="https://api.telegram.org/botSECRET/getUpdates"):
    return urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(json.dumps(payload).encode()))


class _Opener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    def __call__(self, request):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        body = outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode()
        return Response(body)


def _client(*outcomes):
    sleeps = []
    client = TelegramBotClient(TOKEN, sleep=sleeps.append)
    client._open = _Opener(*outcomes)
    return client, sleeps


def test_short_flood_waits_are_slept_long_ones_stop_without_sleeping():
    client, sleeps = _client(
        _http_error(429, {"parameters": {"retry_after": 5}}), {"ok": True, "result": []}
    )
    assert client.call("getUpdates") == []
    assert sleeps == [5.0]

    client, sleeps = _client(_http_error(429, {"parameters": {"retry_after": 3600}}))
    with pytest.raises(TelegramRateLimitError) as info:
        client.call("getUpdates")
    assert info.value.retry_after == 3600 and sleeps == []


@pytest.mark.parametrize(
    "blip",
    [
        ConnectionResetError(104, "reset"),
        http.client.RemoteDisconnected("closed"),
        http.client.IncompleteRead(b"{"),
        TimeoutError(),
    ],
)
def test_a_dropped_connection_is_retried(blip):
    client, sleeps = _client(blip, {"ok": True, "result": []})
    assert client.call("getUpdates") == []
    assert len(sleeps) == 1


def test_a_200_without_json_is_a_telegram_error_without_the_token():
    client, _ = _client(b"<html>bad gateway</html>")
    with pytest.raises(TelegramAPIError) as info:
        client.call("getUpdates")
    assert "secret-token-value" not in str(info.value)


@pytest.mark.parametrize("name", ["Team-Bot", "team bot", "teamBot", "1team"])
def test_a_resource_name_dlt_would_rename_is_refused(name):
    with pytest.raises(ValueError, match="resource_name"):
        telegram_source(token=TOKEN, client=FakeBot(), resource_name=name)


def test_an_empty_resource_name_falls_back_to_the_bot_name():
    source = telegram_source(token=TOKEN, client=FakeBot(), resource_name="")
    assert list(source.resources) == ["telegram_111"]


def test_a_long_flood_wait_ends_the_run_cleanly_and_reports_it():
    class FloodedBot(FakeBot):
        def call(self, method, **params):
            if method == "getUpdates":
                raise TelegramRateLimitError("flood", 3600)
            return super().call(method, **params)

    bot = FloodedBot()
    bot.add(message=msg(GROUP, 1, "waiting"))
    source = telegram_source(token=TOKEN, client=bot)
    assert list(source.resources["telegram_111"]) == []
    assert source.cognee_sync_stats["failed_rate_limit"] == 1
    assert all(type(v) is int for v in source.cognee_sync_stats.values())


# --------------------------------------------------------------------- errors
def test_409_says_whether_a_webhook_or_another_poller_is_the_cause():
    client, _ = _client(
        _http_error(
            409, {"description": "Conflict: can't use getUpdates method while webhook is active"}
        )
    )
    with pytest.raises(TelegramWebhookConflictError, match="webhook"):
        client.call("getUpdates")
    client, _ = _client(
        _http_error(409, {"description": "Conflict: terminated by other getUpdates request"})
    )
    with pytest.raises(TelegramConflictError, match="another process") as info:
        client.call("getUpdates")
    assert not isinstance(info.value, TelegramWebhookConflictError)


def test_a_401_never_carries_the_token():
    client, _ = _client(
        _http_error(
            401, {"description": "Unauthorized"}, url=f"https://api.telegram.org/bot{TOKEN}/getMe"
        )
    )
    with pytest.raises(TelegramAuthError) as info:
        client.call("getMe")
    error = info.value
    assert TOKEN not in str(error) and "secret-token" not in str(error)
    assert error.__cause__ is None and error.__suppress_context__, "the HTTPError (URL) is hidden"


def test_a_rejected_token_is_counted_and_raised():
    class RejectingBot(FakeBot):
        def call(self, method, **params):
            raise TelegramAuthError("rejected")

    source = telegram_source(token=TOKEN, client=RejectingBot())
    with pytest.raises(Exception) as info:
        list(source.resources["telegram_111"])
    chain, error = [], info.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(e, TelegramAuthError) for e in chain)
    assert source.cognee_sync_stats["failed_auth"] == 1


def test_the_client_repr_hides_the_token():
    assert TOKEN not in repr(TelegramBotClient(TOKEN))
    assert "secret" not in repr(telegram_source(token=TOKEN))


def test_privacy_mode_gets_a_warning(monkeypatch):
    lines = []

    class RecordingLogger:
        def info(self, fmt, *args):
            lines.append(fmt % args)

        warning = info

    monkeypatch.setattr(module, "logger", RecordingLogger())
    list(telegram_source(token=TOKEN, client=FakeBot(can_read_all=False)).resources["telegram_111"])
    assert any("privacy mode is on" in line for line in lines)


def test_a_full_page_warns_that_updates_are_waiting(monkeypatch):
    warnings = []

    class RecordingLogger:
        def info(self, fmt, *args):
            pass

        def warning(self, fmt, *args):
            warnings.append(fmt % args)

    monkeypatch.setattr(module, "logger", RecordingLogger())
    bot = FakeBot()
    for i in range(1, 6):
        bot.add(message=msg(GROUP, i, f"m{i}"))
    list(sync_updates(bot, {}, max_updates=5))
    assert any("more are waiting" in w for w in warnings)

    warnings.clear()
    list(sync_updates(FakeBot(), {}, max_updates=5))
    assert warnings == [], "no warning when the page is not full"


def test_upgrade_links_that_form_a_cycle_do_not_loop_forever():
    assert sorted(_chat_and_aliases(-5, {"-5": "-4", "-4": "-5"})) == [-5, -4]
    assert _chat_and_aliases("-5", {}) == [-5]


def test_service_is_an_alias_for_client():
    bot = FakeBot()
    bot.add(message=msg(GROUP, 1, "hi"))
    assert len(list(telegram_source(token=TOKEN, service=bot).resources["telegram_111"])) == 1


def test_max_updates_per_run_must_fit_one_page():
    for bad in (0, 101, None):
        with pytest.raises(ValueError, match="between 1 and 100"):
            telegram_source(token=TOKEN, client=FakeBot(), max_updates_per_run=bad)


def test_titles_use_the_first_line_and_are_shortened():
    row = module.message_to_row(msg(GROUP, 1, "x" * 120 + "\nsecond line"))
    assert len(row["title"]) == 80 and row["title"].endswith("…")


# --------------------------------------------------------------------- state ownership
def test_a_sync_state_from_another_bot_is_refused(tmp_path):
    pipeline = _pipeline(tmp_path)
    first = FakeBot(bot_id=111)
    first.add(message=msg(GROUP, 1, "first bot"))
    pipeline.run(telegram_source(token=TOKEN, client=first, resource_name="team"))

    other = FakeBot(bot_id=222)
    other.add(message=msg(GROUP, 1, "second bot"))
    with pytest.raises(Exception, match="belongs to another bot"):
        pipeline.run(telegram_source(token="222:other", client=other, resource_name="team"))
    assert other.get_updates_calls() == [], "the second bot never read (or confirmed) anything"


def test_the_first_versions_state_and_table_are_kept(tmp_path):
    """A pipeline synced by the first version (table telegram_messages, scope per bot)
    keeps working: edits and removals reach the old rows instead of stranding them."""
    pipeline = _pipeline(tmp_path)
    bot = FakeBot()

    # What the first version left behind: rows in telegram_messages and its state.
    @dlt.resource(
        name=LEGACY_RESOURCE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def telegram_messages():
        state = dlt.current.resource_state()
        state.update(last_update_id=1001, known={"-100": [1, 2]}, migrated_from={})
        for i in (1, 2):
            yield {"id": f"-100:{i}", "title": f"old {i}", "content": f"old {i}", "_deleted": False}

    @dlt.source(name="telegram")
    def first_version():
        return telegram_messages

    pipeline.run(first_version())
    assert _ids(pipeline, LEGACY_RESOURCE_NAME) == ["-100:1", "-100:2"]

    bot.next_id = 1002
    bot.add(edited_message=msg(GROUP, 1, "edited in the new version", edit_date=1_760_000_100))
    bot.add(message=msg(GROUP, 3, "new message"))
    pipeline.run(telegram_source(token=TOKEN, client=bot))
    assert bot.get_updates_calls()[0]["offset"] == 1002, "the old cursor is picked up"
    assert _ids(pipeline, LEGACY_RESOURCE_NAME) == ["-100:1", "-100:2", "-100:3"]
    assert _ids(pipeline, "telegram_111") == [], "no second table next to the old one"

    bot.add(my_chat_member=removed(GROUP))
    pipeline.run(telegram_source(token=TOKEN, client=bot))
    assert _ids(pipeline, LEGACY_RESOURCE_NAME) == [], "removal still forgets the old rows"
