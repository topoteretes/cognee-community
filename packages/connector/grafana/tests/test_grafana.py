import copy
import json

import dlt
import httpx
import pytest

from cognee_community_connector_grafana import (
    GrafanaClient,
    GrafanaSyncError,
    SyncStats,
    grafana_source,
)
from cognee_community_connector_grafana.rendering import cleaner


def dashboard(uid="a", revision="1", folder="f"):
    return {
        "metadata": {
            "name": uid,
            "uid": "resource-uuid-" + uid,
            "namespace": "default",
            "resourceVersion": revision,
            "annotations": {"grafana.app/folder": folder},
        },
        "spec": {
            "schemaVersion": 42,
            "title": "Incident dashboard " + uid,
            "description": "The service returns 201 in ap-south-1 on 2026-10-21.",
            "panels": [
                {"id": 1, "title": "Primary panel", "description": "Ravi lives in Calgary."},
                {"id": 2, "title": "Row", "panels": [{"title": "Nested panel"}]},
            ],
            "tags": ["incident"],
            "targets": [{"password": "never-render-this"}],
        },
        "status": {"conversion": {"failed": False}},
    }


def event(identifier=1, text="Yuna lives in Kyoto.", uid="a", when=50, end=None, tags=None):
    return {
        "id": identifier,
        "time": when,
        "timeEnd": end or when,
        "text": text,
        "dashboardUID": uid,
        "panelId": 1,
        "tags": tags or ["incident"],
        "alertId": 0,
    }


class Service:
    def __init__(self):
        self.dashboards = {"a": dashboard(), "b": dashboard("b")}
        self.events = [event()]
        self.calls = []
        self.fail = None
        self.uid = "service-account:1"
        self.role = "Admin"
        self.disabled = False
        self.permissions = {
            "dashboards:read": ["dashboards:*"],
            "folders:read": ["folders:*"],
            "annotations:read": ["annotations:*"],
        }
        self.hook = None

    def get(self, path, params=None):
        self.calls.append((path, copy.deepcopy(params)))
        if self.hook:
            self.hook(path, params)
        if path == self.fail:
            raise GrafanaSyncError("synthetic provider failure")
        if path == "/api/org":
            return {"id": 1}
        if path == "/api/user":
            return {"id": 0, "uid": self.uid, "orgId": 1}
        if path.startswith("/api/serviceaccounts/"):
            return {
                "id": int(self.uid.split(":")[1]),
                "orgId": 1,
                "role": self.role,
                "isDisabled": self.disabled,
            }
        if path == "/api/access-control/user/permissions":
            return copy.deepcopy(self.permissions)
        if path.endswith("/dashboards"):
            cursor = (params or {}).get("continue")
            items = list(self.dashboards.values())
            offset = int(cursor or 0)
            # One-item pages even though caller requested a larger limit.
            metadata = {"resourceVersion": "snapshot"}
            if offset + 1 < len(items):
                metadata["continue"] = str(offset + 1)
            return {
                "kind": "DashboardList",
                "apiVersion": "dashboard.grafana.app/v1beta1",
                "metadata": metadata,
                "items": copy.deepcopy(items[offset : offset + 1]),
            }
        if path == "/api/annotations":
            query = dict(params)
            selected = [
                item
                for item in self.events
                if item["time"] <= query["to"]
                and item["timeEnd"] >= query["from"]
                and (not query.get("dashboardUID") or item["dashboardUID"] == query["dashboardUID"])
                and all(value in item["tags"] for key, value in params if key == "tags")
            ]
            return copy.deepcopy(selected[: query["limit"]])
        raise AssertionError(path)


@pytest.fixture
def pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="grafana_test",
        dataset_name="test",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(
            credentials=f"sqlite:///{tmp_path / 'staging.sqlite'}"
        ),
    )


def source(service, **options):
    return grafana_source(
        base_url="https://grafana.example",
        token="synthetic-token-value",
        owner_id="owner",
        resource_name="incidents",
        org_id=1,
        annotation_from_ms=1,
        annotation_to_ms=100,
        folder_uids=["f"],
        service=service,
        **options,
    )


def rows(pipeline, src):
    name = next(iter(src.resources.selected))
    pipeline.run(src)
    with pipeline.sql_client() as sql:
        return [
            tuple(row)
            for row in sql.execute_sql(f"SELECT id, title, content FROM {name} ORDER BY id")
        ]


