"""Rendering and sync tests: ``_iter_rows`` against an in-memory Argo CD. No network, no LLM."""

import copy
import json
import random

import pytest
from fake_argocd import FakeArgoSession, FakeResponse, make_app, make_history, make_resource

from cognee_community_connector_argocd import argocd
from cognee_community_connector_argocd.argocd import _ArgoConfig, _iter_rows

REPO = "https://github.com/argoproj/argocd-example-apps.git"


class RecordingLogger:
    def __init__(self):
        self.lines: list[str] = []

    def _record(self, message, *args):
        self.lines.append(message % args if args else message)

    warning = info = debug = _record


@pytest.fixture(autouse=True)
def log(monkeypatch):
    recorder = RecordingLogger()
    monkeypatch.setattr(argocd, "logger", recorder)
    monkeypatch.setattr(argocd, "_warned", set())
    monkeypatch.setattr(argocd, "_sleep", lambda seconds: None)
    return recorder


def config(**overrides) -> _ArgoConfig:
    values = {
        "base_url": "https://argocd.example.com",
        "instance": "prod",
        "projects": (),
        "app_names": (),
        "app_namespace": None,
        "selector": None,
        "include_resources": True,
        "include_history": True,
        "history_limit": None,
        "include_helm_values": False,
        "verify": True,
        "timeout": 60.0,
    }
    values.update(overrides)
    return _ArgoConfig(**values)


def sync(apps, state=None, stats=None, **overrides):
    state = {} if state is None else state
    rows = list(_iter_rows(FakeArgoSession(apps), config(**overrides), state, stats))
    return {row["id"]: row for row in rows}


HEALTHY = """\
Argo CD application: guestbook
Instance: prod
Namespace: argocd
Project: default
Source: https://github.com/argoproj/argocd-example-apps.git, path guestbook, target revision HEAD
Destination: cluster https://kubernetes.default.svc, namespace default
Sync policy: automated (prune, self-heal)
Sync status: Synced (compared to revision abc1234)
Health: Healthy
Last operation: Sync, Succeeded, revision abc1234, started 2026-10-17T09:00:00Z, finished 2026-10-17T09:00:05Z, message: successfully synced (all tasks run)
Images: gcr.io/example/guestbook:0.2
Labels: team=payments

Application guestbook belongs to project default.
Application guestbook deploys https://github.com/argoproj/argocd-example-apps.git, path guestbook, target revision HEAD.
Application guestbook deploys to cluster https://kubernetes.default.svc, namespace default."""  # noqa: E501

DEGRADED = """\
Argo CD application: web
Instance: prod
Namespace: argocd
Project: default
Source: https://github.com/argoproj/argocd-example-apps.git, path guestbook, target revision HEAD
Destination: cluster https://kubernetes.default.svc, namespace default
Sync policy: automated (prune, self-heal)
Sync status: OutOfSync (compared to revision abc1234)
Health: Degraded - Deployment exceeded its progress deadline
Conditions:
- SyncError: one or more objects failed to apply
Last operation: Sync, Succeeded, revision abc1234, started 2026-10-17T09:00:00Z, finished 2026-10-17T09:00:05Z, message: successfully synced (all tasks run)
Resources: 3 total (ConfigMap 1, Deployment 1, Service 1)
Resources needing attention:
- Deployment default/web: OutOfSync, Degraded - ImagePullBackOff
Images: gcr.io/example/guestbook:0.2
Labels: team=payments

Application web belongs to project default.
Application web deploys https://github.com/argoproj/argocd-example-apps.git, path guestbook, target revision HEAD.
Application web deploys to cluster https://kubernetes.default.svc, namespace default.
Application web manages ConfigMap default/cfg, Deployment default/web, Service default/web."""  # noqa: E501


def degraded_app() -> dict:
    app = make_app(
        "web",
        sync="OutOfSync",
        health="Degraded",
        health_message="Deployment exceeded its progress deadline",
        resources=[
            make_resource("Service", "web"),
            make_resource(
                "Deployment", "web", sync="OutOfSync", health="Degraded", message="ImagePullBackOff"
            ),
            make_resource("ConfigMap", "cfg", health=None),
        ],
    )
    app["status"]["conditions"] = [
        {"type": "SyncError", "message": "one or more objects failed to apply"}
    ]
    return app


