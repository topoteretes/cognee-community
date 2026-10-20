"""Sync logic (_iter_rows): change detection, sweeps, moves, deletes and guards.

Each scenario replays an action that was checked on a live Twist workspace
(see conftest.py for what each action changes).
"""

import pytest

from cognee_community_connector_twist.twist import (
    TwistClient,
    TwistError,
    TwistForbiddenError,
    TwistNotFoundError,
    _iter_rows,
    _reset_stats,
)

LAUNCH, DESIGN = 852087, 852086


def run(fake, state, **kwargs):
    """One sync run; returns ({row id: row}, stats). ``state`` is updated in place."""
    stats: dict[str, int] = {}
    _reset_stats(stats)
    kwargs.setdefault("full_sync_every", 0)
    client = TwistClient(fake.token, http_client=fake.http_client())
    rows = _iter_rows(client, workspace_id=fake.workspace_id, state=state, stats=stats, **kwargs)
    return {row["id"]: row for row in rows}, stats


NO_COUNTS = {
    "scanned": 0,
    "changed": 0,
    "unchanged": 0,
    "deleted": 0,
    "skipped": 0,
    "held_back": 0,
    "failed": 0,
    "failed_auth": 0,
    "failed_rate_limit": 0,
}


def counts(**values):
    return {**NO_COUNTS, **values}


def synced(fake):
    """State after a first full run."""
    state: dict = {}
    run(fake, state)
    return state


def test_first_run_emits_every_thread_and_channel(seeded):
    rows, stats = run(seeded, {})
    assert sorted(rows) == sorted(
        [f"thread:{t}" for t in seeded.threads] + [f"channel:{LAUNCH}", f"channel:{DESIGN}"]
    )
    checkout = next(r for r in rows.values() if r["title"] == "Checkout redesign")
    assert "Agreed, let's go with option B." in checkout["content"]
    assert checkout["cognee_node_set"] == [f"twist:{seeded.workspace_id}:{LAUNCH}"]
    assert checkout["url"].startswith(f"https://twist.com/a/{seeded.workspace_id}/ch/{LAUNCH}/t/")
    assert stats == counts(scanned=3, changed=3)


def test_unchanged_rerun_emits_nothing_and_reads_no_comments(seeded):
    state = synced(seeded)
    seeded.requests.clear()
    rows, stats = run(seeded, state)
    assert rows == {}
    assert stats["unchanged"] == 3
    assert not [r for r in seeded.requests if r.url.path.endswith("comments/get")]


def test_new_comment_re_renders_only_that_thread(seeded, thread_named):
    state = synced(seeded)
    launch = thread_named("Launch date")
    seeded.add_comment(launch["id"], "Moved to 15 March.")
    rows, _ = run(seeded, state)
    assert list(rows) == [f"thread:{launch['id']}"]
    assert "Moved to 15 March." in rows[f"thread:{launch['id']}"]["content"]


def test_opening_post_edit_is_caught_by_version(seeded, thread_named):
    # Live: editing the body moved version and last_edited_ts, not last_updated_ts.
    state = synced(seeded)
    launch = thread_named("Launch date")
    seeded.edit_thread_body(launch["id"], "Target launch is 15 March.")
    rows, _ = run(seeded, state)
    assert "15 March" in rows[f"thread:{launch['id']}"]["content"]


def test_latest_comment_edit_is_caught_right_away(seeded, thread_named):
    # Live: editing the latest comment moved the thread's version (6 -> 7).
    state = synced(seeded)
    checkout = thread_named("Checkout redesign")
    last = seeded.comments[checkout["id"]][-1]
    seeded.edit_comment(checkout["id"], last["id"], "Agreed, option B ships first.")
    rows, _ = run(seeded, state)
    assert "ships first" in rows[f"thread:{checkout['id']}"]["content"]


def test_earlier_comment_edit_waits_for_the_sweep(seeded, thread_named):
    # Live: editing an earlier comment changes nothing on the thread, so only a
    # sweep sees it.
    state = synced(seeded)
    checkout = thread_named("Checkout redesign")
    first = seeded.comments[checkout["id"]][0]
    seeded.edit_comment(checkout["id"], first["id"], "Option B tested better with 12 users.")

    rows, _ = run(seeded, state)
    assert rows == {}

    rows, stats = run(seeded, state, full_sync=True)
    assert list(rows) == [f"thread:{checkout['id']}"]
    assert "12 users" in rows[f"thread:{checkout['id']}"]["content"]
    assert stats["changed"] == 1
    assert stats["unchanged"] == 2  # swept, but their text was the same


