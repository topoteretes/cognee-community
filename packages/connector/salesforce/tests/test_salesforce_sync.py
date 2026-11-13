"""Rendering and sync against an in-memory Salesforce org. No network, no LLM.

Each test drives the fake through a change a real org makes - a new comment, a late
Chatter reply, a delete, an undelete, a record leaving a filter - and checks the rows the
connector yields and the state it keeps for the next run.
"""

from datetime import timedelta

import httpx
import pytest
from fake_salesforce import FakeSalesforce, client_for, hide_from_listing, sync

from cognee_community_connector_salesforce import salesforce_source
from cognee_community_connector_salesforce.salesforce import (
    SalesforceAPIError,
    SalesforceSourceError,
    SalesforceUnavailableError,
    _default_resource_name,
    render_record,
)

# an hour: past the few minutes each run re-reads, so a test of a child stream can't pass
# just because the overlap happened to re-render the parent
AGE = 3600


def age_past_overlap(fake, state, **kwargs):
    """Make the seeded records old news, so only a child stream can see what changes next.

    The cursor sits at the newest record's own stamp and every read is ">= cursor", so the
    newest record is re-read each run anyway. Newer records of every type move each cursor
    past the seeded ones, and an hour passes, beyond the re-read overlap. ``kwargs`` are
    the test's own sync arguments: a run with others would change the selection and read
    everything in full.
    """
    fake.tick(AGE)
    for sobject in ("Account", "Contact", "Opportunity", "Case"):
        fake.add(sobject, Name="Newer", Subject="Newer", CaseNumber="99999", IsClosed=False)
    sync(fake, state, **kwargs)
    fake.tick(AGE)


@pytest.fixture
def fake():
    return FakeSalesforce()


def seed(fake):
    """A small org shaped like the recorded test data."""
    alpha = fake.add(
        "Account",
        Name="Alphacorp Logistics",
        Industry="Transportation",
        Description="Regional freight carrier.",
    )
    dana = fake.add("Contact", Name="Dana Okafor", Title="Head of Operations", AccountId=alpha)
    opp = fake.add(
        "Opportunity",
        Name="Alphacorp fleet renewal",
        StageName="Negotiation/Review",
        Amount=120000,
        CloseDate="2026-11-28",
        NextStep="Send revised SLA",
        IsClosed=False,
        IsWon=False,
        AccountId=alpha,
    )
    case = fake.add(
        "Case",
        CaseNumber="00001026",
        Subject="Tracking page shows stale locations",
        Status="New",
        Priority="High",
        Origin="Email",
        IsClosed=False,
        AccountId=alpha,
        ContactId=dana,
    )
    return alpha, dana, opp, case


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_a_case_document_carries_its_comments_tasks_and_chatter(fake):
    *_, case = seed(fake)
    fake.add_comment(case, "Reproduced: the cache TTL is 45 minutes.", published=False)
    fake.add_task("Call Dana about the lag", what_id=case, description="Weekly check-in agreed.")
    post = fake.add_post(case, "Escalating: they missed a delivery window.")
    fake.add_reply(post, "Fix ships in 2.4.1.")

    upserts, _, _ = sync(fake, {})

    document = upserts[f"case:{case}"]["content"]
    assert document.startswith("Case 00001026: Tracking page shows stale locations")
    assert "Status: New · Priority: High · Origin: Email · Open" in document
    assert "Account: Alphacorp Logistics · Contact: Dana Okafor · Owner: Anmol Gupta" in document
    assert "(internal, Anmol Gupta): Reproduced: the cache TTL is 45 minutes." in document
    assert "Completed: Call Dana about the lag - Weekly check-in agreed." in document
    assert "Anmol Gupta: Escalating: they missed a delivery window." in document
    assert "reply Anmol Gupta: Fix ships in 2.4.1." in document


def test_a_record_with_no_children_renders_without_them(fake):
    """Salesforce sends null, not an empty result, for a subquery with no rows."""
    alpha, *_ = seed(fake)

    upserts, _, stats = sync(fake, {})

    document = upserts[f"account:{alpha}"]["content"]
    assert "Tasks" not in document and "Chatter" not in document
    assert stats["failed"] == 0


