import pytest
import dlt
from unittest.mock import patch
from cognee_community_connector_chargebee import chargebee_source

@patch("cognee_community_connector_chargebee.chargebee.requests.get")
def test_chargebee_customers_sync(mock_get):
    # 1. Mock the Chargebee API response
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {
        "list": [
            {"customer": {"id": "cust_1", "first_name": "John", "updated_at": 1600000000}},
            # Simulate a deleted record
            {"customer": {"id": "cust_2", "first_name": "Jane", "deleted": True, "updated_at": 1600000010}}
        ],
        "next_offset": None
    }

    # 2. Create a temporary local pipeline
    pipeline = dlt.pipeline(
        pipeline_name="test_chargebee",
        destination="duckdb",
        dataset_name="test_chargebee_data",
        full_refresh=True
    )

    # 3. Initialize source with dummy credentials
    source = chargebee_source(api_key="dummy_key", site="dummy_site")
    
    # 4. Run the pipeline just for the 'customers' resource to keep tests fast
    info = pipeline.run(source.with_resources("customers"))
    
    # 5. Assertions
    assert info.has_failed_jobs is False
    
    # Verify the data landed correctly and the soft-delete tombstone worked
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id, _dlt_deleted FROM customers ORDER BY id")
        assert len(rows) == 2
        
        # cust_1 should be active (deleted is False or NULL)
        assert rows[0][0] == "cust_1"
        assert not rows[0][1] 
        
        # cust_2 should be marked as deleted by dlt.mark.make_deleted
        assert rows[1][0] == "cust_2"
        assert rows[1][1] is True