"""Tests for the DataHub connector.

* Unit tests against a fake GraphQL session: rendering, scroll pagination,
  scope filters, soft-delete handling, auth and error handling.
* dlt-pipeline tests (temp sqlite destination) for the sync scenarios: first
  sync, ownership/tag/lineage changes re-synced, soft-deleted dataset
  forgotten, failed fetch keeps the last snapshot, no truncation.
"""

import copy
import json

import pytest

from cognee_community_connector_datahub.datahub import (
    PAGE_SIZE,
    DataHubAPIError,
    DataHubAuthError,
    DataHubClient,
    datahub_source,
    iter_rows,
    render_dataset,
    urn_name,
)

BASE = "http://datahub.test"
TOKEN = "pat-secret"


def urn(name, platform="postgres"):
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},PROD)"


def _ref(kind, name):
    return {kind: {"urn": f"urn:li:{kind}:{name}", "properties": {"name": name}}}


def _dataset(name, **overrides):
    entity = {
        "urn": urn(name),
        "name": name.rsplit(".", 1)[-1],
        "origin": "PROD",
        "platform": {"name": "postgres"},
        "properties": {"description": None},
        "editableProperties": {"description": f"About {name}."},
        "institutionalMemory": {"elements": []},
        "parentContainers": {
            "containers": [
                {"properties": {"name": "analytics"}},
                {"properties": {"name": "retail_demo"}},
            ]
        },
        "schemaMetadata": {
            "fields": [
                {
                    "fieldPath": "email",
                    "nativeDataType": "TEXT",
                    "description": None,
                    "globalTags": None,
                    "glossaryTerms": None,
                },
                {
                    "fieldPath": "id",
                    "nativeDataType": "INTEGER",
                    "description": "Key.",
                    "globalTags": None,
                    "glossaryTerms": None,
                },
            ]
        },
        "editableSchemaMetadata": {
            "editableSchemaFieldInfo": [
                {
                    "fieldPath": "email",
                    "description": "Contact address.",
                    "globalTags": {"tags": [_ref("tag", "pii")]},
                    "glossaryTerms": None,
                },
            ]
        },
        "ownership": {
            "owners": [
                {
                    "owner": {
                        "urn": "urn:li:corpGroup:rev",
                        "name": "rev",
                        "properties": {"displayName": "Revenue Analytics"},
                    },
                    "ownershipType": None,
                    "type": "BUSINESS_OWNER",
                },
            ]
        },
        "domain": {"domain": {"urn": "urn:li:domain:retail", "properties": {"name": "Retail"}}},
        "tags": {"tags": [_ref("tag", "finance")]},
        "glossaryTerms": {"terms": [_ref("term", "Revenue")]},
        "status": {"removed": False},
        "upstream": {
            "total": 1,
            "relationships": [
                {
                    "entity": {
                        "urn": urn("retail_demo.raw.orders"),
                        "name": "orders",
                        "platform": {"name": "postgres"},
                    }
                },
            ],
        },
    }
    entity.update(overrides)
    return entity


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeDataHub:
    """Stand-in for DataHub GMS GraphQL, used as a requests.Session."""

    def __init__(self, datasets=None):
        self.datasets = (
            datasets if datasets is not None else [_dataset("retail_demo.analytics.rev")]
        )
        self.inputs: list[dict] = []
        self.fail_on_page: int | None = None
        self.graphql_error = False

    def post(self, url, json=None, headers=None, timeout=None):
        if (headers or {}).get("Authorization") != f"Bearer {TOKEN}":
            return FakeResponse(401, {"message": "Unauthorized"})
        search_input = json["variables"]["input"]
        self.inputs.append(search_input)
        page = int(search_input.get("scrollId") or 0)
        if self.fail_on_page is not None and page == self.fail_on_page:
            return FakeResponse(500, {"message": f"boom {TOKEN}"})
        if self.graphql_error:
            return FakeResponse(
                200,
                {
                    "data": {"scrollAcrossEntities": None},
                    "errors": [{"message": "Timeout fetching lineage"}],
                },
            )
        visible = [d for d in self.datasets if self._in_scope(d, search_input)]
        start = page * search_input["count"]
        chunk = visible[start : start + search_input["count"]]
        more = start + search_input["count"] < len(visible)
        return FakeResponse(
            200,
            {
                "data": {
                    "scrollAcrossEntities": {
                        "nextScrollId": str(page + 1) if more else None,
                        "searchResults": [{"entity": d} for d in chunk],
                    }
                }
            },
        )

    @staticmethod
    def _in_scope(dataset, search_input):
        for clause in (search_input.get("orFilters") or [{"and": []}])[0]["and"]:
            platform = f"urn:li:dataPlatform:{dataset['platform']['name']}"
            if clause["field"] == "platform" and platform not in clause["values"]:
                return False
        return True


