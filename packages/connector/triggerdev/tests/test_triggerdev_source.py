"""The sync logic (_iter_rows) over the HTTP-level fake, with state in a plain dict."""

import pytest
from fake_triggerdev import KEY, FakeTriggerDev, ago

from cognee_community_connector_triggerdev import triggerdev_source
from cognee_community_connector_triggerdev.triggerdev import (
    NODE_SET_COLUMN,
    TriggerDevAPIError,
    TriggerDevAuthError,
    TriggerDevClient,
    TriggerDevSourceError,
    _iter_rows,
)


def _environment(monkeypatch) -> FakeTriggerDev:
    fake = FakeTriggerDev()
    fake.add_schedule("sched_1", "nightly-backup", description="Every day at 02:00")
    fake.add_schedule("sched_2", "sync-stripe")
    fake.add_error("error_1", "send-invoice")
    fake.add_run("run_1", "send-invoice")
    fake.add_run("run_ok", "send-invoice", status="COMPLETED")
    fake.add_deployment("deployment_1", "20261028.1")
    fake.install(monkeypatch)
    return fake


def _sync(state: dict, **options) -> tuple[list[dict], dict]:
    stats: dict[str, int] = {}
    client = TriggerDevClient(KEY, sleep=lambda _: None)
    return list(_iter_rows(client, state, stats, **options)), stats


def _ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if not row.get("_deleted")}


def _deleted(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows if row.get("_deleted")}


# -- first sync and quiet re-sync ----------------------------------------------
def test_a_first_sync_renders_every_kind_and_a_quiet_one_sends_nothing(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    rows, stats = _sync(state)

    assert _ids(rows) == {
        "schedule:sched_1",
        "schedule:sched_2",
        "error:error_1",
        "run:run_1",
        "deployment:deployment_1",
    }
    assert stats == {"scanned": 5, "skipped": 0, "deleted": 0, "failed": 0}

    fake.calls.clear()
    again, stats = _sync(state)
    assert again == []
    assert stats["skipped"] == 5
    # an unchanged failed run is not retrieved again
    assert not any(path.startswith("/api/v3/runs/") for path in fake.paths())


def test_documents_are_readable_and_keep_payloads_outputs_and_git_urls_out(monkeypatch):
    _environment(monkeypatch)
    rows = {row["id"]: row for row in _sync({})[0]}

    run = rows["run:run_1"]
    assert run["title"] == "send-invoice run run_1: failed"
    assert "Error: Error: Stripe webhook signature mismatch" in run["content"]
    assert "at handler (src/trigger/send-invoice.ts:42:11)" in run["content"]
    assert "Tags: billing, customer_42" in run["content"]
    assert run[NODE_SET_COLUMN] == ["task:send-invoice"]

    schedule = rows["schedule:sched_1"]
    assert "Cron: 0 * * * *" in schedule["content"]
    assert "Runs: Every day at 02:00" in schedule["content"]

    deployment = rows["deployment:deployment_1"]
    assert "Commit: 8f3c2a1b9d7e" in deployment["content"]
    assert "Commit message: fix: retry Stripe webhooks" in deployment["content"]

    everything = repr(rows)
    for secret in ("4242", "ada@example.com", "do-not-store", "ghs_leak", "x-access-token"):
        assert secret not in everything


def test_volatile_fields_do_not_resend_a_record(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)

    fake.schedules["sched_1"]["nextRun"] = "2026-10-29T02:00:00.000Z"
    fake.errors["error_1"]["count"] = 40
    rows, stats = _sync(state)

    assert rows == []
    assert stats["skipped"] == 5


def test_a_real_change_resends_only_that_record(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)

    fake.errors["error_1"]["status"] = "resolved"
    fake.schedules["sched_2"]["active"] = False
    rows, _ = _sync(state)

    assert _ids(rows) == {"error:error_1", "schedule:sched_2"}
    error = next(row for row in rows if row["id"] == "error:error_1")
    assert "Status: resolved" in error["content"]


def test_rendering_does_not_depend_on_tag_order(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)

    fake.runs["run_1"]["tags"] = ["billing", "customer_42", "billing"]
    assert _sync(state)[0] == []


# -- schedules: forget only what is confirmed gone -------------------------------
def test_a_deleted_schedule_is_forgotten_after_a_404(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)

    del fake.schedules["sched_2"]
    rows, stats = _sync(state)

    assert _deleted(rows) == {"schedule:sched_2"}
    assert stats["deleted"] == 1
    assert "/api/v1/schedules/sched_2" in fake.paths()
    assert "schedule:sched_2" not in state["fingerprints"]


def test_a_schedule_a_listing_left_out_but_that_still_exists_is_kept(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)

    fake.unlisted.add("sched_2")
    rows, _ = _sync(state)

    assert _deleted(rows) == set()
    assert "schedule:sched_2" in state["fingerprints"]


def test_a_failed_deletion_check_keeps_the_schedule_and_counts_it(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)

    del fake.schedules["sched_2"]
    fake.fail["/api/v1/schedules/sched_2"] = 500
    rows, stats = _sync(state)

    assert _deleted(rows) == set()
    assert stats["failed"] == 1

    del fake.fail["/api/v1/schedules/sched_2"]
    assert _deleted(_sync(state)[0]) == {"schedule:sched_2"}


@pytest.mark.parametrize("status", [500, 401, 403])
def test_a_failed_schedule_listing_forgets_nothing_and_fails_the_run(monkeypatch, status):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)
    before = dict(state["fingerprints"])

    fake.fail["/api/v1/schedules"] = status
    with pytest.raises((TriggerDevAPIError, TriggerDevAuthError, TriggerDevSourceError)):
        _sync(state)

    assert state["fingerprints"] == before