# ---------------------------------------------------------------------------
# Application documents
# ---------------------------------------------------------------------------
def test_healthy_application_golden_output():
    rows = sync([make_app("guestbook")])
    row = rows["argocd:prod:argocd/guestbook"]
    assert row["content"] == HEALTHY
    assert row["title"] == "Argo CD application guestbook"
    assert row["_deleted"] is False
    assert "url" not in row


def test_degraded_application_golden_output():
    rows = sync([degraded_app()])
    assert rows["argocd:prod:argocd/web"]["content"] == DEGRADED


def test_input_order_does_not_change_the_content():
    app = degraded_app()
    app["status"]["conditions"].append({"type": "Another", "message": "z"})
    app["status"]["history"] = [make_history(1), make_history(2), make_history(3)]
    shuffled = copy.deepcopy(app)
    random.Random(7).shuffle(shuffled["status"]["resources"])
    shuffled["status"]["conditions"].reverse()
    shuffled["status"]["history"].reverse()
    shuffled["status"]["summary"]["images"] = ["b:1", "a:1"]
    app["status"]["summary"]["images"] = ["a:1", "b:1"]
    assert sync([app]) == sync([shuffled])


def test_volatile_fields_never_reach_the_content():
    first = make_app("guestbook")
    second = make_app(
        "guestbook",
        resource_version="999",
        reconciled_at="2030-01-01T00:00:00Z",
    )
    second["metadata"]["generation"] = 42
    second["metadata"]["annotations"] = {"other": "x"}
    second["status"]["operationState"]["retryCount"] = 9
    rows_a, rows_b = sync([first]), sync([second])
    assert rows_a == rows_b
    content = rows_a["argocd:prod:argocd/guestbook"]["content"]
    for volatile in ("2026-10-17T10:00:00Z", "do-not-render", "resourceVersion", "retry"):
        assert volatile not in content


def test_helm_values_are_left_out_by_default_and_included_on_opt_in():
    app = make_app("web")
    app["spec"]["source"]["helm"] = {
        "parameters": [
            {"name": "image.tag", "value": "v2"},
            {"name": "db.password", "value": "hunter2"},
        ],
        "valueFiles": ["values-prod.yaml"],
        "values": "replicas: 3",
    }
    default = sync([app])["argocd:prod:argocd/web"]["content"]
    assert "hunter2" not in default
    assert "Helm" not in default
    assert "replicas" not in default

    opted_in = sync([app], include_helm_values=True)["argocd:prod:argocd/web"]["content"]
    assert "Helm (source 1):" in opted_in
    assert "- parameters: db.password=hunter2; image.tag=v2" in opted_in  # sorted
    assert "- value files: values-prod.yaml" in opted_in
    assert "- values: replicas: 3" in opted_in


def test_values_object_is_rendered_deterministically():
    one, two = make_app("web"), make_app("web")
    one["spec"]["source"]["helm"] = {"valuesObject": {"b": 1, "a": {"y": 2, "x": 1}}}
    two["spec"]["source"]["helm"] = {"valuesObject": {"a": {"x": 1, "y": 2}, "b": 1}}
    assert sync([one], include_helm_values=True) == sync([two], include_helm_values=True)


def test_credentials_are_stripped_from_repository_urls():
    app = make_app("web", repo="https://deploy:p4ss@git.example.com/org/repo.git?token=abc#frag")
    app["status"]["history"] = [make_history(1)]
    app["status"]["history"][0]["source"]["repoURL"] = (
        "https://deploy:p4ss@git.example.com/org/repo.git"
    )
    rows = sync([app])
    text = "\n".join(row["content"] for row in rows.values())
    assert "p4ss" not in text
    assert "token=abc" not in text
    assert "https://git.example.com/org/repo.git" in text


def test_scp_style_git_urls_are_left_alone():
    app = make_app("web", repo="git@github.com:org/repo.git")
    assert "git@github.com:org/repo.git" in sync([app])["argocd:prod:argocd/web"]["content"]