def test_case_comment_mirrors_in_the_feed_are_not_rendered_twice(fake):
    """Every comment is also a CaseCommentPost with an EMPTY body in the feed."""
    *_, case = seed(fake)
    fake.add_comment(case, "Only once, please.")
    fake.mirror_comment_in_feed(case)

    upserts, _, _ = sync(fake, {})

    document = upserts[f"case:{case}"]["content"]
    assert document.count("Only once, please.") == 1
    assert "Chatter" not in document


def test_one_node_set_per_object_type(fake):
    """Bounded by four object types, never by the number of records."""
    seed(fake)

    upserts, _, _ = sync(fake, {})

    node_sets = {tuple(row["cognee_node_set"]) for row in upserts.values()}
    assert node_sets == {
        ("salesforce:account",),
        ("salesforce:contact",),
        ("salesforce:opportunity",),
        ("salesforce:case",),
    }


def test_long_text_and_many_comments_are_clipped(fake):
    *_, case = seed(fake)
    fake.records["Case"][case]["Description"] = "x" * 50_000
    for n in range(40):
        fake.add_comment(case, f"comment {n:02d}")

    upserts, _, _ = sync(fake, {})

    document = upserts[f"case:{case}"]["content"]
    assert "more characters]" in document
    assert "Case comments (20, newest first)" in document
    assert "comment 39" in document and "comment 19" not in document
    assert len(document) < 10_000


def test_render_refuses_a_malformed_record_id():
    with pytest.raises(SalesforceAPIError, match="malformed record id"):
        render_record("Case", {"Id": "x' OR Id != '", "Subject": "s"})


# ---------------------------------------------------------------------------
# Incremental sync
# ---------------------------------------------------------------------------


def test_a_second_run_rewrites_nothing(fake):
    seed(fake)
    state: dict = {}
    sync(fake, state)

    upserts, deletes, stats = sync(fake, state)

    assert upserts == {} and deletes == set()
    assert stats["completed"] is True


def test_a_run_cut_short_between_tied_records_loses_none(fake):
    """The org's sample data shares one SystemModstamp to the millisecond across all its
    accounts. Within one listing Salesforce pages by locator, so ties only matter when a
    run stops between pages: the next run starts from the stamp it reached, and a strict
    "> cursor" would skip the tied records it had not read yet."""
    fake.page_size = 2
    ids = [fake.add("Account", Name=f"Tie {n}", same_second=True) for n in range(5)]
    state: dict = {}

    only = {"objects": ("Account",), "chatter": False}  # no object-list request
    # a token and one page: the run stops with two of the five tied records read
    first, _, stats = sync(fake, state, max_requests=2, **only)
    assert stats["failed_budget"] == 1 and len(first) == 2
    fake.tick(AGE)  # and the overlap can't be what saves them
    second, _, _ = sync(fake, state, **only)

    assert set(first) | set(second) == {f"account:{i}" for i in ids}


def test_an_edit_rewrites_the_document(fake):
    alpha, *_ = seed(fake)
    state: dict = {}
    sync(fake, state)
    fake.edit("Account", alpha, Description="Renewal back on track after the fix.")

    upserts, _, _ = sync(fake, state)

    assert "Renewal back on track" in upserts[f"account:{alpha}"]["content"]


def test_a_new_case_comment_rewrites_its_case(fake):
    """Recorded on a live org: a new comment does NOT move its Case's SystemModstamp, so
    the case's own cursor never sees it. Only the comment stream does."""
    *_, case = seed(fake)
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    before = fake.records["Case"][case]["SystemModstamp"]
    fake.add_comment(case, "Cache TTL lowered to 5 minutes; lag gone.")
    assert fake.records["Case"][case]["SystemModstamp"] == before

    upserts, _, _ = sync(fake, state)

    assert "Cache TTL lowered to 5 minutes" in upserts[f"case:{case}"]["content"]


def test_a_deleted_comment_leaves_its_case(fake):
    *_, case = seed(fake)
    comment = fake.add_comment(case, "Posted by mistake.")
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    fake.delete_comment(comment)

    upserts, _, _ = sync(fake, state)

    assert "Posted by mistake." not in upserts[f"case:{case}"]["content"]


def test_a_task_on_a_contact_rewrites_the_contact(fake):
    """A contact's tasks hang off WhoId, not WhatId."""
    _, dana, _, _ = seed(fake)
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    fake.add_task("Send Dana the SLA", who_id=dana)

    upserts, _, _ = sync(fake, state)

    assert "Send Dana the SLA" in upserts[f"contact:{dana}"]["content"]


