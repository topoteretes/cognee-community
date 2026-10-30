"""databricks_source(): the factory users call, run through a real dlt pipeline."""

import sqlite3

import dlt
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
from fake_databricks import HOME, FakeWorkspace, source

from cognee_community_connector_databricks import DatabricksAuthError, databricks_source
from cognee_community_connector_databricks.databricks import _normalize_scopes

ENV = ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_PATHS", "DATABRICKS_TABLES")
SCHEMA = "workspace.sales"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))


def workspace(workspace_id="1111", notebook="report"):
    fake = FakeWorkspace(workspace_id=workspace_id)
    fake.notebook(f"{HOME}/analytics/{notebook}", source("SELECT 1", comment="--"), "SQL")
    fake.schema(SCHEMA, "Sales")
    fake.table(f"{SCHEMA}.orders", [("order_id", "bigint", "")])
    return fake


@pytest.fixture
def pipeline(tmp_path):
    database = tmp_path / "staging.db"
    destination = dlt.destinations.sqlalchemy(f"sqlite:///{database}")
    return dlt.pipeline("databricks_test", destination=destination, dataset_name="main"), database


def run(pipeline, resource):
    pipeline[0].run(resource)
    return staged(pipeline, resource.name)


def staged(pipeline, table="databricks_workspace"):
    with sqlite3.connect(pipeline[1]) as conn:
        return sorted(row[0] for row in conn.execute(f"SELECT id FROM {table}"))


def ids(fake):
    return sorted(
        [
            f"databricks:notebook:{o['object_id']}"
            for o in fake.objects.values()
            if o["object_type"] == "NOTEBOOK"
        ]
        + [f"databricks:table:{t['table_id']}" for t in fake.tables.values()]
    )


def test_resource_opts_into_document_mode_merge_and_pipeline_scope():
    resource = databricks_source(paths=["~/analytics"], client=workspace())

    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "databricks"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == "databricks_workspace"
    assert resource.write_disposition == "merge"
    assert resource.columns["_deleted"]["hard_delete"] is True
    assert resource.cognee_sync_stats == {}


def test_a_pipeline_loads_notebook_and_table_documents(pipeline):
    fake = workspace()
    resource = databricks_source(paths=["~/analytics"], tables=[SCHEMA], client=fake)

    assert run(pipeline, resource) == ids(fake)
    assert resource.cognee_sync_stats == {
        "notebooks": 1,
        "notebooks_changed": 1,
        "tables": 1,
        "tables_changed": 1,
    }


def test_dlt_keeps_the_sync_state_between_runs(pipeline):
    fake = workspace()
    run(pipeline, databricks_source(paths=["~/analytics"], tables=[SCHEMA], client=fake))

    resource = databricks_source(paths=["~/analytics"], tables=[SCHEMA], client=fake)
    run(pipeline, resource)

    assert resource.cognee_sync_stats == {"notebooks": 1, "tables": 1}
    assert fake.exports() == [f"{HOME}/analytics/report"]


def test_a_deleted_notebook_leaves_the_staging_table(pipeline):
    fake = workspace()
    run(pipeline, databricks_source(paths=["~/analytics"], tables=[SCHEMA], client=fake))
    fake.delete(f"{HOME}/analytics/report")

    assert run(
        pipeline, databricks_source(paths=["~/analytics"], tables=[SCHEMA], client=fake)
    ) == ids(fake)


def test_a_second_workspace_under_the_same_name_is_refused_and_forgets_nothing(pipeline):
    first = run(
        pipeline, databricks_source(paths=["~/analytics"], tables=[SCHEMA], client=workspace())
    )
    other = workspace(workspace_id="2222", notebook="other_report")

    with pytest.raises(Exception, match="another Databricks workspace"):
        run(pipeline, databricks_source(paths=["~/analytics"], client=other))

    # Refused after one call, before any folder or table was read: without the
    # check, the first workspace's tables would look deselected.
    assert [path for path, _ in other.calls] == ["/api/2.0/workspace/get-status"]
    assert staged(pipeline) == first


def test_two_workspaces_with_their_own_names_stay_apart_in_one_dataset(pipeline):
    first, second = workspace(), workspace(workspace_id="2222", notebook="other_report")
    run(
        pipeline,
        databricks_source(
            paths=["~/analytics"], tables=[SCHEMA], client=first, resource_name="databricks_a"
        ),
    )
    run(
        pipeline,
        databricks_source(
            paths=["~/analytics"], tables=[SCHEMA], client=second, resource_name="databricks_b"
        ),
    )
    first.delete(f"{HOME}/analytics/report")

    run(
        pipeline,
        databricks_source(
            paths=["~/analytics"], tables=[SCHEMA], client=first, resource_name="databricks_a"
        ),
    )

    assert staged(pipeline, "databricks_a") == ids(first)
    assert staged(pipeline, "databricks_b") == ids(second)