def test_a_schedule_page_without_total_pages_is_not_trusted(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)

    fake.override["/api/v1/schedules"] = {"data": [], "pagination": {}}
    with pytest.raises(TriggerDevAPIError, match="totalPages"):
        _sync(state)


def test_schedules_over_several_pages_are_all_read(monkeypatch):
    fake = FakeTriggerDev(schedule_page_size=2)
    for number in range(5):
        fake.add_schedule(f"sched_{number}", "task")
    fake.install(monkeypatch)

    rows, _ = _sync({}, include=("schedules",))

    assert len(_ids(rows)) == 5
    assert [params["page"] for path, params in fake.calls] == [1, 2, 3]


def test_an_emptied_environment_forgets_each_schedule_after_its_own_check(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)

    fake.schedules.clear()
    rows, _ = _sync(state, include=("schedules",))

    assert _deleted(rows) == {"schedule:sched_1", "schedule:sched_2"}


def test_taking_schedules_out_of_include_forgets_them_but_keeps_history(monkeypatch):
    _environment(monkeypatch)
    state: dict = {}
    _sync(state)

    rows, _ = _sync(state, include=("errors", "runs", "deployments"))

    assert _deleted(rows) == {"schedule:sched_1", "schedule:sched_2"}
    assert "run:run_1" in state["fingerprints"]


# -- history: runs, errors, deployments ------------------------------------------
def test_only_failed_runs_by_default_and_every_run_when_asked(monkeypatch):
    fake = _environment(monkeypatch)
    assert "run:run_ok" not in _ids(_sync({}, include=("runs",))[0])

    fake.calls.clear()
    rows, _ = _sync({}, include=("runs",), run_statuses=None)
    assert {"run:run_1", "run:run_ok"} <= _ids(rows)
    assert "filter[status]" not in dict(fake.calls)["/api/v1/runs"]
    # a successful run is not retrieved, the listing says all there is
    assert "/api/v3/runs/run_ok" not in fake.paths()


