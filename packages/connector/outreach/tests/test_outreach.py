"""Tests for the Outreach dlt connector (ingest, incremental cursor, deletes, privacy).

All HTTP is served by ``FakeOutreach`` (see ``conftest.py``) through the real dlt retrying
session, ``rest_api`` source and ``OutreachAuth``; loads go to a temporary sqlite destination.
No network and no credentials. Fixtures under ``fixtures/`` are obviously fake.
"""

import copy
import io
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from conftest import BASE_URL, LONG_AGO, PATHS, ids, now_iso, read_table, resource

from cognee_community_connector_outreach import (
    OutreachAuthError,
    OutreachDeleteGuardError,
    OutreachError,
    outreach_source,
)
from cognee_community_connector_outreach import outreach as mod
from cognee_community_connector_outreach.outreach import (
    STREAMS,
    _Api,
    _BadRecord,
    _Config,
    _Skips,
    flatten_resource,
    normalise,
    pack_known,
    sync_stream,
    to_snake,
    unpack_known,
)

RUN = {"write_disposition": "merge", "primary_key": "id"}
SEQ, PROS, STATES = "outreach_sequences", "outreach_prospects", "outreach_sequence_states"
PROSPECT_IDS = {f"outreach:prospect:{i}" for i in range(1, 6)}

CONFIG_DEFAULTS = {
    "resources": ("sequences", "prospects", "activity"),
    "sequence_ids": (),
    "sequence_states": (),
    "activity_types": ("mailings", "calls", "tasks", "meetings"),
    "activity_since": timedelta(days=LONG_AGO),
    "include_contact_details": False,
    "include_activity_bodies": False,
    "page_size": 2,
    "overlap_seconds": 1,
    "sweep_every_n_runs": 1,
    "full_reconcile": False,
    "max_delete_ratio": 0.5,
    "force_delete": False,
    "skipped_table": False,
}
NOW = datetime(2026, 6, 1, tzinfo=UTC)


def cfg(**overrides):
    return _Config(**(CONFIG_DEFAULTS | overrides))


@pytest.fixture
def sync(session_for, make_auth):
    """Run ``sync_stream`` directly against a FakeOutreach with an in-memory state dict."""

    def run(name, fake, state, disposition="merge", now=NOW, skips=None, **overrides):
        api = _Api(BASE_URL, make_auth(fake), session_for(fake))
        return list(
            sync_stream(
                STREAMS[name], api, cfg(**overrides), state, disposition, skips or _Skips(), now
            )
        )

    return run


def causes(exc):
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def fail_with(exc_type, call):
    with pytest.raises(Exception) as info:
        call()
    matches = [e for e in causes(info.value) if isinstance(e, exc_type)]
    assert matches, (
        f"expected {exc_type.__name__} in {[type(e).__name__ for e in causes(info.value)]}"
    )
    return matches[0]


def res_state(pipeline, table):
    return pipeline.state["sources"]["outreach"]["resources"][table]


# ---------------------------------------------------------------------------
# 1. JSON:API flattening (pure)
# ---------------------------------------------------------------------------


def test_flatten_maps_attributes_relationships_and_timestamps():
    flat = flatten_resource(
        {
            "type": "prospect",
            "id": 7,
            "attributes": {
                "firstName": "Pat",
                "linkedInUrl": "x",
                "createdAt": "2026-03-01T10:00:00.000Z",
                "updatedAt": "2026-03-02T10:00:00+02:00",
            },
            "relationships": {
                "owner": {"data": {"type": "user", "id": 3}},
                "sequenceStates": {"data": [{"type": "s", "id": 1}, {"type": "s", "id": 2}]},
                "account": {"links": {"related": "https://x"}},  # link only: no call is made
            },
            "links": {"self": "https://api.example.test/prospects/7"},
        }
    )
    assert flat["id"] == "7" and flat["type"] == "prospect"
    assert flat["first_name"] == "Pat" and flat["linked_in_url"] == "x"
    assert flat["owner_id"] == "3"
    assert flat["sequence_states_ids"] == ["1", "2"]
    assert "account_id" not in flat
    assert flat["created_at"] == "2026-03-01T10:00:00+00:00"
    assert flat["updated_at"] == "2026-03-02T10:00:00+02:00"
    assert datetime.fromisoformat(flat["updated_at"]).tzinfo is not None
    assert flat["url"].endswith("/prospects/7")


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ("nope", "not_an_object"),
        ({"attributes": {}}, "missing_id"),
        ({"id": "  "}, "missing_id"),
        ({"id": "1", "attributes": []}, "invalid_attributes"),
        ({"id": "1", "attributes": {"updatedAt": "garbage"}}, "invalid_timestamp:updated_at"),
    ],
)
def test_flatten_rejects_malformed_records(record, reason):
    with pytest.raises(_BadRecord) as info:
        flatten_resource(record)
    assert info.value.reason == reason


