"""The sync logic, driven with a plain dict standing in for dlt's resource state."""

import pytest
from fake_rollbar import PROJECT, TOKEN, FakeRollbar, http_error

from cognee_community_connector_rollbar.rollbar import (
    RollbarAPIError,
    RollbarAuthError,
    RollbarClient,
    _iter_rows,
    item_id,
)


class Sync:
    """Runs the sync against a fake project, keeping state between runs like dlt does."""

    def __init__(self, fake: FakeRollbar, **options):
        self.fake = fake
        self.options = options
        self.state: dict = {}
        self.stats: dict[str, int] = {}

    def run(self, **options) -> list[dict]:
        self.stats = {}
        client = RollbarClient(self.fake.client(), TOKEN, sleep=lambda _: None)
        return list(
            _iter_rows(client, PROJECT, self.state, self.stats, **{**self.options, **options})
        )


def _ids(rows: list[dict], deleted: bool = False) -> set[str]:
    return {row["id"] for row in rows if row["_deleted"] is deleted}


def _key(item: dict) -> str:
    return item_id(PROJECT, item)


def test_a_first_run_emits_every_item_and_a_quiet_one_nothing():
    fake = FakeRollbar()
    first = fake.add_item("KeyError: 'plan_tier'")
    second = fake.add_item("ValueError: invalid coupon code")
    sync = Sync(fake)

    assert _ids(sync.run()) == {_key(first), _key(second)}
    assert sync.run() == []
    assert sync.stats == {
        "scanned": 2,
        "skipped": 2,
        "deleted": 0,
        "failed": 0,
        "excerpts_gone": 0,
    }


def test_a_thousand_new_occurrences_emit_nothing():
    fake = FakeRollbar()
    item = fake.add_item("ValueError: invalid coupon code")
    for _ in range(98):
        fake.occur(item)
    sync = Sync(fake)
    sync.run()

    for _ in range(1000):
        fake.occur(item)

    # 99 -> 1099 occurrences crosses one order of magnitude, the only change rendered
    assert [row["id"] for row in sync.run()] == [_key(item)]
    for _ in range(1000):
        fake.occur(item)
    assert sync.run() == []


def test_resolving_without_a_new_occurrence_re_emits_the_item():
    fake = FakeRollbar()
    item = fake.add_item("KeyError: 'plan_tier'")
    sync = Sync(fake)
    sync.run()

    fake.resolve(item, version="2.4.1")
    (row,) = sync.run()

    assert row["id"] == _key(item)
    assert "Status: resolved" in row["content"]
    assert "Resolved in version: 2.4.1" in row["content"]


def test_a_regression_brings_the_excerpt_of_the_occurrence_that_reactivated_it():
    fake = FakeRollbar()
    item = fake.add_item("ZeroDivisionError: division by zero", code_version="2.4.0")
    sync = Sync(fake)
    sync.run()
    fake.resolve(item, version="2.4.1")
    sync.run()

    fake.occur(item, code_version="2.4.2")
    (row,) = sync.run()

    assert "Status: active" in row["content"]
    assert "Code version: 2.4.2" in row["content"]
    assert "Resolved in version" not in row["content"]


def test_a_deleted_or_expired_occurrence_removes_the_excerpt():
    fake = FakeRollbar()
    item = fake.add_item("ZeroDivisionError: division by zero")
    sync = Sync(fake)
    sync.run()

    del fake.occurrences[item["activating_occurrence_id"]]
    (row,) = sync.run()

    assert row["_deleted"] is False
    assert "Exception" not in row["content"]
    assert sync.stats["excerpts_gone"] == 1


def test_an_item_whose_occurrences_were_all_deleted_is_forgotten():
    fake = FakeRollbar()
    kept = fake.add_item("KeyError: 'plan_tier'")
    gone = fake.add_item("ValueError: invalid coupon code")
    sync = Sync(fake)
    sync.run()

    fake.delete_item_occurrences(gone)
    rows = sync.run()

    assert rows == [{"id": _key(gone), "_deleted": True}]
    assert sync.stats["deleted"] == 1
    assert _key(gone) not in sync.state["fingerprints"]
    assert _key(kept) in sync.state["fingerprints"]