def test_a_late_chatter_reply_rewrites_the_record(fake):
    """Recorded: a reply moves its post's SystemModstamp, so the feed stream catches it."""
    *_, case = seed(fake)
    post = fake.add_post(case, "Escalating.")
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    fake.add_reply(post, "Update: 2.4.1 is live.")

    upserts, _, _ = sync(fake, state)

    assert "reply Anmol Gupta: Update: 2.4.1 is live." in upserts[f"case:{case}"]["content"]


def test_without_access_activities_the_error_names_the_permission(fake):
    seed(fake)
    fake.activities_access = False

    with pytest.raises(SalesforceAPIError, match="Access Activities"):
        sync(fake, {})


def test_tasks_can_be_left_out(fake):
    seed(fake)
    fake.activities_access = False

    _, _, stats = sync(fake, {}, tasks=False)

    assert stats["completed"] is True


# ---------------------------------------------------------------------------
# Deletes
# ---------------------------------------------------------------------------


def test_a_deleted_record_is_forgotten(fake):
    alpha, *_ = seed(fake)
    bravo = fake.add("Account", Name="Bravocorp Retail")
    state: dict = {}
    sync(fake, state)
    fake.delete("Account", bravo)
    fake.tick(60)  # getDeleted needs a window of a minute or more

    _, deletes, stats = sync(fake, state)

    assert deletes == {f"account:{bravo}"}
    assert f"account:{alpha}" in state["rows"]
    assert stats["deleted"] == 1


def test_an_undeleted_record_comes_back(fake):
    """Recorded: an undelete gives the record a new, later SystemModstamp."""
    bravo = fake.add("Account", Name="Bravocorp Retail")
    state: dict = {}
    sync(fake, state)
    fake.delete("Account", bravo)
    fake.tick(60)
    sync(fake, state)
    fake.undelete("Account", bravo)

    upserts, deletes, _ = sync(fake, state)

    assert f"account:{bravo}" in upserts and deletes == set()


def test_a_lagging_latest_date_covered_reports_deletes_again_harmlessly(fake):
    """latestDateCovered lagged hours behind on a live org while newer deletes were
    already listed, so the next window re-reads them."""
    fake.covered_lag = fake.clock - fake.replication_enabled
    bravo = fake.add("Account", Name="Bravocorp Retail")
    state: dict = {}
    sync(fake, state)
    fake.delete("Account", bravo)
    fake.tick(60)

    _, first, _ = sync(fake, state)
    fake.tick(60)
    _, second, stats = sync(fake, state)

    assert first == {f"account:{bravo}"}
    assert second == set() and stats["completed"] is True


def test_a_refused_deleted_records_window_falls_back_to_comparing_ids(fake):
    """Same INVALID_REPLICATION_DATE code, two messages; either way the ids are compared,
    and only a record that answers 404 is forgotten."""
    alpha, *_ = seed(fake)
    bravo = fake.add("Account", Name="Bravocorp Retail")
    state: dict = {}
    sync(fake, state)
    fake.purge("Account", bravo)  # gone without a trace in getDeleted
    fake.replication_enabled = fake.tick(3600)  # the window now starts before it

    _, deletes, _ = sync(fake, state)

    assert deletes == {f"account:{bravo}"}
    assert f"account:{alpha}" in state["rows"]


def test_comparing_ids_keeps_a_record_salesforce_still_serves(fake):
    """No filter, listed nowhere, yet it answers a GET: kept, never guessed away."""
    alpha, *_ = seed(fake)
    state: dict = {"reconcile": []}
    sync(fake, state)
    state["reconcile"].append("Account")
    hide_from_listing(fake, "Account", alpha)

    _, deletes, _ = sync(fake, state)

    assert deletes == set()
    assert f"account:{alpha}" in state["rows"]


def test_a_failing_listing_forgets_nothing(fake):
    seed(fake)
    state: dict = {}
    sync(fake, state)
    rows_before = dict(state["rows"])
    fake.queued = [httpx.Response(500, content=b"{}")] * 10

    with pytest.raises(SalesforceUnavailableError):
        sync(fake, state)

    assert state["rows"] == rows_before


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_a_record_edited_out_of_the_filter_is_forgotten(fake):
    *_, case = seed(fake)
    state: dict = {}
    where = {"Case": "IsClosed = false"}
    upserts, _, _ = sync(fake, state, where=where)
    assert f"case:{case}" in upserts
    fake.edit("Case", case, IsClosed=True, Status="Closed")

    _, deletes, _ = sync(fake, state, where=where)

    assert deletes == {f"case:{case}"}


