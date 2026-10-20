"""Rendering, sync and ``ramp_source`` tests against an in-memory Ramp. No network, no LLM.

Each test drives the fake through a real-world change and checks the rows the
connector yields and the state it keeps for the next run.
"""

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, document_source_tag
from fake_ramp import NOW, FakeRamp, ago, client_for, later, receipt_fetches, serve, sync

from cognee_community_connector_ramp import RampClient, ramp, ramp_source
from cognee_community_connector_ramp.ramp import RampAuthError, _iter_rows, render_transaction


@pytest.fixture
def fake():
    return FakeRamp()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_document_is_written_around_the_memo(fake):
    txn = fake.add(
        "t1",
        ago(3),
        memo="Team offsite dinner - Q4 planning with Acme",
        trip_name="SF offsite",
        attendees=[{"name": "Jane Doe"}, {"name": "Raj Patel"}],
        accounting_field_selections=[
            {"category_info": {"name": "GL account"}, "display_name": "Meals"}
        ],
    )
    receipt = fake.add_receipt("r1", "t1", ago(2), items=("Burrata", "Halibut"))

    row = render_transaction(txn, [receipt])

    assert row["id"] == "transaction:t1"
    assert row["title"] == 'Memo: "Team offsite dinner - Q4 planning with Acme"'
    lines = row["content"].splitlines()
    assert lines[0] == 'Memo: "Team offsite dinner - Q4 planning with Acme"'
    assert lines[1] == (
        "Sam Lee (Engineering) paid Nopa (San Francisco, CA, US) on "
        f"{ago(3)[:10]}: Restaurants, 482.10 USD, cleared."
    )
    assert "Trip: SF offsite" in lines
    assert "Attendees: Jane Doe, Raj Patel" in lines
    assert "Receipt: 2 line items (Burrata, Halibut)" in lines
    assert "Receipt Sales tax: 4.12 USD" in lines
    assert "Accounting: GL account: Meals" in lines
    assert row["cognee_node_set"] == ["department:Engineering"]


def test_amount_only_transaction_is_not_knowledge(fake):
    txn = fake.add("t1", ago(3))

    assert render_transaction(txn, []) is None
    # opt-in: still rendered, without inventing a memo
    row = render_transaction(txn, [], require_context=False)
    assert row["title"] == f"Nopa purchase on {ago(3)[:10]}"
    assert "Memo" not in row["content"]


def test_context_without_memo_still_counts(fake):
    txn = fake.add(
        "t1",
        ago(3),
        policy_violations=[{"memo": "Over the per-person limit", "type": "OVER_LIMIT"}],
        disputes=[{"memo": "Charged twice", "type": "UNRECOGNIZED_CHARGE"}],
        line_items=[{"memo": "Wine", "amount": {"amount": 9000, "currency_code": "USD"}}],
    )

    content = render_transaction(txn, [])["content"]

    assert "Policy note: Over the per-person limit" in content
    assert "Dispute (UNRECOGNIZED_CHARGE): Charged twice" in content
    assert "Line item: Wine (90.00 USD)" in content


def test_attendee_order_does_not_change_the_document(fake):
    a = fake.add("t1", ago(3), memo="Dinner", attendees=[{"name": "Raj"}, {"name": "Jane"}])
    b = dict(a, attendees=[{"name": "Jane"}, {"name": "Raj"}])
    assert render_transaction(a, []) == render_transaction(b, [])


def test_refund_line_carries_no_raw_ids(fake):
    txn = fake.add("t1", ago(2), memo="Returned monitor", original_transaction_id="abc-123")
    content = render_transaction(txn, [])["content"]
    assert "Refund of an earlier transaction." in content and "abc-123" not in content


def test_times_with_any_fraction_parse_on_every_python():
    assert ramp._normal_time("2025-08-02T05:01:38.12345+00:00") == "2025-08-02T05:01:38Z"
    assert ramp._normal_time("2025-08-02T05:01:38.1Z") == "2025-08-02T05:01:38Z"
    assert ramp._normal_time("2025-08-02T05:01:38+00:00") == "2025-08-02T05:01:38Z"