def committed(pipeline):
    return copy.deepcopy(pipeline.state["sources"])


def test_factory_defaults_full_snapshot_incremental_annotations_and_final_deletion(pipeline):
    service, stats = Service(), SyncStats()
    src = source(service, stats=stats)
    assert not service.calls  # construction is network-lazy
    initial = rows(pipeline, src)
    assert len(initial) == 3 and stats.changed == 3
    assert "Nested panel" in json.dumps(initial)
    assert "resource-uuid" not in json.dumps(initial)
    assert "never-render-this" not in json.dumps(initial)
    assert "ap-south-1" in json.dumps(initial) and "returns 201" in json.dumps(initial)
    assert rows(pipeline, source(service, stats=stats)) == initial
    assert stats.unchanged == 3 and stats.changed == 0
    service.events[0]["text"] = "Yuna moved to Osaka."
    edited = rows(pipeline, source(service, stats=stats))
    assert "Osaka" in json.dumps(edited) and "Kyoto" not in json.dumps(edited)
    assert stats.changed == 1 and stats.unchanged == 2
    service.events.append(event(2, "Lina lives in Denver."))
    assert len(rows(pipeline, source(service))) == 4
    service.events.pop(0)
    assert len(rows(pipeline, source(service))) == 3
    # Dashboard rename/panel removal replace obsolete searchable prose.
    service.dashboards["a"]["metadata"]["resourceVersion"] = "2"
    service.dashboards["a"]["spec"]["title"] = "Renamed dashboard"
    service.dashboards["a"]["spec"]["panels"] = []
    updated = rows(pipeline, source(service))
    assert "Renamed dashboard" in json.dumps(updated)
    service.dashboards.pop("b")
    final_dashboard = rows(pipeline, source(service))
    assert len(final_dashboard) == 2 and "Primary panel" not in json.dumps(final_dashboard)
    service.dashboards.clear()
    assert rows(pipeline, source(service, stats=stats)) == []
    assert stats.removed == 2 and stats.failures == 0


def test_selection_changes_reuse_table_and_intentional_empty_retires_corpus(pipeline):
    service = Service()
    initial = source(service)
    assert len(rows(pipeline, initial)) == 3
    changed = grafana_source(
        base_url="https://grafana.example",
        token="synthetic-token-value",
        owner_id="owner",
        resource_name="incidents",
        org_id=1,
        annotation_from_ms=1,
        annotation_to_ms=100,
        dashboard_uids=["b"],
        service=service,
    )
    assert initial.name == changed.name
    assert len(rows(pipeline, changed)) == 1
    service.dashboards["b"]["metadata"]["annotations"]["grafana.app/folder"] = "other"
    assert len(rows(pipeline, changed)) == 1  # UID identity survives a folder move
    retired = grafana_source(
        base_url="https://grafana.example",
        token="synthetic-token-value",
        owner_id="owner",
        resource_name="incidents",
        org_id=1,
        annotation_from_ms=1,
        annotation_to_ms=100,
        dashboard_uids=[],
        service=service,
    )
    assert retired.name == initial.name and rows(pipeline, retired) == []


@pytest.mark.parametrize(
    "failure",
    [
        "later_page",
        "permissions",
        "conversion",
        "schema",
        "repeated_cursor",
        "annotations",
        "render",
        "identity",
    ],
)
def test_failed_run_preserves_committed_rows_and_state_then_recovers(pipeline, failure):
    service = Service()
    initial = rows(pipeline, source(service))
    before = committed(pipeline)
    original = service.get

    def broken(path, params=None):
        result = original(path, params)
        if failure == "later_page" and isinstance(params, dict) and params.get("continue"):
            raise GrafanaSyncError("failed final page")
        if failure == "repeated_cursor" and path.endswith("/dashboards"):
            result["metadata"]["continue"] = "1"
        return result

    service.get = broken
    if failure == "permissions":
        service.permissions["annotations:read"] = ["annotations:type:organization"]
    if failure == "conversion":
        service.dashboards["b"]["status"]["conversion"]["failed"] = True
    if failure == "schema":
        service.dashboards["b"]["spec"]["schemaVersion"] = 99
    if failure == "annotations":
        service.fail = "/api/annotations"
    if failure == "render":
        service.dashboards["b"]["spec"]["panels"] = "not panels"
    if failure == "identity":
        service.uid = "service-account:2"
    stats = SyncStats()
    with pytest.raises(Exception, match="Grafana|synthetic|failed"):
        rows(pipeline, source(service, stats=stats))
    assert stats.failures == 1 and committed(pipeline) == before
    with pipeline.sql_client() as sql:
        table = next(iter(source(Service()).resources.selected))
        assert len(sql.execute_sql(f"SELECT * FROM {table}")) == len(initial)
    assert rows(pipeline, source(Service())) == initial


