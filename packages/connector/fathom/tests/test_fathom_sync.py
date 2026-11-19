"""The sync algorithm against the fake API, with a plain dict as the sync state (no dlt)."""

from datetime import timedelta

import pytest
import responses
from fake_fathom import NOW, FakeFathom, stamp

from cognee_community_connector_fathom import fathom as fathom_module
from cognee_community_connector_fathom.fathom import (
    FathomAPIError,
    FathomClient,
    FathomRateLimitedError,
    Options,
    _Listed,
    plan_windows,
    sync_meetings,
)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(fathom_module, "_now", lambda: NOW)


class Harness:
    """One fake account, one persistent sync state, many runs."""

    def __init__(self, rsps: responses.RequestsMock):
        self.fake = FakeFathom()
        self.fake.install(rsps)
        self.state: dict = {}
        self.client = FathomClient("key", sleep=lambda _seconds: None)

    def run(self, **option_overrides) -> tuple[list[dict], dict]:
        self.fake.requests.clear()
        stats: dict = {}
        rows = list(sync_meetings(self.client, self.state, stats, Options(**option_overrides)))
        return rows, stats

    def live(self, rows: list[dict]) -> set[str]:
        return {row["id"] for row in rows if not row["_deleted"]}

    def tombstones(self, rows: list[dict]) -> set[str]:
        return {row["id"] for row in rows if row["_deleted"]}


@pytest.fixture
def api():
    with responses.RequestsMock() as rsps:
        yield Harness(rsps)


# ---------------------------------------------------------------------------
# first sync and pagination
# ---------------------------------------------------------------------------
def test_first_sync_ingests_every_meeting_across_pages(api):
    for recording_id in range(1, 26):
        api.fake.add(recording_id, days_ago=recording_id + 30)

    rows, stats = api.run(recheck_days=0)

    assert api.live(rows) == {str(i) for i in range(1, 26)}
    assert stats["listed"] == 25
    assert stats["fetched"] == 25
    assert set(api.state["meetings"]) == {str(i) for i in range(1, 26)}


def test_inventory_is_light_and_details_carry_only_the_selected_flags(api):
    api.fake.add(1, days_ago=40)
    api.run(include_transcript=True)

    inventory, detail = api.fake.requests
    assert inventory["params"] == {"include_action_items": "true"}
    assert not inventory["heavy"]
    assert detail["params"] == {
        "include_summary": "true",
        "include_action_items": "true",
        "include_transcript": "true",
    }
    assert detail["heavy"]


def test_a_meeting_with_a_transcript_only_asks_for_it_when_opted_in(api):
    api.fake.add(1, days_ago=40)
    rows, _ = api.run()
    assert "Ten percent off" not in rows[0]["content"]

    api.state.clear()
    rows, _ = api.run(include_transcript=True)
    assert "[00:05:32] Jane Smith: Ten percent off is fine for 1." in rows[0]["content"]


def test_filters_reach_both_passes(api):
    api.fake.add(1, days_ago=40)
    api.run(filters={"teams": ["Sales"], "recorded_by": ["ann@ourco.com"]})

    for request in api.fake.requests:
        assert request["params"]["teams[]"] == ["Sales"]
        assert request["params"]["recorded_by[]"] == ["ann@ourco.com"]


def test_since_bounds_every_request(api):
    api.fake.add(1, days_ago=100)
    api.fake.add(2, days_ago=5)

    rows, _ = api.run(since=NOW - timedelta(days=30))

    assert api.live(rows) == {"2"}
    assert all(
        r["params"]["created_after"] == stamp(NOW - timedelta(days=30)) for r in api.fake.requests
    )


# ---------------------------------------------------------------------------
# incremental behaviour
# ---------------------------------------------------------------------------
def test_a_second_run_with_nothing_new_makes_no_heavy_request_outside_the_window(api):
    for recording_id in range(1, 21):
        api.fake.add(recording_id, days_ago=100 + recording_id)
    api.run()

    rows, stats = api.run()

    assert rows == []
    assert stats["fetched"] == 0
    assert api.fake.heavy_requests == []
    assert len(api.fake.requests) == 2  # the inventory, 2 pages; nothing else


def test_only_new_meetings_are_fetched_with_details(api):
    for recording_id in range(1, 6):
        api.fake.add(recording_id, days_ago=100 + recording_id)
    api.run()
    api.fake.add(6, days_ago=0.5)

    rows, _ = api.run()

    assert api.live(rows) == {"6"}
    assert api.fake.heavy_requests[0]["returned"] == [6]


def test_recent_meetings_are_rechecked_and_old_ones_are_not(api):
    api.fake.add(1, days_ago=100)
    api.fake.add(2, days_ago=3)
    api.run()

    rows, _ = api.run()

    # meeting 2 is inside the 14 day window, so it is fetched again; 1 is left alone
    assert api.live(rows) == {"2"}
    assert api.fake.ids_requested() >= {1, 2}
    assert {i for r in api.fake.heavy_requests for i in r["returned"]} == {2}