def test_an_item_that_slips_between_pages_is_not_forgotten():
    fake = FakeRollbar(page_size=2)
    items = [fake.add_item(f"Error {n}") for n in range(4)]
    sync = Sync(fake)
    sync.run()
    newest, slipped = items[3], items[1]
    calls = []

    def flicker(page):
        # the newest item vanishes after page 1, so page 2 starts one item later
        calls.append(page)
        if len(calls) == 2:
            del fake.items[newest["id"]]
        if len(calls) == 4:
            fake.items[newest["id"]] = newest

    fake.before_page = flicker
    rows = sync.run()

    assert _ids(rows, deleted=True) == set(), f"{_key(slipped)} slipped, it was not deleted"
    assert calls.count(1) == 2, "a second listing must run before anything is forgotten"


def test_leaving_the_selected_statuses_forgets_the_item():
    fake = FakeRollbar()
    item = fake.add_item("KeyError: 'plan_tier'")
    sync = Sync(fake, statuses=("active",))
    sync.run()

    fake.resolve(item)

    assert sync.run() == [{"id": _key(item), "_deleted": True}]


def test_a_rate_limited_listing_ends_the_run_without_forgetting():
    fake = FakeRollbar()
    fake.add_item("KeyError: 'plan_tier'")
    sync = Sync(fake)
    sync.run()

    fake.failing["/api/1/items"] = http_error(429)
    rows = sync.run()

    assert rows == []
    assert sync.stats["failed_rate_limit"] == 1
    assert len(sync.state["fingerprints"]) == 1


def test_a_rate_limit_while_reading_excerpts_keeps_what_was_emitted():
    fake = FakeRollbar()
    first = fake.add_item("KeyError: 'plan_tier'")
    second = fake.add_item("ValueError: invalid coupon code")
    third = fake.add_item("TypeError: bad operand")
    sync = Sync(fake)
    sync.run()
    fake.delete_item_occurrences(third)
    for item in (first, second):
        fake.resolve(item)

    # rows go out in id order: the first item's excerpt reads, the second's is rate limited
    original = fake.handle
    reads = []

    def limited(request):
        if request.url.path.startswith("/api/1/instance/"):
            reads.append(request)
            if len(reads) == 2:
                return http_error(429)
        return original(request)

    fake.handle = limited
    rows = sync.run()

    assert [row["id"] for row in rows] == [_key(first)]
    assert sync.stats["failed_rate_limit"] == 1
    assert _key(third) in sync.state["fingerprints"], "nothing is forgotten on a cut-short run"


def test_a_refused_token_before_any_progress_raises():
    fake = FakeRollbar()
    fake.add_item("KeyError: 'plan_tier'")
    fake.failing["/api/1/items"] = http_error(403, "invalid access token")

    with pytest.raises(RollbarAuthError, match="invalid access token"):
        Sync(fake).run()


def test_losing_access_mid_run_keeps_every_document():
    fake = FakeRollbar()
    first = fake.add_item("KeyError: 'plan_tier'")
    fake.add_item("ValueError: invalid coupon code")
    sync = Sync(fake)
    sync.run()
    fake.resolve(first)
    original = fake.handle
    reads = []

    def revoked(request):
        if request.url.path.startswith("/api/1/instance/"):
            reads.append(request)
            if len(reads) == 2:
                return http_error(403, "token disabled")
        return original(request)

    fake.handle = revoked
    rows = sync.run()

    assert [row["id"] for row in rows] == [_key(first)]
    assert sync.stats["failed_access"] == 1
    assert len(sync.state["fingerprints"]) == 2


def test_items_of_another_project_are_refused():
    fake = FakeRollbar(project_id=999)
    fake.add_item("KeyError: 'plan_tier'")

    with pytest.raises(RollbarAPIError, match="project 999"):
        Sync(fake).run()


def test_state_of_another_project_is_refused():
    fake = FakeRollbar()
    sync = Sync(fake)
    sync.state["project_id"] = 123

    with pytest.raises(ValueError, match="project 123"):
        sync.run()


def test_an_item_without_an_activating_occurrence_reads_no_excerpt():
    fake = FakeRollbar()
    fake.add_item("KeyError: 'plan_tier'", activating_occurrence_id=None)
    item = next(iter(fake.items.values()))
    item["activating_occurrence_id"] = None

    (row,) = Sync(fake).run()

    assert "Exception" not in row["content"]
    assert not any(path.startswith("/api/1/instance/") for path in fake.paths())