def test_concurrent_annotation_or_dashboard_edit_aborts(pipeline):
    service = Service()
    initial = rows(pipeline, source(service))
    n = [0]

    def hook(path, params):
        if path == "/api/annotations":
            n[0] += 1
            if n[0] == 2:
                service.events[0]["text"] = "Changed while scanning"

    service.hook = hook
    with pytest.raises(Exception, match="Annotation inventory changed"):
        rows(pipeline, source(service))
    service.hook = None
    assert len(rows(pipeline, source(service))) == len(initial)


def test_saturated_timestamp_refuses_to_truncate(pipeline):
    service = Service()
    initial = rows(pipeline, source(service))
    service.events = [event(i) for i in range(1, 4)]
    with pytest.raises(Exception, match="one millisecond"):
        rows(pipeline, source(service, annotation_limit=2))
    service.events = [event()]
    assert rows(pipeline, source(service)) == initial


def test_subdivision_regions_org_tags_and_linkage_identity(pipeline):
    service = Service()
    service.events = [
        event(1, when=10),
        event(2, when=50),
        event(3, when=90),
        event(4, uid="", when=30, end=70),
    ]
    first = rows(pipeline, source(service, annotation_limit=2))
    assert len(first) == 5  # two dashboards, three dashboard annotations
    old_ids = {r[0] for r in first}
    service.events[0]["dashboardUID"] = "b"
    linked = rows(pipeline, source(service, annotation_limit=2))
    assert {r[0] for r in linked} == old_ids
    org = grafana_source(
        base_url="https://grafana.example",
        token="synthetic-token-value",
        owner_id="owner",
        resource_name="incidents",
        org_id=1,
        annotation_from_ms=1,
        annotation_to_ms=100,
        organization_annotation_tags=["incident"],
        service=service,
    )
    selected = rows(pipeline, org)
    assert len(selected) == 1 and ":annotation:4" in selected[0][0]


def test_account_token_owner_resource_and_dataset_isolation(pipeline, tmp_path):
    service = Service()
    first = rows(pipeline, source(service))
    rotated = grafana_source(
        base_url="https://grafana.example",
        token="rotated-token-value",
        owner_id="owner",
        resource_name="incidents",
        org_id=1,
        annotation_from_ms=1,
        annotation_to_ms=100,
        folder_uids=["f"],
        service=service,
    )
    assert rows(pipeline, rotated) == first
    other = grafana_source(
        base_url="https://grafana.example",
        token="other-token-value",
        owner_id="owner",
        resource_name="independent",
        org_id=1,
        annotation_from_ms=1,
        annotation_to_ms=100,
        dashboard_uids=["b"],
        service=service,
    )
    assert other.name != rotated.name and len(rows(pipeline, other)) == 1
    service.dashboards.clear()
    assert rows(pipeline, source(service)) == []
    with pipeline.sql_client() as sql:
        assert len(sql.execute_sql(f"SELECT * FROM {other.name}")) == 1
    alternate = dlt.pipeline(
        pipeline_name="alternate",
        dataset_name="alternate",
        pipelines_dir=str(tmp_path / "other"),
        destination=dlt.destinations.sqlalchemy(
            credentials=f"sqlite:///{tmp_path / 'alternate.sqlite'}"
        ),
    )
    assert len(rows(alternate, source(Service()))) == 3


@pytest.mark.parametrize(
    "options",
    [
        dict(annotation_from_ms=0),
        dict(org_id=2),
        dict(namespace="other"),
        dict(token=""),
        dict(base_url="https://user:secret@grafana.example"),
        dict(base_url="http://grafana.example"),
        dict(dashboard_uids="a"),
    ],
)
def test_invalid_configuration(options):
    args = dict(
        base_url="https://grafana.example",
        token="test-token",
        org_id=1,
        owner_id="owner",
        resource_name="incidents",
        annotation_from_ms=1,
        dashboard_uids=["a"],
    )
    args.update(options)
    with pytest.raises(ValueError):
        grafana_source(**args)