# ---------------------------------------------------------------------------
# Incremental sync
# ---------------------------------------------------------------------------


def test_first_run_backfills_and_second_run_emits_nothing(fake):
    fake.add("t1", ago(400), memo="Old conference")
    fake.add("t2", ago(3), memo="Offsite dinner")
    fake.add("t3", ago(2))  # amount only
    state: dict = {}

    upserts, deletes, _ = sync(fake, state)
    assert set(upserts) == {"transaction:t1", "transaction:t2"}
    assert not deletes
    assert state["cursor"] == ramp._normal_time(ago(2))

    upserts, deletes, stats = sync(fake, state)
    assert not upserts and not deletes
    # the window starts lookback_days behind the cursor, so t1 is not re-read
    listing = [c for c in fake.calls if c[1] == "/developer/v1/transactions"][-1]
    assert listing[2]["from_date"] == ramp._iso(
        datetime.fromisoformat(ago(2)) - timedelta(days=ramp.DEFAULT_LOOKBACK_DAYS)
    )
    # t2 is unchanged, and amount-only t3 is remembered so it isn't re-read either
    assert stats["skipped"] == 2


def test_memo_added_days_after_the_purchase_is_caught(fake):
    """The reason for the lookback: a user_transaction_time cursor alone misses this."""
    txn = fake.add("t1", ago(5))
    fake.add("t2", ago(1), memo="Later purchase moves the cursor past t1")
    state: dict = {}
    upserts, _, _ = sync(fake, state)
    assert "transaction:t1" not in upserts  # no memo yet: not knowledge

    txn["memo"] = "Client lunch with Acme"
    upserts, _, _ = sync(fake, state)

    assert upserts["transaction:t1"]["title"] == 'Memo: "Client lunch with Acme"'


def test_settlement_rerenders_the_document(fake):
    txn = fake.add("t1", ago(2), memo="Flight to NYC", state="PENDING")
    state: dict = {}
    assert "pending." in sync(fake, state)[0]["transaction:t1"]["content"]

    txn["state"] = "CLEARED"
    upserts, _, _ = sync(fake, state)

    assert "cleared." in upserts["transaction:t1"]["content"]


def test_future_dated_transaction_does_not_stall_the_sync(fake):
    """Ramp has future-dated transactions; one must not push the cursor past today."""
    fake.add("future", (NOW + timedelta(days=400)).isoformat(), memo="Prepaid 2027 conference")
    fake.add("old", ago(5), memo="Team lunch")
    state: dict = {}
    sync(fake, state)
    assert state["cursor"] <= ramp._iso(datetime.now(timezone.utc))

    fake.add("today", ago(0.1), memo="Bought today")
    del fake.transactions["old"]
    upserts, deletes, _ = sync(fake, state)

    assert "transaction:today" in upserts
    assert deletes == {"transaction:old"}


def test_zero_lookback_still_syncs_new_transactions(fake):
    fake.add("t1", ago(3), memo="A")
    state: dict = {}
    sync(fake, state, lookback_days=0)
    fake.add("t2", ago(0.1), memo="B")
    assert "transaction:t2" in sync(fake, state, lookback_days=0)[0]


def test_user_and_entity_filters_narrow_the_sync(fake):
    fake.add("mine", ago(3), memo="Mine", user_id="usr_me")
    fake.add("theirs", ago(2), memo="Theirs", user_id="usr_other")
    fake.add("other_entity", ago(1), memo="Other entity", user_id="usr_me", entity_id="ent_2")
    upserts, _, _ = sync(fake, {}, user_ids=["usr_me"], entity_id="ent_1")
    assert set(upserts) == {"transaction:mine"}