def test_dropping_an_object_from_the_selection_forgets_its_records(fake):
    alpha, *_, case = seed(fake)
    state: dict = {}
    sync(fake, state)

    _, deletes, _ = sync(fake, state, objects=("Account",))

    assert f"case:{case}" in deletes
    assert f"account:{alpha}" not in deletes


def test_a_narrowed_filter_compares_ids_and_forgets_what_left(fake):
    *_, case = seed(fake)
    other = fake.add(
        "Case",
        CaseNumber="00001027",
        Subject="Billing question",
        Status="New",
        IsClosed=False,
        Priority="Low",
    )
    state: dict = {}
    sync(fake, state)

    _, deletes, _ = sync(fake, state, where={"Case": "Priority = 'High'"})

    assert deletes == {f"case:{other}"}
    assert f"case:{case}" in state["rows"]


def test_bad_arguments_are_refused_up_front(fake):
    for bad in ("Case", ["Lead"], []):
        with pytest.raises(ValueError, match="objects"):
            salesforce_source(service=client_for(fake), objects=bad)
    with pytest.raises(ValueError, match="where"):
        salesforce_source(
            service=client_for(fake), objects=["Account"], where={"Case": "IsClosed = false"}
        )
    with pytest.raises(ValueError, match="api_version"):
        salesforce_source(service=client_for(fake), api_version="latest")
    with pytest.raises(ValueError, match="snake_case"):
        salesforce_source(service=client_for(fake), resource_name="Salesforce-Acme")
    with pytest.raises(ValueError, match="My Domain"):
        salesforce_source(
            login_url="https://login.salesforce.com", client_id="a", client_secret="b"
        )
    with pytest.raises(ValueError, match="client_id"):
        salesforce_source(login_url="https://acme.my.salesforce.com")


def test_the_default_resource_name_stays_put_when_the_selection_changes(fake):
    """A name that followed the selection would move a narrowed source to a fresh table and
    state, and every document of the old selection would stay in the dataset for good."""
    assert _default_resource_name("https://acme-dev-ed.my.salesforce.com") == (
        "salesforce_acme_dev_ed"
    )
    everything = salesforce_source(service=client_for(fake))
    cases = salesforce_source(
        service=client_for(fake), objects=["Case"], where={"Case": "IsClosed = false"}
    )
    assert everything.name == cases.name == "salesforce_acme_dev_ed"


def test_max_requests_must_fit_one_pass_of_the_selection(fake):
    """A budget that can't fit one pass stops at the same place on every run."""
    with pytest.raises(ValueError, match="at least 16"):
        salesforce_source(service=client_for(fake), max_requests=15)
    # a token, one page and one deleted-records call for cases, one comment stream
    salesforce_source(
        service=client_for(fake),
        objects=["Case"],
        include_tasks=False,
        include_chatter=False,
        max_requests=4,
    )


def test_state_from_another_org_is_refused(fake):
    seed(fake)
    state: dict = {}
    sync(fake, state)
    other = FakeSalesforce(org_id="00Dg500000OTHERXXX")
    seed(other)

    with pytest.raises(SalesforceSourceError, match="another Salesforce org"):
        sync(other, state)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_a_run_cut_short_by_its_budget_resumes_and_finishes(fake):
    seed(fake)
    state: dict = {}

    _, _, first = sync(fake, state, max_requests=2)
    _, _, second = sync(fake, state)

    assert first["completed"] is False and first["failed_budget"] == 1
    assert second["completed"] is True
    assert len(state["rows"]) == 4


def test_the_run_leaves_the_last_of_the_org_allowance_alone(fake):
    """Other integrations share the daily allowance; a run stops with 5% of it left."""
    seed(fake)
    fake.api_used = int(fake.api_limit * 0.95)

    _, _, stats = sync(fake, {})

    assert stats["completed"] is False and stats["failed_api_limit"] == 1
    assert fake.api_used < fake.api_limit