def test_cancellation_and_total_budget(pipeline):
    def cancelled():
        raise RuntimeError("sensitive callback details")

    with pytest.raises(Exception, match="cancelled by host"):
        rows(pipeline, source(Service(), check_active=cancelled))
    with pytest.raises(Exception, match="budget exhausted"):
        rows(pipeline, source(Service(), max_requests=10))


@pytest.mark.parametrize("status", [401, 403, 404, 302, 500])
def test_http_failures_never_include_secret_response(status):
    client = GrafanaClient(
        "https://grafana.example/subpath",
        "secret-token",
        1,
        sleep=lambda _: None,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="secret-provider-response")
        ),
    )
    with pytest.raises(GrafanaSyncError) as error:
        client.get("/api/org")
    assert "secret" not in str(error.value)
    client.close()


def test_retry_budget_and_subpath_and_close():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"id": 1})

    waits = []
    client = GrafanaClient(
        "https://grafana.example/subpath",
        "token",
        1,
        sleep=waits.append,
        transport=httpx.MockTransport(handler),
    )
    assert client.get("/api/org") == {"id": 1}
    assert requests[-1].url.path == "/subpath/api/org"
    assert sum(waits) == 1
    client.close()
    client = GrafanaClient(
        "https://grafana.example",
        "token",
        1,
        transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "30"})),
    )
    with pytest.raises(GrafanaSyncError, match="wait budget"):
        client.get("/api/org")
    client.close()


def test_redaction_preserves_prose_and_omits_explicit_credentials():
    secret = "a-credential/with+reserved?symbols"
    clean = cleaner([secret])
    prose = "The service returns 201 in ap-south-1 on 2026-10-21, Content-Type: application/json."
    assert clean(prose) == prose
    for value in [
        secret,
        "a-credential%2Fwith%2Breserved%3Fsymbols",
        "glsa_abcdefghijklmnopqrstuv",
        "a" * 32,
        "eyJabcdefghijk.abcdefghijk.abcdefghijk",
    ]:
        assert value not in clean("Incident mentions " + value)
    assert "long-password" not in clean("password=long-password")
    assert "user:pass" not in clean("https://user:pass@example.test")
    assert "harmless" in clean("query=harmless")


def test_pending_load_replay_before_fresh_extraction(pipeline, monkeypatch):
    service = Service()
    initial = rows(pipeline, source(service))
    service.events[0]["text"] = "Pending replacement text"
    original_load = pipeline.load

    def fail_load(*args, **kwargs):
        raise RuntimeError("synthetic destination failure")

    monkeypatch.setattr(pipeline, "load", fail_load)
    with pytest.raises(Exception, match="synthetic destination failure"):
        pipeline.run(source(service))
    with pipeline.sql_client() as sql:
        table = source(service).name
        existing = sql.execute_sql(f"SELECT content FROM {table}")
        assert "Pending replacement" not in str(existing)
    monkeypatch.setattr(pipeline, "load", original_load)
    before_calls = len(service.calls)
    pipeline.run()  # Replay the retained normalized package without contacting Grafana.
    assert len(service.calls) == before_calls
    with pipeline.sql_client() as sql:
        assert "Pending replacement" in str(sql.execute_sql(f"SELECT content FROM {table}"))
    replayed = rows(pipeline, source(service))
    assert len(replayed) == len(initial) and "Pending replacement" in str(replayed)


def test_version_only_edit_keeps_documents_and_renderer_cache_skips_unchanged(
    pipeline, monkeypatch
):
    import importlib

    module = importlib.import_module("cognee_community_connector_grafana.grafana")
    service = Service()
    first = rows(pipeline, source(service))
    original = module.dashboard_row
    monkeypatch.setattr(module, "dashboard_row", lambda *a: pytest.fail("unchanged rendered again"))
    assert rows(pipeline, source(service)) == first
    monkeypatch.setattr(module, "dashboard_row", original)
    service.dashboards["a"]["metadata"]["resourceVersion"] = "2"
    assert rows(pipeline, source(service)) == first