def test_recheck_window_is_configurable_and_can_be_switched_off(api):
    api.fake.add(1, days_ago=3)
    api.run(recheck_days=0)

    rows, _ = api.run(recheck_days=0)
    assert rows == []

    rows, _ = api.run(recheck_days=7)
    assert api.live(rows) == {"1"}


def test_a_summary_that_was_still_processing_is_filled_in_by_the_recheck(api):
    api.fake.add(1, days_ago=2, default_summary=None, action_items=[])
    first, stats = api.run()
    # nothing to index yet: it is deferred, not ingested as an empty shell
    assert first == []
    assert stats["deferred"] == 1
    assert "1" not in api.state["meetings"]

    api.fake.meetings[1]["default_summary"] = {"markdown_formatted": "We agreed on price."}
    second, _ = api.run()

    assert api.live(second) == {"1"}
    assert "We agreed on price." in second[0]["content"]


def test_a_meeting_that_never_gets_content_is_ingested_once_it_leaves_the_window(api):
    api.fake.add(1, days_ago=20, default_summary=None, action_items=[])

    rows, stats = api.run()  # older than the 14 day window: nothing to wait for

    assert api.live(rows) == {"1"}
    assert stats["deferred"] == 0


def test_a_known_meeting_that_loses_its_content_is_still_updated(api):
    api.fake.add(1, days_ago=2)
    api.run()
    api.fake.meetings[1]["default_summary"] = None
    api.fake.meetings[1]["action_items"] = []

    rows, _ = api.run()

    assert api.live(rows) == {"1"}


def test_completing_an_action_item_on_an_old_meeting_refetches_just_that_meeting(api):
    for recording_id in range(1, 31):
        api.fake.add(recording_id, days_ago=100 + recording_id)
    api.run()
    api.fake.meetings[15]["action_items"][0]["completed"] = True

    rows, _ = api.run()

    assert api.live(rows) == {"15"}
    assert "- [x] Send the proposal 15" in rows[0]["content"]
    heavy = api.fake.heavy_requests
    # one window around the changed meeting, not the 15 meetings older than it
    assert len(heavy) == 1
    assert heavy[0]["returned"] == [15]


def test_unchanged_action_items_never_trigger_a_refetch_whatever_their_order(api):
    api.fake.add(
        1,
        days_ago=100,
        action_items=[
            {"description": "A", "completed": False},
            {"description": "B", "completed": True},
        ],
    )
    api.run()
    api.fake.meetings[1]["action_items"].reverse()

    rows, _ = api.run()

    assert rows == []


def test_without_action_items_old_meetings_are_not_watched(api):
    api.fake.add(1, days_ago=100)
    api.run(include_action_items=False)
    api.fake.meetings[1]["action_items"][0]["completed"] = True

    rows, _ = api.run(include_action_items=False)

    assert rows == []
    assert all("include_action_items" not in r["params"] for r in api.fake.requests)


def test_changing_what_is_ingested_re_renders_every_meeting(api):
    for recording_id in (1, 2, 3):
        api.fake.add(recording_id, days_ago=100)
    api.run()

    rows, _ = api.run(include_transcript=True)

    assert api.live(rows) == {"1", "2", "3"}
    assert all("## Transcript" in row["content"] for row in rows)
    assert api.run(include_transcript=True)[0] == []  # and then it settles


def test_a_failed_re_render_is_retried_on_the_next_run(api):
    api.fake.add(1, days_ago=100)
    api.fake.add(2, days_ago=100)
    api.run()
    api.fake.hide_in_details = {2}  # listed, but gone by the time details are fetched

    api.run(include_transcript=True)
    assert api.state["config"] != Options(include_transcript=True).fingerprint

    api.fake.hide_in_details = set()
    rows, _ = api.run(include_transcript=True)
    assert api.live(rows) == {"1", "2"}  # everything is re-rendered again, then it settles
    assert api.state["config"] == Options(include_transcript=True).fingerprint


def test_a_meeting_that_finished_during_the_run_is_picked_up(api):
    api.fake.add(1, days_ago=100)
    api.run()
    api.fake.add(2, days_ago=0.1)
    api.fake.hide = {2: 1}  # absent from the inventory, present when details are fetched
    api.fake.meetings[1]["action_items"][0]["completed"] = True

    rows, _ = api.run()

    assert api.live(rows) == {"1", "2"}


# ---------------------------------------------------------------------------
# window planning
# ---------------------------------------------------------------------------
def _inventory(count: int) -> dict[str, _Listed]:
    return {str(i): _Listed(NOW - timedelta(days=count - i), "") for i in range(count)}


def test_plan_windows_for_everything_is_one_open_window():
    inventory = _inventory(30)
    assert plan_windows(inventory, set(inventory)) == [(None, None)]


def test_plan_windows_opens_the_newest_side_for_the_recent_window():
    inventory = _inventory(30)
    [(after, before)] = plan_windows(inventory, {str(i) for i in range(25, 30)})

    assert after == inventory["25"].created_at - timedelta(seconds=1)
    assert before is None


