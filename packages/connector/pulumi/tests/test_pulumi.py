"""Unit and dlt-pipeline tests for the Pulumi Cloud connector. No live account needed.

* Client tests run ``PulumiClient`` against ``httpx.MockTransport``.
* Rendering tests cover stack and update documents and the no-secrets rule.
* Sync tests drive ``_iter_rows`` with a fake API and a plain dict as state, and
  ``pulumi_source`` runs through a dlt pipeline into a temp sqlite destination.
"""

import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cognee.tasks.ingestion import dlt_utils
from conftest import NOW, ORG, SECRET_CONFIG, SECRET_ENV, FakePulumi

from cognee_community_connector_pulumi import pulumi as pulumi_module
from cognee_community_connector_pulumi.pulumi import (
    PulumiAPIError,
    PulumiAuthError,
    PulumiClient,
    PulumiRateLimitedError,
    _iter_rows,
    _PulumiConfig,
    pulumi_source,
    render_stack,
    render_update,
)

NOW_DT = datetime.fromtimestamp(NOW, tz=timezone.utc)
CONSOLE = "https://app.pulumi.com"


def _run(pulumi, state, stats=None, now=NOW_DT, check_active=None, **overrides):
    stats = stats if stats is not None else {}
    config = _PulumiConfig(organization=ORG, **overrides)
    return list(_iter_rows(pulumi, config, state, stats, check_active, now=now))


def _ids(rows):
    return sorted(row["id"] for row in rows)


def _live(rows):
    return sorted(row["id"] for row in rows if not row.get("_deleted"))


def _gone(rows):
    return sorted(row["id"] for row in rows if row.get("_deleted"))


def _history_calls(pulumi):
    return [p for p in pulumi.paths("/api/stacks/") if p.endswith("/updates")]


# ---------------------------------------------------------------------------
# PulumiClient over httpx.MockTransport
# ---------------------------------------------------------------------------
def _client(responses, **kwargs):
    queue = list(responses)
    requests, sleeps = [], []

    def handler(request):
        requests.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = PulumiClient(
        "pul-token-1",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        **kwargs,
    )
    return client, requests, sleeps


def test_client_sends_the_token_and_the_documented_media_type():
    client, requests, _ = _client([httpx.Response(200, json={"stacks": []})])
    assert client.get("/api/user/stacks", {"organization": ORG}) == {"stacks": []}
    request = requests[0]
    assert request.headers["Authorization"] == "token pul-token-1"
    assert request.headers["Accept"] == "application/vnd.pulumi+8"
    assert request.url.host == "api.pulumi.com" and "pul-token-1" not in str(request.url)


