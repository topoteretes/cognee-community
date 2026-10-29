"""Rendering and sync against an in-memory Sentry. No network, no LLM.

Each test drives the fake through a change a real Sentry account would make (a resolve,
a delete, a merge, an issue aging out of retention) and checks the rows the connector
yields and the state it keeps for the next run.
"""

import copy

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, document_source_tag
from fake_sentry import (
    FakeSentry,
    ago,
    client_for,
    event_calls,
    listing_calls,
    serve,
    sync,
)

from cognee_community_connector_sentry import sentry, sentry_source
from cognee_community_connector_sentry.sentry import (
    MAX_EVENT_SAMPLE,
    SentryAuthError,
    SentryClient,
    _iter_rows,
    event_lines,
    render_issue,
)


@pytest.fixture
def fake():
    return FakeSentry()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_one_document_per_grouped_issue(fake):
    issue = fake.add_issue(
        "i1",
        title="ValueError: database is locked",
        assigned_to={"id": "9", "name": "Sam Lee", "type": "user", "email": "sam@example.com"},
    )
    event = fake.events["i1"]

    row = render_issue(issue, event_lines(event, "recommended"))

    assert row["id"] == "issue:i1"
    assert row["title"] == "ValueError: database is locked"
    lines = row["content"].splitlines()
    assert lines[0] == "ValueError: database is locked"
    assert lines[1] == (
        "Project: backend-api · Level: error · Status: unresolved (ongoing) · "
        "Short id: BACKEND-API-I1"
    )
    assert "412 events" in lines[2] and "37 users affected" in lines[2]
    assert "Culprit: app/views.py in handle_request" in lines
    assert "Assigned to: Sam Lee" in lines
    assert "Unhandled: the code did not catch this." in lines
    assert "Event sample (recommended): ValueError: database is locked" in lines
    assert "  in handle_request (app/views.py:42)" in lines
    assert lines[-1] == f"https://{fake.org}.sentry.io/issues/i1/"
    assert row["cognee_node_set"] == ["sentry:project:backend-api", "sentry:level:error"]
    assert row["_deleted"] is False


def test_only_in_app_frames_are_kept_and_they_are_capped(fake):
    frames = [{"filename": "lib/x.py", "function": "deep", "lineno": 1, "in_app": False}]
    frames += [
        {"filename": f"app/m{index}.py", "function": f"f{index}", "lineno": index, "in_app": True}
        for index in range(12)
    ]
    event = fake.add_event("i1", frames=frames)

    lines = event_lines(event, "recommended")

    assert len(lines) == 6, "one header plus five frames"
    assert all("lib/x.py" not in line for line in lines), "framework frames are dropped"
    # the innermost in-app frames are the ones kept
    assert lines[-1] == "  in f11 (app/m11.py:11)"
    assert lines[1] == "  in f7 (app/m7.py:7)"
    assert len(event_lines(event, "recommended", max_frames=2)) == 3


def test_a_trace_with_no_in_app_frames_falls_back_to_the_innermost_ones(fake):
    frames = [
        {"module": f"vendor.m{index}", "function": f"f{index}", "lineno": index, "in_app": False}
        for index in range(10)
    ]
    event = fake.add_event("i1", frames=frames)

    lines = event_lines(event, "latest")

    assert len(lines) == 4, "one header plus the three innermost frames"
    assert lines[-1] == "  in f9 (vendor.m9:9)" + " [not in-app]"


def test_breadcrumbs_and_contexts_are_never_ingested(fake):
    """Asserted on the whole synced document, not on the renderer that cannot see them.

    That is where the promise matters: breadcrumbs and contexts are where both the
    volume and most of the PII live, and the fake serves 50 breadcrumbs per event.
    """
    fake.add_issue("i1")

    upserts, _, _ = sync(fake, {}, event_sample=1)

    document = upserts["issue:i1"]["content"]
    assert "noise" not in document, "breadcrumbs reached the document"
    assert "CPython" not in document, "contexts reached the document"


def test_an_event_without_an_exception_entry_renders_nothing(fake):
    assert event_lines({"entries": [{"type": "message", "data": {}}]}, "latest") == []
    assert event_lines({}, "latest") == []


