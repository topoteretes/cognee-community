import pytest
from unittest.mock import MagicMock
from cognee_community_connector_raindrop.raindrop import raindrop_bookmarks, RAINDROP_SOURCE_NAME, DOCUMENT_SOURCE_ATTR

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

    # Stateful mock to simulate deletion on second run
    class State:
        run_count = 0

    def mock_get(url, headers, params):
        page = params.get("page", 0)
        State.run_count += 1
        
        if page == 0:
            if State.run_count == 1:
                return MockResponse({
                    "items": [
                        {"_id": 1, "title": "Google", "link": "https://google.com"},
                        {"_id": 2, "title": "GitHub", "link": "https://github.com"}
                    ]
                })
            else:
                return MockResponse({
                    "items": [
                        {"_id": 1, "title": "Google", "link": "https://google.com"}
                    ]
                })
        return MockResponse({"items": []})

    monkeypatch.setattr(requests, "get", mock_get)

def _run_sync(dlt_mod, tmp_path, api_token="dummy"):
    db_path = (tmp_path / "raindrop.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="rd_test",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="rd_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(raindrop_bookmarks(api_token=api_token))
    return pipeline

def _read_items(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM raindrop_items") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}

def test_raindrop_yields_correct_rows(mock_requests, tmp_path):
    import dlt
    pipeline = _run_sync(dlt, tmp_path)
    
    rows = _read_items(pipeline)
    assert len(rows) == 2
    
    # Second run should omit item 2 and issue a tombstone, so only 1 row remains active
    pipeline = _run_sync(dlt, tmp_path)
    rows_inc = _read_items(pipeline)
    assert len(rows_inc) == 1

def test_raindrop_missing_token():
    with pytest.raises(Exception, match="API token is required"):
        import dlt
        list(raindrop_bookmarks(api_token=None))

def test_raindrop_document_marker():
    source = raindrop_bookmarks(api_token="dummy")
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == RAINDROP_SOURCE_NAME