def test_resources_needing_attention_are_capped_at_twenty():
    resources = [
        make_resource("Pod", f"pod-{i:02d}", health="Degraded", message="crash") for i in range(30)
    ]
    content = sync([make_app("web", resources=resources)])["argocd:prod:argocd/web"]["content"]
    assert "Resources: 30 total (Pod 30)" in content
    assert content.count("- Pod default/pod-") == 20
    assert "- Pod default/pod-19: Synced, Degraded - crash" in content
    assert "pod-20" not in content
    assert "- and 10 more" in content
    assert "manages " in content
    assert "and 10 more." in content  # the lineage list is capped too


def test_include_resources_false_leaves_resource_text_out():
    app = degraded_app()
    content = sync([app], include_resources=False)["argocd:prod:argocd/web"]["content"]
    assert "Resources" not in content
    assert "manages" not in content
    assert "Health: Degraded" in content


def test_long_messages_are_clipped_and_flattened():
    app = make_app("web", health="Degraded", health_message="line one\n\nline two " + "x" * 900)
    health = next(
        line
        for line in sync([app])["argocd:prod:argocd/web"]["content"].splitlines()
        if line.startswith("Health:")
    )
    assert "\n" not in health
    assert health.endswith("...")
    assert len(health) < 600


def test_missing_keys_never_raise():
    rows = sync(
        [
            {"metadata": {"name": "bare"}},
            {"metadata": {"name": "nulls", "namespace": "argocd"}, "spec": None, "status": None},
            {
                "metadata": {"name": "odd", "namespace": "argocd"},
                "status": {"resources": [None, {}]},
            },
        ]
    )
    assert set(rows) == {
        "argocd:prod:default/bare",
        "argocd:prod:argocd/nulls",
        "argocd:prod:argocd/odd",
    }
    assert "Health: Unknown" in rows["argocd:prod:default/bare"]["content"]
    assert "Sync policy: manual" in rows["argocd:prod:default/bare"]["content"]


def test_an_application_without_a_name_is_skipped():
    stats: dict = {}
    rows = sync([{"metadata": {}}, make_app("ok")], stats=stats)
    assert list(rows) == ["argocd:prod:argocd/ok"]
    assert stats["skipped"] == 1


def test_missing_namespace_uses_default_and_warns_once(log):
    sync([make_app("a", namespace=None), make_app("b", namespace=None)])
    assert (
        log.lines.count(
            "Argo CD application without a namespace; using 'default' in its document id."
        )
        == 1
    )


def test_multiple_sources_are_listed_in_order():
    app = make_app("multi")
    del app["spec"]["source"]
    app["spec"]["sources"] = [
        {"repoURL": "https://charts.example.com", "chart": "nginx", "targetRevision": "1.2.3"},
        {"repoURL": REPO, "ref": "values", "targetRevision": "main"},
    ]
    content = sync([app])["argocd:prod:argocd/multi"]["content"]
    assert "Sources:\n- https://charts.example.com, chart nginx, target revision 1.2.3\n" in content
    assert f"- {REPO}, target revision main, ref values" in content
    assert "Application multi deploys https://charts.example.com, chart nginx" in content


def test_same_name_in_two_namespaces_gets_two_ids():
    rows = sync([make_app("web", namespace="team-a"), make_app("web", namespace="team-b")])
    assert set(rows) == {"argocd:prod:team-a/web", "argocd:prod:team-b/web"}


def test_empty_project_means_default_and_other_projects_are_named():
    app = make_app("web", project="")
    assert "Project: default" in sync([app])["argocd:prod:argocd/web"]["content"]
    assert (
        "Project: payments"
        in sync([make_app("web", project="payments")])["argocd:prod:argocd/web"]["content"]
    )


def test_manual_sync_policy():
    app = make_app("web")
    app["spec"]["syncPolicy"] = {"syncOptions": ["CreateNamespace=true", "ApplyOutOfSyncOnly=true"]}
    content = sync([app])["argocd:prod:argocd/web"]["content"]
    assert "Sync policy: manual; options: ApplyOutOfSyncOnly=true, CreateNamespace=true" in content


# ---------------------------------------------------------------------------
# History documents
# ---------------------------------------------------------------------------
def history_app() -> dict:
    return make_app(
        "web", history=[make_history(1), make_history(3), make_history(2, automated=True)]
    )