def test_an_issue_without_an_id_raises(fake):
    with pytest.raises(Exception, match="without an id"):
        render_issue({"title": "x"})


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def test_first_run_ingests_every_issue_and_the_second_emits_nothing(fake):
    fake.add_issue("i1", title="ValueError: database is locked")
    fake.add_issue("i2", title="KeyError: 'user_id'", project="frontend", level="warning")
    state: dict = {}

    upserts, deletes, stats = sync(fake, state)

    assert set(upserts) == {"issue:i1", "issue:i2"}
    assert not deletes
    assert stats["scanned"] == 2 and stats["deleted"] == 0 and stats["failed"] == 0
    assert set(state["rows"]) == {"issue:i1", "issue:i2"}

    upserts, deletes, stats = sync(fake, state)
    assert not upserts and not deletes
    assert stats["skipped"] == 2


def test_the_listing_asks_for_every_status_explicitly(fake):
    """Without ``query=``, Sentry answers is:unresolved and a resolve looks like a delete."""
    fake.add_issue("i1")
    sync(fake, {}, event_sample=0)

    listing = listing_calls(fake)[0][2]
    assert listing["query"] == "", "an explicit empty query is what returns every status"
    assert listing["sort"] == "date"
    assert listing["limit"] == "100"
    assert "statsPeriod" not in listing, "no window by default: retention is not deletion"


def test_resolving_an_issue_rewrites_the_document_and_forgets_nothing(fake):
    """A resolve moves no timestamp, so only a full listing each run can catch it."""
    fake.add_issue("i1")
    fake.add_issue("i2")
    state: dict = {}
    sync(fake, state)

    fake.issues["i1"]["status"] = "resolved"
    fake.issues["i1"]["substatus"] = None
    upserts, deletes, stats = sync(fake, state)

    assert not deletes, "a resolved issue is still an issue"
    assert set(upserts) == {"issue:i1"}
    assert "Status: resolved" in upserts["issue:i1"]["content"]
    assert stats["skipped"] == 1, "the untouched issue is skipped"
    # the resolved issue came back from the listing itself: no deletion check was
    # needed, and it was never treated as having left Sentry's window
    assert [call for call in fake.calls if call[1] == "/api/0/issues/i1/"] == []
    assert state["aged"] == {}
    assert stats["aged"] == 0


def test_archiving_an_issue_keeps_it_with_its_substatus(fake):
    fake.add_issue("i1")
    state: dict = {}
    sync(fake, state)

    fake.issues["i1"].update(status="ignored", substatus="archived_until_escalating")
    upserts, deletes, _ = sync(fake, state)

    assert not deletes
    assert "Status: ignored (archived_until_escalating)" in upserts["issue:i1"]["content"]


def test_a_status_change_does_not_re_read_the_event_sample(fake):
    fake.add_issue("i1")
    state: dict = {}
    sync(fake, state)
    before = len(event_calls(fake))

    fake.issues["i1"]["status"] = "resolved"
    upserts, _, _ = sync(fake, state)

    assert set(upserts) == {"issue:i1"}
    assert len(event_calls(fake)) == before, "lastSeen did not move, so the sample is reused"
    assert "in handle_request (app/views.py:42)" in upserts["issue:i1"]["content"]


def test_new_events_re_read_the_sample(fake):
    fake.add_issue("i1")
    state: dict = {}
    sync(fake, state)
    before = len(event_calls(fake))

    fake.issues["i1"].update(lastSeen=ago(0), count="500")
    fake.add_event("i1", kind="ValueError", value="database is still locked")
    upserts, _, _ = sync(fake, state)

    assert len(event_calls(fake)) == before + 1
    assert "database is still locked" in upserts["issue:i1"]["content"]


def test_the_event_sample_is_bounded_and_configurable(fake):
    fake.add_issue("i1")

    sync(fake, {}, event_sample=0)
    assert event_calls(fake) == [], "no sample means no event request"

    fake.calls.clear()
    sync(fake, {}, event_sample=MAX_EVENT_SAMPLE)
    slots = [call[1].rstrip("/").rsplit("/", 1)[-1] for call in event_calls(fake)]
    assert slots == ["recommended", "latest", "oldest"]

    with pytest.raises(ValueError, match="event_sample"):
        sentry_source(auth_token="t", org_slug="o", event_sample=MAX_EVENT_SAMPLE + 1)


