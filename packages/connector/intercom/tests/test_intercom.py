import pytest
from unittest.mock import patch, MagicMock
from cognee_community_connector_intercom.intercom import intercom_source

@patch("cognee_community_connector_intercom.intercom.requests.get")
def test_intercom_yields_contacts(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "contacts": [
            {
                "id": "contact_1",
                "name": "Jane Doe",
                "email": "jane@example.com"
            }
        ],
        "pages": {
            "next": None
        }
    }
    mock_get.return_value = mock_response

    source = intercom_source(access_token="fake_token", endpoints=["contacts"])
    
    data = list(source)
    assert len(data) == 1
    
    resource = data[0]
    
    assert resource["id"] == "contact_1"
    assert resource["name"] == "Jane Doe"
    assert "_cognee_document_source" in resource

def test_intercom_missing_token():
    with pytest.raises(ValueError):
        intercom_source(access_token=None)

@patch("cognee_community_connector_intercom.intercom.requests.get")
def test_intercom_document_marker(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "contacts": [
            {
                "id": "contact_1",
                "name": "Jane Doe"
            }
        ]
    }
    mock_get.return_value = mock_response

    source = intercom_source(access_token="fake_token", endpoints=["contacts"])
    
    for resource in source:
        assert resource["_cognee_document_source"] == "intercom"