def test_every_history_entry_is_its_own_document():
    rows = sync([history_app()])
    assert set(rows) == {
        "argocd:prod:argocd/web",
        "argocd:prod:argocd/web:history:1",
        "argocd:prod:argocd/web:history:2",
        "argocd:prod:argocd/web:history:3",
    }
    entry = rows["argocd:prod:argocd/web:history:2"]
    assert entry["title"] == "Argo CD sync 2 of web"
    assert "Initiated by: automated sync" in entry["content"]
    assert "Revision: abc1234" in entry["content"]
    assert "Deployed: 2026-10-12T08:00:09Z" in entry["content"]
    assert entry["_deleted"] is False


def test_history_limit_keeps_the_newest_entries():
    rows = sync([history_app()], history_limit=2)
    assert {i.rsplit(":", 1)[-1] for i in rows if ":history:" in i} == {"3", "2"}


def test_history_can_be_turned_off():
    stats: dict = {}
    rows = sync([history_app()], include_history=False, stats=stats)
    assert list(rows) == ["argocd:prod:argocd/web"]
    assert stats["applications"] == 1
    assert stats["history"] == 0


def test_history_entries_without_an_integer_id_are_skipped():
    app = make_app("web", history=[{"revision": "x"}, {"id": "7"}, make_history(1)])
    rows = sync([app])
    assert [i for i in rows if ":history:" in i] == ["argocd:prod:argocd/web:history:1"]


def test_history_documents_do_not_change_when_the_application_does():
    before = make_app("web", history=[make_history(1)])
    after = make_app(
        "web", history=[make_history(1)], health="Degraded", sync="OutOfSync", project="moved"
    )
    key = "argocd:prod:argocd/web:history:1"
    assert sync([before])[key] == sync([after])[key]


def test_multi_source_history_lists_each_revision():
    entry = make_history(1)
    del entry["revision"], entry["source"]
    entry["revisions"] = ["aaa", "bbb"]
    entry["sources"] = [{"repoURL": REPO, "path": "a"}, {"repoURL": REPO, "path": "b"}]
    content = sync([make_app("web", history=[entry])])["argocd:prod:argocd/web:history:1"][
        "content"
    ]
    assert "Revision: aaa, bbb" in content
    assert "Sources:" in content


# ---------------------------------------------------------------------------
# Skips, empty lists and repeatability
# ---------------------------------------------------------------------------
def test_an_application_being_deleted_is_skipped():
    gone = make_app("gone")
    gone["metadata"]["deletionTimestamp"] = "2026-10-17T11:00:00Z"
    stats: dict = {}
    rows = sync([gone, make_app("stays")], stats=stats)
    assert list(rows) == ["argocd:prod:argocd/stays"]
    assert stats["skipped"] == 1


def test_an_empty_list_yields_nothing_and_warns(log):
    assert sync([]) == {}
    assert any("no applications" in line for line in log.lines)


def test_stats_count_applications_history_and_skips():
    stats: dict = {}
    sync([history_app(), make_app("other"), {"metadata": {}}], stats=stats)
    assert stats == {"applications": 2, "history": 3, "deleted": 0, "unchanged": 0, "skipped": 1}


def test_failures_surface_before_any_row_is_emitted():
    session = FakeArgoSession([make_app("a")])
    session.script = [RuntimeError("boom")]
    iterator = _iter_rows(session, config(), {})
    with pytest.raises(RuntimeError, match="boom"):
        next(iterator)


# ---------------------------------------------------------------------------
# Incremental sync, tombstones and guards
# ---------------------------------------------------------------------------
def step(session, state, **overrides):
    """One sync run. Returns (tombstone ids, upserted rows by id, stats, all rows in order)."""
    stats: dict = {}
    rows = list(_iter_rows(session, config(**overrides), state, stats))
    deleted = [row["id"] for row in rows if row["_deleted"]]
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    return deleted, upserts, stats, rows


def many(count, **kwargs):
    return [make_app(f"app-{i}", **kwargs) for i in range(count)]


def snapshot(state) -> str:
    return json.dumps(state, sort_keys=True)


APP = "argocd:prod:argocd/web"


def counting(monkeypatch) -> list[int]:
    """Count how often an application is rendered."""
    renders: list[int] = []
    original = argocd._application_row
    monkeypatch.setattr(
        argocd, "_application_row", lambda *args: renders.append(1) or original(*args)
    )
    return renders


