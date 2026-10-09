import pytest
from unittest.mock import patch, MagicMock
from cognee_community_connector_klaviyo.klaviyo import klaviyo_source

@patch("cognee_community_connector_klaviyo.klaviyo.requests.get")
def test_klaviyo_yields_campaigns(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {
                "id": "campaign1",
                "attributes": {
                    "name": "Summer Sale",
                    "status": "sent"
                }
            }
        ],
        "links": {
            "next": None
        }
    }
    mock_get.return_value = mock_response

    source = klaviyo_source(api_key="fake_key", endpoints=["campaigns"])
    
    data = list(source)
    assert len(data) == 1
    
    resource = data[0]
    
    assert resource["id"] == "campaign1"
    assert resource["name"] == "Summer Sale"
    assert "_cognee_document_source" in resource

def test_klaviyo_missing_api_key():
    with pytest.raises(ValueError):
        klaviyo_source(api_key=None)

@patch("cognee_community_connector_klaviyo.klaviyo.requests.get")
def test_klaviyo_document_marker(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {
                "id": "campaign1",
                "attributes": {
                    "name": "Test Campaign"
                }
            }
        ],
        "links": {}
    }
    mock_get.return_value = mock_response

    source = klaviyo_source(api_key="fake_key")
    
    for resource in source:
        assert resource["_cognee_document_source"] == "klaviyo"