def test_client_honours_retry_after_and_retries_5xx():
    client, requests, sleeps = _client(
        [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    assert client.get("/x") == {"ok": True}
    assert len(requests) == 3 and sum(sleeps) == 3 + 2


def test_client_stops_on_a_long_retry_after_or_endless_429():
    client, _, sleeps = _client([httpx.Response(429, headers={"Retry-After": "120"})])
    with pytest.raises(PulumiRateLimitedError):
        client.get("/x")
    assert sleeps == []
    client, requests, _ = _client([httpx.Response(429)])
    with pytest.raises(PulumiRateLimitedError):
        client.get("/x")
    assert len(requests) == 5


def test_client_wait_budget_stops_retries_until_reset():
    client, _, _ = _client([httpx.Response(503)], max_sleep=4)
    with pytest.raises(PulumiRateLimitedError, match="wait budget"):
        client.get("/x")
    client.reset_sleep_budget()
    with pytest.raises(PulumiRateLimitedError, match="wait budget"):
        client.get("/x")


def test_client_errors_carry_the_status_only():
    client, _, _ = _client([httpx.Response(404, text="secret body pul-xyz")])
    with pytest.raises(PulumiAPIError) as info:
        client.get("/x")
    assert info.value.status == 404 and "secret body" not in str(info.value)
    client, _, _ = _client([httpx.Response(401)])
    with pytest.raises(PulumiAuthError):
        client.get("/x")


def test_client_rejects_bad_tokens_without_echoing_them():
    for bad in ("", "  ", "has space", "bad\ntoken", "nön-ascii"):
        with pytest.raises(ValueError) as info:
            PulumiClient(bad)
        assert bad.strip() not in str(info.value) or not bad.strip()


def test_client_waits_in_short_steps_asking_check_active_between():
    checks = []

    def revoke_after_three_checks():
        checks.append(True)
        if len(checks) > 3:
            raise PermissionError("access revoked")

    client, requests, sleeps = _client(
        [httpx.Response(429, headers={"Retry-After": "30"})], check_active=revoke_after_three_checks
    )
    with pytest.raises(PermissionError):
        client.get("/x")
    # A 30 s wait is stopped within its first seconds, not after all of it.
    assert len(requests) == 1 and sleeps == [1.0, 1.0, 1.0]


def test_client_closes_only_the_http_client_it_made():
    def transport():
        return httpx.MockTransport(lambda request: httpx.Response(200, json={}))

    injected = httpx.Client(transport=transport())
    PulumiClient("pul-1", http=injected).close()
    assert not injected.is_closed

    owned = PulumiClient("pul-1")
    owned._http = made = httpx.Client(transport=transport())
    owned.close()
    assert made.is_closed and owned._http is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_stack_lists_names_resources_and_sorted_tags(pulumi):
    stack_id = pulumi.add_stack(
        "payments", "prod", tags={"team": "core", "owner": "sam"}, resources=4
    )
    row = render_stack(pulumi.stacks[stack_id], pulumi.tags[stack_id], CONSOLE)
    assert row["id"] == f"stack:{stack_id}"
    assert row["title"] == "Pulumi stack acme/payments/prod"
    assert row["url"] == "https://app.pulumi.com/acme/payments/prod"
    assert row["content"] == (
        "Stack: acme/payments/prod\nOrganization: acme\nProject: payments\nResources: 4\n\n"
        "Tags:\n- owner: sam\n- pulumi:project: payments\n- team: core"
    )


def test_render_update_has_kind_result_changes_who_and_commit(pulumi):
    stack_id = pulumi.add_stack("payments", "prod")
    update = pulumi.run(
        stack_id,
        "update",
        "failed",
        message="Roll out the new queue",
        changes={"create": 2, "update": 1, "same": 0},
        resources=7,
        commit={"sha": "0123456789abcdef", "slug": "acme/infra", "message": "Add queue\n\nbody"},
    )
    row = render_update(update, pulumi.stacks[stack_id], CONSOLE)
    assert row["id"] == f"update:{stack_id}:1"
    assert row["title"] == "update failed on acme/payments/prod (version 1)"
    assert row["url"] == "https://app.pulumi.com/acme/payments/prod/updates/1"
    content = row["content"]
    for expected in (
        "Kind: update",
        "Result: failed",
        "Version: 1",
        "Resource changes: create 2, update 1",
        "Resources after: 7",
        "Requested by: Priya Shah (priya)",
        "Commit: acme/infra@0123456789ab",
        "Commit message: Add queue",
        "Roll out the new queue",
    ):
        assert expected in content
    assert "same" not in content


def test_config_environment_and_tokens_never_reach_a_document(pulumi):
    stack_id = pulumi.add_stack("payments", "prod")
    pulumi.run(stack_id, message="deploy")
    rows = _run(pulumi, {})
    text = json.dumps(rows)
    for secret in (SECRET_CONFIG, SECRET_ENV, "pul-requested-by-token-secret"):
        assert secret not in text
    assert "db:password" not in text and "AWS_SECRET_ACCESS_KEY" not in text


def test_secrets_in_messages_are_scrubbed(pulumi):
    stack_id = pulumi.add_stack("payments", "prod")
    pulumi.run(stack_id, message='rotate password = "correct horse battery staple"')
    stats = {}
    rows = _run(pulumi, {}, stats=stats)
    text = json.dumps(rows)
    assert "horse battery" not in text and stats["redacted"] == 1


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def test_first_sync_emits_stacks_and_finished_updates_in_the_window(pulumi):
    a = pulumi.add_stack("payments", "prod")
    b = pulumi.add_stack("web", "dev")
    pulumi.run(a, start=NOW - 200 * 86400)  # older than since_days
    pulumi.run(a, message="recent")
    pulumi.run(b, "refresh")
    rows = _run(pulumi, {})
    assert _ids(rows) == sorted([f"stack:{a}", f"stack:{b}", f"update:{a}:2", f"update:{b}:1"])


def test_no_change_reads_only_the_listing(pulumi):
    a = pulumi.add_stack("payments", "prod")
    pulumi.run(a)
    state = {}
    _run(pulumi, state)
    pulumi.calls.clear()
    stats = {}
    assert _run(pulumi, state, stats=stats) == []
    assert pulumi.paths() == ["/api/user/stacks"] and stats["unchanged"] == 1


def test_a_new_update_reads_only_newer_versions(pulumi):
    a = pulumi.add_stack("payments", "prod")
    pulumi.run(a)
    state = {}
    _run(pulumi, state)
    pulumi.run(a, "refresh", message="nightly refresh")
    rows = _run(pulumi, state)
    assert _ids(rows) == [f"update:{a}:2"]
    assert state["stacks"][a]["cursor"] == 2 and state["stacks"][a]["versions"] == [1, 2]


def test_history_pages_until_the_cursor(pulumi, monkeypatch):
    monkeypatch.setattr(pulumi_module, "_PAGE_SIZE", 3)
    a = pulumi.add_stack("payments", "prod")
    for _ in range(7):
        pulumi.run(a)
    state = {}
    assert len(_run(pulumi, state)) == 8
    assert len(_history_calls(pulumi)) == 3
    pulumi.calls.clear()
    pulumi.run(a)
    assert _ids(_run(pulumi, state)) == [f"update:{a}:8"]
    assert len(_history_calls(pulumi)) == 1


def test_a_running_update_is_read_again_until_it_finishes(pulumi):
    a = pulumi.add_stack("payments", "prod")
    # Without a checkpoint lastUpdate never moves, so only the pending list finds it again.
    pulumi.run(a, result="in-progress", checkpoint=False)
    state, stats = {}, {}
    rows = _run(pulumi, state, stats=stats)
    assert _ids(rows) == [f"stack:{a}"] and stats["pending"] == 1
    assert state["stacks"][a]["pending"] == [1]

    pulumi.finish(a, 1, "failed", checkpoint=False)
    pulumi.calls.clear()
    rows = _run(pulumi, state)
    assert _ids(rows) == [f"update:{a}:1"] and "Result: failed" in rows[0]["content"]
    assert f"/api/stacks/{ORG}/payments/prod/updates/1" in pulumi.paths()
    assert state["stacks"][a]["pending"] == []
    pulumi.calls.clear()
    assert _run(pulumi, state) == [] and pulumi.paths() == ["/api/user/stacks"]


def test_a_pending_update_that_vanished_is_dropped(pulumi):
    a = pulumi.add_stack("payments", "prod")
    pulumi.run(a, result="in-progress")
    state = {}
    _run(pulumi, state)
    pulumi.updates[a].clear()
    assert _run(pulumi, state) == []
    assert state["stacks"][a]["pending"] == []


def test_a_removed_stack_is_forgotten_with_its_updates(pulumi):
    a = pulumi.add_stack("payments", "prod")
    b = pulumi.add_stack("web", "dev")
    pulumi.run(a)
    pulumi.run(a)
    pulumi.run(b)
    state = {}
    _run(pulumi, state)
    pulumi.remove(a)
    stats = {}
    rows = _run(pulumi, state, stats=stats)
    assert _gone(rows) == sorted([f"stack:{a}", f"update:{a}:1", f"update:{a}:2"])
    assert stats["deleted"] == 3 and a not in state["stacks"] and b in state["stacks"]


def test_an_empty_listing_waits_for_confirmation(pulumi):
    a = pulumi.add_stack("payments", "prod")
    pulumi.run(a)
    state = {}
    _run(pulumi, state)
    pulumi.remove(a)
    assert _run(pulumi, state) == [] and a in state["stacks"]
    assert _gone(_run(pulumi, state)) == [f"stack:{a}", f"update:{a}:1"]


def test_a_listing_without_its_list_raises_and_forgets_nothing(pulumi):
    a = pulumi.add_stack("payments", "prod")
    state = {}
    _run(pulumi, state)
    real_get = pulumi.get
    pulumi.get = lambda path, params=None: (
        {} if path == "/api/user/stacks" else real_get(path, params)
    )
    with pytest.raises(PulumiAPIError, match="stack list"):
        _run(pulumi, state)
    assert a in state["stacks"]


def test_a_listed_stack_without_its_names_raises(pulumi):
    a = pulumi.add_stack("payments", "prod")
    state = {}
    _run(pulumi, state)
    real_get = pulumi.get

    def broken(path, params=None):
        body = real_get(path, params)
        if path == "/api/user/stacks":
            body["stacks"].append({"id": "x", "orgName": ORG, "projectName": "p"})
        return body

    pulumi.get = broken
    with pytest.raises(PulumiAPIError, match="without its names"):
        _run(pulumi, state)
    assert a in state["stacks"]


def test_the_listing_follows_continuation_tokens(pulumi):
    for i in range(5):
        pulumi.add_stack("p", f"s{i}")
    rows = _run(pulumi, {})
    assert len(_live(rows)) == 5
    assert pulumi.paths().count("/api/user/stacks") == 3


def test_a_listing_that_repeats_its_token_raises():
    class Stuck:
        def get(self, path, params=None):
            return {"stacks": [], "continuationToken": "same"}

    with pytest.raises(PulumiAPIError, match="did not advance"):
        _run(Stuck(), {})


def test_projects_and_tag_filters_reach_the_listing(pulumi):
    a = pulumi.add_stack("payments", "prod", tags={"env": "prod"})
    b = pulumi.add_stack("web", "prod", tags={"env": "prod"})
    pulumi.add_stack("web", "dev", tags={"env": "dev"})
    pulumi.add_stack("other", "prod", tags={"env": "prod"})
    rows = _run(pulumi, {}, projects=("payments", "web"), tag_name="env", tag_value="prod")
    assert _live(rows) == sorted([f"stack:{a}", f"stack:{b}"])
    listing = [p for path, p in pulumi.calls if path == "/api/user/stacks"]
    assert {p["project"] for p in listing} == {"payments", "web"}
    assert all(p["tagName"] == "env" and p["tagValue"] == "prod" for p in listing)


def test_stacks_of_another_organization_are_ignored(pulumi):
    a = pulumi.add_stack("payments", "prod")
    pulumi.add_stack("payments", "prod", org="other")
    real_get = pulumi.get
    pulumi.get = lambda path, params=None: real_get(
        path, {k: v for k, v in (params or {}).items() if k != "organization"}
    )
    assert _live(_run(pulumi, {})) == [f"stack:{a}"]


def test_a_stack_that_answers_404_after_the_listing_is_kept(pulumi):
    a = pulumi.add_stack("payments", "prod")
    pulumi.run(a)
    state = {}
    _run(pulumi, state)
    pulumi.run(a)
    pulumi.errors[f"/api/stacks/{ORG}/payments/prod"] = 404
    stats = {}
    assert _run(pulumi, state, stats=stats) == []
    assert stats["unreadable"] == 1 and state["stacks"][a]["versions"] == [1]


def test_tags_changed_without_an_update_show_up_within_a_day(pulumi):
    a = pulumi.add_stack("payments", "prod")
    state = {}
    _run(pulumi, state)
    pulumi.tags[a]["owner"] = "sam"
    assert _run(pulumi, state, now=NOW_DT + timedelta(hours=1)) == []
    rows = _run(pulumi, state, now=NOW_DT + timedelta(days=1))
    assert _ids(rows) == [f"stack:{a}"] and "owner: sam" in rows[0]["content"]


def test_an_update_without_a_checkpoint_shows_up_within_a_day(pulumi):
    a = pulumi.add_stack("payments", "prod")
    pulumi.run(a)
    state = {}
    _run(pulumi, state)
    pulumi.run(a, checkpoint=False)  # lastUpdate stays where it was
    assert _run(pulumi, state, now=NOW_DT + timedelta(hours=1)) == []
    rows = _run(pulumi, state, now=NOW_DT + timedelta(days=1))
    assert _ids(rows) == [f"update:{a}:2"]
    pages = [params["page"] for path, params in pulumi.calls if path.endswith("/updates")]
    assert pages and set(pages) == {1}, "the daily check reads only the newest page"


def test_a_renamed_stack_renders_its_updates_again(pulumi):
    a = pulumi.add_stack("payments", "prod")
    pulumi.run(a)
    state = {}
    _run(pulumi, state)
    pulumi.stacks[a]["stackName"] = "production"
    rows = _run(pulumi, state)
    assert _ids(rows) == [f"stack:{a}", f"update:{a}:1"]
    assert all("acme/payments/production" in r["content"] for r in rows)


def test_full_resync_renders_every_update_again(pulumi):
    a = pulumi.add_stack("payments", "prod")
    pulumi.run(a)
    state = {}
    _run(pulumi, state)
    assert _ids(_run(pulumi, state, full_resync=True)) == [f"update:{a}:1"]


def test_budget_stops_after_a_stack_and_resumes_with_failed_set(pulumi):
    stacks = [pulumi.add_stack("p", f"s{i}") for i in range(3)]
    for stack_id in stacks:
        pulumi.run(stack_id)
    state = {}
    seen, runs = set(), []
    for _ in range(6):
        stats = {}
        seen.update(_live(_run(pulumi, state, stats=stats, max_requests=4)))
        runs.append(stats["failed_budget"])
        assert stats["failed"] == stats["failed_budget"]
        if not stats["failed_budget"]:
            break
    assert runs[0] == 1 and runs[-1] == 0
    assert seen == {f"stack:{s}" for s in stacks} | {f"update:{s}:1" for s in stacks}
    assert "resume" not in state


def test_a_rate_limit_stops_cleanly_and_keeps_finished_stacks(pulumi):
    a = pulumi.add_stack("p", "a")
    b = pulumi.add_stack("p", "b")
    pulumi.run(a)
    pulumi.run(b)
    pulumi.rate_limited.add("/api/stacks/acme/p/b")
    stats = {}
    state = {}
    rows = _run(pulumi, state, stats=stats)
    assert _ids(rows) == [f"stack:{a}", f"update:{a}:1"]
    assert stats["failed"] == 1 and stats["failed_rate_limit"] == 1
    assert a in state["stacks"] and b not in state["stacks"]


def test_a_token_revoked_mid_run_stops_cleanly_but_a_bad_token_raises(pulumi):
    a = pulumi.add_stack("p", "a")
    b = pulumi.add_stack("p", "b")
    pulumi.errors["/api/stacks/acme/p/b"] = 401
    stats = {}
    assert _ids(_run(pulumi, {}, stats=stats)) == [f"stack:{a}"]
    assert stats["failed_auth"] == 1
    pulumi.errors.clear()
    pulumi.bad_token = True
    with pytest.raises(PulumiAuthError):
        _run(pulumi, {})
    assert b


def test_check_active_runs_before_every_request(pulumi):
    pulumi.add_stack("p", "a")
    calls = []

    def check_active():
        calls.append(len(pulumi.calls))
        if len(calls) == 3:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        _run(pulumi, {}, check_active=check_active)
    assert calls == [0, 1, 2] and len(pulumi.calls) == 2


def test_other_errors_raise_and_keep_the_previous_state(pulumi):
    a = pulumi.add_stack("p", "a")
    pulumi.run(a)
    state = {}
    _run(pulumi, state)
    before = copy.deepcopy(state)
    pulumi.run(a)
    pulumi.errors["/updates"] = 500
    with pytest.raises(PulumiAPIError):
        _run(pulumi, state)
    assert state["stacks"] == before["stacks"]


# ---------------------------------------------------------------------------
# pulumi_source factory
# ---------------------------------------------------------------------------
def test_source_declares_document_path_and_scope(pulumi):
    source = pulumi_source(ORG, service=pulumi)
    assert getattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR) == "pulumi"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "pulumi_acme"
    assert source.name == "pulumi_acme"


def test_default_resource_name_follows_the_organization_and_filters(pulumi):
    assert pulumi_source("Acme-Co", service=pulumi).name.startswith("pulumi_acme_co_")
    assert (
        pulumi_source("Acme-Co", service=pulumi).name
        != pulumi_source("acme_co", service=pulumi).name
    )
    plain = pulumi_source(ORG, service=pulumi).name
    filtered = pulumi_source(ORG, service=pulumi, projects=["web"]).name
    tagged = pulumi_source(ORG, service=pulumi, tag_name="env").name
    assert len({plain, filtered, tagged}) == 3
    for org in ("日本", "---", "o" * 80):
        name = pulumi_source(org, service=pulumi).name
        assert name.startswith("pulumi_") and "__" not in name and len(name) <= 64


@pytest.mark.parametrize("name", ["", "Pulumi-Team", "pulumi team", "p" * 65, 7])
def test_invalid_resource_name_is_rejected(pulumi, name):
    with pytest.raises(ValueError, match="resource_name"):
        pulumi_source(ORG, service=pulumi, resource_name=name)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"projects": "web"}, TypeError),
        ({"projects": ["web", " "]}, ValueError),
        ({"tag_value": "prod"}, ValueError),
        ({"since_days": 0}, ValueError),
        ({"max_requests": 0}, ValueError),
    ],
)
def test_settings_are_validated(pulumi, kwargs, error):
    with pytest.raises(error):
        pulumi_source(ORG, service=pulumi, **kwargs)


