import pytest
from unittest.mock import MagicMock
from cognee_community_connector_brex.brex import brex_expenses, BREX_SOURCE_NAME, DOCUMENT_SOURCE_ATTR

class MockResponse:
    def __init__(self, json_data, status_code=200):
        self.json_data = json_data
        self.status_code = status_code

    def json(self):
        return self.json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception("API Error")

@pytest.fixture
def mock_requests(monkeypatch):
    from dlt.sources.helpers import requests

    def mock_get(url, headers, params):
        if "expenses" in url:
            cursor = params.get("cursor")
            if not cursor:
                return MockResponse({
                    "items": [
                        {"id": "e1", "merchant_name": "Acme Corp", "memo": "Software", "amount": {"amount": 100, "currency": "USD"}}
                    ],
                    "next_cursor": "c1"
                })
            else:
                return MockResponse({"items": [], "next_cursor": None})
        elif "budgets" in url:
            return MockResponse({
                "items": [
                    {"id": "b1", "name": "Marketing", "limit": {"amount": 5000, "currency": "USD"}}
                ],
                "next_cursor": None
            })
        return MockResponse({"items": []}, status_code=404)

    monkeypatch.setattr(requests, "get", mock_get)

def _run_sync(dlt_mod, tmp_path, api_token="dummy"):
    db_path = (tmp_path / "brex.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="brex_test",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="brex_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(brex_expenses(api_token=api_token))
    return pipeline

def _read_items(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM brex_items") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}

def test_brex_yields_correct_rows(mock_requests, tmp_path):
    import dlt
    pipeline = _run_sync(dlt, tmp_path)
    
    rows = _read_items(pipeline)
    assert len(rows) == 2
    
    expense = rows["expense_e1"]
    assert expense["title"] == "Expense: Acme Corp"
    assert "100 USD" in expense["content"]
    
    budget = rows["budget_b1"]
    assert budget["title"] == "Budget: Marketing"
    assert "5000 USD" in budget["content"]

def test_brex_missing_token():
    with pytest.raises(Exception, match="API token is required"):
        import dlt
        list(brex_expenses(api_token=None))

def test_brex_document_marker():
    source = brex_expenses(api_token="dummy")
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == BREX_SOURCE_NAME