def test_unchanged_transactions_do_not_refetch_receipts(fake):
    fake.add("t1", ago(3), memo="Dinner")
    fake.add_receipt("r1", "t1", ago(2))
    state: dict = {}
    sync(fake, state)
    before = len([c for c in fake.calls if c[1] == "/developer/v1/receipts"])

    sync(fake, state)

    receipt_calls = [c for c in fake.calls if c[1] == "/developer/v1/receipts"][before:]
    # only the late-receipt cursor read, no per-transaction receipt fetch
    assert [c[2].get("transaction_id") for c in receipt_calls] == [None]


def test_transactions_without_a_document_do_not_refetch_receipts(fake):
    fake.add("t1", ago(3))  # no memo
    receipt = fake.add_receipt("r1", "t1", ago(2))
    receipt["ocr"] = {"currency_code": None, "line_items": [], "taxes": []}  # read, nothing found
    state: dict = {}
    assert not sync(fake, state)[0]
    before = len(fake.calls)
    sync(fake, state)
    assert not receipt_fetches(fake, before)


def test_receipt_ocr_that_lands_later_reaches_the_document(fake):
    fake.add("t1", ago(3), memo="Client dinner")
    receipt = fake.add_receipt("r1", "t1", ago(0.5), items=("Burrata",))
    ocr = receipt["ocr"]
    receipt["ocr"] = None  # uploaded, not read yet
    state: dict = {}
    assert "Receipt" not in sync(fake, state)[0]["transaction:t1"]["content"]

    receipt["ocr"] = ocr
    upserts, _, _ = sync(fake, state)
    assert "Receipt: 1 line item (Burrata)" in upserts["transaction:t1"]["content"]

    # once OCR is in, the transaction is skipped again without refetching receipts
    before = len(fake.calls)
    sync(fake, state)
    assert not receipt_fetches(fake, before)


def test_old_receipts_without_ocr_are_not_refetched_forever(fake):
    fake.add("t1", ago(30), memo="Old dinner")
    fake.add_receipt("r1", "t1", ago(29))["ocr"] = None
    state: dict = {}
    sync(fake, state)
    before = len(fake.calls)
    sync(fake, state)
    assert not receipt_fetches(fake, before)


def test_late_receipt_rerenders_a_transaction_older_than_the_window(fake):
    fake.add("t1", ago(200), memo="Annual offsite venue")
    fake.add("t2", ago(1), memo="Recent")
    state: dict = {}
    sync(fake, state)

    fake.add_receipt("r1", "t1", later(), items=("Venue hire",))
    upserts, _, _ = sync(fake, state)

    assert "Receipt: 1 line item (Venue hire)" in upserts["transaction:t1"]["content"]
    # caught by the receipt cursor and one GET by id, not by re-listing 200 days
    assert ("GET", "/developer/v1/transactions/t1", {}) in fake.calls


def test_record_behind_a_vanished_page_token_is_forgotten_on_the_restart(fake):
    fake.page_size_cap = 2
    for n in range(6):
        fake.add(f"t{n}", ago(30 - n), memo=f"m{n}", state="PENDING")
    state: dict = {}
    sync(fake, state, max_requests=2)
    token = state["walk"]["listing"]["start"]
    del fake.transactions[token]

    _, deletes, _ = sync(fake, state, lookback_days=0)

    assert deletes == {f"transaction:{token}"}


def test_late_receipt_backlog_bigger_than_the_budget_is_read_over_several_runs(fake):
    fake.page_size_cap = 2
    for n in range(10):
        fake.add(f"old{n}", ago(300 + n), memo=f"old {n}")
    fake.add("recent", ago(1), memo="Recent")
    state: dict = {}
    sync(fake, state)
    for n in range(10):
        fake.add_receipt(f"r{n}", f"old{n}", later(), items=(f"Item {n}",))

    receipted: set = set()
    for _ in range(12):
        upserts, _, _ = sync(fake, state, max_requests=4)
        receipted |= {k for k, v in upserts.items() if "Receipt" in v["content"]}
    assert receipted == {f"transaction:old{n}" for n in range(10)}


