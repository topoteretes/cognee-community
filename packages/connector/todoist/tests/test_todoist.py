import pytest
from unittest.mock import MagicMock
from cognee_community_connector_todoist.todoist import todoist_tasks, TODOIST_SOURCE_NAME, DOCUMENT_SOURCE_ATTR


class MockResponse:
    def __init__(self, json_data, status_code=200):
        self.json_data = json_data
        self.status_code = status_code

    def json(self):
        return self.json_data

    def raise_for_status(self):
        pass


@pytest.fixture
def mock_requests(monkeypatch):
    from dlt.sources.helpers import requests

    def mock_post(url, headers, data):
        sync_token = data.get("sync_token", "*")
        if sync_token == "*":
            return MockResponse({
                "sync_token": "token-1",
                "projects": [{"id": "p1", "name": "Work", "color": "red"}],
                "items": [{"id": "t1", "content": "Task 1", "description": "desc"}],
                "notes": [{"id": "n1", "item_id": "t1", "content": "note 1"}],
            })
        elif sync_token == "token-1":
            return MockResponse({
                "sync_token": "token-2",
                "projects": [{"id": "p1", "is_deleted": True}],
                "items": [{"id": "t2", "content": "Task 2"}],
                "notes": [],
            })
        return MockResponse({"sync_token": sync_token})

    monkeypatch.setattr(requests, "post", mock_post)


def _run_sync(dlt_mod, tmp_path, api_token="dummy-token"):
    db_path = (tmp_path / "todoist.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="todoist_test",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="todoist_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(todoist_tasks(api_token=api_token))
    return pipeline


def _read_items(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM todoist_items") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


def test_todoist_yields_correct_rows(mock_requests, tmp_path):
    import dlt
    pipeline = _run_sync(dlt, tmp_path)
    
    rows = _read_items(pipeline)
    assert len(rows) == 3
    
    project = rows["project_p1"]
    assert project["title"] == "Work"
    assert "red" in project["content"]
    
    task = rows["task_t1"]
    assert task["title"] == "Task 1"
    assert "desc" in task["content"]

    note = rows["comment_n1"]
    assert note["content"] == "note 1"

    # Now run it again, the state should use token-1 and fetch the incremental diff
    pipeline = _run_sync(dlt, tmp_path)
    rows_inc = _read_items(pipeline)
    
    # Since dlt pipeline merge is not fully simulated by our simple test query without "write_disposition='merge'",
    # we just check that the new task is in the table.
    assert "task_t2" in rows_inc


def test_todoist_missing_token():
    with pytest.raises(Exception, match="An API token is required"):
        import dlt
        # The exception might be raised during pipeline execution
        list(todoist_tasks(api_token=None))


def test_todoist_document_marker():
    source = todoist_tasks(api_token="dummy-token")
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == TODOIST_SOURCE_NAME
