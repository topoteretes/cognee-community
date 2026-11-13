import pytest
from unittest.mock import patch, MagicMock
from cognee_community_connector_calendly.calendly import calendly_source

@patch("cognee_community_connector_calendly.calendly.requests.get")
def test_calendly_yields_events(mock_get):
    # Mocking two API calls: users/me and the endpoint
    def side_effect(url, **kwargs):
        mock_response = MagicMock()
        mock_response.status_code = 200
        if "users/me" in url:
            mock_response.json.return_value = {
                "resource": {
                    "uri": "https://api.calendly.com/users/user_1",
                    "current_organization": "https://api.calendly.com/organizations/org_1"
                }
            }
        else:
            mock_response.json.return_value = {
                "collection": [
                    {
                        "uri": "https://api.calendly.com/scheduled_events/event_1",
                        "name": "Meeting with Client"
                    }
                ],
                "pagination": {
                    "next_page": None
                }
            }
        return mock_response

    mock_get.side_effect = side_effect

    source = calendly_source(personal_access_token="fake_token", endpoints=["scheduled_events"])
    
    data = list(source)
    assert len(data) == 1
    
    resource = data[0]
    
    assert resource["id"] == "event_1"
    assert resource["name"] == "Meeting with Client"
    assert "_cognee_document_source" in resource

def test_calendly_missing_token():
    with pytest.raises(ValueError):
        calendly_source(personal_access_token=None)

@patch("cognee_community_connector_calendly.calendly.requests.get")
def test_calendly_document_marker(mock_get):
    def side_effect(url, **kwargs):
        mock_response = MagicMock()
        mock_response.status_code = 200
        if "users/me" in url:
            mock_response.json.return_value = {
                "resource": {
                    "uri": "https://api.calendly.com/users/user_1",
                    "current_organization": "https://api.calendly.com/organizations/org_1"
                }
            }
        else:
            mock_response.json.return_value = {
                "collection": [
                    {
                        "uri": "https://api.calendly.com/scheduled_events/event_1",
                        "name": "Meeting with Client"
                    }
                ]
            }
        return mock_response

    mock_get.side_effect = side_effect

    source = calendly_source(personal_access_token="fake_token", endpoints=["scheduled_events"])
    
    for resource in source:
        assert resource["_cognee_document_source"] == "calendly"