def test_backfill_resumes_after_running_out_of_budget(fake):
    fake.page_size_cap = 2
    for n in range(7):
        fake.add(f"t{n}", ago(30 - n), memo=f"memo {n}")
    state: dict = {}

    # 2 listing pages, then the budget stops it (the token request isn't counted)
    upserts, _, stats = sync(fake, state, max_requests=2)
    assert set(upserts) == {"transaction:t0", "transaction:t1", "transaction:t2", "transaction:t3"}
    assert stats["failed_budget"] == 1
    assert state["walk"]["listing"]["start"] == "t3"

    upserts, _, _ = sync(fake, state)
    assert set(upserts) == {"transaction:t4", "transaction:t5", "transaction:t6"}
    assert state["walk"] is None


def test_large_backfill_with_receipts_always_makes_progress(fake):
    fake.page_size_cap = 2
    for n in range(6):
        fake.add(f"t{n}", ago(30 - n), memo=f"m{n}")
        fake.add_receipt(f"r{n}", f"t{n}", ago(29 - n))
    state: dict = {}
    seen: set = set()
    for _ in range(10):
        upserts, _, _ = sync(fake, state, max_requests=3)
        seen |= set(upserts)
        if state.get("walk") is None and len(seen) == 6:
            break
    assert seen == {f"transaction:t{n}" for n in range(6)}


def test_resuming_after_the_saved_page_token_vanished(fake):
    """Seen on the sandbox: a start id that no longer exists answers 422."""
    fake.page_size_cap = 2
    for n in range(6):
        fake.add(f"t{n}", ago(30 - n), memo=f"m{n}", state="PENDING")
    state: dict = {}
    sync(fake, state, max_requests=2)  # two listing pages, then cut short
    token = state["walk"]["listing"]["start"]
    del fake.transactions[token]  # the hold the token points at expired

    seen: set = set()
    for _ in range(4):
        upserts, _, _ = sync(fake, state, max_requests=3)
        seen |= set(upserts)
        if state["walk"] is None:
            break
    assert state["walk"] is None, "the listing must recover, not fail forever"
    assert {f"transaction:t{n}" for n in (4, 5)} <= seen
    sync(fake, state)  # finishes the queued deletion checks
    assert f"transaction:{token}" not in state["rows"]


def test_queueing_many_rechecks_stays_fast(fake):
    state = {"rows": {f"transaction:t{n}": ["f", ago(1), "k"] for n in range(50_000)}}
    walker = ramp._Walker(
        client_for(fake),
        state,
        {},
        since=None,
        lookback=timedelta(days=60),
        include_declined=False,
        require_context=True,
        department_ids=set(),
        user_ids=set(),
        entity_id=None,
        max_requests=10,
        check_active=None,
    )
    started = time.perf_counter()
    for n in range(50_000):
        walker._queue(f"t{n}")
        walker._queue(f"t{n}")
    assert time.perf_counter() - started < 1.0
    assert len(state["recheck"]) == 50_000


# ---------------------------------------------------------------------------
# Selection and state
# ---------------------------------------------------------------------------


def test_turning_on_declined_backfills_older_declines(fake):
    fake.add("old_decline", ago(300), memo="Card blocked at hotel", state="DECLINED")
    fake.add("t2", ago(1), memo="Recent")
    state: dict = {}
    assert "transaction:old_decline" not in sync(fake, state)[0]

    upserts, _, _ = sync(fake, state, include_declined=True)
    assert "transaction:old_decline" in upserts


def test_narrowing_departments_forgets_rows_older_than_the_window(fake):
    fake.add("design_old", ago(300), memo="Design tools", department_id="dep_design")
    fake.add("eng", ago(1), memo="Laptops")
    state: dict = {}
    sync(fake, state)

    _, deletes, _ = sync(fake, state, department_ids=["dep_eng"])
    assert deletes == {"transaction:design_old"}