def test_to_snake_and_known_pack_roundtrip():
    assert to_snake("sequenceStepCount") == "sequence_step_count"
    assert to_snake("linkedInUrl") == "linked_in_url"
    known = {"10": 5, "2": 7, "abc": 0}
    assert unpack_known(pack_known(known)) == known
    assert unpack_known(None) == {} and unpack_known("not-base64!!") == {}


# ---------------------------------------------------------------------------
# 2. Ingest path
# ---------------------------------------------------------------------------


def test_multi_page_ingest_follows_links_next(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, page_size=2), **RUN)

    assert ids(pipeline, PROS) == PROSPECT_IDS
    assert ids(pipeline, SEQ) == {f"outreach:sequence:{i}" for i in (1, 2, 3)}
    assert ids(pipeline, STATES) == {f"outreach:sequence_state:{i}" for i in range(1, 5)}
    assert len(ids(pipeline, "outreach_mailings")) == 3
    # 5 prospects at 2 per page => 3 list requests (+1 startup probe), each following links.next
    assert fake.count("/prospects") == 4
    pages = [c for c in fake.list_calls("/prospects") if c.get("page[size]") == "2"]
    assert [c.get("page[after]") for c in pages] == [None, "2", "4"]
    assert all(call[2] == "Bearer at-0" for call in fake.calls)


def test_source_is_a_document_source_and_row_shape(fake, make_source, pipeline_factory):
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = make_source(fake)
    assert document_source_tag(source) == "outreach"
    pipeline = pipeline_factory()
    pipeline.run(source, **RUN)
    rows = read_table(pipeline, PROS, "id, title, content, url, updated_at, owner_id, account_id")
    row = next(r for r in rows if r[0] == "outreach:prospect:1")
    assert row[1] == "Outreach prospect: Pat1 Example"
    assert "- Title: Buyer" in row[2] and "- Company: Acme Fake" in row[2]
    assert row[3].endswith("/prospects/1") and row[4].startswith("2026-03-01")
    assert (row[5], row[6]) == ("1", "9")


def test_request_shape_filters_sorting_sparse_fieldsets(fake, make_source, pipeline_factory):
    pipeline_factory().run(make_source(fake, resources=["prospects"]), **RUN)
    data = next(c for c in fake.list_calls("/prospects") if "sort" in c)
    assert data["sort"] == "updatedAt" and data["count"] == "false" and data["page[size]"] == "2"
    assert "filter[updatedAt]" not in data  # first run: no cursor
    fields = data["fields[prospect]"].split(",")
    assert "firstName" in fields and "owner" in fields and "emails" not in fields


def test_resources_and_activity_types_selection(fake, make_source, pipeline_factory):
    source = make_source(fake, resources=["sequences", "activity"], activity_types=["calls"])
    assert sorted(source.resources.keys()) == ["outreach_calls", "outreach_sequences"]
    pipeline_factory().run(source, **RUN)
    assert "/prospects" not in fake.paths() and "/mailings" not in fake.paths()