def test_authority_rechecked_after_reads_and_unexpected_errors_sanitized(pipeline):
    service = Service()
    before = rows(pipeline, source(service))
    calls = [0]

    def revoke(path, params):
        if path == "/api/access-control/user/permissions":
            calls[0] += 1
            if calls[0] == 2:
                service.permissions["dashboards:read"] = []

    service.hook = revoke
    with pytest.raises(Exception, match="Complete inventory requires"):
        rows(pipeline, source(service))
    service = Service()
    service.get = lambda *a, **k: (_ for _ in ()).throw(ValueError("secret-response-body"))
    with pytest.raises(Exception, match="service read failed") as error:
        rows(pipeline, source(service))
    assert "secret-response-body" not in str(error.value)
    assert rows(pipeline, source(Service())) == before


def test_request_budget_counts_both_annotation_scans_and_pages(pipeline):
    service = Service()
    service.events = [event(i, when=i * 10) for i in range(1, 9)]
    with pytest.raises(Exception, match="budget exhausted"):
        rows(pipeline, source(service, annotation_limit=2, max_requests=12))


def test_library_panels_are_explicitly_unsupported(pipeline):
    service = Service()
    service.dashboards["a"]["spec"]["panels"][0]["libraryPanel"] = {"uid": "model"}
    with pytest.raises(Exception, match="Library panels"):
        rows(pipeline, source(service))


@pytest.mark.parametrize("role,disabled", [("Viewer", False), ("Admin", True)])
def test_stored_role_catches_cached_wildcard_grants(pipeline, role, disabled):
    service = Service()
    initial = rows(pipeline, source(service))
    service.role, service.disabled = role, disabled
    # Effective grants intentionally remain stale and fully permissive.
    assert "dashboards:*" in service.permissions["dashboards:read"]
    with pytest.raises(Exception, match="enabled organization Admin"):
        rows(pipeline, source(service))
    assert rows(pipeline, source(Service())) == initial


@pytest.mark.parametrize(
    "credential_text",
    [
        "Authorization: Token SYNTHETIC_CRED_77",
        "Authorization: Bearer SYNTHETIC_CRED_77",
        "Authorization: Basic SYNTHETIC_CRED_77",
        "Authorization: CustomScheme SYNTHETIC_CRED_77",
        '{"Authorization": "Token SYNTHETIC_CRED_77"}',
        '{"password": "SYNTHETIC_CRED_77 with spaces"}',
        "{'passwd': 'SYNTHETIC_CRED_77 with spaces'}",
        r'{"password": "SYNTHETIC_CRED_77 escaped \" quote"}',
        "db_pwd=SYNTHETIC_CRED_77",
        "db_pass=SYNTHETIC_CRED_77",
        "database_password=SYNTHETIC_CRED_77",
        "api_key=SYNTHETIC_CRED_77",
        '{"api_token": "SYNTHETIC_CRED_77"}',
        "API-TOKEN=SYNTHETIC_CRED_77",
        "refresh_token=SYNTHETIC_CRED_77",
        'Authorization: Basic "SYNTHETIC_CRED_77"',
        "Authorization: Token 'SYNTHETIC_CRED_77'",
        'Authorization: Digest username="grafana", realm="test", '
        'nonce="challenge", response="SYNTHETIC_CRED_77"',
        'Proxy-Authorization: CustomScheme "SYNTHETIC_CRED_77"',
        '{"Authorization": "Basic SYNTHETIC_CRED_77", "region": "EU-WEST-1"}',
        "postgresql://grafana:SYNTHETIC_CRED_77@db.example/metrics",
        "postgresql+asyncpg://grafana:SYNTHETIC_CRED_77@db.example/metrics",
        "mysql://grafana:SYNTHETIC_CRED_77@db.example/metrics",
        "mongodb+srv://grafana:SYNTHETIC_CRED_77@db.example/metrics",
        "redis://grafana:SYNTHETIC_CRED_77@db.example/0",
        "redis://:SYNTHETIC_CRED_77@cache.example:6379/0",
        "rediss://:SYNTHETIC_CRED_77@cache.example:6380/0",
        "postgresql://:SYNTHETIC_CRED_77@db.example/metrics",
    ],
)
def test_explicit_credential_context_never_reaches_staging(pipeline, credential_text):
    service = Service()
    service.dashboards["a"]["spec"]["description"] = credential_text
    service.events = [event(text=credential_text)]
    staged = rows(pipeline, source(service))
    assert "SYNTHETIC_CRED_77" not in json.dumps(staged)
    assert "[REDACTED]" in json.dumps(staged)