def test_sweep_runs_every_nth_run(seeded, thread_named):
    state = synced(seeded)  # run 0 is a sweep
    checkout = thread_named("Checkout redesign")
    first = seeded.comments[checkout["id"]][0]
    seeded.edit_comment(checkout["id"], first["id"], "Edited.")
    rows, _ = run(seeded, state, full_sync_every=2)  # run 1
    assert rows == {}
    rows, _ = run(seeded, state, full_sync_every=2)  # run 2: sweep
    assert "Edited." in rows[f"thread:{checkout['id']}"]["content"]


def test_rename_updates_the_title_without_the_system_message(seeded, thread_named):
    state = synced(seeded)
    logo = thread_named("Logo refresh")
    seeded.rename(logo["id"], "Logo refresh v2")
    rows, _ = run(seeded, state)
    row = rows[f"thread:{logo['id']}"]
    assert row["title"] == "Logo refresh v2"
    assert "changed the subject" not in row["content"]


def test_deleted_comment_disappears_from_the_thread(seeded, thread_named):
    # Live: the comment stays listed with deleted=true; only the thread version moves.
    state = synced(seeded)
    checkout = thread_named("Checkout redesign")
    second = seeded.comments[checkout["id"]][1]
    seeded.delete_comment(checkout["id"], second["id"])
    rows, _ = run(seeded, state)
    assert "Agreed" not in rows[f"thread:{checkout['id']}"]["content"]


def test_closed_thread_is_kept_and_marked(seeded, thread_named):
    state = synced(seeded)
    launch = thread_named("Launch date")
    seeded.close(launch["id"])
    rows, stats = run(seeded, state)
    assert "Status: closed" in rows[f"thread:{launch['id']}"]["content"]
    assert stats["deleted"] == 0


def test_move_between_synced_channels_keeps_the_id(seeded, thread_named):
    state = synced(seeded)
    logo = thread_named("Logo refresh")
    seeded.move(logo["id"], LAUNCH)
    rows, stats = run(seeded, state)
    row = rows[f"thread:{logo['id']}"]
    assert row["cognee_node_set"] == [f"twist:{seeded.workspace_id}:{LAUNCH}"]
    assert f"/ch/{LAUNCH}/" in row["url"]
    assert stats["deleted"] == 0


def test_move_out_of_the_selected_channels_forgets_the_thread(seeded, thread_named):
    state: dict = {}
    run(seeded, state, channel_ids=[LAUNCH])
    launch = thread_named("Launch date")
    seeded.move(launch["id"], DESIGN)
    rows, _ = run(seeded, state, channel_ids=[LAUNCH])
    assert rows == {f"thread:{launch['id']}": {"id": f"thread:{launch['id']}", "_deleted": True}}


def test_deleted_thread_becomes_a_tombstone(seeded, thread_named):
    state = synced(seeded)
    launch = thread_named("Launch date")
    seeded.delete_thread(launch["id"])
    rows, stats = run(seeded, state)
    assert rows == {f"thread:{launch['id']}": {"id": f"thread:{launch['id']}", "_deleted": True}}
    assert stats["deleted"] == 1
    assert f"thread:{launch['id']}" not in state["items"]


def test_archived_channels_are_opt_in(seeded):
    # Live: archived channels are still returned by channels/get, flagged archived.
    seeded.channels[DESIGN]["archived"] = True
    rows, _ = run(seeded, {})
    assert f"channel:{DESIGN}" not in rows
    rows, _ = run(seeded, {}, include_archived=True)
    assert "Status: archived" in rows[f"channel:{DESIGN}"]["content"]
    rows, _ = run(seeded, {}, channel_ids=[DESIGN])
    assert f"channel:{DESIGN}" in rows