def test_scope_filters_are_sent_for_sequences_and_memberships(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(
        make_source(
            fake, resources=["sequences", "prospects"], sequence_ids=[1], sequence_states=["active"]
        ),
        **RUN,
    )
    assert ids(pipeline, SEQ) == {"outreach:sequence:1"}
    assert ids(pipeline, STATES) == {"outreach:sequence_state:2", "outreach:sequence_state:4"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resources": []},
        {"resources": ["nope"]},
        {"activity_types": ["emails"]},
        {"max_delete_ratio": 0},
        {"page_size": 0},
        {"sweep_every_n_runs": 0},
    ],
)
def test_invalid_arguments_rejected(kwargs):
    with pytest.raises(ValueError):
        outreach_source(**kwargs)


def test_page_size_capped(fake, make_source):
    make_source(fake, page_size=mod.MAX_PAGE_SIZE + 500)  # warns, does not raise
    assert cfg(page_size=min(5000, mod.MAX_PAGE_SIZE)).page_size == mod.MAX_PAGE_SIZE


# ---------------------------------------------------------------------------
# 3. Incremental cursor
# ---------------------------------------------------------------------------


def test_second_run_uses_stored_cursor_and_only_emits_changes(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, resources=["prospects"]), **RUN)
    assert res_state(pipeline, PROS)["cursor_updated_at"] == "2026-03-05T00:00:00+00:00"

    fake.touch("/prospects", 2, "2026-04-01T00:00:00.000Z", title="Director")
    fake.calls.clear()
    pipeline.run(make_source(fake, resources=["prospects"]), **RUN)

    data = [c for c in fake.list_calls("/prospects") if "sort" in c]
    # cursor 2026-03-05 minus the 1s overlap, inclusive lower bound, open upper bound
    assert data[0]["filter[updatedAt]"] == "2026-03-04T23:59:59.000Z..inf"
    emitted = res_state(pipeline, PROS)["last_run"]["emitted"]
    assert emitted == 2  # the edited record and the cursor-boundary record (overlap), not all 5
    assert res_state(pipeline, PROS)["cursor_updated_at"] == "2026-04-01T00:00:00+00:00"
    assert "Director" in _content(pipeline, PROS)
    assert ids(pipeline, PROS) == PROSPECT_IDS


def test_unchanged_second_run_emits_only_the_overlap_boundary(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, resources=["prospects"]), **RUN)
    pipeline.run(make_source(fake, resources=["prospects"]), **RUN)
    assert res_state(pipeline, PROS)["last_run"]["emitted"] == 1
    assert ids(pipeline, PROS) == PROSPECT_IDS


def test_equal_timestamp_records_across_page_boundary_are_not_dropped(
    fake, make_source, pipeline_factory
):
    same = "2026-05-01T12:00:00.000Z"
    fake.data["/prospects"] = [
        resource("prospect", i, same, firstName=f"P{i}") for i in range(1, 6)
    ]
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, resources=["prospects"], page_size=2), **RUN)
    assert ids(pipeline, PROS) == {f"outreach:prospect:{i}" for i in range(1, 6)}

    # A record created later with the *same* timestamp as the cursor is still delivered.
    fake.put("/prospects", resource("prospect", 99, same, firstName="Late"))
    pipeline.run(make_source(fake, resources=["prospects"], page_size=2), **RUN)
    assert "outreach:prospect:99" in ids(pipeline, PROS)
    assert res_state(pipeline, PROS)["last_run"]["fetched"] == 6


def test_cursor_not_advanced_when_data_pass_fails(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, resources=["prospects"]), **RUN)
    before = copy.deepcopy(res_state(pipeline, PROS))

    fake.touch("/prospects", 1, "2026-04-01T00:00:00.000Z")
    fake.fail_from_call["/prospects"] = (fake.count("/prospects") + 2, 403)  # revoked on data pass
    fail_with(
        OutreachAuthError,
        lambda: pipeline_factory().run(make_source(fake, resources=["prospects"]), **RUN),
    )
    assert res_state(pipeline_factory(), PROS) == before


def test_cursor_not_advanced_on_failed_load(fake, make_source, pipeline_factory, monkeypatch):
    from dlt.pipeline.pipeline import Pipeline

    real_load = Pipeline.load
    pipeline = pipeline_factory()
    monkeypatch.setattr(
        Pipeline, "load", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the failure
        pipeline.run(make_source(fake, resources=["prospects"]), **RUN)
    monkeypatch.setattr(Pipeline, "load", real_load)

    # Nothing was loaded, so the resumed run must still deliver every record.
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, resources=["prospects"]), **RUN)
    assert ids(pipeline, PROS) == PROSPECT_IDS