def test_first_run_emits_everything_and_fills_the_state():
    session = FakeArgoSession([make_app("web", history=[make_history(1), make_history(2)])])
    state: dict = {}
    deleted, upserts, stats, _ = step(session, state)
    assert deleted == []
    assert set(upserts) == {APP, f"{APP}:history:1", f"{APP}:history:2"}
    stored = state["instances"]["prod"]
    assert isinstance(stored["config"], str)
    entry = stored["apps"][APP]
    assert entry["rv"] == "100"
    assert len(entry["hash"]) == 64
    assert entry["history"] == [f"{APP}:history:1", f"{APP}:history:2"]
    assert stats["applications"] == 1
    assert stats["history"] == 2


def test_an_unchanged_second_run_emits_nothing_with_one_list_call():
    session = FakeArgoSession([make_app("web", history=[make_history(1)])])
    state: dict = {}
    step(session, state)
    before = snapshot(state)
    _, _, stats, rows = step(session, state)
    assert rows == []
    assert len(session.calls) == 2  # exactly one list call per run
    assert stats["unchanged"] == 1
    assert snapshot(state) == before


def test_a_new_resource_version_with_the_same_content_only_updates_the_cursor():
    session = FakeArgoSession([make_app("web", resource_version="100")])
    state: dict = {}
    step(session, state)
    session.apps = [make_app("web", resource_version="101", reconciled_at="2030-01-01T00:00:00Z")]
    _, _, stats, rows = step(session, state)
    assert rows == []
    assert stats["unchanged"] == 1
    assert state["instances"]["prod"]["apps"][APP]["rv"] == "101"


def test_an_equal_resource_version_skips_rendering(monkeypatch):
    session = FakeArgoSession(many(3))
    state: dict = {}
    step(session, state)
    renders = counting(monkeypatch)
    step(session, state)
    assert renders == []


def test_a_real_change_emits_exactly_one_application_row():
    session = FakeArgoSession([make_app("web"), make_app("other")])
    state: dict = {}
    step(session, state)
    session.apps = [
        make_app("web", health="Degraded", health_message="oom", resource_version="101"),
        make_app("other"),
    ]
    deleted, upserts, stats, _ = step(session, state)
    assert deleted == []
    assert list(upserts) == [APP]
    assert "Health: Degraded - oom" in upserts[APP]["content"]
    assert stats["applications"] == 1
    assert stats["unchanged"] == 1


def test_a_new_history_entry_emits_exactly_one_history_row():
    session = FakeArgoSession([make_app("web", history=[make_history(1)])])
    state: dict = {}
    step(session, state)
    session.apps = [
        make_app("web", history=[make_history(1), make_history(2)], resource_version="101")
    ]
    _, upserts, stats, _ = step(session, state)
    assert list(upserts) == [f"{APP}:history:2"]  # same application text: not re-emitted
    assert stats["history"] == 1
    assert state["instances"]["prod"]["apps"][APP]["history"] == [
        f"{APP}:history:1",
        f"{APP}:history:2",
    ]
    assert step(session, state)[3] == []


def test_a_removed_application_is_forgotten_with_all_its_history():
    session = FakeArgoSession(
        [make_app("web", history=[make_history(1), make_history(2)]), make_app("other")]
    )
    state: dict = {}
    step(session, state)
    session.apps = [make_app("other")]
    deleted, upserts, stats, _ = step(session, state)
    assert deleted == [APP, f"{APP}:history:1", f"{APP}:history:2"]
    assert upserts == {}
    assert stats["deleted"] == 1
    assert set(state["instances"]["prod"]["apps"]) == {"argocd:prod:argocd/other"}


def test_an_application_being_deleted_is_forgotten_at_once():
    session = FakeArgoSession([make_app("web", history=[make_history(1)]), make_app("other")])
    state: dict = {}
    step(session, state)
    dying = make_app("web", history=[make_history(1)], resource_version="101")
    dying["metadata"]["deletionTimestamp"] = "2026-10-17T11:00:00Z"
    session.apps = [dying, make_app("other")]
    deleted, _, _, _ = step(session, state)
    assert deleted == [APP, f"{APP}:history:1"]
    assert APP not in state["instances"]["prod"]["apps"]
    # Still listed while Argo CD cleans up: nothing more to do.
    assert step(session, state)[3] == []