def test_inbox_state_is_ignored(seeded, thread_named):
    state = synced(seeded)
    seeded.threads[thread_named("Launch date")["id"]]["is_archived"] = False
    rows, stats = run(seeded, state)
    assert rows == {}
    assert stats["deleted"] == 0


def test_unknown_selected_channel_stops_without_forgetting(seeded):
    state: dict = {}
    run(seeded, state, channel_ids=[LAUNCH])
    before = dict(state)
    with pytest.raises(TwistNotFoundError, match="Nothing was synced or forgotten"):
        list(run(seeded, state, channel_ids=[LAUNCH, 999])[0])
    assert state == before


def test_thread_gone_mid_run_is_skipped_not_forgotten(seeded, thread_named):
    # A 404 can also mean lost access, so it never produces a tombstone by itself.
    state = synced(seeded)
    launch = thread_named("Launch date")
    seeded.add_comment(launch["id"], "New reply.")
    seeded.hidden_comment_threads.add(launch["id"])
    rows, stats = run(seeded, state)
    assert rows == {}
    assert stats["skipped"] == 1
    assert f"thread:{launch['id']}" in state["items"]


def _twelve_threads(fake):
    fake.add_channel(LAUNCH, "launch")
    for i in range(12):
        fake.add_thread(LAUNCH, f"Thread {i}")


def test_mass_delete_is_held_back_until_the_next_run_confirms_it(fake):
    _twelve_threads(fake)
    state = synced(fake)
    for thread_id in list(fake.threads)[:11]:
        fake.delete_thread(thread_id)
    rows, stats = run(fake, state)
    assert rows == {}  # no error: a scheduled sync keeps running
    assert (stats["held_back"], stats["deleted"]) == (11, 0)
    assert len(state["items"]) == 13  # still tracked, so the next run sees them gone
    rows, stats = run(fake, state)
    assert (stats["held_back"], stats["deleted"]) == (0, 11)
    assert all(row["_deleted"] for row in rows.values())
    assert "pending_delete" not in state


def test_held_back_items_that_come_back_are_not_forgotten(fake):
    # A wrong channel selection for one run, then the right one again.
    _twelve_threads(fake)
    fake.add_channel(852086, "design")
    state = synced(fake)
    _, stats = run(fake, state, channel_ids=[852086])
    assert stats["held_back"] == 13
    rows, stats = run(fake, state)
    assert rows == {}
    assert (stats["deleted"], stats["unchanged"]) == (0, 12)
    assert "pending_delete" not in state


def test_a_rolling_mass_delete_is_forgotten_not_held_forever(fake):
    # Review case: 16 of 30 deleted, then one more before every run. An exact
    # match on the held set would hold 16, 17, 18, ... and never forget.
    fake.add_channel(LAUNCH, "launch")
    for i in range(29):
        fake.add_thread(LAUNCH, f"Thread {i}")
    state = synced(fake)  # 29 threads + 1 channel = 30 items
    ids = list(fake.threads)
    for thread_id in ids[:16]:
        fake.delete_thread(thread_id)
    _, stats = run(fake, state)
    assert (stats["held_back"], stats["deleted"]) == (16, 0)
    for n in range(16, 20):
        fake.delete_thread(ids[n])
        _, stats = run(fake, state)
        # The ones still gone are confirmed; the single new one is ordinary.
        expected = 17 if n == 16 else 1
        assert (stats["held_back"], stats["deleted"]) == (0, expected)
    assert len(state["items"]) == 30 - 20


def test_an_ordinary_delete_after_a_mass_delete_is_not_held(fake):
    _twelve_threads(fake)
    state = synced(fake)
    ids = list(fake.threads)
    for thread_id in ids[:10]:
        fake.delete_thread(thread_id)
    _, stats = run(fake, state)
    assert stats["held_back"] == 10
    fake.delete_thread(ids[10])
    _, stats = run(fake, state)
    assert (stats["held_back"], stats["deleted"]) == (0, 11)
    assert "pending_delete" not in state


