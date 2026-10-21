"""Offline tests for the Backstage connector: a fake catalog and TechDocs backend."""

import copy

import pytest
import requests

from cognee_community_connector_backstage import backstage as bs
from cognee_community_connector_backstage.backstage import (
    BackstageAPI,
    SyncConfig,
    backstage_source,
    catalog_filters,
    entity_to_row,
    sync_catalog,
    techdocs_pages,
)

BASE = "https://backstage.example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _entity(kind, name, namespace="default", relations=(), spec=None, **metadata):
    return {
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace, **metadata},
        "spec": spec or {},
        "relations": [{"type": t, "targetRef": ref} for t, ref in relations],
    }


def _catalog():
    return [
        _entity(
            "Component",
            "billing-service",
            description="Charges customers and issues invoices.",
            tags=["python", "payments"],
            links=[{"url": "https://runbooks.example.com/billing", "title": "Runbook"}],
            annotations={
                "github.com/project-slug": "acme/billing",
                "backstage.io/managed-by-location": "url:https://internal.example.com/x.yaml",
            },
            spec={"type": "service", "lifecycle": "production"},
            relations=[
                ("partOf", "system:default/payments"),
                ("ownedBy", "group:default/payments-team"),
                ("consumesApi", "api:default/fx-rates"),
            ],
        ),
        _entity(
            "Group",
            "payments-team",
            title="Payments Team",
            spec={"type": "team", "profile": {"email": "payments@example.com"}},
            relations=[
                ("hasMember", "user:default/asha"),
                ("ownerOf", "component:default/billing-service"),
            ],
        ),
        _entity(
            "User",
            "asha",
            spec={"profile": {"displayName": "Asha Rao", "email": "asha@example.com"}},
            relations=[("memberOf", "group:default/payments-team")],
        ),
        _entity("System", "payments", description="Everything that moves money."),
        _entity("Location", "generated-123"),
    ]


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, str):  # a web page, not JSON
            raise ValueError("not JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def _matches(entity, flt):
    for condition in flt.split(","):
        key, _, value = condition.partition("=")
        metadata = entity["metadata"]
        actual = entity["kind"] if key == "kind" else metadata.get("namespace", "default")
        if actual.lower() != value.lower():
            return False
    return True


class FakeBackstage:
    """Serves /api/catalog/entities/by-query with cursor paging, plus TechDocs files."""

    def __init__(self, entities, docs=None, page_size=2):
        self.closed = False
        self.stuck_cursor = False
        self.entities = entities
        self.docs = docs or {}  # site path -> {"metadata": {...}, "index": {...}}
        self.page_size = page_size
        self.calls = []
        self.scripted = []
        self.fail_on_call = None
        self.headers = {}
        self._cursors = {}

    def request(self, method, url, params=None, timeout=None):
        path = url[len(BASE) :]
        self.calls.append((path, params))
        if self.fail_on_call == len(self.calls):
            return FakeResponse(400, {"error": "boom"})
        if self.scripted:
            return self.scripted.pop(0)
        if path == "/api/catalog/entities/by-query":
            return self._query(dict(params) if params else {}, params or [])
        for site, files in self.docs.items():
            if path == f"{site}/techdocs_metadata.json":
                return FakeResponse(200, files["metadata"])
            if path == f"{site}/search/search_index.json":
                return FakeResponse(200, files["index"])
        return FakeResponse(404, {"error": "not found"})

    def _query(self, single, params):
        assert ("fields", bs._LIST_FIELDS) in params  # every page keeps the field selection
        if self.stuck_cursor and "cursor" in single:
            filters, offset = [], 0  # the backend keeps handing out the same cursor
        elif "cursor" in single:
            filters, offset = self._cursors[single["cursor"]]
        else:
            filters, offset = [v for k, v in params if k == "filter"], 0
        rows = [e for e in self.entities if any(_matches(e, f) for f in filters)]
        page = rows[offset : offset + self.page_size]
        info = {}
        if self.stuck_cursor:
            return FakeResponse(
                200, {"items": copy.deepcopy(page), "pageInfo": {"nextCursor": "c"}}
            )
        if offset + self.page_size < len(rows):
            cursor = f"c{len(self._cursors)}"
            self._cursors[cursor] = (filters, offset + self.page_size)
            info["nextCursor"] = cursor
        return FakeResponse(200, {"items": copy.deepcopy(page), "pageInfo": info})

    def close(self):
        self.closed = True


@pytest.fixture
def _no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(bs.time, "sleep", slept.append)
    return slept


def _api(backend):
    return BackstageAPI(backend, BASE)


def _rows(backend, state, **config):
    return list(sync_catalog(_api(backend), SyncConfig(**config), state))


def _titles():
    return {bs.entity_ref(e): bs.display_name(e) for e in _catalog()}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_component_document_renders_relations_as_sentences():
    row = entity_to_row(BASE, _catalog()[0], _titles())

    assert row["id"] == "entity:component:default/billing-service"
    assert row["title"] == "Component: billing-service"
    assert row["url"] == f"{BASE}/catalog/default/component/billing-service"
    assert row["content"].splitlines() == [
        "Backstage component billing-service.",
        "Charges customers and issues invoices.",
        "Type: service",
        "Lifecycle: production",
        "Tags: python, payments",
        "Link: Runbook — https://runbooks.example.com/billing",
        "github.com/project-slug: acme/billing",
        "billing-service consumes the API fx-rates.",
        "billing-service is owned by the group Payments Team.",
        "billing-service is part of the system payments.",
    ]
    assert "internal.example.com" not in row["content"]  # annotations are allowlisted


def test_user_and_group_documents_use_display_names():
    group, user = _catalog()[1], _catalog()[2]

    group_row = entity_to_row(BASE, group, _titles())
    user_row = entity_to_row(BASE, user, _titles())

    assert group_row["title"] == "Group: Payments Team"
    assert "Payments Team includes the user Asha Rao." in group_row["content"]
    assert "Email: payments@example.com" in group_row["content"]
    assert user_row["content"].splitlines() == [
        "Backstage user asha (Asha Rao).",
        "Email: asha@example.com",
        "Asha Rao is a member of the group Payments Team.",
    ]


def test_relations_outside_the_catalog_and_other_namespaces():
    entity = _entity(
        "AiResource",
        "embedder",
        namespace="ml",
        relations=[("dependsOn", "resource:ml/gpu-pool"), ("customRel", "component:default/x")],
    )

    row = entity_to_row(BASE, entity, {})

    assert row["title"] == "Ai Resource: embedder"
    assert row["content"].splitlines() == [
        "Backstage ai resource embedder in namespace ml.",
        "embedder has a customRel relation to the component x.",
        "embedder depends on the resource gpu-pool in namespace ml.",
    ]
    assert row["url"] == f"{BASE}/catalog/ml/airesource/embedder"


def test_entity_refs_are_parsed_and_validated():
    assert bs._parse_ref("Component:Default/Billing") == (
        "component",
        "default",
        "billing",
        "component:default/billing",
    )
    assert bs._parse_ref("api:fx-rates")[3] == "api:default/fx-rates"
    with pytest.raises(ValueError):
        bs._parse_ref("billing-service")


# ---------------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------------
def test_source_declares_document_marker_and_scope():
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

    source = backstage_source(base_url=BASE + "/", session=FakeBackstage([]))

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "backstage"
    assert getattr(source, PIPELINE_SCOPE_ATTR) == f"backstage:{BASE}"


def test_source_validates_configuration(monkeypatch):
    pytest.importorskip("dlt")
    monkeypatch.delenv("BACKSTAGE_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="BACKSTAGE_BASE_URL"):
        backstage_source(session=FakeBackstage([]))
    with pytest.raises(ValueError, match="kinds"):
        backstage_source(base_url=BASE, kinds=[], session=FakeBackstage([]))
    with pytest.raises(ValueError, match="kind:namespace/name"):
        backstage_source(base_url=BASE, techdocs=["billing"], session=FakeBackstage([]))


def test_service_and_session_are_one_client():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="not both"):
        backstage_source(base_url=BASE, service=FakeBackstage([]), session=FakeBackstage([]))


def test_malformed_token_is_rejected_without_echoing_it():
    assert bs._check_token("  s3cret\n") == "s3cret"  # a pasted newline is stripped
    with pytest.raises(ValueError) as error:
        bs._check_token("s3c ret")
    assert "s3c" not in str(error.value)


def test_token_is_sent_as_bearer():
    session = bs._make_session("s3cret")
    assert session.headers["Authorization"] == "Bearer s3cret"
    assert "Authorization" not in bs._make_session(None).headers


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def test_filters_are_one_per_kind_and_namespace():
    assert catalog_filters(["Component", "API"], []) == ["kind=Component", "kind=API"]
    assert catalog_filters(["Component"], ["default", "ops"]) == [
        "kind=Component,metadata.namespace=default",
        "kind=Component,metadata.namespace=ops",
    ]


def test_listing_follows_the_cursor_and_skips_unselected_kinds():
    backend = FakeBackstage(_catalog(), page_size=2)

    names = [
        e["metadata"]["name"] for e in _api(backend).entities(catalog_filters(bs.DEFAULT_KINDS, []))
    ]

    assert names == ["billing-service", "payments-team", "asha", "payments"]  # no Location
    assert len(backend.calls) == 2


def test_rate_limit_is_retried_and_auth_errors_name_the_token(_no_sleep):
    backend = FakeBackstage([])
    backend.scripted = [FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse(503)]
    assert list(_api(backend).entities(["kind=Component"])) == []
    assert _no_sleep == [3.0, 2.0]

    backend.scripted = [FakeResponse(401)]
    with pytest.raises(requests.HTTPError, match="externalAccess"):
        list(_api(backend).entities(["kind=Component"]))
    assert _no_sleep == [3.0, 2.0]  # no retry for 401


def test_web_page_instead_of_json_points_at_the_base_url():
    backend = FakeBackstage([])
    backend.scripted = [FakeResponse(200, "<html>Backstage</html>")]

    with pytest.raises(RuntimeError, match="backend"):
        list(_api(backend).entities(["kind=Component"]))


def test_repeated_cursor_stops_the_run():
    backend = FakeBackstage(_catalog(), page_size=1)
    backend.stuck_cursor = True

    with pytest.raises(bs.BackstageSyncError, match="same page cursor"):
        list(_api(backend).entities(["kind=Component"]))


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def test_first_sync_emits_every_entity_and_a_rerun_emits_nothing():
    backend = FakeBackstage(_catalog())
    state = {}

    first = _rows(backend, state)
    second = _rows(backend, state)

    assert sorted(r["id"] for r in first) == [
        "entity:component:default/billing-service",
        "entity:group:default/payments-team",
        "entity:system:default/payments",
        "entity:user:default/asha",
    ]
    assert second == []


def test_edit_emits_only_the_edited_entity():
    backend = FakeBackstage(_catalog())
    state = {}
    _rows(backend, state)

    backend.entities[3]["metadata"]["description"] = "Moves money, now with refunds."
    rows = _rows(backend, state)

    assert [r["id"] for r in rows] == ["entity:system:default/payments"]
    assert "now with refunds" in rows[0]["content"]


def test_renamed_relation_target_re_emits_entities_that_mention_it():
    backend = FakeBackstage(_catalog())
    state = {}
    _rows(backend, state)

    backend.entities[1]["metadata"]["title"] = "Billing & Payments"
    rows = _rows(backend, state)

    assert sorted(r["id"] for r in rows) == [
        "entity:component:default/billing-service",
        "entity:group:default/payments-team",
        "entity:user:default/asha",
    ]


def test_deleted_entity_is_tombstoned():
    backend = FakeBackstage(_catalog())
    state = {}
    _rows(backend, state)

    del backend.entities[3]  # the payments system
    rows = _rows(backend, state)

    assert {"id": "entity:system:default/payments", "_deleted": True} in rows
    assert "entity:system:default/payments" not in state["entities"]


def test_kind_dropped_from_the_selection_is_tombstoned():
    backend = FakeBackstage(_catalog())
    state = {}
    _rows(backend, state)

    rows = _rows(backend, state, kinds=["Component", "Group", "System"])

    assert {"id": "entity:user:default/asha", "_deleted": True} in rows
    # The group's member is now named by its id, so the group document changes too.
    assert any(r["id"] == "entity:group:default/payments-team" for r in rows)


def test_empty_listing_for_the_same_selection_forgets_nothing():
    backend = FakeBackstage(_catalog())
    state = {}
    _rows(backend, state)
    before = copy.deepcopy(state)

    backend.entities = []  # e.g. the token lost its catalog read permission
    with pytest.raises(bs.BackstageSyncError, match="Refusing to forget"):
        _rows(backend, state)
    assert state == before

    # Choosing a selection that is legitimately empty does forget the old entities.
    rows = _rows(backend, state, kinds=["Domain"])
    assert len(rows) == 4 and all(row["_deleted"] for row in rows)


def test_malformed_entity_is_skipped_and_counted():
    backend = FakeBackstage([*_catalog(), {"kind": "Component", "metadata": {}}])
    stats = {}

    rows = list(sync_catalog(_api(backend), SyncConfig(), {}, stats=stats))

    assert len(rows) == 4
    assert stats["failed"] == 1


def test_stats_count_what_a_run_did():
    backend = FakeBackstage(_catalog(), docs=_docs())
    state, stats = {}, {}
    config = SyncConfig(techdocs=["component:default/billing-service"])
    list(sync_catalog(_api(backend), config, state, stats=stats))
    assert stats == {
        "scanned": 4,
        "skipped": 0,
        "changed": 4,
        "deleted": 0,
        "failed": 0,
        "techdocs_pages": 2,
        "requests": 4,  # two listing pages, TechDocs metadata and index
        "complete": 1,
    }

    del backend.entities[3]
    list(sync_catalog(_api(backend), config, state, stats=stats))
    assert (stats["scanned"], stats["skipped"], stats["deleted"], stats["requests"]) == (3, 3, 1, 3)


def test_check_active_can_stop_a_run_before_anything_is_published():
    backend = FakeBackstage(_catalog())
    state = {}
    calls = []

    def check_active():
        calls.append(1)
        if len(calls) == 4:  # after both listing pages, at the second row
            raise PermissionError("revoked")

    rows = sync_catalog(_api(backend), SyncConfig(), state, check_active=check_active)
    with pytest.raises(PermissionError):
        list(rows)
    assert "entities" not in state or state["entities"] == {}


def test_failed_page_aborts_before_any_row_or_state_change():
    backend = FakeBackstage(_catalog())
    state = {}
    _rows(backend, state)
    before = copy.deepcopy(state)

    del backend.entities[3]
    backend.calls.clear()
    backend.fail_on_call = 2  # the second page of the listing
    with pytest.raises(requests.HTTPError):
        _rows(backend, state)

    assert state == before  # nothing tombstoned, nothing forgotten


# ---------------------------------------------------------------------------
# TechDocs
# ---------------------------------------------------------------------------
SITE = "/api/techdocs/static/docs/default/component/billing-service"


def _index(*pages):
    docs = []
    for location, title, text in pages:
        docs.append({"location": location, "title": title, "text": text})
    return {"docs": docs}


def _docs(build="1", pages=None):
    pages = pages or [
        ("", "Billing", "<p>How billing works.</p>"),
        ("runbook/", "Runbook", "Restart the worker."),
        ("runbook/#alerts", "Alerts", "Page the on-call engineer."),
    ]
    return {
        SITE: {
            "metadata": {"site_name": "Billing docs", "build_timestamp": build},
            "index": _index(*pages),
        }
    }


def test_techdocs_index_becomes_one_document_per_page():
    rows = techdocs_pages(
        BASE, "component:default/billing-service", "Billing docs", _docs()[SITE]["index"]
    )

    assert [(r["id"], r["title"]) for r in rows] == [
        ("techdocs:component:default/billing-service/", "Billing docs: Billing"),
        ("techdocs:component:default/billing-service/runbook/", "Billing docs: Runbook"),
    ]
    assert rows[1]["content"].splitlines() == [
        "TechDocs for billing-service (Billing docs), page Runbook.",
        "Restart the worker.",
        "Alerts",
        "Page the on-call engineer.",
    ]
    assert rows[1]["url"] == f"{BASE}/docs/default/component/billing-service/runbook/"


def test_techdocs_index_is_fetched_again_only_for_a_new_build():
    backend = FakeBackstage(_catalog(), docs=_docs(build="1"))
    state = {}
    first = _rows(backend, state, techdocs=["component:default/billing-service"])
    backend.calls.clear()
    second = _rows(backend, state, techdocs=["component:default/billing-service"])

    assert sum(r["id"].startswith("techdocs:") for r in first) == 2
    assert second == []
    assert not any(path.endswith("search_index.json") for path, _ in backend.calls)


def test_removed_page_and_dropped_entity_are_tombstoned():
    backend = FakeBackstage(_catalog(), docs=_docs(build="1"))
    state = {}
    refs = ["component:default/billing-service"]
    _rows(backend, state, techdocs=refs)

    backend.docs = _docs(build="2", pages=[("", "Billing", "How billing works, v2.")])
    rows = _rows(backend, state, techdocs=refs)
    assert {"id": "techdocs:component:default/billing-service/runbook/", "_deleted": True} in rows
    assert any(r["id"].endswith("/") and "v2" in r.get("content", "") for r in rows)

    rows = _rows(backend, state, techdocs=[])
    assert rows == [{"id": "techdocs:component:default/billing-service/", "_deleted": True}]


def test_missing_techdocs_forget_their_pages():
    backend = FakeBackstage(_catalog(), docs=_docs())
    state = {}
    refs = ["component:default/billing-service"]
    _rows(backend, state, techdocs=refs)

    backend.docs = {}  # docs were removed: TechDocs answers 404
    rows = _rows(backend, state, techdocs=refs)

    assert sorted(r["id"] for r in rows) == [
        "techdocs:component:default/billing-service/",
        "techdocs:component:default/billing-service/runbook/",
    ]
    assert all(r["_deleted"] for r in rows)
    assert state["techdocs"] == {}


# ---------------------------------------------------------------------------
# End to end through dlt merge (what cognee.remember does)
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_sync(dlt, tmp_path, backend, **kwargs):
    db_path = (tmp_path / "backstage.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="backstage_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="backstage_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        backstage_source(base_url=BASE, session=backend, **kwargs),
        write_disposition="merge",
        primary_key="id",
    )
    return pipeline


def _read(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM backstage_documents") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_source_closes_the_session_it_opens_and_reports_stats(dlt_mod, tmp_path, monkeypatch):
    backend = FakeBackstage(_catalog())
    monkeypatch.setattr(bs, "_make_session", lambda token: backend)
    db_path = (tmp_path / "own.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="backstage_own",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="backstage_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    source = backstage_source(base_url=BASE)

    pipeline.run(source, write_disposition="merge", primary_key="id")

    assert backend.closed
    assert source.cognee_sync_stats["complete"] == 1
    assert source.cognee_sync_stats["changed"] == 4


def test_dlt_merge_applies_edits_and_deletions(dlt_mod, tmp_path):
    backend = FakeBackstage(_catalog(), docs=_docs())
    refs = ["component:default/billing-service"]

    pipeline = _run_sync(dlt_mod, tmp_path, backend, techdocs=refs)
    assert len(_read(pipeline)) == 6  # 4 entities + 2 TechDocs pages

    backend.entities[3]["metadata"]["description"] = "Moves money, now with refunds."
    del backend.entities[2]  # the user leaves
    pipeline = _run_sync(dlt_mod, tmp_path, backend, techdocs=refs)
    rows = _read(pipeline)

    assert "entity:user:default/asha" not in rows
    assert "now with refunds" in rows["entity:system:default/payments"]
    assert (
        "billing-service consumes the API fx-rates."
        in rows["entity:component:default/billing-service"]
    )
    assert len(rows) == 5