def _rows(fake, **kwargs):
    return list(iter_rows(DataHubClient(BASE, token=TOKEN, session=fake), **kwargs))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_urn_name():
    assert (
        urn_name(urn("retail_demo.analytics.daily_revenue"))
        == "retail_demo.analytics.daily_revenue"
    )
    assert urn_name("urn:li:corpGroup:x") is None


def test_document_states_ownership_domain_tags_terms_lineage_and_columns():
    (row,) = _rows(FakeDataHub())
    text = row["content"]
    name = "retail_demo.analytics.rev"
    assert row["id"] == urn(name)
    assert row["title"] == f"DataHub dataset {name}"
    assert f"URN: {urn(name)}" in text
    assert "Platform: postgres; environment: PROD; container: retail_demo > analytics" in text
    assert f"Dataset {name} is owned by Revenue Analytics." in text
    assert f"Revenue Analytics is the Business Owner of dataset {name}." in text
    assert "(Business Owner)" not in text
    assert f"Dataset {name} belongs to domain Retail." in text
    assert f"Dataset {name} is tagged finance." in text
    assert f"Dataset {name} is linked to glossary term Revenue." in text
    assert f"Dataset retail_demo.raw.orders is upstream of {name}" in text
    assert "- column email (TEXT): Contact address." in text
    assert f"Column email of dataset {name} is tagged pii." in text
    assert "- column id (INTEGER): Key." in text


def test_missing_metadata_is_stated_not_invented():
    bare = _dataset(
        "retail_demo.raw.products",
        ownership=None,
        domain=None,
        tags=None,
        glossaryTerms=None,
        editableProperties=None,
        upstream={"total": 0, "relationships": []},
        schemaMetadata=None,
    )
    text = render_dataset(bare)
    assert "Description: none recorded in DataHub" in text
    assert "has no owner recorded in DataHub." in text
    assert "has no domain recorded in DataHub." in text
    assert "has no upstream lineage recorded in DataHub." in text
    assert "No schema recorded in DataHub." in text
    assert "tagged" not in text


def test_upstream_lists_datasets_only_and_flags_real_truncation():
    job = "urn:li:dataJob:(urn:li:dataFlow:(airflow,retail,PROD),transform_revenue)"
    ds = _dataset(
        "retail_demo.analytics.rev",
        upstream={
            "total": 2,
            "relationships": [
                {
                    "entity": {
                        "urn": urn("retail_demo.raw.orders"),
                        "platform": {"name": "postgres"},
                    }
                },
                {"entity": {"urn": job, "name": "transform_revenue"}},
            ],
        },
    )
    text = render_dataset(ds)
    assert "retail_demo.raw.orders is upstream of" in text
    assert "transform_revenue" not in text
    assert "truncated" not in text
    ds["upstream"]["total"] = 150
    assert "DataHub reports 150 upstream entities and only the first 2 were read" in (
        render_dataset(ds)
    )


def test_document_is_stable_under_reordering():
    a = _dataset("d")
    b = copy.deepcopy(a)
    b["ownership"]["owners"].append(
        {
            "owner": {"urn": "urn:li:corpuser:z", "username": "z"},
            "ownershipType": None,
            "type": "TECHNICAL_OWNER",
        }
    )
    c = copy.deepcopy(b)
    c["ownership"]["owners"].reverse()
    c["schemaMetadata"]["fields"].reverse()
    assert render_dataset(b) == render_dataset(c)


# ---------------------------------------------------------------------------
# Scroll, scope, soft-delete, auth, errors
# ---------------------------------------------------------------------------


def test_scroll_follows_scroll_id_to_the_end():
    datasets = [_dataset(f"db.s.t{i:03d}") for i in range(PAGE_SIZE * 2 + 3)]
    fake = FakeDataHub(datasets)
    assert len(_rows(fake)) == len(datasets)
    assert [i.get("scrollId") for i in fake.inputs] == [None, "1", "2"]
    assert all(i["count"] == PAGE_SIZE for i in fake.inputs)