def test_future_timestamp_cannot_push_cursor_past_now(fake, sync):
    fake.data["/prospects"] = [resource("prospect", 1, "2999-01-01T00:00:00.000Z")]
    state = {}
    sync("prospects", fake, state)
    assert state["cursor_updated_at"] == NOW.isoformat()


def test_activity_first_sync_uses_lookback_window(fake, sync):
    state = {}
    sync("mailings", fake, state, activity_since=timedelta(days=30))
    first = next(c for c in fake.list_calls("/mailings") if "sort" in c)
    assert first["filter[updatedAt]"] == "2026-05-02T00:00:00.000Z..inf"


# ---------------------------------------------------------------------------
# 4. Deletes: sweep, guards, windows
# ---------------------------------------------------------------------------


def test_sweep_tombstones_missing_ids_and_forgets_them(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, resources=["prospects"]), **RUN)

    fake.remove("/prospects", 3)
    fake.calls.clear()
    pipeline.run(make_source(fake, resources=["prospects"]), **RUN)

    assert ids(pipeline, PROS) == PROSPECT_IDS - {"outreach:prospect:3"}
    sweep = fake.sweep_calls("/prospects")
    assert sweep and sweep[0]["fields[prospect]"] == "updatedAt" and "sort" not in sweep[0]
    assert res_state(pipeline, PROS)["last_run"]["tombstoned"] == 1
    assert "3" not in unpack_known(res_state(pipeline, PROS)["known"])


def test_sweep_runs_every_n_runs(fake, sync):
    state = {}
    sync("prospects", fake, state, sweep_every_n_runs=3)  # run 1: first sync
    for _ in range(2):
        sync("prospects", fake, state, sweep_every_n_runs=3)  # runs 2, 3
    sweeps = [c for c in fake.sweep_calls("/prospects") if "page[after]" not in c]
    assert len(sweeps) == 1 and state["runs"] == 3  # only run 3 swept


def test_incomplete_sweep_emits_no_deletes(fake, sync):
    state = {}
    sync("prospects", fake, state)
    fake.remove("/prospects", 3)
    fake.status_for[("/prospects", "sweep")] = 503  # exhausts retries
    rows = sync("prospects", fake, state)
    assert not [r for r in rows if r.get("_deleted")]
    assert state["last_run"]["sweep"] == "incomplete:http_503"
    assert "3" in unpack_known(state["known"])  # still known: a later clean sweep can delete it
    fake.status_for.clear()
    rows = sync("prospects", fake, state)
    assert [r["source_id"] for r in rows if r.get("_deleted")] == ["3"]


def test_forbidden_sweep_does_not_wipe_the_graph(fake, sync):
    state = {}
    sync("prospects", fake, state)
    fake.status_for[("/prospects", "sweep")] = 403
    rows = sync("prospects", fake, state)
    assert not [r for r in rows if r.get("_deleted")]
    assert state["last_run"]["sweep"] == "incomplete:http_403"
    assert len(unpack_known(state["known"])) == 5


def test_empty_sweep_with_known_records_is_not_trusted(fake, sync):
    state = {}
    sync("prospects", fake, state)
    fake.data["/prospects"] = []
    rows = sync("prospects", fake, state)
    assert not [r for r in rows if r.get("_deleted")]
    assert state["last_run"]["sweep"] == "incomplete:empty_response"


def test_repeated_page_makes_sweep_incomplete_and_data_pass_fail(fake, sync):
    state = {}
    sync("prospects", fake, state)
    fake.repeat_page = True
    with pytest.raises(OutreachError):
        sync("prospects", fake, state)  # strict data pass: pagination loop
    assert state["runs"] == 1