def test_confirmed_deletes_go_through_while_a_new_batch_is_held(fake):
    # With a lower threshold a second batch can be large on its own: the first
    # batch is confirmed and forgotten in the same run the second is held.
    fake.add_channel(LAUNCH, "launch")
    for i in range(29):
        fake.add_thread(LAUNCH, f"Thread {i}")
    state = synced(fake)  # 30 items
    ids = list(fake.threads)
    for thread_id in ids[:10]:
        fake.delete_thread(thread_id)
    _, stats = run(fake, state, max_delete_ratio=0.3)
    assert stats["held_back"] == 10
    for thread_id in ids[10:20]:
        fake.delete_thread(thread_id)
    rows, stats = run(fake, state, max_delete_ratio=0.3)
    assert (stats["deleted"], stats["held_back"]) == (10, 10)
    assert sorted(rows) == sorted(f"thread:{t}" for t in ids[:10])


def test_held_items_partly_back_are_kept_and_the_rest_forgotten(fake):
    # A glitch that clears for some items: those come back unchanged, the ones
    # still gone in the second run are forgotten.
    _twelve_threads(fake)
    state = synced(fake)
    ids = list(fake.threads)
    hidden = {thread_id: fake.threads[thread_id] for thread_id in ids[:12]}
    for thread_id in ids[:12]:
        fake.threads.pop(thread_id)
    _, stats = run(fake, state)
    assert stats["held_back"] == 12
    for thread_id in ids[:6]:  # six come back
        fake.threads[thread_id] = hidden[thread_id]
    rows, stats = run(fake, state)
    assert (stats["deleted"], stats["held_back"], stats["unchanged"]) == (6, 0, 6)
    assert sorted(rows) == sorted(f"thread:{t}" for t in ids[6:12])


def test_allow_mass_delete_forgets_at_once(fake):
    _twelve_threads(fake)
    state = synced(fake)
    for thread_id in list(fake.threads)[:11]:
        fake.delete_thread(thread_id)
    rows, stats = run(fake, state, allow_mass_delete=True)
    assert stats["deleted"] == 11
    assert all(row["_deleted"] for row in rows.values())


def test_small_deletes_never_trip_the_guard(seeded):
    state = synced(seeded)
    for thread_id in list(seeded.threads):
        seeded.delete_thread(thread_id)
    _, stats = run(seeded, state)
    assert stats["deleted"] == 3


def test_check_active_runs_even_when_nothing_changed(seeded):
    # A long run of unchanged threads yields no rows, but a host must still be
    # able to stop it.
    state = synced(seeded)
    calls = []
    rows, _ = run(seeded, state, check_active=lambda: calls.append(1))
    assert rows == {}
    assert len(calls) >= 3 + 2  # every thread and every channel


def test_unread_thread_still_gets_all_its_comments(seeded, thread_named):
    # Twist's docs: last_obj_index is the reader's position, -1 for a thread the
    # token's user has not read. The comments must be read anyway.
    checkout = thread_named("Checkout redesign")
    seeded.unread_threads.add(checkout["id"])
    rows, _ = run(seeded, {})
    content = rows[f"thread:{checkout['id']}"]["content"]
    assert "Option B tested better with users." in content
    assert "Agreed, let's go with option B." in content


def test_stale_zero_comment_count_still_gets_the_comments(seeded, thread_named):
    # Review case: a thread listed with comment_count 0 that has a comment.
    checkout = thread_named("Checkout redesign")
    seeded.threads[checkout["id"]]["comment_count"] = 0
    rows, _ = run(seeded, {})
    assert "Agreed, let's go with option B." in rows[f"thread:{checkout['id']}"]["content"]


def test_thread_without_a_comment_count_still_gets_its_comments(seeded, thread_named):
    checkout = thread_named("Checkout redesign")
    seeded.threads[checkout["id"]]["comment_count"] = None
    rows, _ = run(seeded, {})
    assert "Agreed, let's go with option B." in rows[f"thread:{checkout['id']}"]["content"]


def test_forbidden_channel_stops_the_run_and_forgets_nothing(seeded):
    # Losing access to a channel is not a delete: the run stops instead.
    state = synced(seeded)
    before = dict(state)
    seeded.forbidden_channels.add(DESIGN)
    with pytest.raises(TwistForbiddenError):
        run(seeded, state)
    assert state == before


