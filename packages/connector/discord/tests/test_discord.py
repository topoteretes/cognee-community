"""Unit and dlt-pipeline tests for the Discord connector. No live bot needed.

* Client tests run ``DiscordClient`` against ``httpx.MockTransport``: headers,
  rate limits, error codes and token handling.
* Rendering tests cover snowflakes, mentions, replies, thread starters and days.
* Sync tests drive ``_iter_rows`` with a fake API and a plain dict as state, and
  run ``discord_source`` through a dlt pipeline into a temp sqlite destination to
  prove the incremental cursor and forget-on-delete.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import httpx
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from conftest import BOT, FORUM, GENERAL, GUILD, NOW, SAM

from cognee_community_connector_discord.discord import (
    USER_AGENT,
    DiscordAPIError,
    DiscordAuthError,
    DiscordClient,
    DiscordIntentError,
    DiscordRateLimitedError,
    DiscordSourceError,
    _archived_threads,
    _DiscordConfig,
    _iter_rows,
    _messages_after,
    _Names,
    discord_source,
    render_day,
    render_message,
    snowflake_time,
    time_snowflake,
)

TODAY = NOW.replace(hour=9)
YESTERDAY = TODAY - timedelta(days=1)
NAMES = _Names(channels={GENERAL: "general"}, roles={"50": "maintainers"})


def _config(**overrides):
    values = {
        "guild_id": GUILD,
        "channel_ids": (),
        "include_threads": True,
        "include_forums": True,
        "include_private_threads": False,
        "include_bot_messages": False,
        "since_days": 90,
        "rescan_days": 7,
        "full_resync": False,
    }
    values.update(overrides)
    return _DiscordConfig(**values)


def _run(discord, state, now=NOW, stats=None, check_active=None, **overrides):
    stats = stats if stats is not None else {}
    rows = _iter_rows(
        discord, _config(**overrides), state, stats, now=lambda: now, check_active=check_active
    )
    return list(rows)


def _by_id(rows):
    return {row["id"]: row for row in rows}


def _day_id(channel, moment):
    return f"discord:{channel}:{moment.date().isoformat()}"


# ---------------------------------------------------------------------------
# DiscordClient over httpx.MockTransport
# ---------------------------------------------------------------------------
def _client(responses):
    queue = list(responses)
    requests = []
    sleeps = []

    def handler(request):
        requests.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = DiscordClient(
        "Bot tok-123",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    return client, requests, sleeps


def test_client_sends_bot_token_and_user_agent():
    client, requests, _ = _client([httpx.Response(200, json={"id": "1"})])
    assert client.get("/applications/@me") == {"id": "1"}
    assert requests[0].url == "https://discord.com/api/v10/applications/@me"
    assert requests[0].headers["Authorization"] == "Bot tok-123"
    assert requests[0].headers["User-Agent"] == USER_AGENT


def test_client_waits_out_429_and_5xx():
    client, _, sleeps = _client(
        [
            httpx.Response(429, json={"retry_after": 1.5, "global": False}),
            httpx.Response(502),
            httpx.Response(200, json=[]),
        ]
    )
    assert client.get("/channels/1/messages") == []
    assert sleeps == [1.5, 2.0]


@pytest.mark.parametrize("retry_after", [900.0, 1.0])
def test_client_gives_up_on_a_long_or_endless_429(retry_after):
    client, requests, sleeps = _client(
        [httpx.Response(429, json={"retry_after": retry_after, "global": True})]
    )
    with pytest.raises(DiscordRateLimitedError):
        client.get("/channels/1/messages")
    assert sleeps == ([] if retry_after > 60 else [1.0] * 4)
    assert len(requests) == (1 if retry_after > 60 else 5)


def test_client_waits_when_the_bucket_is_exhausted():
    client, _, sleeps = _client(
        [
            httpx.Response(
                200,
                json=[],
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.75"},
            )
        ]
    )
    client.get("/channels/1/messages")
    assert sleeps == [0.75]


def test_client_does_not_retry_401_or_403():
    client, requests, _ = _client([httpx.Response(401, json={"code": 0})])
    with pytest.raises(DiscordAuthError):
        client.get("/guilds/1")
    assert len(requests) == 1

    client, requests, _ = _client([httpx.Response(403, json={"code": 50001})])
    with pytest.raises(DiscordAPIError) as info:
        client.get("/channels/1/messages")
    assert (info.value.status, info.value.code) == (403, 50001)
    assert len(requests) == 1


def test_client_rejects_bad_tokens_without_echoing_them():
    with pytest.raises(ValueError) as info:
        DiscordClient("tok with spaces")
    assert "spaces" not in str(info.value)
    assert "s3cr3t" not in repr(DiscordClient("s3cr3t-value"))


# ---------------------------------------------------------------------------
# Snowflakes, paging and rendering
# ---------------------------------------------------------------------------
def test_snowflake_matches_discord_reference_example():
    # The example id from Discord's reference docs.
    assert snowflake_time("175928847299117063") == datetime(
        2016, 4, 30, 11, 18, 25, 796000, tzinfo=timezone.utc
    )
    moment = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert snowflake_time(time_snowflake(moment)) == moment


def test_messages_after_pages_forward_through_newest_first_pages(discord):
    for minute in range(150):
        discord.message(GENERAL, TODAY + timedelta(minutes=minute), f"m{minute}")
    messages = _messages_after(discord, GENERAL, 0)
    assert next(messages)["content"] == "m0" and len(discord.calls) == 1
    assert [m["content"] for m in messages] == [f"m{i}" for i in range(1, 150)]
    afters = [p["after"] for path, p in discord.calls if path.endswith("/messages")]
    assert len(afters) == 2 and afters[0] == "0"


def test_archived_threads_page_by_archive_timestamp():
    pages = [
        {
            "threads": [{"id": "1", "thread_metadata": {"archive_timestamp": "2026-09-02"}}],
            "has_more": True,
        },
        {"threads": [{"id": "2"}], "has_more": False},
    ]
    seen = []

    class Pages:
        def get(self, path, params):
            seen.append(params.get("before"))
            return pages[len(seen) - 1]

    assert [t["id"] for t in _archived_threads(Pages(), "5", private=False)] == ["1", "2"]
    assert seen == [None, "2026-09-02"]


def test_render_message_resolves_mentions_and_keeps_file_names(discord):
    message = discord.message(
        GENERAL,
        TODAY,
        "<@12> see <#901>, ping <@&50> :ok: <:party:123>",
        mentions=[SAM],
        attachments=[{"filename": "plan.pdf", "url": "https://cdn/x?ex=1"}],
    )
    assert render_message(message, NAMES) == (
        "[09:00] Priya Shah: @Sam Lee see #general, ping @maintainers :ok: :party: "
        "[attachment: plan.pdf]"
    )


def test_render_message_marks_replies_and_thread_starters(discord):
    original = discord.message(GENERAL, TODAY, "Export is broken", author=SAM)
    reply = discord.message(GENERAL, TODAY, "On it", type=19, referenced_message=original)
    starter = discord.message("8", TODAY, "", type=21, referenced_message=original)
    assert render_message(reply, NAMES) == "[09:00] Priya Shah (reply to Sam Lee): On it"
    assert render_message(starter, NAMES) == "[09:00] Sam Lee: Export is broken"


def test_render_message_keeps_embeds_stickers_and_polls(discord):
    embed = {
        "type": "rich",
        "title": "PR #12 merged",
        "description": "Adds CSV export",
        "url": "https://github.com/acme/app/pull/12",
    }
    preview = {"type": "link", "title": "Acme", "url": "https://acme.dev"}
    poll = {
        "question": {"text": "Ship Friday?"},
        "answers": [{"poll_media": {"text": "Yes"}}, {"poll_media": {"text": "No"}}],
    }
    message = discord.message(
        GENERAL, TODAY, "", embeds=[embed, preview], sticker_items=[{"name": "wave"}], poll=poll
    )
    assert render_message(message, NAMES) == (
        "[09:00] Priya Shah: [embed: PR #12 merged: Adds CSV export "
        "https://github.com/acme/app/pull/12] [sticker: wave] [poll: Ship Friday? (Yes / No)]"
    )
    assert render_message(discord.message(GENERAL, TODAY, "", embeds=[preview]), NAMES) is None


def test_messages_without_text_are_counted(discord):
    discord.message(GENERAL, TODAY, "hello")
    discord.message(GENERAL, TODAY, "", embeds=[{"type": "link", "url": "https://acme.dev"}])
    discord.message(GENERAL, TODAY, "", attachments=[])
    stats = {}
    _run(discord, {}, stats=stats)
    assert (stats["skipped_no_text"], stats["empty"], stats["scanned"]) == (1, 1, 3)


def test_render_day_for_a_forum_post():
    container = {
        "id": "77",
        "kind": "forum_post",
        "name": "Dark mode",
        "parent": "ideas",
        "tags": ["ui"],
    }
    row = render_day(
        container,
        date(2026, 10, 9),
        ["[09:00] Sam Lee: please"],
        "5",
        {
            "id": GUILD,
            "name": "Acme",
        },
    )
    assert row["id"] == "discord:77:2026-10-09"
    assert row["title"] == "Dark mode 2026-10-09"
    assert row["content"].splitlines()[:4] == [
        "Forum post: Dark mode (in #ideas)",
        "Tags: ui",
        "Server: Acme",
        "Date: 2026-10-09",
    ]
    assert row["url"] == f"https://discord.com/channels/{GUILD}/77/5"


def test_row_becomes_a_discord_document():
    row = render_day(
        {"id": GENERAL, "kind": "channel", "name": "general"},
        date(2026, 10, 9),
        ["[09:00] Sam Lee: hi"],
        "5",
        {"id": GUILD, "name": "Acme"},
    )
    dlt_row = SimpleNamespace(
        table_name="discord_messages", primary_key_value=row["id"], row_data=row, content_hash="h"
    )
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, row["id"]), "discord")
    assert item.data.startswith("# #general 2026-10-09")
    assert item.system_metadata["source"] == "discord"


# ---------------------------------------------------------------------------
# Sync state machine (_iter_rows with a dict as state)
# ---------------------------------------------------------------------------
def test_missing_message_content_intent_stops_the_sync(discord):
    discord.flags = 0
    with pytest.raises(DiscordIntentError):
        _run(discord, {})


def test_first_sync_builds_day_documents_for_channels_threads_and_posts(discord):
    discord.message(GENERAL, YESTERDAY, "Release is Friday")
    discord.message(GENERAL, TODAY, "Export fix merged", author=SAM)
    discord.message(GENERAL, TODAY, "deploy finished", author=BOT)
    discord.message(GENERAL, TODAY, "", type=7)  # member joined
    discord.thread("81", GENERAL, "export bug")
    discord.message("81", TODAY, "Stack trace attached")
    discord.thread("82", FORUM, "Dark mode", archived=True, tags=["72"])
    discord.message("82", YESTERDAY, "Please add dark mode")
    discord.thread("83", GENERAL, "secret", type_=12)
    discord.message("83", TODAY, "private talk")

    rows = _by_id(_run(discord, {}))
    assert set(rows) == {
        _day_id(GENERAL, YESTERDAY),
        _day_id(GENERAL, TODAY),
        _day_id("81", TODAY),
        _day_id("82", YESTERDAY),
    }
    today = rows[_day_id(GENERAL, TODAY)]["content"]
    assert "[09:00] Sam Lee: Export fix merged" in today
    assert "deploy finished" not in today
    assert rows[_day_id("82", YESTERDAY)]["content"].startswith(
        "Forum post: Dark mode (in #ideas)\nTags: ui"
    )


def test_no_change_sync_emits_nothing(discord):
    discord.message(GENERAL, TODAY, "hello")
    state = {}
    _run(discord, state)
    assert _run(discord, state) == []


def test_new_message_reprocesses_only_its_day(discord):
    discord.message(GENERAL, YESTERDAY, "old")
    discord.message(GENERAL, TODAY, "first")
    state = {}
    _run(discord, state)

    discord.message(GENERAL, TODAY + timedelta(hours=1), "second")
    rows = _run(discord, state)
    assert [r["id"] for r in rows] == [_day_id(GENERAL, TODAY)]
    assert "second" in rows[0]["content"]


def test_edit_inside_the_window_is_picked_up(discord):
    message = discord.message(GENERAL, YESTERDAY, "Release is Friday")
    state = {}
    _run(discord, state)

    message["content"] = "Release moved to Monday"
    rows = _run(discord, state)
    assert "Monday" in rows[0]["content"]


def test_deleted_message_rerenders_the_day_and_an_empty_day_is_forgotten(discord):
    discord.message(GENERAL, YESTERDAY, "only message")
    keep = discord.message(GENERAL, TODAY, "keep")
    discord.message(GENERAL, TODAY, "drop")
    state = {}
    _run(discord, state)

    discord.messages[GENERAL] = [keep]
    rows = _by_id(_run(discord, state))
    assert rows[_day_id(GENERAL, YESTERDAY)] == {
        "id": _day_id(GENERAL, YESTERDAY),
        "_deleted": True,
    }
    assert "drop" not in rows[_day_id(GENERAL, TODAY)]["content"]


def test_old_edits_need_a_full_resync(discord):
    old = discord.message(GENERAL, TODAY - timedelta(days=20), "v1")
    discord.message(GENERAL, TODAY, "recent")
    state = {}
    _run(discord, state)

    old["content"] = "v2"
    assert _run(discord, state) == []
    rows = _run(discord, state, full_resync=True)
    assert [r["id"] for r in rows] == [_day_id(GENERAL, TODAY - timedelta(days=20))]
    assert "v2" in rows[0]["content"]


def test_quiet_channel_rereads_the_cursor_day_whole(discord):
    early = TODAY - timedelta(days=20)
    discord.message(GENERAL, early.replace(hour=0, minute=5), "morning")
    discord.message(GENERAL, early.replace(hour=23), "night")
    state = {}
    _run(discord, state)

    discord.message(GENERAL, TODAY, "back again")
    rows = _run(discord, state)
    # The cursor's day is re-read from midnight, so it is unchanged, not cut in half.
    assert [r["id"] for r in rows] == [_day_id(GENERAL, TODAY)]


def test_deleted_thread_is_forgotten(discord):
    discord.thread("81", GENERAL, "export bug")
    discord.message("81", TODAY, "trace")
    state = {}
    _run(discord, state)

    discord.threads.clear()
    assert _run(discord, state) == [{"id": _day_id("81", TODAY), "_deleted": True}]


@pytest.mark.parametrize(("status", "code"), [(404, 10003), (403, 50001), (403, 50013)])
def test_unreadable_channel_is_left_unchanged(discord, status, code):
    discord.message(GENERAL, TODAY, "hello")
    state = {}
    _run(discord, state)

    discord.errors[GENERAL] = (status, code)
    stats = {}
    assert _run(discord, state, stats=stats) == []
    assert stats["unreadable"] == 1 and state["containers"][GENERAL]["days"]


def test_hidden_channel_counts_as_unreadable_once(discord):
    discord.message(GENERAL, TODAY, "hello")
    state = {}
    _run(discord, state)

    # A channel the bot cannot see refuses its thread list and its messages alike.
    discord.errors[GENERAL] = (403, 50001)
    discord.thread_errors[GENERAL] = (403, 50001)
    stats = {}
    assert _run(discord, state, stats=stats) == []
    assert stats["unreadable"] == 1 and state["containers"][GENERAL]["days"]


def test_deleted_channel_is_forgotten(discord):
    discord.message(GENERAL, TODAY, "hello")
    state = {}
    _run(discord, state)

    discord.channels = [c for c in discord.channels if c["id"] != GENERAL]
    assert _run(discord, state) == [{"id": _day_id(GENERAL, TODAY), "_deleted": True}]


def test_channel_ids_that_are_missing_or_unreadable_raise(discord):
    with pytest.raises(DiscordSourceError, match="999"):
        _run(discord, {}, channel_ids=("999",))
    discord.errors[GENERAL] = (403, 50001)
    with pytest.raises(DiscordSourceError, match=GENERAL):
        _run(discord, {}, channel_ids=(GENERAL,))

    # A thread list it cannot read only skips the threads.
    discord.errors.clear()
    discord.message(GENERAL, TODAY, "hello")
    discord.thread_errors[GENERAL] = (403, 50013)
    stats = {}
    rows = _run(discord, {}, stats=stats, channel_ids=(GENERAL,), include_private_threads=True)
    assert [r["id"] for r in rows] == [_day_id(GENERAL, TODAY)] and stats["unreadable"] == 1


def test_unlistable_archived_threads_skip_the_channel_and_keep_its_threads(discord):
    discord.thread("81", GENERAL, "export bug", archived=True)
    discord.message("81", TODAY, "trace")
    state = {}
    _run(discord, state)

    discord.thread_errors[GENERAL] = (403, 50001)
    discord.thread("82", FORUM, "Dark mode")
    discord.message("82", TODAY, "please")
    stats = {}
    rows = _run(discord, state, stats=stats)
    assert [r["id"] for r in rows] == [_day_id("82", TODAY)]
    assert stats["unreadable"] == 1 and "81" in state["containers"]


def test_deselected_channel_is_forgotten(discord):
    discord.message(GENERAL, TODAY, "hello")
    state = {}
    _run(discord, state)
    assert _run(discord, state, channel_ids=(FORUM,), include_forums=False) == [
        {"id": _day_id(GENERAL, TODAY), "_deleted": True}
    ]


def test_other_errors_raise_instead_of_forgetting(discord):
    discord.message(GENERAL, TODAY, "hello")
    state = {}
    _run(discord, state)

    discord.errors[GENERAL] = (500, 0)
    with pytest.raises(DiscordAPIError):
        _run(discord, state)


def test_lost_history_permission_does_not_look_like_deletion(discord):
    discord.message(GENERAL, TODAY, "hello")
    discord.thread("81", GENERAL, "export bug")
    discord.message("81", TODAY, "trace")
    state = {}
    _run(discord, state)

    real_get = discord.get

    def no_history(path, params=None):
        result = real_get(path, params)
        return [] if path.endswith("/messages") else result

    discord.get = no_history
    stats = {}
    assert _run(discord, state, stats=stats) == []
    assert stats["unreadable"] == 2
    assert state["containers"][GENERAL]["days"] and state["containers"]["81"]["days"]


def test_window_emptied_behind_a_stale_last_message_id_is_forgotten(discord):
    discord.message(GENERAL, TODAY - timedelta(days=20), "old")
    recent = discord.message(GENERAL, YESTERDAY, "recent")
    state = {}
    _run(discord, state)

    discord.messages[GENERAL].remove(recent)
    discord.stale_last[GENERAL] = recent["id"]
    assert _run(discord, state) == [{"id": _day_id(GENERAL, YESTERDAY), "_deleted": True}]


def test_channel_added_later_backfills_since_days_from_now(discord):
    state = {}
    _run(discord, state, since_days=10)

    later = NOW + timedelta(days=30)
    discord.channels.append({"id": "904", "type": 0, "name": "late"})
    discord.message("904", later - timedelta(days=5), "inside")
    discord.message("904", later - timedelta(days=15), "outside")
    contents = " ".join(r["content"] for r in _run(discord, state, now=later, since_days=10))
    assert "inside" in contents
    assert "outside" not in contents


def test_quiet_old_thread_costs_no_request(discord):
    discord.thread("82", FORUM, "Dark mode", archived=True)
    discord.message("82", TODAY - timedelta(days=20), "please")
    state = {}
    _run(discord, state)

    discord.calls.clear()
    assert _run(discord, state) == []
    assert not [path for path, _ in discord.calls if path == "/channels/82/messages"]


def test_old_days_keep_only_their_key_and_are_still_forgotten(discord):
    old = TODAY - timedelta(days=20)
    discord.message(GENERAL, old, "old")
    state = {}
    _run(discord, state)

    later = NOW + timedelta(days=80)
    recent = later.replace(hour=9) - timedelta(days=1)
    discord.message(GENERAL, recent, "recent")
    _run(discord, state, now=later)
    days = state["containers"][GENERAL]["days"]
    assert days[old.date().isoformat()] == "" and days[recent.date().isoformat()]
    discord.channels = [c for c in discord.channels if c["id"] != GENERAL]
    assert _run(discord, state, now=later) == [
        {"id": _day_id(GENERAL, old), "_deleted": True},
        {"id": _day_id(GENERAL, recent), "_deleted": True},
    ]


def test_rescan_never_reaches_before_since_days(discord):
    discord.message(GENERAL, TODAY - timedelta(days=5), "too old")
    discord.message(GENERAL, TODAY - timedelta(days=2), "inside")
    state = {}
    emitted = [[r["id"] for r in _run(discord, state, since_days=3)] for _ in range(3)]
    assert emitted == [[_day_id(GENERAL, TODAY - timedelta(days=2))], [], []]


def test_empty_channel_does_not_reach_the_day_before_since(discord):
    state = {}
    assert _run(discord, state, since_days=3) == []

    discord.message(GENERAL, TODAY - timedelta(days=4), "before since")
    discord.message(GENERAL, TODAY, "today")
    assert [r["id"] for r in _run(discord, state, since_days=3)] == [_day_id(GENERAL, TODAY)]


def test_quiet_channel_cursor_day_keeps_its_digest_after_aging_past_since(discord):
    old = TODAY - timedelta(days=8)
    discord.message(GENERAL, old, "before the quiet spell")
    state = {}
    _run(discord, state, since_days=10)

    # The cursor day is now older than since_days, but a new message makes the next
    # run re-read from it, so it must still compare as unchanged.
    later = NOW + timedelta(days=5)
    assert _run(discord, state, now=later, since_days=10) == []
    discord.message(GENERAL, later.replace(hour=9), "back again")
    rows = _run(discord, state, now=later, since_days=10)
    assert [r["id"] for r in rows] == [_day_id(GENERAL, later)]


def test_rate_limit_stops_cleanly_and_resumes(discord):
    discord.message(GENERAL, TODAY, "hello")
    discord.thread("81", GENERAL, "export bug")
    discord.message("81", TODAY, "trace")
    state = {}
    real_get = discord.get

    def limited(path, params=None):
        if path == f"/channels/{GENERAL}/messages":
            raise DiscordRateLimitedError("Discord rate limit exceeded (HTTP 429)")
        return real_get(path, params)

    discord.get = limited
    stats = {}
    rows = _run(discord, state, stats=stats)
    assert [r["id"] for r in rows] == [_day_id("81", TODAY)]
    assert stats["failed"] == 1 and stats["failed_rate_limit"] == 1
    assert state["resume"] == "81" and GENERAL not in state["containers"]

    discord.get = real_get
    stats = {}
    assert [r["id"] for r in _run(discord, state, stats=stats)] == [_day_id(GENERAL, TODAY)]
    assert stats["failed"] == 0 and "resume" not in state


def test_budget_stops_inside_a_channel_and_the_next_run_goes_further(discord):
    for day, count in ((30, 120), (29, 120), (28, 1)):
        for index in range(count):
            moment = TODAY - timedelta(days=day) + timedelta(seconds=index)
            discord.message(GENERAL, moment, f"d{day} m{index}")
    state = {}
    options = {"include_threads": False, "include_forums": False, "max_requests": 6}

    stats = {}
    rows = _run(discord, state, stats=stats, **options)
    assert [r["id"] for r in rows] == [_day_id(GENERAL, TODAY - timedelta(days=30))]
    assert stats["failed_budget"] == 1

    stats = {}
    rows = _run(discord, state, stats=stats, **options)
    assert [r["id"] for r in rows] == [
        _day_id(GENERAL, TODAY - timedelta(days=29)),
        _day_id(GENERAL, TODAY - timedelta(days=28)),
    ]
    assert stats["failed"] == 0


def test_budget_cut_runs_resume_after_the_last_finished_container(discord):
    discord.message(GENERAL, TODAY, "hello")
    discord.thread("81", GENERAL, "export bug")
    discord.message("81", TODAY, "trace")
    discord.thread("82", FORUM, "Dark mode")
    discord.message("82", TODAY, "please")
    state = {}
    # Seven discovery requests, then one container per run.
    emitted = [[r["id"] for r in _run(discord, state, max_requests=8)] for _ in range(3)]
    assert emitted == [[_day_id("81", TODAY)], [_day_id("82", TODAY)], [_day_id(GENERAL, TODAY)]]


def test_refused_private_listing_only_protects_private_threads(discord):
    discord.thread("81", GENERAL, "public", archived=True)
    discord.message("81", TODAY, "public note")
    discord.thread("83", GENERAL, "secret", archived=True, type_=12)
    discord.message("83", TODAY, "private note")
    state = {}
    _run(discord, state, include_private_threads=True)

    discord.thread_errors[f"{GENERAL}:private"] = (403, 50013)
    discord.threads = [t for t in discord.threads if t["id"] != "81"]
    stats = {}
    rows = _run(discord, state, stats=stats, include_private_threads=True)
    assert rows == [{"id": _day_id("81", TODAY), "_deleted": True}]
    assert stats["unreadable"] == 0 and "83" in state["containers"]


def test_budget_cut_runs_still_forget_deleted_threads(discord):
    discord.message(GENERAL, TODAY, "hello")
    for tid in ("81", "82", "84", "85"):
        discord.thread(tid, GENERAL, f"thread {tid}")
        discord.message(tid, TODAY, f"note {tid}")
    state = {}
    _run(discord, state)

    discord.threads = [t for t in discord.threads if t["id"] != "82"]
    stats = {}
    rows = _run(discord, state, stats=stats, max_requests=9)
    assert {"id": _day_id("82", TODAY), "_deleted": True} in rows
    assert stats["failed_budget"] == 1 and stats["failed"] == 0
    assert "82" not in state["containers"]


def test_check_active_runs_before_every_request(discord):
    calls = []

    def check_active():
        calls.append(len(discord.calls))
        if len(calls) == 3:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        _run(discord, {}, check_active=check_active)
    assert calls == [0, 1, 2] and len(discord.calls) == 2


def test_private_threads_and_bot_messages_are_opt_in(discord):
    discord.thread("83", GENERAL, "secret", type_=12)
    discord.message("83", TODAY, "private talk")
    discord.message(GENERAL, TODAY, "deploy finished", author=BOT)
    rows = _by_id(_run(discord, {}, include_private_threads=True, include_bot_messages=True))
    assert _day_id("83", TODAY) in rows
    assert "deploy finished" in rows[_day_id(GENERAL, TODAY)]["content"]


# ---------------------------------------------------------------------------
# discord_source factory
# ---------------------------------------------------------------------------
def test_source_declares_document_path_and_scope(discord):
    source = discord_source(guild_id=GUILD, service=discord, resource_name="discord_acme")
    assert getattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR) == "discord"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "discord_acme"
    assert source.name == "discord_acme"


def test_missing_settings_name_the_env_vars(monkeypatch):
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="DISCORD_GUILD_ID"):
        discord_source()
    with pytest.raises(ValueError, match="DISCORD_BOT_TOKEN"):
        discord_source(guild_id=GUILD)


# ---------------------------------------------------------------------------
# dlt pipeline: incremental cursor + forget-on-delete
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "discord.db").as_posix()
    return dlt.pipeline(
        pipeline_name="discord_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="discord_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _staged(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM discord_messages") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_pipeline_resyncs_only_changes_and_forgets_deletions(dlt_mod, tmp_path, discord, fixed_now):
    discord.message(GENERAL, YESTERDAY, "Release is Friday")
    discord.thread("81", GENERAL, "export bug")
    discord.message("81", TODAY, "trace")
    pipeline = _pipeline(dlt_mod, tmp_path)

    pipeline.run(discord_source(guild_id=GUILD, client=discord))
    assert set(_staged(pipeline)) == {_day_id(GENERAL, YESTERDAY), _day_id("81", TODAY)}

    # The cursor lives in dlt state, so a fresh source object resumes from it.
    source = discord_source(guild_id=GUILD, client=discord)
    pipeline.run(source)
    assert source.cognee_sync_stats["emitted"] == 0

    discord.threads.clear()
    discord.message(GENERAL, TODAY, "new day")
    pipeline.run(discord_source(guild_id=GUILD, client=discord))
    assert set(_staged(pipeline)) == {_day_id(GENERAL, YESTERDAY), _day_id(GENERAL, TODAY)}


def test_failed_run_keeps_staging_and_cursor(dlt_mod, tmp_path, discord, fixed_now):
    discord.message(GENERAL, TODAY, "hello")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(discord_source(guild_id=GUILD, client=discord))

    discord.messages[GENERAL] = []
    discord.errors[GENERAL] = (500, 0)
    with pytest.raises(Exception, match="500"):
        pipeline.run(discord_source(guild_id=GUILD, client=discord))
    assert set(_staged(pipeline)) == {_day_id(GENERAL, TODAY)}

    discord.errors.clear()
    pipeline.run(discord_source(guild_id=GUILD, client=discord))
    assert _staged(pipeline) == {}