def test_an_issue_whose_events_aged_out_still_gets_a_document(fake):
    """The issue is listed, but no event is within retention: a 404 on the sample."""
    fake.add_issue("i1")
    del fake.events["i1"]

    upserts, deletes, stats = sync(fake, {})

    assert not deletes and stats["failed"] == 0
    assert "Event sample" not in upserts["issue:i1"]["content"]


# ---------------------------------------------------------------------------
# Deletes
# ---------------------------------------------------------------------------


def test_a_deleted_issue_is_tombstoned_after_a_404(fake):
    """``DELETE`` answers 202 and queues the job; the issue then 404s and drops out."""
    fake.add_issue("i1")
    fake.add_issue("i2")
    state: dict = {}
    sync(fake, state)

    fake.delete_issue("i1")
    upserts, deletes, stats = sync(fake, state)

    assert deletes == {"issue:i1"}
    assert not upserts
    assert stats["deleted"] == 1
    assert "issue:i1" not in state["rows"]
    # exactly one confirming GET, and only for the missing id
    assert [call[1] for call in fake.calls if call[1] == "/api/0/issues/i1/"] == [
        "/api/0/issues/i1/"
    ]


def test_a_merged_away_issue_is_forgotten_through_its_redirect(fake):
    """Sentry answers a merged-away id with 200 and the *surviving* issue's id."""
    fake.add_issue("i1")
    fake.add_issue("i2")
    state: dict = {}
    sync(fake, state)

    fake.merge_into("i1", "i2")
    _, deletes, _ = sync(fake, state)

    assert deletes == {"issue:i1"}, "the old id must be forgotten, 200 or not"
    assert "issue:i1" not in state["rows"]
    assert "issue:i2" in state["rows"]
    assert "issue:i2" not in deletes


def test_an_issue_outside_the_listing_window_is_kept_not_forgotten(fake):
    """Retention filters the listing; ``GET /issues/{id}/`` still answers 200."""
    fake.add_issue("i1", last_seen=ago(45))
    fake.add_issue("i2")
    state: dict = {}
    sync(fake, state)

    fake.age_out("i1")
    upserts, deletes, stats = sync(fake, state)

    assert not deletes, "aged out of the listing window is not deleted"
    assert stats["aged"] == 1
    assert "issue:i1" in state["rows"]
    assert "last state Sentry reported" in upserts["issue:i1"]["content"]

    # and it is not re-checked on every run
    fake.calls.clear()
    upserts, deletes, _ = sync(fake, state)
    assert not deletes and not upserts
    assert [call for call in fake.calls if call[1] == "/api/0/issues/i1/"] == []


def test_an_aged_out_issue_that_comes_back_loses_the_marking(fake):
    fake.add_issue("i1", last_seen=ago(45))
    state: dict = {}
    sync(fake, state)
    fake.age_out("i1")
    sync(fake, state)

    fake.aged_out.clear()
    fake.issues["i1"]["lastSeen"] = ago(0)
    upserts, deletes, _ = sync(fake, state)

    assert not deletes
    assert "last state Sentry reported" not in upserts["issue:i1"]["content"]
    assert "issue:i1" not in state["aged"]


def test_a_partial_listing_forgets_nothing(fake):
    """The run stops mid-listing; the issues it never reached must survive."""
    for index in range(150):
        fake.add_issue(f"i{index:03d}", last_seen=ago(index))
    state: dict = {}
    sync(fake, state, event_sample=0)

    fake.delete_issue("i140")
    # two listing pages are needed, and the budget only pays for the first
    _, deletes, stats = sync(fake, state, event_sample=0, max_requests=1)

    assert not deletes, "an incomplete listing may never queue a deletion check"
    assert stats["failed_budget"] == 1
    assert state["walk"] is not None, "the walk resumes at the page it was reading"

    # the next run finishes the listing and only then tombstones
    _, deletes, _ = sync(fake, state, event_sample=0)
    assert deletes == {"issue:i140"}


