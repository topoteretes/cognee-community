import pytest
from unittest.mock import patch, MagicMock
from cognee_community_connector_revenuecat import (
    RevenueCatConnector,
    fetch_subscriber_info,
    format_subscriber_markdown
)

MOCK_SUBSCRIBER = {
    "original_app_user_id": "$RCAnonymousID:abc123456",
    "first_seen": "2026-08-15T09:12:00Z",
    "management_url": "https://apps.apple.com/account/subscriptions",
    "entitlements": {
        "pro_features": {
            "expires_date": "2027-08-15T09:12:00Z",
            "product_identifier": "com.antigravity.pro.annual",
            "purchase_date": "2026-08-15T09:12:00Z"
        }
    },
    "subscriptions": {
        "com.antigravity.pro.annual": {
            "original_purchase_date": "2026-08-15T09:12:00Z",
            "expires_date": "2027-08-15T09:12:00Z",
            "period_type": "normal",
            "store": "app_store",
            "unsubscribe_detected_at": None
        }
    }
}

def test_format_subscriber_markdown():
    md = format_subscriber_markdown("user_999", MOCK_SUBSCRIBER)
    assert "# RevenueCat Subscriber Profile: $RCAnonymousID:abc123456" in md
    assert "- **App User ID:** user_999" in md
    assert "pro_features" in md
    assert "com.antigravity.pro.annual" in md
    assert "Store: app_store" in md
    assert "Active" in md

@patch("requests.Session.get")
def test_fetch_subscriber_info(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"subscriber": MOCK_SUBSCRIBER}
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    sub = fetch_subscriber_info("sk_test_123", "user_999")
    assert sub["original_app_user_id"] == "$RCAnonymousID:abc123456"
    assert "pro_features" in sub["entitlements"]

@patch("cognee_community_connector_revenuecat.revenuecat.fetch_subscriber_info")
def test_revenuecat_connector_fetch_documents(mock_fetch):
    mock_fetch.return_value = MOCK_SUBSCRIBER

    connector = RevenueCatConnector(api_key="sk_key_123")
    docs = connector.fetch_documents_for_users(["user_999"])

    assert len(docs) == 1
    assert docs[0]["metadata"]["type"] == "subscriber"
    assert docs[0]["metadata"]["app_user_id"] == "user_999"
    assert "pro_features" in docs[0]["metadata"]["entitlements"]
    assert docs[0]["metadata"]["has_active_subscription"] is True

def test_revenuecat_connector_invalid_key():
    with pytest.raises(ValueError):
        RevenueCatConnector(api_key="")