def test_since_applies_to_late_receipts_too(fake):
    fake.add("before", ago(100), memo="Before since")
    fake.add("after", ago(5), memo="After since")
    since = ago(30)
    state: dict = {}
    assert set(sync(fake, state, since=since)[0]) == {"transaction:after"}

    fake.add_receipt("r1", "before", later())
    upserts, _, _ = sync(fake, state, since=since)
    assert "transaction:before" not in upserts


def test_state_of_another_ramp_app_is_refused(fake):
    fake.add("t1", ago(3), memo="A")
    state: dict = {}
    sync(fake, state)

    other = RampClient("another-client", "secret", http=fake.client())
    with pytest.raises(ramp.RampSourceError, match="resource_name"):
        list(_iter_rows(other, state, {}))


def test_new_app_for_the_same_business_can_adopt_the_state(fake):
    fake.add("t1", ago(3), memo="Kept")
    fake.add("t2", ago(2), memo="Deleted while switching apps")
    state: dict = {}
    sync(fake, state)
    del fake.transactions["t2"]
    fake.credentials = ("new-app", "secret")
    new_app = RampClient("new-app", "secret", http=fake.client(), sleep=lambda _s: None)

    with pytest.raises(ramp.RampSourceError, match="adopt_state_from"):
        list(_iter_rows(new_app, state, {}))
    # naming some other app adopts nothing
    with pytest.raises(ramp.RampSourceError):
        list(_iter_rows(new_app, state, {}, adopt_state_from="unrelated-app"))
    rows = list(_iter_rows(new_app, state, {}, adopt_state_from="client"))

    assert [row["id"] for row in rows] == ["transaction:t2"] and rows[0]["_deleted"]
    assert set(state["rows"]) == {"transaction:t1"}
    assert state["identity"] == new_app.identity
    # leaving the option set is harmless once adopted
    assert not list(_iter_rows(new_app, state, {}, adopt_state_from="client"))


# ---------------------------------------------------------------------------
# Forget-on-delete and errors
# ---------------------------------------------------------------------------


def test_expired_authorization_is_forgotten(fake):
    fake.add("t1", ago(2), memo="Hotel hold", state="PENDING")
    fake.add("t2", ago(1), memo="Taxi")
    state: dict = {}
    sync(fake, state)

    del fake.transactions["t1"]  # the hold dropped off and Ramp no longer has it
    _, deletes, _ = sync(fake, state)

    assert deletes == {"transaction:t1"}
    assert "transaction:t1" not in state["rows"]
    assert ("GET", "/developer/v1/transactions/t1", {}) in fake.calls


def test_declined_transaction_is_forgotten_unless_included(fake):
    txn = fake.add("t1", ago(2), memo="Software subscription")
    state: dict = {}
    sync(fake, state)

    txn["state"] = "DECLINED"
    _, deletes, _ = sync(fake, state)
    assert deletes == {"transaction:t1"}

    state = {}
    upserts, _, _ = sync(fake, state, include_declined=True)
    assert "declined." in upserts["transaction:t1"]["content"]


def test_error_state_counts_as_never_happened(fake):
    txn = fake.add("t1", ago(2), memo="Failed charge")
    state: dict = {}
    sync(fake, state)
    txn["state"] = "ERROR"
    _, deletes, _ = sync(fake, state)
    assert deletes == {"transaction:t1"}


def test_removed_memo_forgets_an_amount_only_transaction(fake):
    txn = fake.add("t1", ago(2), memo="Typo memo")
    state: dict = {}
    sync(fake, state)

    txn["memo"] = None
    _, deletes, _ = sync(fake, state)

    assert deletes == {"transaction:t1"}


def test_out_of_scope_department_is_forgotten(fake):
    txn = fake.add("t1", ago(3), memo="Design tools", department_id="dep_design")
    fake.add("t2", ago(2), memo="Laptops")
    state: dict = {}
    upserts, _, _ = sync(fake, state, department_ids=["dep_eng", "dep_design"])
    assert set(upserts) == {"transaction:t1", "transaction:t2"}

    txn["card_holder"]["department_id"] = "dep_sales"
    _, deletes, _ = sync(fake, state, department_ids=["dep_eng", "dep_design"])

    assert deletes == {"transaction:t1"}