def test_a_refused_request_limit_stops_the_run_and_keeps_its_rows(fake):
    seed(fake)
    state: dict = {}
    sync(fake, state)
    rows = dict(state["rows"])
    client = client_for(fake)
    client.connect()
    fake.queued = [
        httpx.Response(
            403,
            json=[
                {"message": "TotalRequests Limit exceeded.", "errorCode": "REQUEST_LIMIT_EXCEEDED"}
            ],
        )
    ]

    _, deletes, stats = sync(fake, state, client=client)

    assert stats["completed"] is False and stats["failed_rate_limit"] == 1
    assert deletes == set() and state["rows"] == rows


# ---------------------------------------------------------------------------
# Runs cut short, filters, dependents and feed tracking
# ---------------------------------------------------------------------------


def test_a_backfill_cut_short_still_catches_children_of_records_it_read(fake):
    """The child streams start before the first parent is read. Started only at the end
    of a full read, a cut-short backfill would leave them unset, and the next run would
    start them at its own start, past a comment added in between."""
    first = fake.add("Case", CaseNumber="1", Subject="Early", IsClosed=False)
    fake.tick(AGE)
    fake.add("Case", CaseNumber="2", Subject="Later", IsClosed=False)
    fake.add("Case", CaseNumber="3", Subject="Latest", IsClosed=False)
    fake.page_size = 1
    state: dict = {}
    only = {"objects": ("Case",), "tasks": False, "chatter": False}
    # a token and two pages: cases 1 and 2 are read, the cursor sits on case 2
    _, _, stats = sync(fake, state, max_requests=3, **only)
    assert stats["failed_budget"] == 1
    fake.add_comment(first, "Customer confirmed the fix.")
    fake.tick(AGE)

    upserts, _, stats = sync(fake, state, **only)

    assert stats["completed"] is True
    assert "Customer confirmed the fix." in upserts[f"case:{first}"]["content"]


def test_a_record_that_leaves_the_filter_without_changing_is_forgotten(fake):
    """A formula field or a relative date ("CloseDate = THIS_MONTH") can stop matching
    while SystemModstamp stays put. The filtered listing is the selection, so no GET."""
    *_, case = seed(fake)
    state: dict = {}
    where = {"Case": "IsClosed = false"}
    sync(fake, state, where=where)
    age_past_overlap(fake, state, where=where)
    fake.records["Case"][case]["IsClosed"] = True  # no SystemModstamp change
    fake.calls.clear()

    _, deletes, _ = sync(fake, state, where=where)

    assert deletes == {f"case:{case}"}
    assert not any(path.endswith(f"/Case/{case}") for _m, path, _p in fake.calls)


def test_a_record_that_enters_the_filter_without_changing_is_read(fake):
    *_, case = seed(fake)
    fake.records["Case"][case]["IsClosed"] = True
    state: dict = {}
    where = {"Case": "IsClosed = false"}
    sync(fake, state, where=where)
    age_past_overlap(fake, state, where=where)
    fake.records["Case"][case]["IsClosed"] = False  # no SystemModstamp change

    upserts, _, _ = sync(fake, state, where=where)

    assert f"case:{case}" in upserts


def test_a_comment_on_a_record_outside_the_filter_does_not_bring_it_in(fake):
    seed(fake)
    closed = fake.add("Case", CaseNumber="7", Subject="Done", IsClosed=True)
    state: dict = {}
    where = {"Case": "IsClosed = false"}
    sync(fake, state, where=where)
    age_past_overlap(fake, state, where=where)
    fake.add_comment(closed, "Thanks, all good.")

    upserts, _, _ = sync(fake, state, where=where)

    assert f"case:{closed}" not in upserts and f"case:{closed}" not in state["rows"]


def test_renaming_an_account_rewrites_the_documents_that_show_its_name(fake):
    """A rename moves the account's SystemModstamp, not its contacts', opportunities' or
    cases' - yet each of their documents says "Account: <name>"."""
    alpha, dana, opp, case = seed(fake)
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    fake.edit("Account", alpha, Name="Alphacorp Freight")

    upserts, _, _ = sync(fake, state)

    for row_id in (f"contact:{dana}", f"opportunity:{opp}", f"case:{case}"):
        assert "Account: Alphacorp Freight" in upserts[row_id]["content"], row_id


