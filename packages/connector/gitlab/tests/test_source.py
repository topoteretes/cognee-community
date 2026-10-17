from unittest.mock import patch

import dlt
import httpx
import pytest

from cognee_community_connector_gitlab import gitlab_source

HTTPClient = httpx.Client


@pytest.fixture(autouse=True)
def stop_patches():
    yield
    patch.stopall()


def build_source(selection, *, owner_id=7, token="test-token", instance="https://gitlab.example"):
    def handler(request):
        path = request.url.path
        if path.endswith("/self"):
            payload = {"active": True, "scopes": ["read_api"], "user_id": owner_id}
        elif path.endswith("/projects"):
            payload = [{"id": 42, "path_with_namespace": "team/app"}]
        elif path.endswith("/wikis"):
            payload = [{"slug": "home", "title": "Home", "content": "Welcome"}]
        else:
            payload = [
                {
                    "id": 99,
                    "iid": 1,
                    "project_id": 42,
                    "title": "Example",
                    "description": "Body",
                    "updated_at": "2026-10-17T10:00:00Z",
                }
            ]
        return httpx.Response(200, json=payload)

    patch(
        "cognee_community_connector_gitlab.gitlab.httpx.Client",
        side_effect=lambda **kwargs: HTTPClient(**kwargs, transport=httpx.MockTransport(handler)),
    ).start()
    return gitlab_source(token, resource_types=selection, base_url=instance)


def test_source_yields_mapped_documents():
    source = build_source(["issues", "merge_requests", "wiki"])
    assert source.cognee_document_source == "gitlab"
    assert source.cognee_pipeline_scope.startswith("gitlab_documents_")
    rows = list(source)
    assert len(rows) == 3
    assert all(set(row) == {"id", "title", "content", "url"} for row in rows)
    assert len({row["id"] for row in rows}) == 3
    assert "Body" in rows[0]["content"]
    assert "Welcome" in rows[2]["content"]


def test_staging_identity_survives_token_rotation_and_selection_order():
    first = build_source(["issues", "wiki"])
    rotated = build_source(["wiki", "issues", "issues"], token="rotated-token")
    assert first.cognee_pipeline_scope == rotated.cognee_pipeline_scope
    assert list(first) == list(rotated)
    assert first.cognee_pipeline_scope != build_source(["wiki"]).cognee_pipeline_scope
    assert (
        first.cognee_pipeline_scope
        != build_source(["issues", "wiki"], owner_id=8).cognee_pipeline_scope
    )
    assert (
        first.cognee_pipeline_scope
        != build_source(["issues", "wiki"], instance="https://other.example").cognee_pipeline_scope
    )


def test_dlt_pipeline_loads_document_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")
    pipeline = dlt.pipeline(
        pipeline_name="test_gitlab_documents",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(credentials=f"sqlite:///{tmp_path / 'staging.db'}"),
        dataset_name="gitlab_test",
    )
    source = build_source(["issues", "merge_requests", "wiki"])
    table_name = source.cognee_pipeline_scope
    load_info = pipeline.run(source, write_disposition="merge", primary_key="id")
    assert not load_info.has_failed_jobs
    with pipeline.sql_client() as client:
        assert client.execute_sql(f'SELECT count(*) FROM "{table_name}"')[0][0] == 3
    pipeline.run(build_source(["issues", "merge_requests", "wiki"]), write_disposition="merge")
    with pipeline.sql_client() as client:
        assert client.execute_sql(f'SELECT count(*) FROM "{table_name}"')[0][0] == 3


def test_comments_rejected_before_http():
    with (
        patch("cognee_community_connector_gitlab.gitlab.httpx.Client") as client,
        pytest.raises(NotImplementedError, match="Comment ingestion"),
    ):
        gitlab_source("test-token", resource_types=["issues", "comments"])
    client.assert_not_called()


def test_persistent_state_and_hard_deletion_across_pipeline_instances(tmp_path):
    records = [
        {
            "id": n,
            "iid": n,
            "project_id": 42,
            "title": f"Issue {n}",
            "updated_at": "2026-10-17T10:00:00Z",
        }
        for n in (1, 2)
    ]
    requests = []
    fail_inventory = False
    project_deleted = False

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/self"):
            payload = {"active": True, "scopes": ["read_api"], "user_id": 7}
        elif path.endswith("/projects"):
            payload = [] if project_deleted else [{"id": 42, "path_with_namespace": "team/app"}]
        elif path.endswith("/projects/42"):
            return httpx.Response(404)
        elif "/issues/" in path:
            match = next(
                (item for item in records if str(item["iid"]) == path.rsplit("/", 1)[1]), None
            )
            return httpx.Response(200, json=match) if match else httpx.Response(404)
        elif fail_inventory and "updated_after" not in request.url.params:
            return httpx.Response(500)
        else:
            payload = records
        return httpx.Response(200, json=payload)

    def pipeline():
        return dlt.pipeline(
            pipeline_name="gitlab_checkpoint_test",
            pipelines_dir=str(tmp_path / "pipelines"),
            destination=dlt.destinations.sqlalchemy(
                credentials=f"sqlite:///{tmp_path / 'staging.db'}"
            ),
            dataset_name="gitlab_test",
        )

    def source():
        return gitlab_source(
            "test-token", base_url="https://gitlab.example", resource_types=["issues"]
        )

    with patch(
        "cognee_community_connector_gitlab.gitlab.httpx.Client",
        side_effect=lambda **kwargs: HTTPClient(**kwargs, transport=httpx.MockTransport(handler)),
    ):
        first = source()
        table = first.cognee_pipeline_scope
        first_pipeline = pipeline()
        first_pipeline.run(first, write_disposition="merge")
        checkpoint = first_pipeline.state["sources"][first.name]["resources"][table]["projects"]
        assert checkpoint["42"]["issues"]["updated_after"] == "2026-10-17T10:00:00+00:00"
        records.pop(0)
        records[0]["title"] = "Updated issue"
        records[0]["updated_at"] = "2026-10-17T11:00:00Z"
        second_pipeline = pipeline()
        assert not second_pipeline.run(source(), write_disposition="merge").has_failed_jobs
        with second_pipeline.sql_client() as client:
            assert client.execute_sql(f'SELECT title FROM "{table}"') == [("Updated issue",)]
        assert any("updated_after" in request.url.params for request in requests)
        before = second_pipeline.state["sources"][first.name]["resources"][table]["projects"]
        fail_inventory = True
        with pytest.raises(Exception, match="HTTP 500"):
            second_pipeline.run(source(), write_disposition="merge")
        assert (
            second_pipeline.state["sources"][first.name]["resources"][table]["projects"] == before
        )
        with second_pipeline.sql_client() as client:
            assert client.execute_sql(f'SELECT count(*) FROM "{table}"')[0][0] == 1
        fail_inventory = False
        project_deleted = True
        assert not second_pipeline.run(source(), write_disposition="merge").has_failed_jobs
        with second_pipeline.sql_client() as client:
            assert client.execute_sql(f'SELECT count(*) FROM "{table}"')[0][0] == 0
        assert second_pipeline.state["sources"][first.name]["resources"][table]["projects"] == {}
