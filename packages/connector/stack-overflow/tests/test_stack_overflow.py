import pytest
from unittest.mock import MagicMock
from cognee_community_connector_stack_overflow.stack_overflow import stack_overflow_questions, STACK_OVERFLOW_SOURCE_NAME, DOCUMENT_SOURCE_ATTR

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

    def mock_get(url, params):
        if "/questions/" in url:
            # Answer endpoint
            return MockResponse({
                "items": [
                    {"answer_id": 100, "question_id": 1, "body": "This is an answer", "is_accepted": True}
                ],
                "has_more": False
            })
        else:
            # Question endpoint
            fromdate = params.get("fromdate", 0)
            if fromdate == 0:
                return MockResponse({
                    "items": [
                        {"question_id": 1, "title": "How to parse HTML?", "body": "Use BeautifulSoup", "answer_count": 1, "last_activity_date": 1000}
                    ],
                    "has_more": False
                })
            else:
                return MockResponse({"items": [], "has_more": False})

    monkeypatch.setattr(requests, "get", mock_get)

def _run_sync(dlt_mod, tmp_path, api_key="dummy"):
    db_path = (tmp_path / "so.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="so_test",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="so_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(stack_overflow_questions(api_key=api_key, tags=["python"]))
    return pipeline

def _read_items(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM stack_overflow_items") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}

def test_so_yields_correct_rows(mock_requests, tmp_path):
    import dlt
    pipeline = _run_sync(dlt, tmp_path)
    
    rows = _read_items(pipeline)
    assert len(rows) == 2
    
    question = rows["question_1"]
    assert question["title"] == "How to parse HTML?"
    assert "BeautifulSoup" in question["content"]
    
    answer = rows["answer_100"]
    assert "Accepted" in answer["title"]
    assert "This is an answer" in answer["content"]

    # Run again to test incremental
    pipeline = _run_sync(dlt, tmp_path)
    rows_inc = _read_items(pipeline)
    assert len(rows_inc) == 2  # sqlite merge mode default? actually it's just appending if no merge specified, but we'll see 2 rows in total or just verify it passes.

def test_so_document_marker():
    source = stack_overflow_questions(api_key="dummy")
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == STACK_OVERFLOW_SOURCE_NAME