def test_cut_short_listing_forgets_nothing(fake):
    fake.page_size_cap = 1
    fake.add("t1", ago(3), memo="A")
    fake.add("t2", ago(2), memo="B")
    state: dict = {}
    sync(fake, state)

    del fake.transactions["t1"]
    rows_before = dict(state["rows"])
    _, deletes, stats = sync(fake, state, max_requests=2)

    assert not deletes
    assert stats["failed_budget"] == 1
    assert set(state["rows"]) == set(rows_before)


def test_failing_api_aborts_without_forgetting(fake):
    fake.add("t1", ago(3), memo="A")
    state: dict = {}
    sync(fake, state)
    rows_before = dict(state["rows"])

    fake.queued = [httpx.Response(500, content=b"{}")] * 10
    # no token can be issued, so no request can succeed: fatal, not a per-record failure
    with pytest.raises(RampAuthError, match="no access token"):
        sync(fake, state)
    assert state["rows"] == rows_before


def test_malformed_listing_raises_and_forgets_nothing(fake):
    fake.add("t1", ago(3), memo="A")
    state: dict = {}
    client = client_for(fake)
    list(_iter_rows(client, state, {}))
    # the client holds its token, so the next response Ramp sends is the listing
    fake.queued = [httpx.Response(200, content=b'{"unexpected": true}')]
    with pytest.raises(ramp.RampAPIError, match="without data or page"):
        list(_iter_rows(client, state, {}))
    assert "transaction:t1" in state["rows"] and not state["recheck"]


def test_one_failing_record_does_not_block_the_run(fake):
    fake.add("t1", ago(200), memo="Old venue")
    fake.add("t2", ago(1), memo="Recent")
    state: dict = {}
    sync(fake, state)

    fake.add_receipt("r1", "t1", later(), items=("Venue hire",))
    fake.add("t3", ago(0.5), memo="New purchase")
    undo = serve(fake, "/transactions/t1", 500)
    upserts, _, stats = sync(fake, state)
    assert "transaction:t3" in upserts
    assert stats["failed"] == 1
    assert state["recheck_failed"] == ["t1"]

    undo()
    upserts, _, _ = sync(fake, state)
    assert "Receipt: 1 line item (Venue hire)" in upserts["transaction:t1"]["content"]
    assert state["recheck_failed"] == []


def test_failed_deletion_check_is_retried_not_dropped(fake):
    fake.add("t1", ago(3), memo="Hold")
    state: dict = {}
    sync(fake, state)
    del fake.transactions["t1"]
    undo = serve(fake, "/transactions/t1", 503)
    _, deletes, _ = sync(fake, state)
    assert not deletes and "transaction:t1" in state["rows"]

    undo()
    _, deletes, _ = sync(fake, state, lookback_days=0)  # window no longer covers t1
    assert deletes == {"transaction:t1"}


def test_403_on_one_transaction_does_not_stall_the_sync(fake):
    fake.add("t1", ago(200), memo="Other entity's card")
    fake.add("t2", ago(1), memo="Recent")
    state: dict = {}
    sync(fake, state)
    fake.add_receipt("r1", "t1", later())
    fake.add("t3", ago(0.5), memo="New purchase")
    serve(fake, "/transactions/t1", 403)
    for _ in range(2):
        _, _, stats = sync(fake, state)
        assert stats["failed"] == 1
    assert "transaction:t3" in state["rows"]
    assert state["recheck_failed"] == ["t1"]


def test_transaction_that_keeps_failing_is_eventually_given_up_on(fake):
    fake.add("t1", ago(200), memo="Forbidden")
    fake.add("t2", ago(1), memo="Recent")
    state: dict = {}
    sync(fake, state)
    fake.add_receipt("r1", "t1", later())
    serve(fake, "/transactions/t1", 403)
    for _ in range(ramp._MAX_ATTEMPTS):
        _, _, stats = sync(fake, state)
    assert stats.get("abandoned") == 1
    _, _, stats = sync(fake, state)
    assert stats["failed"] == 0 and not state["recheck_failed"]
    assert "transaction:t1" in state["rows"], "giving up never deletes"