def test_a_revoked_token_forgets_nothing(fake):
    """A dead token is a 401 everywhere, so no deletion check can be confirmed."""
    fake.add_issue("i1")
    fake.add_issue("i2")
    state: dict = {}
    client = client_for(fake)
    sync(fake, state, client=client)
    # deep, or an in-place edit of a per-row dict during the failed run would pass
    rows_before = copy.deepcopy(state["rows"])

    fake.delete_issue("i1")  # i1 really is gone, so a check would tombstone it
    fake.valid_token = "rotated-in-sentry"  # the client still holds the old one
    with pytest.raises(SentryAuthError):
        sync(fake, state, client=client)

    assert state["rows"] == rows_before, "nothing is forgotten without a confirmed 404"


def test_a_server_error_on_the_listing_forgets_nothing(fake):
    fake.add_issue("i1")
    state: dict = {}
    sync(fake, state)

    fake.delete_issue("i1")
    fake.queued = [httpx.Response(500, content=b"{}")] * 10
    with pytest.raises(Exception, match="HTTP 500"):
        sync(fake, state)
    assert "issue:i1" in state["rows"]


def test_an_unrecognised_404_on_one_issue_never_deletes_it(fake):
    fake.add_issue("i1")
    fake.add_issue("i2")
    state: dict = {}
    sync(fake, state)

    del fake.issues["i1"]
    undo = serve(fake, "/issues/i1/", 404, b'{"detail": "Invalid organization"}')
    try:
        _, deletes, stats = sync(fake, state)
    finally:
        undo()

    assert not deletes, "only Sentry's documented missing-resource body may delete"
    assert stats["failed"] == 1
    assert state["recheck_failed"] == ["i1"]
    assert "issue:i1" in state["rows"]


def test_an_issue_that_keeps_failing_is_given_up_on_not_deleted(fake):
    fake.add_issue("i1")
    state: dict = {}
    sync(fake, state)
    del fake.issues["i1"]

    undo = serve(fake, "/issues/i1/", 403, b'{"detail": "no"}')
    abandoned = 0
    try:
        for _ in range(6):
            _, deletes, stats = sync(fake, state)
            assert not deletes
            abandoned += stats.get("abandoned", 0)
        reads_at_give_up = len([call for call in fake.calls if call[1].endswith("/issues/i1/")])
        # a seventh run must not read it again: it is given up on for a week
        sync(fake, state)
        reads_after = len([call for call in fake.calls if call[1].endswith("/issues/i1/")])
    finally:
        undo()

    assert state["attempts"]["i1|refresh"][0] >= 5
    assert abandoned == 1, "giving up is announced once, not every run"
    assert reads_after == reads_at_give_up, "a given-up issue is not re-read for a week"
    assert "issue:i1" in state["rows"], "a document is kept when its issue cannot be read"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_project_slugs_filter_the_listing_by_numeric_id(fake):
    fake.add_issue("i1", project="backend-api")
    fake.add_issue("i2", project="frontend")

    upserts, _, _ = sync(fake, {}, project_slugs=["backend-api"], event_sample=0)

    assert set(upserts) == {"issue:i1"}
    assert listing_calls(fake)[0][2]["project"] == str(fake.projects["backend-api"])


def test_an_unknown_project_slug_is_an_error_not_a_whole_org_sync(fake):
    fake.add_issue("i1")
    with pytest.raises(Exception, match="has no project"):
        sync(fake, {}, project_slugs=["nope"], event_sample=0)


def test_issues_that_leave_the_selection_are_forgotten(fake):
    fake.add_issue("i1", project="backend-api")
    fake.add_issue("i2", project="frontend")
    state: dict = {}
    upserts, _, _ = sync(fake, state, event_sample=0)
    assert set(upserts) == {"issue:i1", "issue:i2"}

    upserts, deletes, _ = sync(fake, state, project_slugs=["backend-api"], event_sample=0)

    assert deletes == {"issue:i2"}, "a project that leaves the selection is forgotten"
    assert set(state["rows"]) == {"issue:i1"}


def test_a_changed_event_sample_re_renders_the_documents_it_keeps(fake):
    """``event_sample`` changes what a document says, so the kept ones are rewritten."""
    fake.add_issue("i1")
    state: dict = {}
    sync(fake, state, event_sample=1)

    upserts, deletes, _ = sync(fake, state, event_sample=0)

    assert not deletes
    assert "Event sample" not in upserts["issue:i1"]["content"]