def test_an_application_that_reappears_is_emitted_as_new():
    session = FakeArgoSession([make_app("web", history=[make_history(1)]), make_app("other")])
    state: dict = {}
    step(session, state)
    session.apps = [make_app("other")]
    step(session, state)
    session.apps = [make_app("web", history=[make_history(1)]), make_app("other")]
    deleted, upserts, _, _ = step(session, state)
    assert deleted == []
    assert set(upserts) == {APP, f"{APP}:history:1"}


def test_tombstones_come_before_upserts():
    session = FakeArgoSession([make_app("old"), make_app("keep")])
    state: dict = {}
    step(session, state)
    session.apps = [make_app("keep", health="Degraded", resource_version="9"), make_app("new")]
    _, _, _, rows = step(session, state)
    assert [row["_deleted"] for row in rows] == [True, False, False]


def test_history_that_rolled_off_argo_cd_is_kept():
    session = FakeArgoSession([make_app("web", history=[make_history(1), make_history(2)])])
    state: dict = {}
    step(session, state)
    session.apps = [make_app("web", history=[make_history(2)], resource_version="101")]
    deleted, upserts, _, _ = step(session, state)
    assert deleted == []
    assert upserts == {}
    assert len(state["instances"]["prod"]["apps"][APP]["history"]) == 2


# --- guards ---------------------------------------------------------------
def test_an_empty_list_with_known_applications_raises_and_keeps_the_state():
    session = FakeArgoSession(many(3))
    state: dict = {}
    step(session, state)
    before = snapshot(state)
    session.apps = []
    with pytest.raises(RuntimeError, match="no applications") as error:
        step(session, state)
    assert "allow_mass_delete=True" in str(error.value)
    assert "permission" in str(error.value)
    assert snapshot(state) == before


def test_an_empty_list_with_an_empty_state_only_warns(log):
    state: dict = {}
    _, _, _, rows = step(FakeArgoSession([]), state)
    assert rows == []
    assert any("no applications" in line for line in log.lines)


def test_losing_more_than_half_of_at_least_five_raises():
    session = FakeArgoSession(many(10))
    state: dict = {}
    step(session, state)
    before = snapshot(state)
    session.apps = many(4)
    with pytest.raises(RuntimeError, match="6 of the 10"):
        step(session, state)
    assert snapshot(state) == before


def test_exactly_half_or_fewer_than_five_is_allowed():
    session = FakeArgoSession(many(10))
    state: dict = {}
    step(session, state)
    session.apps = many(5)  # 5 of 10 is not more than half
    assert len(step(session, state)[0]) == 5
    session = FakeArgoSession(many(4))
    state = {}
    step(session, state)
    session.apps = many(1)  # 3 of 4 is more than half, but fewer than five
    assert len(step(session, state)[0]) == 3


def test_a_small_shrink_works():
    session = FakeArgoSession(many(10))
    state: dict = {}
    step(session, state)
    session.apps = many(9)
    deleted, _, _, _ = step(session, state)
    assert deleted == ["argocd:prod:argocd/app-9"]


def test_allow_mass_delete_bypasses_both_guards():
    session = FakeArgoSession(many(10))
    state: dict = {}
    step(session, state)
    session.apps = many(2)
    assert len(step(session, state, allow_mass_delete=True)[0]) == 8
    session.apps = []
    assert len(step(session, state, allow_mass_delete=True)[0]) == 2
    assert state["instances"]["prod"]["apps"] == {}


# --- errors ---------------------------------------------------------------
@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("boom"),
        FakeResponse(401, {"message": "invalid session"}),
        FakeResponse(403, {"message": "permission denied"}),
        FakeResponse(503, {}),
    ],
    ids=["exception", "401", "403", "503"],
)
def test_a_failed_list_call_leaves_the_state_untouched(failure):
    session = FakeArgoSession(many(3))
    state: dict = {}
    step(session, state)
    before = snapshot(state)
    # A 503 is retried five times before it gives up.
    session.script = [failure] * 6
    session.apps = []  # must not be read as "everything was deleted"
    with pytest.raises(RuntimeError):
        step(session, state)
    assert snapshot(state) == before