def test_given_up_transaction_is_retried_a_week_later(fake):
    fake.add("t1", ago(3), memo="Dinner")
    fake.add("t2", ago(2), memo="Taxi")
    state: dict = {}
    sync(fake, state)
    undo = serve(fake, "/transactions/t1", 403)
    del fake.transactions["t1"]
    for _ in range(ramp._MAX_ATTEMPTS):
        sync(fake, state)
    undo()

    _, deletes, _ = sync(fake, state)
    assert not deletes, "within the week it isn't re-read"

    last_week = (NOW - timedelta(days=ramp._RETRY_GIVEN_UP_DAYS + 1)).isoformat()
    state["attempts"]["t1"][1] = ramp._normal_time(last_week)
    state["recheck"].append("t1")
    _, deletes, _ = sync(fake, state)
    assert deletes == {"transaction:t1"}
    assert "t1" not in state["attempts"]


def test_selection_change_retries_given_up_transactions(fake):
    fake.add("t1", ago(3), memo="Dinner")
    state: dict = {}
    sync(fake, state)
    undo = serve(fake, "/transactions/t1", 403)
    del fake.transactions["t1"]
    for _ in range(ramp._MAX_ATTEMPTS):
        sync(fake, state)
    undo()

    _, deletes, _ = sync(fake, state, since="2000-01-01T00:00:00Z")
    assert deletes == {"transaction:t1"}


def test_outage_never_gives_up_on_a_deletion_check(fake):
    fake.add("t1", ago(3), memo="Hold", state="PENDING")
    fake.add("t2", ago(2), memo="Taxi")
    state: dict = {}
    sync(fake, state)
    del fake.transactions["t1"]

    undo = serve(fake, "/transactions/t1", 503)
    for _ in range(ramp._MAX_ATTEMPTS + 1):
        _, deletes, stats = sync(fake, state)
        assert not deletes and "abandoned" not in stats
    undo()
    _, deletes, _ = sync(fake, state)
    assert deletes == {"transaction:t1"}


def test_running_out_of_budget_during_a_reread_keeps_it_queued(fake):
    fake.add("t1", ago(200), memo="Old venue")
    fake.add("t2", ago(1), memo="Recent")
    state: dict = {}
    sync(fake, state)
    fake.add_receipt("r1", "t1", later(), items=("Venue hire",))
    # listing page + late-receipt listing + GET t1, then the receipts fetch is cut short
    sync(fake, state, max_requests=3)
    assert "t1" in state["recheck"]

    upserts, _, _ = sync(fake, state)
    assert "Receipt: 1 line item (Venue hire)" in upserts["transaction:t1"]["content"]


# ---------------------------------------------------------------------------
# ramp_source
# ---------------------------------------------------------------------------


def test_source_validates_its_arguments(fake):
    with pytest.raises(ValueError, match="resource_name"):
        ramp_source(resource_name=" ", service=client_for(fake))
    with pytest.raises(ValueError, match="client_id"):
        ramp_source(resource_name="ramp_acme")
    with pytest.raises(ValueError, match="ISO 8601"):
        ramp_source(resource_name="ramp_acme", service=client_for(fake), since="last tuesday")

    source = ramp_source(resource_name="ramp_acme", service=client_for(fake))
    assert document_source_tag(source) == "ramp"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "ramp_acme"


def test_sandbox_flag_targets_the_demo_api():
    source = ramp_source(resource_name="r", client_id="c", client_secret="s", sandbox=True)
    assert source is not None
    assert RampClient("c", "s", base_url=ramp.SANDBOX_URL).base_url == "https://demo-api.ramp.com"


def test_max_requests_must_leave_room_for_progress(fake):
    with pytest.raises(ValueError, match="at least 3"):
        ramp_source(resource_name="r", service=client_for(fake), max_requests=2)