def test_a_run_that_cannot_be_retrieved_is_retried_next_run(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    fake.fail["/api/v3/runs/run_1"] = 500
    rows, stats = _sync(state, include=("runs",))
    assert rows == [] and stats["failed"] == 1

    del fake.fail["/api/v3/runs/run_1"]
    rows, _ = _sync(state, include=("runs",))
    assert _ids(rows) == {"run:run_1"}
    assert "Stripe webhook signature mismatch" in rows[0]["content"]


def test_a_run_gone_before_retrieval_keeps_what_the_listing_said(monkeypatch):
    fake = _environment(monkeypatch)
    fake.fail["/api/v3/runs/run_1"] = 404
    rows, stats = _sync({}, include=("runs",))
    assert _ids(rows) == {"run:run_1"}
    assert "Error:" not in rows[0]["content"]
    assert stats["failed"] == 0


def test_runs_follow_the_cursor_across_pages(monkeypatch):
    fake = FakeTriggerDev()
    for number in range(230):
        fake.add_run(f"run_{number:03d}", "task", created_at=ago(minutes=number))
    fake.install(monkeypatch)

    rows, _ = _sync({}, include=("runs",))

    assert len(_ids(rows)) == 230
    listings = [params for path, params in fake.calls if path == "/api/v1/runs"]
    assert len(listings) == 3
    assert "page[after]" not in listings[0]
    afters = [listing["page[after]"] for listing in listings[1:]]
    assert len(set(afters)) == 2 and all(afters)


def test_a_cursor_that_does_not_advance_stops_the_run(monkeypatch):
    fake = _environment(monkeypatch)
    fake.override["/api/v1/errors"] = {"data": [], "pagination": {"next": "error_same"}}
    with pytest.raises(TriggerDevAPIError, match="did not advance"):
        _sync({}, include=("errors",))


def test_history_that_ages_out_stays_in_memory_and_leaves_state(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)

    fake.runs["run_1"]["createdAt"] = ago(days=30)
    rows, _ = _sync(state)

    assert _deleted(rows) == set()
    assert "run:run_1" not in state["fingerprints"]


def test_the_lookback_window_is_sent(monkeypatch):
    fake = _environment(monkeypatch)
    _sync({}, include=("runs",), lookback_days=30)
    since = dict(fake.calls)["/api/v1/runs"]["filter[createdAt][from]"]
    assert since[:10] == ago(days=30)[:10]


# -- environment guard -----------------------------------------------------------
def test_state_of_another_environment_is_refused(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)
    assert state["environment_id"] == "env_prod_1"

    fake.add_run("run_2", "send-invoice", env_id="env_staging_9")
    with pytest.raises(TriggerDevSourceError, match=r"another Trigger\.dev environment"):
        _sync(state)


# -- bounded runs ----------------------------------------------------------------
def test_a_spent_budget_stops_cleanly_and_the_next_run_finishes(monkeypatch):
    _environment(monkeypatch)
    state: dict = {}
    rows, stats = _sync(state, max_requests=2)

    assert stats["failed_budget"] == 1
    assert _ids(rows) == {"schedule:sched_1", "schedule:sched_2", "error:error_1"}

    rows, stats = _sync(state)
    assert _ids(rows) == {"run:run_1", "deployment:deployment_1"}
    assert "failed_budget" not in stats


def test_a_429_stops_cleanly_without_forgetting(monkeypatch):
    fake = _environment(monkeypatch)
    state: dict = {}
    _sync(state)
    del fake.schedules["sched_2"]

    fake.fail["/api/v1/schedules"] = 429
    rows, stats = _sync(state)

    assert rows == []
    assert stats["failed_rate_limit"] == 1
    assert "schedule:sched_2" in state["fingerprints"]


def test_a_low_quota_header_stops_before_the_limit(monkeypatch):
    fake = _environment(monkeypatch)
    fake.extra_headers = {"x-ratelimit-remaining": "3"}
    _, stats = _sync({})
    assert stats["failed_rate_limit"] == 1
    assert len(fake.calls) == 1


def test_a_key_revoked_mid_run_keeps_what_was_sent(monkeypatch):
    fake = _environment(monkeypatch)
    fake.fail["/api/v1/runs"] = 401
    state: dict = {}
    rows, stats = _sync(state)

    assert stats["failed_auth"] == 1
    assert {"schedule:sched_1", "error:error_1"} <= _ids(rows)


def test_a_bad_key_fails_the_first_request_loudly(monkeypatch):
    fake = _environment(monkeypatch)
    fake.fail["/api/v1/schedules"] = 401
    with pytest.raises(TriggerDevAuthError):
        _sync({})


def test_check_active_runs_before_every_request(monkeypatch):
    fake = _environment(monkeypatch)
    checks = []
    _sync({}, check_active=lambda: checks.append(len(fake.calls)))
    assert len(checks) == len(fake.calls)

    def revoked():
        raise PermissionError("access revoked")

    with pytest.raises(PermissionError):
        _sync({}, check_active=revoked)


# -- the public factory ------------------------------------------------------------
@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"resource_name": ""}, "resource_name"),
        ({"resource_name": "   "}, "resource_name"),
        ({"resource_name": " prod "}, "resource_name"),
        ({"resource_name": "x" * 65}, "resource_name"),
        ({"include": ()}, "include"),
        ({"include": ("tasks",)}, "include"),
        ({"run_statuses": ()}, "run_statuses"),
        ({"run_statuses": ("BROKEN",)}, "run_statuses"),
        ({"lookback_days": 0}, "lookback_days"),
        ({"lookback_days": 91}, "lookback_days"),
        ({"lookback_days": 7.5}, "lookback_days"),
        ({"max_requests": 0}, "max_requests"),
        ({"api_key": None}, "api_key"),
    ],
)
def test_the_factory_refuses_bad_options(options, message):
    kwargs = {"resource_name": "triggerdev_prod", "api_key": KEY, **options}
    with pytest.raises(ValueError, match=message):
        triggerdev_source(**kwargs)


def test_the_resource_is_tagged_scoped_and_reports_stats():
    from cognee.tasks.ingestion import dlt_utils

    resource = triggerdev_source(resource_name="triggerdev_prod", api_key=KEY)
    assert getattr(resource, dlt_utils.DOCUMENT_SOURCE_ATTR) == "triggerdev"
    assert getattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR) == "triggerdev_prod"
    assert resource.cognee_sync_stats == {}


def test_lowercase_statuses_are_accepted():
    triggerdev_source(resource_name="p", api_key=KEY, run_statuses=("failed", "crashed"))