def test_renaming_a_contact_rewrites_their_cases(fake):
    _, dana, _, case = seed(fake)
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    fake.edit("Contact", dana, Name="Dana Okafor-Reyes")

    upserts, _, _ = sync(fake, state)

    assert "Contact: Dana Okafor-Reyes" in upserts[f"case:{case}"]["content"]


def test_dependents_left_over_by_a_cut_short_run_are_done_next_run(fake):
    alpha, *_, case = seed(fake)
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    fake.edit("Account", alpha, Name="Alphacorp Freight")
    # token, object list, a page per object (4) and per child stream (6), a deleted-records
    # call per object (4): the run stops before the dependents are rendered
    _, _, stats = sync(fake, state, max_requests=16)
    assert stats["failed_budget"] == 1 and state["renamed"]

    upserts, _, _ = sync(fake, state)

    assert "Account: Alphacorp Freight" in upserts[f"case:{case}"]["content"]
    assert state["renamed"] == {}


def test_an_object_without_feed_tracking_is_synced_without_chatter(fake):
    """Feed tracking is per object; querying an untracked object's feed is an error."""
    alpha, *_, case = seed(fake)
    fake.add_post(case, "Escalating.")
    fake.feed_enabled = {"Case"}

    upserts, _, stats = sync(fake, {})

    assert stats["completed"] is True and stats["failed"] == 0
    assert "Escalating." in upserts[f"case:{case}"]["content"]
    assert f"account:{alpha}" in upserts


def test_turning_feed_tracking_on_reads_the_object_again(fake):
    """Posts from before tracking went off come back with it, older than any window the
    new feed stream would read, so the object is read in full."""
    alpha, *_ = seed(fake)
    fake.add_post(alpha, "Renewal signed.")
    fake.feed_enabled = {"Case"}
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    fake.feed_enabled.add("Account")

    upserts, _, _ = sync(fake, state)

    assert "Renewal signed." in upserts[f"account:{alpha}"]["content"]


def test_a_comment_emptied_from_the_recycle_bin_still_leaves_its_case(fake):
    """queryAll sees a deleted comment only while it is in the Recycle Bin (15 days).
    After a longer gap the case is read in full instead."""
    *_, case = seed(fake)
    comment = fake.add_comment(case, "Posted by mistake.")
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    fake.delete_comment(comment)
    fake.tick(int(timedelta(days=16).total_seconds()))
    fake.purge_comment(comment)

    upserts, _, _ = sync(fake, state)

    assert "Posted by mistake." not in upserts[f"case:{case}"]["content"]


def test_comments_with_one_created_date_render_the_same_every_time(fake):
    """Two comments on one live case share a CreatedDate to the second (recorded); an
    order that changed between reads would rewrite an unchanged document."""
    *_, case = seed(fake)
    fake.add_comment(case, "First of two.")
    fake.add_comment(case, "Second of two.", same_second=True)

    first, _, _ = sync(fake, {})
    second, _, _ = sync(fake, {})

    assert first[f"case:{case}"]["content"] == second[f"case:{case}"]["content"]


# ---------------------------------------------------------------------------
# Deleted-records windows
# ---------------------------------------------------------------------------


def test_the_delete_window_starts_at_latest_date_covered(fake):
    """latestDateCovered lags; the next window starts there, not at the end asked for."""
    fake.covered_lag = timedelta(minutes=30)
    seed(fake)
    state: dict = {}
    sync(fake, state)
    fake.tick(3600)
    fake.calls.clear()

    sync(fake, state)

    windows = [p for _m, path, p in fake.calls if path.endswith("/Account/deleted/")]
    end = windows[0]["end"]
    assert state["deleted_cursors"]["Account"] < end.replace("Z", "")


def test_a_run_seconds_after_the_last_skips_the_deleted_records_call(fake):
    """Salesforce refuses a window under a minute; asking anyway would fall back to listing
    every id of every object on each frequent sync."""
    seed(fake)
    state: dict = {}
    sync(fake, state)
    fake.tick(20)
    fake.calls.clear()

    _, _, stats = sync(fake, state)

    paths = [path for _m, path, _p in fake.calls]
    assert stats["completed"] is True
    assert not any(path.endswith("/deleted/") for path in paths)
    queries = [p.get("q", "") for _m, _path, p in fake.calls]
    assert not any(q.startswith("SELECT Id FROM") for q in queries)