def test_check_active_runs_before_every_request_and_row(pipeline):
    fake = workspace()
    calls = []
    run(
        pipeline,
        databricks_source(
            paths=["~/analytics"],
            tables=[SCHEMA],
            client=fake,
            check_active=lambda: calls.append(len(fake.calls)),
        ),
    )

    # Before every request, plus before each of the two rows.
    assert set(range(len(fake.calls))) <= set(calls)
    assert len(calls) >= len(fake.calls) + 2


def test_a_revoked_host_stops_the_sync(pipeline):
    class RevokedError(Exception):
        pass

    fake = workspace()

    def check():
        if fake.calls:
            raise RevokedError

    with pytest.raises(Exception) as excinfo:
        run(pipeline, databricks_source(paths=["~/analytics"], client=fake, check_active=check))

    chain, error = [], excinfo.value
    while error is not None:
        chain.append(type(error).__name__)
        error = error.__cause__ or error.__context__
    assert "RevokedError" in chain
    assert len(fake.calls) == 1


def test_when_everything_fails_the_run_raises_and_forgets_nothing(pipeline):
    from cognee_community_connector_databricks import DatabricksConnectorError

    fake = workspace()
    first = run(pipeline, databricks_source(paths=["~/analytics"], tables=[SCHEMA], client=fake))
    fake.delete(f"{HOME}/analytics/report")
    fake.failing[f"{HOME}/analytics"] = DatabricksConnectorError("Databricks returned HTTP 500.")
    fake.failing["sales"] = DatabricksConnectorError("Databricks returned HTTP 500.")

    with pytest.raises(Exception, match="Nothing was forgotten"):
        run(pipeline, databricks_source(paths=["~/analytics"], tables=[SCHEMA], client=fake))

    assert staged(pipeline) == first


def test_home_is_written_out_for_paths_with_a_tilde():
    fake = workspace()
    resource = databricks_source(paths=["~/analytics/"], client=fake)

    list(resource)

    assert ("/api/2.0/workspace/list", {"path": f"{HOME}/analytics"}) in fake.calls


def test_selection_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("DATABRICKS_PATHS", "~/analytics, /Shared/reports/ ,~/analytics")
    monkeypatch.setenv("DATABRICKS_TABLES", "Workspace.Sales, `workspace`.`sales`.`orders`")
    fake = workspace()

    list(databricks_source(client=fake))

    listed = [params.get("path") for path, params in fake.calls if path.endswith("/list")]
    assert f"{HOME}/analytics" in listed and "/Shared/reports" in listed


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        (["Workspace.Sales"], ["workspace.sales"]),
        (["`main`.`sales`.`orders`", "main.sales.orders"], ["main.sales.orders"]),
        ("main", ["main"]),
    ],
)
def test_table_selections_follow_unity_catalog_naming(typed, stored):
    assert _normalize_scopes(typed) == stored


@pytest.mark.parametrize("bad", [["a.b.c.d"], ["main.sales orders"], ["main..x"]])
def test_a_malformed_table_selection_is_refused(bad):
    with pytest.raises(ValueError, match=r"catalog\.schema\.table"):
        _normalize_scopes(bad)


@pytest.mark.parametrize("bad", [["analytics"], ["Users/someone"]])
def test_a_path_that_is_not_absolute_is_refused(bad):
    with pytest.raises(ValueError, match="start with / or ~/"):
        databricks_source(paths=bad, client=workspace())


@pytest.mark.parametrize("empty", [None, [], [""], "  "])
def test_an_empty_selection_is_refused_instead_of_forgetting_everything(empty):
    with pytest.raises(ValueError, match="Nothing is selected"):
        databricks_source(paths=empty, tables=empty, client=workspace())


def test_a_missing_token_fails_when_the_source_is_built(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc-1234.cloud.databricks.com")

    with pytest.raises(DatabricksAuthError, match="DATABRICKS_TOKEN"):
        databricks_source(paths=["~/analytics"])


def test_a_missing_host_fails_when_the_source_is_built(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-test-token")

    with pytest.raises(ValueError, match="workspace URL"):
        databricks_source(paths=["~/analytics"])