# ---------------------------------------------------------------------------
# The dlt resource
# ---------------------------------------------------------------------------


def test_the_resource_is_tagged_for_cognee_document_mode(fake):
    fake.add_issue("i1")
    source = sentry_source(service=client_for(fake), org_slug=fake.org)

    assert document_source_tag(source) == "sentry"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == "sentry_mergetober_test"
    assert source.name == "sentry_mergetober_test"
    assert source.write_disposition == "merge"
    columns = source.columns
    assert columns["_deleted"]["hard_delete"] is True


def test_the_resource_name_defaults_to_the_organization(fake):
    source = sentry_source(service=client_for(FakeSentry(org="Other.Org")), org_slug="Other.Org")
    assert source.name == "sentry_other_org"
    with pytest.raises(ValueError, match="org_slug"):
        sentry_source(auth_token="t", org_slug="   ")


def test_check_active_can_stop_a_run(fake):
    fake.add_issue("i1")

    def revoked():
        raise PermissionError("the host revoked this sync")

    with pytest.raises(PermissionError):
        list(_iter_rows(client_for(fake), {}, {}, check_active=revoked))


def test_bad_arguments_are_refused_up_front(fake):
    with pytest.raises(ValueError, match="max_frames"):
        sentry_source(auth_token="t", org_slug="o", max_frames=0)
    with pytest.raises(ValueError, match="max_requests"):
        sentry_source(auth_token="t", org_slug="o", max_requests=1)
    with pytest.raises(ValueError, match="project_slugs"):
        sentry_source(auth_token="t", org_slug="o", project_slugs=[])
    with pytest.raises(ValueError, match="auth_token"):
        sentry_source(auth_token="", org_slug="o")
    # a name dlt would rename writes rows to one table while keeping two states
    with pytest.raises(ValueError, match="snake_case"):
        sentry_source(auth_token="t", org_slug="o", resource_name="Sentry-A")


def test_a_renamed_project_does_not_forget_its_live_issues(fake):
    """Sentry filters the listing by project *id*; a rename changes only the slug.

    Matching the selection on the slug would tombstone every live issue of a project
    that was renamed between two runs, with no error and no upstream deletion.
    """
    fake.add_issue("i1", project="backend-api")
    state: dict = {}
    upserts, _, _ = sync(fake, state, project_slugs=["backend-api"], event_sample=0)
    assert set(upserts) == {"issue:i1"}

    # the project keeps its id but is renamed upstream; the listing still returns it
    fake.issues["i1"]["project"]["slug"] = "backend-api-renamed"

    upserts, deletes, _ = sync(fake, state, project_slugs=["backend-api"], event_sample=0)

    assert deletes == set(), "a renamed project must not look like a deletion"
    assert set(state["rows"]) == {"issue:i1"}


def test_an_issue_without_a_readable_project_is_kept(fake):
    """Absence of evidence is not a deletion: the listing was already filtered."""
    fake.add_issue("i1", project="backend-api")
    state: dict = {}
    sync(fake, state, project_slugs=["backend-api"], event_sample=0)

    fake.issues["i1"]["project"] = None

    _, deletes, _ = sync(fake, state, project_slugs=["backend-api"], event_sample=0)

    assert deletes == set(), "an unreadable project field must not delete a live issue"
    assert set(state["rows"]) == {"issue:i1"}


def test_two_selections_of_one_org_get_different_resource_names():
    """Same org, different project selections, one dataset: they must not share state.

    A shared dlt resource state makes each run treat the other source's issues as gone
    from its selection and tombstone them, every run, without ever converging.
    """
    from cognee_community_connector_sentry.sentry import _default_resource_name

    backend = _default_resource_name("acme", ["backend-api"])
    frontend = _default_resource_name("acme", ["frontend"])
    whole_org = _default_resource_name("acme")

    assert backend != frontend, "two selections of one org must not share a resource"
    assert backend != whole_org and frontend != whole_org
    # stable across orderings and repeat calls, so a rename of the table never happens
    assert backend == _default_resource_name("acme", ["backend-api"])
    assert _default_resource_name("acme", ["a", "b"]) == _default_resource_name("acme", ["b", "a"])