def test_max_delete_ratio_aborts_and_force_overrides(fake, sync, make_source, pipeline_factory):
    state = {}
    sync("prospects", fake, state)
    before = copy.deepcopy(state)
    fake.data["/prospects"] = fake.data["/prospects"][:1]  # 4 of 5 vanish: 80% > 50%
    with pytest.raises(OutreachDeleteGuardError, match="force_delete"):
        sync("prospects", fake, state)
    assert state == before  # nothing committed

    rows = sync("prospects", fake, state, force_delete=True)
    assert len([r for r in rows if r.get("_deleted")]) == 4
    assert state["last_run"]["tombstoned"] == 4


def test_guard_abort_through_a_pipeline_loads_nothing(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, resources=["prospects"]), **RUN)
    fake.data["/prospects"] = fake.data["/prospects"][:1]
    fail_with(
        OutreachDeleteGuardError,
        lambda: pipeline_factory().run(make_source(fake, resources=["prospects"]), **RUN),
    )
    assert ids(pipeline_factory(), PROS) == PROSPECT_IDS
    pipeline_factory().run(make_source(fake, resources=["prospects"], force_delete=True), **RUN)
    assert ids(pipeline_factory(), PROS) == {"outreach:prospect:1"}


def test_activity_outside_window_is_not_swept(fake, sync):
    """A known activity that ages out of ``activity_since`` must not be tombstoned."""
    fake.data["/mailings"] = [
        resource("mailing", 1, "2026-05-30T00:00:00.000Z"),
        resource("mailing", 2, "2026-05-31T00:00:00.000Z"),
    ]
    state = {}
    sync("mailings", fake, state, activity_since=timedelta(days=10))
    assert set(unpack_known(state["known"])) == {"1", "2"}

    later = NOW + timedelta(days=9)  # mailing 1 is now outside the 10-day window, 2 is not
    fake.remove("/mailings", 1)  # deleted upstream AFTER leaving the window: undetectable
    rows = sync("mailings", fake, state, activity_since=timedelta(days=10), now=later)
    assert not [r for r in rows if r.get("_deleted")]
    assert set(unpack_known(state["known"])) == {"2"}  # pruned; state does not grow unbounded

    fake.remove("/mailings", 2)  # still in the window: detected
    rows = sync("mailings", fake, state, activity_since=timedelta(days=10), now=later)
    # sole record vanished -> empty sweep distrusted (see test_empty_sweep...)
    assert state["last_run"]["sweep"] == "incomplete:empty_response"


def test_state_stays_compact(fake, sync):
    fake.data["/prospects"] = [
        resource("prospect", 100000 + i, "2026-03-01T00:00:00.000Z") for i in range(3000)
    ]
    state = {}
    sync("prospects", fake, state, page_size=1000)
    assert len(state["known"]) < 3000 * 8  # well under the naive ~12 bytes/id JSON list


# ---------------------------------------------------------------------------
# 5. Write disposition / full reconcile
# ---------------------------------------------------------------------------


