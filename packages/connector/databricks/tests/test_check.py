"""check_connection(): one look at the token and the selection before the first sync."""

import pytest
from fake_databricks import HOME, FakeWorkspace, source

from cognee_community_connector_databricks import DatabricksAuthError, check_connection


@pytest.fixture
def workspace():
    fake = FakeWorkspace()
    fake.notebook(f"{HOME}/analytics/report", source("x = 1"))
    fake.schema("workspace.sales", "Sales")
    fake.table("workspace.sales.orders", [("order_id", "bigint", "")])
    return fake


def test_a_working_setup_reports_what_it_will_sync(workspace):
    report = check_connection(
        ["~/analytics"], ["workspace.sales"], warehouse_id="wh-1", client=workspace
    )

    assert report == {
        "workspace_id": "1111",
        "user": "someone@example.com",
        "paths": {"~/analytics": "ok, 1 notebook(s)"},
        "tables": {"workspace.sales": "ok, 1 table(s)"},
        "lineage": "ok",
        "warehouse": "stopped, stops after 10 idle minute(s)",
        "problems": [],
    }


def test_the_warehouse_is_only_read_never_started(workspace):
    check_connection(warehouse_id="wh-1", client=workspace)

    assert not [call for call in workspace.calls if call[0] == "/api/2.0/sql/statements"]


def test_problems_are_listed_in_plain_words(workspace):
    report = check_connection(
        ["~/missing"], ["workspace.nothing_here"], warehouse_id="no-such", client=workspace
    )

    assert report["paths"] == {"~/missing": "does not exist"}
    assert report["tables"] == {"workspace.nothing_here": "does not exist or is empty"}
    assert len(report["problems"]) == 3


def test_a_refused_token_raises(workspace):
    workspace.failing["/api/2.0/preview/scim/v2/Me"] = DatabricksAuthError(
        "Databricks refused the token (HTTP 403)."
    )

    with pytest.raises(DatabricksAuthError):
        check_connection(["~/analytics"], client=workspace)