def test_a_huge_title_and_message_do_not_make_an_unbounded_document(fake):
    """One issue's document stays bounded: exception text can be megabytes.

    Run at the *default* ``event_sample``, because the sampled event carries the
    exception value a second time and is also cached in dlt state.
    """
    fake.add_issue("i1")
    fake.issues["i1"]["title"] = "ValueError: " + "x" * 200_000
    fake.issues["i1"]["metadata"] = {"value": "y" * 100_000, "type": "ValueError"}
    fake.add_event("i1", value="z" * 300_000, frames=[{"inApp": True, "filename": "a" * 9_000}])
    state: dict = {}

    upserts, _, _ = sync(fake, state, event_sample=1)

    document = upserts["issue:i1"]["content"]
    assert len(document) < 8_000, f"document grew to {len(document)} characters"
    assert "more characters]" in document, "the cut must be visible in the document"
    cached = state["rows"]["issue:i1"]["sample"]
    assert len("".join(cached)) < 4_000, "the sample is cached in state on every load"


def test_an_issue_that_failed_last_run_gets_one_row_not_two(fake):
    """The queue pass before the listing must not emit a "last known state" row.

    An issue whose event read failed is re-read by id at the start of the next run.
    It is still in the listing, so marking it aged there would emit a stale document
    *and* the fresh one in a single load - two rows for one primary key, with dlt free
    to keep either.
    """
    fake.add_issue("i1", last_seen=ago(2))
    state: dict = {}
    sync(fake, state, event_sample=1)
    assert set(state["rows"]) == {"issue:i1"}
    # a new event arrives, so the sample is re-read - and that read fails, which puts
    # the id in the by-id queue for the start of the next run
    fake.issues["i1"]["lastSeen"] = ago(0)
    undo = serve(fake, "/events/recommended/", 403, b'{"detail": "forbidden"}')
    _, _, second = sync(fake, state, event_sample=1)
    assert second["failed"] == 1 and state["recheck_failed"] == ["i1"]
    undo()

    rows = list(_iter_rows(client_for(fake), state, {}, event_sample=1))

    ids = [row["id"] for row in rows]
    assert ids == ["issue:i1"], f"one issue, one row: got {ids}"
    assert "last state Sentry reported" not in rows[0]["content"], "a listed issue is not aged"
    assert state["aged"] == {}, "a listed issue must not be recorded as aged"


def test_an_issue_that_left_the_selection_is_forgotten_even_after_a_failed_run(fake):
    """The same pass must not resurrect an issue that has left the selection.

    Before the listing runs, the selected project ids are not resolved yet, so the
    scope check cannot pass there - it has to wait for the pass afterwards.
    """
    fake.add_issue("keep", project="backend-api")
    fake.add_issue("drop", project="frontend")
    both = ["backend-api", "frontend"]
    state: dict = {}
    sync(fake, state, project_slugs=both, event_sample=1)
    assert set(state["rows"]) == {"issue:keep", "issue:drop"}
    # a new event on "drop", and its event read fails: the id is retried by id next run
    fake.issues["drop"]["lastSeen"] = ago(0)
    undo = serve(fake, "/events/recommended/", 403, b'{"detail": "forbidden"}')
    sync(fake, state, project_slugs=both, event_sample=1)
    undo()
    assert state["recheck_failed"] == ["drop"]

    _, deletes, _ = sync(fake, state, project_slugs=["backend-api"], event_sample=1)

    assert deletes == {"issue:drop"}, "the issue out of scope is forgotten, not re-ingested"
    assert "issue:drop" not in state["rows"]


def test_the_default_resource_name_stays_within_the_dlt_limit(fake):
    """A long org slug plus the selection suffix must not outgrow the table name."""
    from cognee_community_connector_sentry.sentry import (
        _MAX_RESOURCE_NAME,
        _default_resource_name,
    )

    long_slug = "a" * 60
    plain = _default_resource_name(long_slug)
    selected = _default_resource_name(long_slug, ["backend-api"])

    assert len(plain) <= _MAX_RESOURCE_NAME
    assert len(selected) <= _MAX_RESOURCE_NAME
    assert selected != plain, "the selection must still change the name"
    assert selected != _default_resource_name(long_slug, ["frontend"])