def test_archived_channel_synced_only_on_request_is_dropped_afterwards(seeded, thread_named):
    # Review case: kept-after-archiving must not keep a channel that was only
    # synced because it was included or listed.
    seeded.channels[DESIGN]["archived"] = True
    logo = thread_named("Logo refresh")
    for opt_in in ({"include_archived": True}, {"channel_ids": [DESIGN]}):
        state: dict = {}
        rows, _ = run(seeded, state, **opt_in)
        assert f"thread:{logo['id']}" in rows
        rows, _ = run(seeded, state)  # back to the default selection
        assert rows[f"thread:{logo['id']}"] == {"id": f"thread:{logo['id']}", "_deleted": True}


def test_a_failed_archived_listing_never_forgets_a_known_channel(seeded):
    # Twist's docs: the plain channels/get leaves archived channels out.
    seeded.plain_hides_archived = True
    state = synced(seeded)
    seeded.fail_archived_listing = 400
    _, stats = run(seeded, state)  # every synced channel is still listed
    assert stats["deleted"] == 0
    seeded.fail_archived_listing = None
    seeded.channels[DESIGN]["archived"] = True  # kept: synced while active
    run(seeded, state)
    seeded.fail_archived_listing = 400
    before = dict(state)
    with pytest.raises(TwistError, match="archived=true failed"):
        run(seeded, state)
    assert state == before


def test_archived_listing_failure_stops_a_run_that_asks_for_archived_channels(seeded):
    # The plain listing may leave archived channels out, so they cannot be skipped quietly.
    seeded.plain_hides_archived = True
    seeded.channels[DESIGN]["archived"] = True
    seeded.fail_archived_listing = 400
    for opt_in in ({"include_archived": True}, {"channel_ids": [DESIGN]}):
        state: dict = {}
        with pytest.raises(TwistError, match="archived=true failed"):
            run(seeded, state, **opt_in)
        assert state == {}
    # Without asking for archived channels a fresh sync still runs on the plain listing.
    rows, stats = run(seeded, {})
    assert f"channel:{LAUNCH}" in rows and stats["deleted"] == 0


def test_state_from_the_previous_version_keeps_archived_channels(seeded, thread_named):
    # 0462b02 wrote no active_channels and kept every synced channel.
    state = synced(seeded)
    del state["active_channels"]
    seeded.channels[DESIGN]["archived"] = True
    rows, stats = run(seeded, state)
    assert stats["deleted"] == 0
    assert DESIGN in state["active_channels"]
    logo = thread_named("Logo refresh")
    seeded.add_comment(logo["id"], "Still synced after the upgrade.")
    rows, _ = run(seeded, state)
    assert "Still synced after the upgrade." in rows[f"thread:{logo['id']}"]["content"]


def test_active_channels_mark_is_dropped_only_when_the_channel_is_forgotten(seeded):
    state = synced(seeded)
    assert sorted(state["active_channels"]) == sorted([LAUNCH, DESIGN])
    seeded.channels[DESIGN]["archived"] = True
    # A small wrong selection forgets DESIGN at once (below the mass-delete floor)...
    _, stats = run(seeded, state, channel_ids=[LAUNCH])
    assert stats["deleted"] == 2  # the channel and its one thread
    assert state["active_channels"] == [LAUNCH]
    # ...and as an archived channel it now comes back only when asked for.
    rows, _ = run(seeded, state)
    assert f"channel:{DESIGN}" not in rows
    rows, _ = run(seeded, state, include_archived=True)
    assert f"channel:{DESIGN}" in rows


def test_channel_archived_after_sync_is_kept_not_forgotten(seeded, thread_named):
    # Archiving deletes nothing: a channel synced before stays synced, and its
    # document says "archived". Its threads are kept.
    state = synced(seeded)
    logo = thread_named("Logo refresh")
    seeded.channels[DESIGN]["archived"] = True
    rows, stats = run(seeded, state)
    assert f"thread:{logo['id']}" not in rows
    assert "Status: archived" in rows[f"channel:{DESIGN}"]["content"]
    assert stats["deleted"] == 0
    # And it keeps syncing: a new comment there is still picked up.
    seeded.add_comment(logo["id"], "Final logo approved.")
    rows, _ = run(seeded, state)
    assert "Final logo approved." in rows[f"thread:{logo['id']}"]["content"]