def test_a_purged_window_falls_back_to_comparing_ids(fake):
    """earliestDateAvailable later than the window's start: deletes before it are gone."""
    alpha, *_ = seed(fake)
    bravo = fake.add("Account", Name="Bravocorp Retail")
    state: dict = {}
    sync(fake, state)
    fake.tick(60)
    fake.purge("Account", bravo)

    _, deletes, _ = sync(fake, state)

    assert deletes == {f"account:{bravo}"}
    assert f"account:{alpha}" in state["rows"]


def test_a_month_without_a_run_compares_ids_instead_of_asking_for_deletes(fake):
    """getDeleted refuses a start more than 30 days back."""
    seed(fake)
    bravo = fake.add("Account", Name="Bravocorp Retail")
    state: dict = {}
    sync(fake, state)
    fake.purge("Account", bravo)
    fake.tick(int(timedelta(days=31).total_seconds()))
    fake.calls.clear()

    _, deletes, stats = sync(fake, state)

    assert deletes == {f"account:{bravo}"} and stats["completed"] is True
    assert not any(path.endswith("/deleted/") for _m, path, _p in fake.calls)


def test_a_record_salesforce_will_not_describe_is_kept_and_counted(fake):
    """Only a 404 NOT_FOUND forgets a record; any other answer keeps it."""
    alpha, *_ = seed(fake)
    state: dict = {"reconcile": []}
    sync(fake, state)
    state["reconcile"].append("Account")
    hide_from_listing(fake, "Account", alpha)
    original_record = fake._record

    def refuse(sobject, record_id):
        if record_id == alpha:
            return fake._json(403, [{"message": "no", "errorCode": "INSUFFICIENT_ACCESS"}])
        return original_record(sobject, record_id)

    fake._record = refuse
    _, deletes, stats = sync(fake, state)

    assert deletes == set() and f"account:{alpha}" in state["rows"]
    assert stats["failed"] == 1 and stats["completed"] is True


def test_the_same_query_page_twice_is_an_error_not_a_loop(fake):
    fake.page_size = 1
    for n in range(3):
        fake.add("Account", Name=f"A{n}")

    def same_again(locator):
        body = {"totalSize": 3, "done": False, "records": []}
        body["nextRecordsUrl"] = f"/services/data/v66.0/query/{locator}"
        return fake._json(200, body)

    fake._more = same_again

    with pytest.raises(SalesforceAPIError, match="same query page twice"):
        sync(fake, {}, objects=("Account",))


def test_a_revoked_authorization_stops_the_run_mid_way(fake):
    """check_active runs before every request, so a revoke lands within one request."""
    seed(fake)
    asked: list[int] = []

    def check_active():
        asked.append(1)
        if len(asked) >= 3:
            raise PermissionError("revoked")

    client = client_for(fake)
    client.check_active = check_active

    with pytest.raises(PermissionError):
        sync(fake, {}, client=client)
    assert len(fake.calls) == 2


def test_the_document_sync_version_is_checked(fake, monkeypatch):
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)

    with pytest.raises(RuntimeError, match=r"version 2 or later.*reports 1"):
        salesforce_source(service=client_for(fake))


def test_a_rename_seen_while_one_object_is_read_in_full_reaches_its_dependents(fake):
    """Turning feed tracking off re-reads only that object in full; a rename in that read
    must still reach the documents that show the name."""
    alpha, dana, *_ = seed(fake)
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    fake.edit("Account", alpha, Name="Alphacorp Freight")
    fake.feed_enabled.discard("Account")

    upserts, _, _ = sync(fake, state)

    assert "Account: Alphacorp Freight" in upserts[f"contact:{dana}"]["content"]


def test_a_rename_first_seen_through_a_child_stream_reaches_its_dependents(fake):
    """The account is renamed after its own read but before the task stream re-renders it."""
    alpha, dana, *_ = seed(fake)
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    fake.add_task("Renewal call", what_id=alpha)
    original = fake._query

    def rename_before_tasks(soql, *, include_deleted):
        if soql.startswith("SELECT WhatId, WhoId") and "Freight" not in str(fake.records):
            fake.records["Account"][alpha]["Name"] = "Alphacorp Freight"  # stamp unmoved
        return original(soql, include_deleted=include_deleted)

    fake._query = rename_before_tasks
    first, _, _ = sync(fake, state)
    fake._query = original
    fake.tick(AGE)
    second, _, _ = sync(fake, state)

    contact = {**first, **second}[f"contact:{dana}"]
    assert "Account: Alphacorp Freight" in contact["content"]