def test_sentry_moving_its_own_fields_rewrites_nothing(fake):
    """Sentry re-prioritises and scores issues on its own, with no new event.

    None of that reaches a document, so a run that sees only those changes must emit
    no row and read no event - otherwise every issue is rewritten on every sync.
    """
    fake.add_issue("i1")
    state: dict = {}
    sync(fake, state, event_sample=1)
    events_before = len(event_calls(fake))
    fake.issues["i1"].update(
        {
            "priority": "high",
            "seerFixabilityScore": 0.81,
            "seerAutofixLastTriggered": ago(0),
            "stats": {"24h": [[1761600000, 9]]},
            "hasSeen": True,
            "annotations": [{"displayName": "PROJ-1"}],
        }
    )

    upserts, deletes, stats = sync(fake, state, event_sample=1)

    assert upserts == {} and deletes == set(), "none of that changes the document"
    assert stats["skipped"] == 1
    assert len(event_calls(fake)) == events_before, "and no event is re-read"


def test_the_fields_sentry_moves_on_its_own_are_all_noise(fake):
    """The fast skip only works if it ignores the fields that move by themselves.

    Nothing is wrong when one is missing - the rendered fingerprint still catches it -
    but every issue is then re-rendered on every run. This pins the list to the issue
    payload a live organization actually sent (observations/experiment.json).
    """
    from cognee_community_connector_sentry.sentry import _NOISE_FIELDS

    fake.add_issue("i1")
    live_payload = set(fake.issues["i1"])
    moves_on_its_own = {
        "priority",
        "priorityLockedAt",
        "seerFixabilityScore",
        "seerAutofixLastTriggered",
        "seerExplorerAutofixLastTriggered",
        "stats",
        "lifetime",
        "filtered",
        "annotations",
        "hasSeen",
        "isSubscribed",
        "subscriptionDetails",
    }

    assert moves_on_its_own <= live_payload, "the fake must serve what the live API sends"
    assert moves_on_its_own <= set(_NOISE_FIELDS), (
        f"not treated as noise: {sorted(moves_on_its_own - set(_NOISE_FIELDS))}"
    )


def test_changing_the_selection_under_the_default_name_changes_the_scope(fake):
    """The documented consequence of folding the selection into the default name.

    Two selections get two resources, so they cannot delete each other's rows - and
    editing a selection therefore starts a fresh scope rather than forgetting the
    issues that left. A fixed ``resource_name`` is what keeps one scope across a change.
    """
    both = sentry_source(service=client_for(fake), org_slug=fake.org, project_slugs=["a", "b"])
    narrowed = sentry_source(service=client_for(fake), org_slug=fake.org, project_slugs=["a"])
    pinned = sentry_source(
        service=client_for(fake), org_slug=fake.org, project_slugs=["a"], resource_name="sentry_fix"
    )
    pinned_wider = sentry_source(
        service=client_for(fake),
        org_slug=fake.org,
        project_slugs=["a", "b"],
        resource_name="sentry_fix",
    )

    assert both.name != narrowed.name, "two selections never share state"
    assert pinned.name == pinned_wider.name == "sentry_fix", "a fixed name spans the change"


def test_the_same_event_is_not_put_in_the_document_three_times(fake):
    """``recommended``, ``latest`` and ``oldest`` overlap on a quiet issue.

    Sentry answers all three with the same event when an issue has few events, so
    rendering per slot would repeat one trace two or three times - and cache it that
    way in dlt state.
    """
    fake.add_issue("i1")

    upserts, _, _ = sync(fake, {}, event_sample=3)

    document = upserts["issue:i1"]["content"]
    assert document.count("Event sample") == 1, document
    # the culprit line also names the function, so count the rendered frame itself
    assert document.count("in handle_request (app/views.py:42)") == 1, document