def test_organization_and_token_are_required_and_never_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PULUMI_ACCESS_TOKEN", "pul-from-env")
    with pytest.raises(ValueError, match="organization"):
        pulumi_source(" ")
    with pytest.raises(ValueError, match="token"):
        pulumi_source(ORG)


def test_client_is_built_with_the_check_active_hook(monkeypatch):
    seen = {}

    def build(token, api_url, **kwargs):
        seen.update(token=token, api_url=api_url, **kwargs)
        return "c"

    monkeypatch.setattr(pulumi_module, "PulumiClient", build)
    pulumi_source(ORG, token="pul-1", api_url="https://pulumi.example.com")
    assert seen == {"token": "pul-1", "api_url": "https://pulumi.example.com", "check_active": None}


# ---------------------------------------------------------------------------
# dlt pipeline: incremental sync + forget-on-delete
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "pulumi.db").as_posix()
    return dlt.pipeline(
        pipeline_name="pulumi_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="pulumi_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _staged(pipeline, table="pulumi_acme"):
    with pipeline.sql_client() as client, client.execute_query(f"SELECT id FROM {table}") as cursor:
        return {row[0] for row in cursor.fetchall()}


def test_pipeline_syncs_incrementally_and_forgets_a_removed_stack(
    dlt_mod, tmp_path, pulumi, monkeypatch
):
    monkeypatch.setattr(pulumi_module, "_utcnow", lambda: NOW_DT)
    a = pulumi.add_stack("payments", "prod")
    b = pulumi.add_stack("web", "dev")
    pulumi.run(a)
    pulumi.run(b)
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(pulumi_source(ORG, service=pulumi))
    assert _staged(pipeline) == {f"stack:{a}", f"update:{a}:1", f"stack:{b}", f"update:{b}:1"}

    # The state lives in dlt, so a fresh source object resumes from it.
    source = pulumi_source(ORG, service=pulumi)
    pipeline.run(source)
    assert source.cognee_sync_stats["emitted"] == 0

    pulumi.remove(b)
    pipeline.run(pulumi_source(ORG, service=pulumi))
    assert _staged(pipeline) == {f"stack:{a}", f"update:{a}:1"}


def test_two_organizations_in_one_dataset_leave_each_other_alone(dlt_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(pulumi_module, "_utcnow", lambda: NOW_DT)
    one, two = FakePulumi(), FakePulumi()
    a = one.add_stack("payments", "prod")
    b = two.add_stack("web", "dev", org="beta")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(pulumi_source(ORG, service=one))
    pipeline.run(pulumi_source("beta", service=two))
    one.remove(a)
    for _ in range(2):  # the second run confirms the empty listing
        pipeline.run(pulumi_source(ORG, service=one))
    pipeline.run(pulumi_source("beta", service=two))
    assert _staged(pipeline, "pulumi_acme") == set()
    assert _staged(pipeline, "pulumi_beta") == {f"stack:{b}"}


def test_owned_client_is_closed_and_stats_carry_every_key(dlt_mod, tmp_path, pulumi, monkeypatch):
    closed = []

    class Owned:
        def __init__(self, token, api_url, **kwargs):
            pass

        def get(self, path, params=None):
            return pulumi.get(path, params)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(pulumi_module, "PulumiClient", Owned)
    pulumi.add_stack("payments", "prod")
    source = pulumi_source(ORG, token="pul-1")
    _pipeline(dlt_mod, tmp_path).run(source)
    assert closed == [True]
    assert set(pulumi_module._STATS) <= set(source.cognee_sync_stats)
    assert source.cognee_sync_stats["failed"] == 0