def test_scope_filters_are_sent():
    fake = FakeDataHub([_dataset("a"), _dataset("b", platform={"name": "snowflake"})])
    rows = _rows(
        fake,
        platforms=["postgres"],
        domains=["urn:li:domain:retail"],
        containers=["urn:li:container:abc"],
    )
    assert [r["id"] for r in rows] == [urn("a")]
    clauses = fake.inputs[-1]["orFilters"][0]["and"]
    assert {"field": "platform", "values": ["urn:li:dataPlatform:postgres"]} in clauses
    assert {"field": "domains", "values": ["urn:li:domain:retail"]} in clauses
    assert {"field": "container", "values": ["urn:li:container:abc"]} in clauses


def test_soft_deleted_dataset_is_excluded():
    fake = FakeDataHub([_dataset("live"), _dataset("gone", status={"removed": True})])
    assert [r["id"] for r in _rows(fake)] == [urn("live")]


def test_auth_failure_and_errors_abort(monkeypatch):
    monkeypatch.setattr("cognee_community_connector_datahub.datahub.time.sleep", lambda s: None)
    with pytest.raises(DataHubAuthError):
        list(iter_rows(DataHubClient(BASE, token="wrong", session=FakeDataHub())))
    fake = FakeDataHub([_dataset(f"t{i}") for i in range(PAGE_SIZE + 1)])
    fake.fail_on_page = 1
    with pytest.raises(DataHubAPIError) as err:
        _rows(fake)
    assert TOKEN not in str(err.value)
    partial = FakeDataHub()
    partial.graphql_error = True
    with pytest.raises(DataHubAPIError, match="Timeout fetching lineage"):
        _rows(partial)


def test_source_declares_document_marker_and_scope():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, document_source_tag

    resource = datahub_source(base_url=BASE, token=TOKEN, resource_name="datahub_prod")
    assert document_source_tag(resource) == "datahub"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "datahub_prod"
    assert resource.write_disposition == "replace"


# ---------------------------------------------------------------------------
# dlt pipeline: full scoped snapshot + forget-on-delete
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _sync(dlt, tmp_path, fake):
    db = (tmp_path / "datahub.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="datahub_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db}"),
        dataset_name="datahub_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(datahub_source(base_url=BASE, token=TOKEN, session=fake))
    with pipeline.sql_client() as sql:
        return {r[0]: r[1] for r in sql.execute_sql("SELECT id, content FROM datahub_datasets")}


def test_ownership_tag_and_lineage_changes_are_reflected(dlt_mod, tmp_path):
    fake = FakeDataHub()
    name = "retail_demo.analytics.rev"
    assert list(_sync(dlt_mod, tmp_path, fake)) == [urn(name)]
    changed = fake.datasets[0]
    changed["ownership"]["owners"][0]["owner"]["properties"]["displayName"] = "Finance Ops"
    changed["tags"] = {"tags": [_ref("tag", "critical")]}
    changed["upstream"]["relationships"].append(
        {"entity": {"urn": urn("retail_demo.raw.products"), "platform": {"name": "postgres"}}}
    )
    text = _sync(dlt_mod, tmp_path, fake)[urn(name)]
    assert "owned by Finance Ops" in text
    assert "is tagged critical." in text
    assert "is tagged finance." not in text
    assert "retail_demo.raw.products is upstream of" in text


def test_soft_deleted_dataset_drops_out_of_staging(dlt_mod, tmp_path):
    fake = FakeDataHub([_dataset("a"), _dataset("b")])
    assert len(_sync(dlt_mod, tmp_path, fake)) == 2
    fake.datasets[1]["status"] = {"removed": True}
    assert list(_sync(dlt_mod, tmp_path, fake)) == [urn("a")]


def test_failed_fetch_keeps_previous_snapshot(dlt_mod, tmp_path, monkeypatch):
    monkeypatch.setattr("cognee_community_connector_datahub.datahub.time.sleep", lambda s: None)
    fake = FakeDataHub([_dataset(f"t{i:02d}") for i in range(PAGE_SIZE + 5)])
    before = _sync(dlt_mod, tmp_path, fake)
    fake.fail_on_page = 1
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error
        _sync(dlt_mod, tmp_path, fake)
    fake.fail_on_page = None
    assert _sync(dlt_mod, tmp_path, fake) == before


def test_large_catalogs_are_not_truncated(dlt_mod, tmp_path):
    rows = _sync(dlt_mod, tmp_path, FakeDataHub([_dataset(f"t{i:03d}") for i in range(75)]))
    assert len(rows) == 75