def test_distinct_events_per_slot_are_all_rendered(fake):
    """The de-duplication must key on the event, not drop every slot after the first."""
    fake.add_issue("i1")
    fake.add_event("i1", slot="latest", event_id="ev-latest", value="a later failure")
    fake.add_event("i1", slot="oldest", event_id="ev-oldest", value="the first failure")

    upserts, _, _ = sync(fake, {}, event_sample=3)

    document = upserts["issue:i1"]["content"]
    assert document.count("Event sample") == 3, document
    assert "a later failure" in document and "the first failure" in document


def test_max_frames_cannot_be_raised_until_the_document_is_unbounded(fake):
    from cognee_community_connector_sentry.sentry import MAX_FRAMES_LIMIT

    with pytest.raises(ValueError, match="max_frames"):
        sentry_source(auth_token="t", org_slug="o", max_frames=MAX_FRAMES_LIMIT + 1)
    with pytest.raises(ValueError, match="max_frames"):
        sentry_source(auth_token="t", org_slug="o", max_frames=0)


def test_a_malformed_stats_period_is_refused_by_name(fake):
    """Otherwise it comes back as an opaque HTTP 400 naming the endpoint."""
    with pytest.raises(ValueError, match="stats_period"):
        sentry_source(auth_token="t", org_slug="o", stats_period="two weeks")
    sentry_source(auth_token="t", org_slug="o", stats_period="14d")


def test_giving_up_on_an_event_read_does_not_disable_the_deletion_check(fake):
    """The two reads an issue can fail are counted apart.

    One counter would mean an issue whose event sample keeps 403ing is eventually
    skipped by the by-id pass too - and that pass is the only thing that can notice the
    issue was deleted.
    """
    fake.add_issue("i1")
    state: dict = {}
    sync(fake, state, event_sample=1)
    undo = serve(fake, "/events/recommended/", 403, b'{"detail": "no"}')
    for run in range(6):
        fake.issues["i1"]["lastSeen"] = ago(0.1 * (6 - run))
        sync(fake, state, event_sample=1)
    undo()
    assert state["attempts"]["i1|listing"][0] >= 5, "the event read was given up on"

    del fake.issues["i1"]
    _, deletes, _ = sync(fake, state, event_sample=1)

    assert deletes == {"issue:i1"}, "the deletion check must still run"


def test_the_same_issue_in_a_different_field_order_is_not_rewritten(fake):
    """Sentry gives no order guarantee for a JSON object's keys.

    The input key and the fingerprint are both hashed from the payload, so hashing it
    in the order it arrived would rewrite every document whenever the API reshuffled.
    """
    fake.add_issue("i1")
    state: dict = {}
    sync(fake, state, event_sample=1)
    before = dict(state["rows"]["issue:i1"])

    issue = fake.issues["i1"]
    fake.issues["i1"] = dict(reversed(list(issue.items())))

    upserts, deletes, stats = sync(fake, state, event_sample=1)

    assert upserts == {} and deletes == set(), "reordering the payload changes nothing"
    assert stats["skipped"] == 1
    assert state["rows"]["issue:i1"] == before


def test_a_client_only_closes_the_pool_it_made(fake):
    """An injected client belongs to the caller; the tests reuse theirs across runs."""
    injected = fake.client()
    borrowed = SentryClient(fake.valid_token, org_slug=fake.org, http=injected)

    borrowed.close()

    assert not injected.is_closed, "a caller's client must survive being handed over"
    owned = SentryClient(fake.valid_token, org_slug=fake.org)
    owned.close()
    assert owned._http.is_closed


def test_the_resource_closes_the_client_it_made(fake, monkeypatch, tmp_path):
    """A scheduled sync builds a source per load; an unclosed pool leaks every time."""
    import dlt

    closed: list[str] = []

    def build(token, *, org_slug, host):
        client = SentryClient(token, org_slug=org_slug, host=host, http=fake.client())
        monkeypatch.setattr(client, "close", lambda: closed.append("closed"))
        return client

    fake.add_issue("i1")
    monkeypatch.setattr(sentry, "SentryClient", build)
    pipeline = dlt.pipeline(
        pipeline_name="sentry_close_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'close.db').as_posix()}"),
        dataset_name="sentry_close_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    pipeline.run(sentry_source(auth_token=fake.valid_token, org_slug=fake.org))

    assert closed == ["closed"], "the client the source made must be closed when the run ends"