def test_many_renames_under_a_small_budget_still_finish_and_never_starve_deletes(fake):
    """Renamed names are worked off 200 at a time, and deletes come first."""
    accounts = [fake.add("Account", Name=f"A{n}") for n in range(201)]
    case = fake.add("Case", CaseNumber="1", Subject="s", IsClosed=False, AccountId=accounts[0])
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    for account in accounts:
        fake.edit("Account", account, Name=f"{fake.records['Account'][account]['Name']} Ltd")
    fake.delete("Account", accounts[-1])
    fake.tick(60)

    for _ in range(10):
        _, _, stats = sync(fake, state, max_requests=16)
        if stats["completed"]:
            break

    assert stats["completed"] is True and state["renamed"] == {}
    assert f"account:{accounts[-1]}" not in state["rows"]
    assert f"case:{case}" in state["rows"]


def test_a_quiet_feed_does_not_force_a_full_read_two_weeks_later(fake):
    """A stream's cursor follows the runs, not only its rows: an object nobody posts on for
    14 days is not read in full again because of it."""
    for n in range(10):
        fake.add("Account", Name=f"Quiet {n}")
        fake.tick(600)  # apart by more than the re-read overlap
    state: dict = {}
    sync(fake, state)
    scanned = []
    for _ in range(16):
        fake.tick(24 * 3600)
        _, _, stats = sync(fake, state)
        scanned.append(stats["scanned"])

    assert max(scanned) <= 1, f"only the newest record is re-read each day: {scanned}"


def test_a_deleted_account_is_no_longer_named_on_its_cases(fake):
    """Whether Salesforce keeps the case's link to a deleted account is not recorded; if it
    does, the case must not go on naming it."""
    alpha, *_, case = seed(fake)
    state: dict = {}
    sync(fake, state)
    age_past_overlap(fake, state)
    fake.delete("Account", alpha)
    fake.tick(60)

    upserts, _, _ = sync(fake, state)

    assert "Alphacorp" not in upserts[f"case:{case}"]["content"]


def test_rich_text_chatter_loses_its_markup_and_plain_text_keeps_its_brackets(fake):
    *_, case = seed(fake)
    fake.add_post(case, "<p>Fish &amp; chips <b>today</b></p>", rich=True)
    fake.add_post(case, "Plain: a<b and c>d &amp; e")

    upserts, _, _ = sync(fake, {})

    document = upserts[f"case:{case}"]["content"]
    assert "Anmol Gupta: Fish & chips today" in document
    assert "Anmol Gupta: Plain: a<b and c>d &amp; e" in document


def test_an_injected_client_gets_a_fresh_wait_budget_each_run(fake):
    """A client passed in with service= serves every run; retries spent in one run must
    not leave the next with none."""
    seed(fake)
    sleeps: list[float] = []
    client = client_for(fake, sleeps)
    state: dict = {}
    for _ in range(25):  # 25 runs x 3s of backoff > the 60s budget of one run
        client.connect()
        fake.queued = [httpx.Response(503, content=b""), httpx.Response(503, content=b"")]
        _, _, stats = sync(fake, state, client=client)
        assert stats["completed"] is True


def test_a_record_whose_check_keeps_failing_does_not_stop_every_run(fake):
    """One id answering 5xx must not block the rest; it is kept, never forgotten."""
    alpha, *_ = seed(fake)
    bravo = fake.add("Account", Name="Bravocorp Retail")
    state: dict = {"reconcile": []}
    sync(fake, state)
    state["reconcile"].append("Account")
    hide_from_listing(fake, "Account", alpha)
    fake.purge("Account", bravo)
    original_record = fake._record

    def fail(sobject, record_id):
        if record_id == alpha:
            return fake._json(503, [{"message": "down", "errorCode": "SERVER_UNAVAILABLE"}])
        return original_record(sobject, record_id)

    fake._record = fail
    _, deletes, stats = sync(fake, state)

    assert stats["completed"] is True and stats["failed"] == 1
    assert deletes == {f"account:{bravo}"} and f"account:{alpha}" in state["rows"]