@pytest.mark.parametrize(
    "prose",
    [
        "The service returns 201 in ap-south-1 on 2026-10-21.",
        "Cache-Control: no-cache, Content-Encoding: gzip, X-Region: EU-WEST-1",
        '{"status": "active", "region": "EU-WEST-1", "date": "2026-10-21"}',
        "The password rotation restored the service. Authorization is required.",
        "query=harmless&sort=ascending",
        "postgresql://db.example/metrics and https://example.test/docs",
        '{"page_token": "next-page", "region": "EU-WEST-1"}',
        "Authorization is required.\nCache-Control: no-cache; Content-Type: application/json.",
    ],
)
def test_credential_filter_preserves_ordinary_selected_prose(pipeline, prose):
    service = Service()
    service.events = [event(text=prose)]
    staged = rows(pipeline, source(service))
    assert prose in next(content for ident, title, content in staged if ":annotation:" in ident)


def test_render_policy_upgrade_invalidates_unchanged_snapshot_cache(pipeline, monkeypatch):
    import cognee_community_connector_grafana.grafana as module

    service = Service()
    monkeypatch.setattr(module, "_RENDER_VERSION", 3)
    monkeypatch.setattr(module, "cleaner", lambda values: lambda value: value)
    service.events = [event(text="redis://:SYNTHETIC_CRED_77@cache.example:6379/0")]
    service.dashboards["a"]["spec"]["description"] = service.events[0]["text"]
    assert "SYNTHETIC_CRED_77" in json.dumps(rows(pipeline, source(service)))
    monkeypatch.undo()
    upgraded = rows(pipeline, source(service))
    assert "SYNTHETIC_CRED_77" not in json.dumps(upgraded)


def test_quoted_authorization_keeps_neighboring_fields_and_next_line():
    value = '{"Authorization": "Basic opaque-credential", "region": "EU-WEST-1"}\n2026-10-21'
    cleaned = cleaner([])(value)
    assert "opaque-credential" not in cleaned
    assert '"region": "EU-WEST-1"' in cleaned
    assert cleaned.endswith("2026-10-21")
    header = (
        'Authorization: Digest username="grafana", response="opaque-credential"\n'
        "Healthy in EU-WEST-1"
    )
    cleaned = cleaner([])(header)
    assert "opaque-credential" not in cleaned
    assert cleaned.endswith("Healthy in EU-WEST-1")


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer SYNTHETIC_CRED_77",
        'Basic "SYNTHETIC_CRED_77"',
        "Token 'SYNTHETIC_CRED_77'",
        "CustomScheme SYNTHETIC_CRED_77",
        'Digest username="grafana", realm="test", response="SYNTHETIC_CRED_77"',
    ],
)
def test_authorization_preserves_same_line_incident_outcome(pipeline, authorization):
    outcome = "; outcome: checkout recovered in EU-WEST-1 after rollback."
    prose = "Authorization: " + authorization + outcome
    service = Service()
    service.dashboards["a"]["spec"]["description"] = prose
    service.events = [event(text=prose)]
    staged = rows(pipeline, source(service))
    for ident, _title, content in staged:
        if ":annotation:" in ident or ident.endswith(":dashboard:a"):
            assert "SYNTHETIC_CRED_77" not in content
            assert outcome in content


def test_policy_upgrade_restores_cached_incident_prose(pipeline, monkeypatch):
    import cognee_community_connector_grafana.grafana as module

    outcome = "; outcome: checkout recovered in EU-WEST-1 after rollback."
    service = Service()
    service.dashboards = {"a": dashboard()}
    service.dashboards["a"]["spec"]["description"] = "Authorization: Bearer synthetic" + outcome
    monkeypatch.setattr(module, "_RENDER_VERSION", 3)
    monkeypatch.setattr(module, "cleaner", lambda values: lambda value: value.replace(outcome, ""))
    assert outcome not in json.dumps(rows(pipeline, source(service)))
    monkeypatch.undo()
    assert outcome in json.dumps(rows(pipeline, source(service)))