def test_replace_disposition_falls_back_to_full_snapshot(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    run = {"write_disposition": "replace"}  # cognee's default when the caller says nothing
    pipeline.run(make_source(fake, resources=["prospects"]), **run)
    fake.remove("/prospects", 3)
    fake.calls.clear()
    pipeline.run(make_source(fake, resources=["prospects"]), **run)

    # No updatedAt filter and nothing wiped: every live record is re-sent each run, and the
    # deleted one simply drops out of staging (cognee's orphan_cleanup then forgets it).
    data = [c for c in fake.list_calls("/prospects") if "sort" in c]
    assert all("filter[updatedAt]" not in c for c in data)
    assert ids(pipeline, PROS) == PROSPECT_IDS - {"outreach:prospect:3"}
    assert res_state(pipeline, PROS)["last_run"]["mode"] == "snapshot"


def test_full_reconcile_under_merge_resends_everything_and_deletes(fake, sync):
    state = {}
    sync("prospects", fake, state)
    fake.remove("/prospects", 2)
    rows = sync("prospects", fake, state, full_reconcile=True)
    assert len([r for r in rows if not r.get("_deleted")]) == 4
    assert [r["source_id"] for r in rows if r.get("_deleted")] == ["2"]
    assert state["last_run"]["mode"] == "snapshot"


def test_partial_snapshot_aborts_instead_of_forgetting(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    run = {"write_disposition": "replace"}
    pipeline.run(make_source(fake, resources=["prospects"]), **run)
    fake.fail_from_call["/prospects"] = (fake.count("/prospects") + 3, 503)
    with pytest.raises(Exception):  # noqa: B017
        pipeline_factory().run(make_source(fake, resources=["prospects"]), **run)
    assert ids(pipeline_factory(), PROS) == PROSPECT_IDS


# ---------------------------------------------------------------------------
# 6. Privacy: allowlist, contact details, bodies, logs
# ---------------------------------------------------------------------------


def _content(pipeline, table):
    return "\n".join(r[0] for r in read_table(pipeline, table, "content"))


def test_defaults_mask_contact_details_and_bodies(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake), **RUN)
    prospects, mail = _content(pipeline, PROS), _content(pipeline, "outreach_mailings")
    for secret in ("example.test", "+1555000", "Fake St", "Faketown"):
        assert secret not in prospects
    assert "SUBJECT" not in mail and "BODY" not in mail
    assert "NOTE" not in _content(pipeline, "outreach_calls")
    assert "TASKNOTE" not in _content(pipeline, "outreach_tasks")
    assert "TITLE" not in _content(pipeline, "outreach_meetings")
    assert "+1555999" not in _content(pipeline, "outreach_calls")
    # and they were not even requested from the API
    asked = " ".join(c.get("fields[prospect]", "") for c in fake.list_calls("/prospects"))
    assert "emails" not in asked and "addressStreet" not in asked


def test_opt_ins_work(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(
        make_source(fake, include_contact_details=True, include_activity_bodies=True), **RUN
    )
    assert "pat1@example.test" in _content(pipeline, PROS)
    assert "1 Fake St" in _content(pipeline, PROS)
    assert "SUBJECT1" in _content(pipeline, "outreach_mailings")
    assert "BODY1" in _content(pipeline, "outreach_mailings")
    assert "<b>" not in _content(pipeline, "outreach_mailings")  # html body is never allowlisted
    assert "NOTE1" in _content(pipeline, "outreach_calls") and "+1555999" in _content(
        pipeline, "outreach_calls"
    )
    assert "TITLE1" in _content(pipeline, "outreach_meetings")


def test_allowlist_drops_unknown_attributes(fake, make_source, pipeline_factory):
    pipeline = pipeline_factory()
    pipeline.run(
        make_source(fake, include_contact_details=True, include_activity_bodies=True), **RUN
    )
    everything = " ".join(
        _content(pipeline, t) for t in (SEQ, PROS, STATES, "outreach_mailings", "outreach_calls")
    )
    assert "SECRET-UNLISTED" not in everything and "000-00-0000" not in everything
    row = normalise(STREAMS["prospects"], fake.data["/prospects"][0], cfg())
    assert set(row) >= {"id", "title", "content", "url", "updated_at", "owner_id", "_deleted"}
    assert "ssn" not in row["content"].lower() and "emails" not in row["content"].lower()


def test_no_content_or_secrets_in_logs(fake, make_source, pipeline_factory, caplog):
    fake.put("/prospects", {"id": "77", "attributes": "not-a-dict"})  # logged as a skip
    caplog.set_level(logging.DEBUG)
    pipeline_factory().run(
        make_source(fake, include_contact_details=True, include_activity_bodies=True), **RUN
    )
    text = caplog.text
    for secret in ("Pat1", "example.test", "SUBJECT", "NOTE", "at-0", "rt-0", "fake-client-secret"):
        assert secret not in text
    assert "skipped record 77 (invalid_attributes)" in text


# ---------------------------------------------------------------------------
# 7. Resilience: Retry-After, backoff, bad records, startup validation
# ---------------------------------------------------------------------------


def test_429_retry_after_is_honoured_and_5xx_retried(fake, sync, monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    fake.scripted["/sequences"] = [(429, {"Retry-After": "7"}), (503, {})]
    state = {}
    rows = sync("sequences", fake, state, page_size=10)
    assert {r["source_id"] for r in rows} == {"1", "2", "3"}
    assert any(s >= 7 for s in sleeps)  # the server-provided delay was waited out
    assert fake.count("/sequences") == 3


def test_persistent_5xx_in_data_pass_fails_and_leaves_state(fake, sync):
    state = {}
    sync("prospects", fake, state)
    before = copy.deepcopy(state)
    fake.touch("/prospects", 1, "2026-04-01T00:00:00.000Z")
    fake.status_for[("/prospects", "data")] = 500
    with pytest.raises(OutreachError):
        sync("prospects", fake, state)
    assert state == before


def test_bad_record_is_skipped_with_reason_and_sync_continues(fake, make_source, pipeline_factory):
    fake.data["/prospects"].insert(1, {"id": "bad", "attributes": {"updatedAt": "garbage"}})
    fake.data["/prospects"].insert(2, {"attributes": {}})  # no id
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, resources=["prospects"], skipped_table=True), **RUN)

    assert ids(pipeline, PROS) == PROSPECT_IDS
    skipped = read_table(pipeline, "outreach_skipped", "record_type, source_id, reason, content")
    reasons = {(r[0], r[1], r[2]) for r in skipped}
    assert ("prospects", "bad", "invalid_timestamp:updated_at") in reasons
    assert ("prospects", None, "missing_id") in reasons
    assert res_state(pipeline, PROS)["last_run"]["skipped"] == 2
    assert all("garbage" not in r[3] for r in skipped)  # reasons only, never content


def test_skipped_table_is_off_by_default(fake, make_source, pipeline_factory):
    fake.data["/prospects"].insert(0, {"attributes": {}})
    pipeline = pipeline_factory()
    pipeline.run(make_source(fake, resources=["prospects"]), **RUN)
    assert "outreach_skipped" not in pipeline.default_schema.tables


def test_skipped_known_record_is_not_tombstoned(fake, sync):
    state = {}
    sync("prospects", fake, state)
    fake.data["/prospects"][0]["attributes"]["updatedAt"] = "garbage"  # exists, now malformed
    rows = sync("prospects", fake, state)
    assert not [r for r in rows if r.get("_deleted")]
    assert "1" in unpack_known(state["known"])


@pytest.mark.parametrize(("status", "needle"), [(401, "401"), (403, "scope")])
def test_startup_validation_fails_fast_with_a_clear_message(
    fake, make_source, pipeline_factory, status, needle
):
    fake.status_for[("/sequences", "any")] = status
    err = fail_with(OutreachAuthError, lambda: pipeline_factory().run(make_source(fake), **RUN))
    assert needle in str(err) and "outreach-auth" in str(err)
    # one cheap call (plus the single post-refresh retry on 401), before any sync request
    assert fake.count("/sequences") == (2 if status == 401 else 1)
    assert all("sort" not in c for c in fake.list_calls("/sequences"))
    assert "sequences.read" in str(err) or status == 401


def test_missing_credentials_are_reported(monkeypatch, tmp_path):
    monkeypatch.delenv("OUTREACH_CLIENT_ID", raising=False)
    monkeypatch.delenv("OUTREACH_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(mod, "_dlt_secret", lambda key: None)
    with pytest.raises(OutreachAuthError, match="OUTREACH_CLIENT_ID"):
        mod.build_auth(token_store=tmp_path / "t.json")


def test_now_iso_helper_is_utc():
    assert now_iso().endswith("Z")


def test_paths_table_matches_streams():
    assert {s.path for s in STREAMS.values()} == set(PATHS.values())


def test_cross_host_next_link_is_never_followed(fake, sync):
    """A hostile ``links.next`` must not receive the bearer token."""
    real = fake._list

    def evil(request, path, params, parsed):
        response = real(request, path, params, parsed)
        body = json.loads(response.raw.getvalue())
        body["links"] = {"next": "https://evil.example.test/steal"}
        response.raw = io.BytesIO(json.dumps(body).encode())
        return response

    fake._list = evil
    with pytest.raises(OutreachError, match="pagination"):
        sync("prospects", fake, {})
    assert all("evil" not in str(c) for c in fake.calls)