def test_an_interrupted_run_does_not_save_the_state():
    session = FakeArgoSession([make_app("old"), make_app("keep")])
    state: dict = {}
    step(session, state)
    before = snapshot(state)
    session.apps = [make_app("keep"), make_app("new")]
    iterator = _iter_rows(session, config(), state)
    next(iterator)  # the first row; the consumer then gives up
    iterator.close()
    assert snapshot(state) == before


# --- configuration changes --------------------------------------------------
def test_a_render_version_bump_re_renders_everything_but_emits_only_changes(monkeypatch):
    session = FakeArgoSession(many(3))
    state: dict = {}
    step(session, state)
    monkeypatch.setattr(argocd, "RENDER_VERSION", 2)
    renders = counting(monkeypatch)
    rows = step(session, state)[3]
    assert len(renders) == 3  # the resourceVersion shortcut is off
    assert rows == []  # the text did not change, so nothing is emitted
    assert step(session, state)[3] == []
    assert len(renders) == 3  # and the shortcut is back

    monkeypatch.setattr(argocd, "RENDER_VERSION", 3)
    monkeypatch.setattr(argocd, "_health_line", lambda status: "Health: reworded")
    upserts = step(session, state)[1]
    assert len(upserts) == 3
    assert all("Health: reworded" in row["content"] for row in upserts.values())


def test_toggling_include_resources_changes_the_content_and_re_emits():
    resources = [make_resource("Deployment", "web", health="Degraded")]
    session = FakeArgoSession([make_app("web", resources=resources)])
    state: dict = {}
    first = step(session, state)[1][APP]["content"]
    assert "Resources:" in first
    upserts = step(session, state, include_resources=False)[1]
    assert "Resources:" not in upserts[APP]["content"]
    upserts = step(session, state, include_resources=True)[1]
    assert upserts[APP]["content"] == first


def test_turning_history_off_forgets_the_stored_history_documents():
    session = FakeArgoSession(
        [make_app("web", history=[make_history(1), make_history(2)]), make_app("other")]
    )
    state: dict = {}
    step(session, state)
    deleted, upserts, _, _ = step(session, state, include_history=False)
    assert deleted == [f"{APP}:history:1", f"{APP}:history:2"]
    assert upserts == {}  # the application text itself did not change
    assert state["instances"]["prod"]["apps"][APP]["history"] == []
    assert step(session, state, include_history=False)[3] == []


def test_lowering_history_limit_keeps_the_old_history_documents():
    session = FakeArgoSession([make_app("web", history=[make_history(i) for i in (1, 2, 3)])])
    state: dict = {}
    step(session, state)
    deleted, upserts, _, _ = step(session, state, history_limit=1)
    assert deleted == []
    assert upserts == {}
    assert len(state["instances"]["prod"]["apps"][APP]["history"]) == 3


def test_a_filter_change_forgets_the_applications_outside_the_new_scope():
    session = FakeArgoSession(many(4))
    state: dict = {}
    step(session, state)
    session.apps = many(3)  # what the narrower request returns
    deleted, _, _, _ = step(session, state, projects=("payments",))
    assert deleted == ["argocd:prod:argocd/app-3"]


def test_a_filter_change_that_drops_most_applications_trips_the_guard():
    session = FakeArgoSession(many(10))
    state: dict = {}
    step(session, state)
    before = snapshot(state)
    session.apps = many(2)
    with pytest.raises(RuntimeError, match="would be forgotten"):
        step(session, state, selector="team=payments")
    assert snapshot(state) == before


def test_instances_keep_separate_state():
    state: dict = {}
    step(FakeArgoSession(many(2)), state)
    step(FakeArgoSession([make_app("x")]), state, instance="staging")
    assert set(state["instances"]) == {"prod", "staging"}
    assert step(FakeArgoSession(many(2)), state)[3] == []


def test_state_and_logs_contain_no_secrets_and_survive_json(log):
    app = make_app("web", repo="https://user:p4ss@git.example.com/org/r.git")
    app["status"]["history"] = [make_history(1)]
    state: dict = {}
    step(FakeArgoSession([app]), state)
    dumped = json.dumps(state)
    assert json.loads(dumped) == state
    assert "p4ss" not in dumped
    assert "argocd.example.com" not in dumped  # no base URL in the state
    assert "p4ss" not in "\n".join(log.lines)
    assert set(state) == {"instances"}