def test_plan_windows_keeps_distant_targets_apart_and_merges_close_ones():
    inventory = _inventory(60)
    windows = plan_windows(inventory, {"5", "8", "50"})

    assert len(windows) == 2
    assert windows[0] == (
        inventory["5"].created_at - timedelta(seconds=1),
        inventory["8"].created_at + timedelta(seconds=1),
    )
    assert windows[1][0] == inventory["50"].created_at - timedelta(seconds=1)


def test_plan_windows_without_targets_is_empty():
    assert plan_windows(_inventory(5), set()) == []


# ---------------------------------------------------------------------------
# forget-on-delete
# ---------------------------------------------------------------------------
def test_a_deleted_meeting_gets_a_tombstone_and_leaves_the_state(api):
    api.fake.add(1, days_ago=100)
    api.fake.add(2, days_ago=100)
    api.run()
    api.fake.remove(2)

    rows, stats = api.run()

    assert rows == [{"id": "2", "_deleted": True}]
    assert stats["deleted"] == 1
    assert set(api.state["meetings"]) == {"1"}
    assert api.run()[0] == []  # and it is not forgotten twice


def test_a_meeting_that_only_vanished_from_one_walk_is_not_forgotten(api):
    # a delete mid-walk shifts the page-number cursor and hides a live meeting once
    for recording_id in (1, 2, 3):
        api.fake.add(recording_id, days_ago=100)
    api.run()
    api.fake.hide = {2: 1}

    rows, stats = api.run()

    assert rows == []
    assert stats["deleted"] == 0
    assert "2" in api.state["meetings"]


def test_a_reappearing_meeting_is_ingested_again(api):
    api.fake.add(1, days_ago=100)
    api.fake.add(2, days_ago=100)
    api.run()
    hidden = api.fake.meetings.pop(2)
    api.run()
    api.fake.meetings[2] = hidden  # e.g. shared with the key owner again

    rows, _ = api.run()

    assert api.live(rows) == {"2"}


def test_a_failed_inventory_forgets_nothing_and_leaves_the_state_alone(api):
    for recording_id in range(1, 26):
        api.fake.add(recording_id, days_ago=100 + recording_id)
    api.run()
    snapshot = {**api.state, "meetings": dict(api.state["meetings"])}
    api.fake.remove(3)
    api.fake.fail_when = lambda params, n: 500 if "cursor" in params else None  # dies on page 2

    with pytest.raises(FathomAPIError):
        api.run()

    assert api.state == snapshot


def test_a_failed_detail_pass_leaves_the_state_alone(api):
    api.fake.add(1, days_ago=100)
    api.run()
    api.fake.add(2, days_ago=100)
    api.fake.remove(1)
    api.fake.fail_when = lambda params, n: 500 if params.get("include_summary") else None
    before = {**api.state, "meetings": dict(api.state["meetings"])}

    with pytest.raises(FathomAPIError):
        api.run()

    assert api.state == before


def test_a_run_rate_limited_for_good_raises_and_commits_nothing(api):
    api.fake.add(1, days_ago=100)
    api.fake.script = [429] * 50

    with pytest.raises(FathomRateLimitedError):
        api.run()

    assert api.state == {}


def test_an_empty_listing_with_a_big_history_is_refused_not_obeyed(api):
    for recording_id in range(1, 21):
        api.fake.add(recording_id, days_ago=100)
    api.run()
    api.fake.meetings.clear()  # e.g. the owner left the team: the key now sees nothing

    rows, stats = api.run()

    assert rows == []
    assert stats["failed"] == 1
    assert stats["failed_delete_guard"] == 20
    assert len(api.state["meetings"]) == 20


def test_the_delete_guard_can_be_widened_on_purpose(api):
    for recording_id in range(1, 21):
        api.fake.add(recording_id, days_ago=100)
    api.run()
    api.fake.meetings.clear()

    rows, stats = api.run(max_delete_fraction=1.0)

    assert api.tombstones(rows) == {str(i) for i in range(1, 21)}
    assert stats["failed"] == 0
    assert api.state["meetings"] == {}


def test_a_few_deletions_never_trip_the_guard(api):
    for recording_id in range(1, 5):
        api.fake.add(recording_id, days_ago=100)
    api.run()
    api.fake.meetings.clear()  # all four, but a small account may really lose them

    rows, _ = api.run()

    assert api.tombstones(rows) == {"1", "2", "3", "4"}


def test_a_proportionate_cleanup_in_a_big_account_passes_the_guard(api):
    for recording_id in range(1, 41):
        api.fake.add(recording_id, days_ago=100)
    api.run()
    for recording_id in range(1, 9):
        api.fake.remove(recording_id)

    rows, stats = api.run()

    assert len(api.tombstones(rows)) == 8
    assert stats["failed"] == 0


def test_meetings_without_an_id_are_refused(api):
    api.fake.add(1, days_ago=100)
    api.fake.meetings[1]["recording_id"] = None

    with pytest.raises(FathomAPIError, match="recording_id"):
        api.run()
